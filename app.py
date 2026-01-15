import base64
import ctypes
import json
import logging
import queue
import random
import re
import struct
import threading
import time
import requests  # 改为使用标准 requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

# -------------------------- 日志配置 --------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")

app = FastAPI()

# 添加 CORS 中间件，允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# 模板目录
templates = Jinja2Templates(directory="templates")

# ----------------------------------------------------------------------
# (1) 配置文件的读写函数
# ----------------------------------------------------------------------
CONFIG_PATH = "config.json"


def load_config():
    """从 config.json 加载配置，出错则返回空 dict"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[load_config] 无法读取配置文件: {e}")
        return {}


def save_config(cfg):
    """将配置写回 config.json"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[save_config] 写入 config.json 失败: {e}")


CONFIG = load_config()

# -------------------------- 全局账号队列 --------------------------
account_queue = []  # 维护所有可用账号
claude_api_key_queue = []  # 维护所有可用的Claude API keys


def init_account_queue():
    """初始化时从配置加载账号"""
    global account_queue
    account_queue = CONFIG.get("accounts", [])[:]  # 深拷贝
    random.shuffle(account_queue)  # 初始随机排序


def init_claude_api_key_queue():
    """Claude API keys由用户自己的token提供，这里初始化为空"""
    global claude_api_key_queue
    claude_api_key_queue = []


init_account_queue()
init_claude_api_key_queue()

# ----------------------------------------------------------------------
# (2) DeepSeek 相关常量
# ----------------------------------------------------------------------
DEEPSEEK_HOST = "chat.deepseek.com"
DEEPSEEK_LOGIN_URL = f"https://{DEEPSEEK_HOST}/api/v0/users/login"
DEEPSEEK_CREATE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/create"
DEEPSEEK_CREATE_POW_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/create_pow_challenge"
DEEPSEEK_COMPLETION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/completion"
BASE_HEADERS = {
    "Host": "chat.deepseek.com",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.3 Safari/605.1.15",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-client-platform": "android",
    "x-client-version": "1.3.0-auto-resume",
    "x-client-locale": "zh_CN",
    "accept-charset": "UTF-8",
}

# ----------------------------------------------------------------------
# (2.1) Claude 相关常量 - 基于OpenAI接口转换
# ----------------------------------------------------------------------
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-20250514"  # Claude统一默认模型

# WASM 模块文件路径
WASM_PATH = "sha3_wasm_bg.7b9ca65ddd.wasm"


# ----------------------------------------------------------------------
# 辅助函数：获取账号唯一标识（优先 email，否则 mobile）
# ----------------------------------------------------------------------
def get_account_identifier(account):
    """返回账号的唯一标识，优先使用 email，否则使用 mobile"""
    return account.get("email", "").strip() or account.get("mobile", "").strip()


# ----------------------------------------------------------------------
# (3) 登录函数：支持使用 email 或 mobile 登录
# ----------------------------------------------------------------------
def login_deepseek_via_account(account):
    """使用 account 中的 email 或 mobile 登录 DeepSeek，
    成功后将返回的 token 写入 account 并保存至配置文件，返回新 token。
    """
    email = account.get("email", "").strip()
    mobile = account.get("mobile", "").strip()
    password = account.get("password", "").strip()
    if not password or (not email and not mobile):
        raise HTTPException(
            status_code=400,
            detail="账号缺少必要的登录信息（必须提供 email 或 mobile 以及 password）",
        )
    if email:
        payload = {
            "email": email,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android",
        }
    else:
        payload = {
            "mobile": mobile,
            "area_code": None,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android",
        }
    try:
        resp = requests.post(DEEPSEEK_LOGIN_URL, headers=BASE_HEADERS, json=payload)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] 登录请求异常: {e}")
        raise HTTPException(status_code=500, detail="Account login failed: 请求异常")
    try:
        logger.warning(f"[login_deepseek_via_account] {resp.text}")
        data = resp.json()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] JSON解析失败: {e}")
        raise HTTPException(
            status_code=500, detail="Account login failed: invalid JSON response"
        )
    # 校验响应数据格式是否正确
    if (
        data.get("data") is None
        or data["data"].get("biz_data") is None
        or data["data"]["biz_data"].get("user") is None
    ):
        logger.error(f"[login_deepseek_via_account] 登录响应格式错误: {data}")
        raise HTTPException(
            status_code=500, detail="Account login failed: invalid response format"
        )
    new_token = data["data"]["biz_data"]["user"].get("token")
    if not new_token:
        logger.error(f"[login_deepseek_via_account] 登录响应中缺少 token: {data}")
        raise HTTPException(
            status_code=500, detail="Account login failed: missing token"
        )
    account["token"] = new_token
    save_config(CONFIG)
    return new_token


# ----------------------------------------------------------------------
# (4) 从 accounts 中随机选择一个未忙且未尝试过的账号
# ----------------------------------------------------------------------
def choose_new_account(exclude_ids=None):
    """选择策略：
    1. 遍历队列，找到第一个未被 exclude_ids 包含的账号
    2. 从队列中移除该账号
    3. 返回该账号（由后续逻辑保证最终会重新入队）
    """
    if exclude_ids is None:
        exclude_ids = []
        
    for i in range(len(account_queue)):
        acc = account_queue[i]
        acc_id = get_account_identifier(acc)
        if acc_id and acc_id not in exclude_ids:
            # 从队列中移除并返回
            logger.info(f"[choose_new_account] 新选择账号: {acc_id}")
            return account_queue.pop(i)

    logger.warning("[choose_new_account] 没有可用的账号或所有账号都在使用中")
    return None


def release_account(account):
    """将账号重新加入队列末尾"""
    account_queue.append(account)


# ----------------------------------------------------------------------
# Claude API key 管理函数（简化版本）
# ----------------------------------------------------------------------
def choose_claude_api_key():
    """选择一个可用的Claude API key - 现在直接由用户提供"""
    return None


def release_claude_api_key(api_key):
    """释放Claude API key - 现在无需操作"""
    pass


# ----------------------------------------------------------------------
# (5) 判断调用模式：配置模式 vs 用户自带 token
# ----------------------------------------------------------------------
def determine_mode_and_token(request: Request):
    """
    根据请求头 Authorization 判断使用哪种模式：
    - 如果 Bearer token 出现在 CONFIG["keys"] 中，则为配置模式，从 CONFIG["accounts"] 中随机选择一个账号（排除已尝试账号），
      检查该账号是否已有 token，否则调用登录接口获取；
    - 否则，直接使用请求中的 Bearer 值作为 DeepSeek token。
    结果存入 request.state.deepseek_token；配置模式下同时存入 request.state.account 与 request.state.tried_accounts。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Unauthorized: missing Bearer token."
        )
    caller_key = auth_header.replace("Bearer ", "", 1).strip()
    config_keys = CONFIG.get("keys", [])
    if caller_key in config_keys:
        request.state.use_config_token = True
        request.state.tried_accounts = []  # 初始化已尝试账号
        selected_account = choose_new_account()
        if not selected_account:
            raise HTTPException(
                status_code=429,
                detail="No accounts configured or all accounts are busy.",
            )
        if not selected_account.get("token", "").strip():
            try:
                login_deepseek_via_account(selected_account)
            except Exception as e:
                logger.error(
                    f"[determine_mode_and_token] 账号 {get_account_identifier(selected_account)} 登录失败：{e}"
                )
                raise HTTPException(status_code=500, detail="Account login failed.")

        request.state.deepseek_token = selected_account.get("token")
        request.state.account = selected_account

    else:
        request.state.use_config_token = False
        request.state.deepseek_token = caller_key


def get_auth_headers(request: Request):
    """返回 DeepSeek 请求所需的公共请求头"""
    return {**BASE_HEADERS, "authorization": f"Bearer {request.state.deepseek_token}"}


# ----------------------------------------------------------------------
# Claude 认证相关函数
# ----------------------------------------------------------------------
def determine_claude_mode_and_token(request: Request):
    """
    Claude认证：沿用现有的OpenAI接口认证逻辑
    """
    # 直接调用现有的认证逻辑
    determine_mode_and_token(request)


# ----------------------------------------------------------------------
# OpenAI到Claude格式转换函数
# ----------------------------------------------------------------------
def convert_claude_to_deepseek(claude_request):
    """将Claude格式的请求转换为DeepSeek格式（基于现有OpenAI接口）"""
    messages = claude_request.get("messages", [])
    model = claude_request.get("model", CLAUDE_DEFAULT_MODEL)
    
    # 从配置文件读取Claude模型映射
    claude_mapping = CONFIG.get("claude_model_mapping", {
        "fast": "deepseek-chat",
        "slow": "deepseek-chat"
    })
    
    # Claude模型映射到DeepSeek模型 - 基于配置和模型特征判断
    if "opus" in model.lower() or "reasoner" in model.lower() or "slow" in model.lower():
        deepseek_model = claude_mapping.get("slow", "deepseek-chat")
    else:
        deepseek_model = claude_mapping.get("fast", "deepseek-chat")
    
    deepseek_request = {
        "model": deepseek_model,
        "messages": messages.copy()
    }
    
    # 处理system消息 - 将system参数转换为system role消息
    if "system" in claude_request:
        system_msg = {"role": "system", "content": claude_request["system"]}
        deepseek_request["messages"].insert(0, system_msg)
    
    # 添加可选参数
    if "temperature" in claude_request:
        deepseek_request["temperature"] = claude_request["temperature"]
    if "top_p" in claude_request:
        deepseek_request["top_p"] = claude_request["top_p"]
    if "stop_sequences" in claude_request:
        deepseek_request["stop"] = claude_request["stop_sequences"]
    if "stream" in claude_request:
        deepseek_request["stream"] = claude_request["stream"]
        
    return deepseek_request


def convert_deepseek_to_claude_format(deepseek_response, original_claude_model=CLAUDE_DEFAULT_MODEL):
    """将DeepSeek响应转换为Claude格式的OpenAI响应"""
    # DeepSeek响应已经是OpenAI格式，只需要修改模型名称
    if isinstance(deepseek_response, dict):
        claude_response = deepseek_response.copy()
        claude_response["model"] = original_claude_model
        return claude_response
    
    return deepseek_response








# ----------------------------------------------------------------------
# Claude API 调用函数
# ----------------------------------------------------------------------
async def call_claude_via_openai(request: Request, claude_payload):
    """通过现有OpenAI接口调用Claude（实际调用DeepSeek）"""
    # 将Claude请求转换为DeepSeek请求
    deepseek_payload = convert_claude_to_deepseek(claude_payload)
    
    # 直接调用现有的chat_completions逻辑
    try:
        # 使用现有的逻辑创建session和pow
        session_id = create_session(request)
        if not session_id:
            raise HTTPException(status_code=401, detail="invalid token.")
        
        pow_resp = get_pow_response(request)
        if not pow_resp:
            raise HTTPException(
                status_code=401,
                detail="Failed to get PoW (invalid token or unknown error).",
            )
        
        # 准备DeepSeek API调用
        model = deepseek_payload.get("model", "deepseek-chat")
        messages = deepseek_payload.get("messages", [])
        
        # 判断模型特性
        model_lower = model.lower()
        if model_lower in ["deepseek-v3", "deepseek-chat"]:
            thinking_enabled = False
            search_enabled = False
        elif model_lower in ["deepseek-r1", "deepseek-reasoner"]:
            thinking_enabled = True
            search_enabled = False
        elif model_lower in ["deepseek-v3-search", "deepseek-chat-search"]:
            thinking_enabled = False
            search_enabled = True
        elif model_lower in ["deepseek-r1-search", "deepseek-reasoner-search"]:
            thinking_enabled = True
            search_enabled = True
        else:
            thinking_enabled = False
            search_enabled = False
        
        # 使用 messages_prepare 函数构造最终 prompt
        final_prompt = messages_prepare(messages)
        
        headers = {**get_auth_headers(request), "x-ds-pow-response": pow_resp}
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": final_prompt,
            "ref_file_ids": [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
        }

        deepseek_resp = call_completion_endpoint(payload, headers, max_attempts=3)
        return deepseek_resp
        
    except Exception as e:
        logger.error(f"[call_claude_via_openai] 调用失败: {e}")
        return None


# ----------------------------------------------------------------------
# (6) 封装对话接口调用的重试机制
# ----------------------------------------------------------------------
def call_completion_endpoint(payload, headers, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            deepseek_resp = requests.post(
                DEEPSEEK_COMPLETION_URL, headers=headers, json=payload, stream=True
            )
        except Exception as e:
            logger.warning(f"[call_completion_endpoint] 请求异常: {e}")
            time.sleep(1)
            attempts += 1
            continue
        if deepseek_resp.status_code == 200:
            return deepseek_resp
        else:
            logger.warning(
                f"[call_completion_endpoint] 调用对话接口失败, 状态码: {deepseek_resp.status_code}"
            )
            deepseek_resp.close()
            time.sleep(1)
            attempts += 1
    return None


# ----------------------------------------------------------------------
# (7) 创建会话 & 获取 PoW（重试时，配置模式下错误会切换账号；用户自带 token 模式下仅重试）
# ----------------------------------------------------------------------
def create_session(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        headers = get_auth_headers(request)
        try:
            resp = requests.post(
                DEEPSEEK_CREATE_SESSION_URL, headers=headers, json={"agent": "chat"}
            )
        except Exception as e:
            logger.error(f"[create_session] 请求异常: {e}")
            attempts += 1
            continue
        try:
            logger.warning(f"[create_session] {resp.text}")
            data = resp.json()
            
        except Exception as e:
            logger.error(f"[create_session] JSON解析异常: {e}")
            data = {}
        if resp.status_code == 200 and data.get("code") == 0:
            session_id = data["data"]["biz_data"]["id"]

            resp.close()
            return session_id
        else:
            code = data.get("code")
            logger.warning(
                f"[create_session] 创建会话失败, code={code}, msg={data.get('msg')}"
            )
            resp.close()
            if request.state.use_config_token:
                current_id = get_account_identifier(request.state.account)
                if not hasattr(request.state, "tried_accounts"):
                    request.state.tried_accounts = []
                if current_id not in request.state.tried_accounts:
                    request.state.tried_accounts.append(current_id)
                new_account = choose_new_account(request.state.tried_accounts)
                if new_account is None:
                    break
                try:
                    login_deepseek_via_account(new_account)
                except Exception as e:
                    logger.error(
                        f"[create_session] 账号 {get_account_identifier(new_account)} 登录失败：{e}"
                    )
                    attempts += 1
                    continue
                request.state.account = new_account
                request.state.deepseek_token = new_account.get("token")
            else:
                attempts += 1
                continue
        attempts += 1
    return None


# ----------------------------------------------------------------------
# (7.1) 纯Python计算 PoW 答案的辅助函数
# ----------------------------------------------------------------------
from powc import compute_pow_answer
# ----------------------------------------------------------------------
# (7.2) 获取 PoW 响应，融合计算 answer 逻辑
# ----------------------------------------------------------------------
def get_pow_response(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        headers = get_auth_headers(request)
        try:
            resp = requests.post(
                DEEPSEEK_CREATE_POW_URL,
                headers=headers,
                json={"target_path": "/api/v0/chat/completion"},
                timeout=30,
            )
        except Exception as e:
            logger.error(f"[get_pow_response] 请求异常: {e}")
            attempts += 1
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"[get_pow_response] JSON解析异常: {e}")
            data = {}
        if resp.status_code == 200 and data.get("code") == 0:
            challenge = data["data"]["biz_data"]["challenge"]
            difficulty = challenge.get("difficulty", 144000)
            expire_at = challenge.get("expire_at", 1680000000)
            try:
                answer = compute_pow_answer(
                    challenge["algorithm"],
                    challenge["challenge"],
                    challenge["salt"],
                    difficulty,
                    expire_at,
                    challenge["signature"],
                    challenge["target_path"],
                    WASM_PATH,  # 保持参数一致
                    max_time=30  # 最大30秒计算时间
                )
            except Exception as e:
                logger.error(f"[get_pow_response] PoW 答案计算异常: {e}")
                answer = None
            if answer is None:
                logger.warning("[get_pow_response] PoW 答案计算失败，重试中...")
                resp.close()
                attempts += 1
                continue
            pow_dict = {
                "algorithm": challenge["algorithm"],
                "challenge": challenge["challenge"],
                "salt": challenge["salt"],
                "answer": answer,  # 整数形式答案
                "signature": challenge["signature"],
                "target_path": challenge["target_path"],
            }
            pow_str = json.dumps(pow_dict, separators=(",", ":"), ensure_ascii=False)
            encoded = base64.b64encode(pow_str.encode("utf-8")).decode("utf-8").rstrip()
            resp.close()
            return encoded
        else:
            code = data.get("code")
            logger.warning(
                f"[get_pow_response] 获取 PoW 失败, code={code}, msg={data.get('msg')}"
            )
            resp.close()
            if request.state.use_config_token:
                current_id = get_account_identifier(request.state.account)
                if not hasattr(request.state, "tried_accounts"):
                    request.state.tried_accounts = []
                if current_id not in request.state.tried_accounts:
                    request.state.tried_accounts.append(current_id)
                new_account = choose_new_account(request.state.tried_accounts)
                if new_account is None:
                    break
                try:
                    login_deepseek_via_account(new_account)
                except Exception as e:
                    logger.error(
                        f"[get_pow_response] 账号 {get_account_identifier(new_account)} 登录失败：{e}"
                    )
                    attempts += 1
                    continue
                request.state.account = new_account
                request.state.deepseek_token = new_account.get("token")
            else:
                attempts += 1
                continue
            attempts += 1
    return None


# ----------------------------------------------------------------------
# (8) 路由：/v1/models
# ----------------------------------------------------------------------
@app.get("/v1/models")
def list_models():
    models_list = [
        {
            "id": "deepseek-chat",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": [],
        },
        {
            "id": "deepseek-reasoner",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": [],
        },
        {
            "id": "deepseek-chat-search",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": [],
        },
        {
            "id": "deepseek-reasoner-search",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": [],
        },
    ]
    data = {"object": "list", "data": models_list}
    return JSONResponse(content=data, status_code=200)


# ----------------------------------------------------------------------
# Claude 路由：模型列表
# ----------------------------------------------------------------------
@app.get("/anthropic/v1/models")
def list_claude_models():
    models_list = [
        {
            "id": "claude-sonnet-4-20250514",
            "object": "model",
            "created": 1715635200,
            "owned_by": "anthropic",
        },
        {
            "id": "claude-sonnet-4-20250514-fast",
            "object": "model",
            "created": 1715635200,
            "owned_by": "anthropic",
        },
        {
            "id": "claude-sonnet-4-20250514-slow",
            "object": "model",
            "created": 1715635200,
            "owned_by": "anthropic",
        },
    ]
    data = {"object": "list", "data": models_list}
    return JSONResponse(content=data, status_code=200)


# ----------------------------------------------------------------------
# 消息预处理函数，将多轮对话合并成最终 prompt
# ----------------------------------------------------------------------
def messages_prepare(messages: list) -> str:
    """处理消息列表，合并连续相同角色的消息，并添加角色标签：
    - 对于 assistant 消息，加上 <｜Assistant｜> 前缀及  结束标签；
    - 对于 user/system 消息（除第一条外）加上 <｜User｜> 前缀；
    - 如果消息 content 为数组，则提取其中 type 为 "text" 的部分；
    - 最后移除 markdown 图片格式的内容。
    """
    processed = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [
                item.get("text", "") for item in content if item.get("type") == "text"
            ]
            text = "\n".join(texts)
        else:
            text = str(content)
        processed.append({"role": role, "text": text})
    if not processed:
        return ""
    # 合并连续同一角色的消息
    merged = [processed[0]]
    for msg in processed[1:]:
        if msg["role"] == merged[-1]["role"]:
            merged[-1]["text"] += "\n\n" + msg["text"]
        else:
            merged.append(msg)
    # 添加标签
    parts = []
    for idx, block in enumerate(merged):
        role = block["role"]
        text = block["text"]
        if role == "assistant":
            parts.append(f"<｜Assistant｜>{text} ")
        elif role in ("user", "system"):
            if idx > 0:
                parts.append(f"<｜User｜>{text}")
            else:
                parts.append(text)
        else:
            parts.append(text)
    final_prompt = "".join(parts)
    # 仅移除 markdown 图片格式(不全部移除 !）
    final_prompt = re.sub(r"!\[(.*?)\]\((.*?)\)", r"[\1](\2)", final_prompt)
    return final_prompt


# 添加保活超时配置（5秒）
KEEP_ALIVE_TIMEOUT = 5


# ----------------------------------------------------------------------
# (10) 路由：/v1/chat/completions
# ----------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        # 处理 token 相关逻辑，若登录失败则直接返回错误响应
        try:
            determine_mode_and_token(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code, content={"error": exc.detail}
            )
        except Exception as exc:
            logger.error(f"[chat_completions] determine_mode_and_token 异常: {exc}")
            return JSONResponse(
                status_code=500, content={"error": "Account login failed."}
            )

        req_data = await request.json()
        model = req_data.get("model")
        messages = req_data.get("messages", [])
        if not model or not messages:
            raise HTTPException(
                status_code=400, detail="Request must include 'model' and 'messages'."
            )
        # 判断是否启用"思考"或"搜索"功能（这里根据模型名称判断）
        model_lower = model.lower()
        if model_lower in ["deepseek-v3", "deepseek-chat"]:
            thinking_enabled = False
            search_enabled = False
        elif model_lower in ["deepseek-r1", "deepseek-reasoner"]:
            thinking_enabled = True
            search_enabled = False
        elif model_lower in ["deepseek-v3-search", "deepseek-chat-search"]:
            thinking_enabled = False
            search_enabled = True
        elif model_lower in ["deepseek-r1-search", "deepseek-reasoner-search"]:
            thinking_enabled = True
            search_enabled = True
        else:
            raise HTTPException(
                status_code=503, detail=f"Model '{model}' is not available."
            )
        # 使用 messages_prepare 函数构造最终 prompt
        final_prompt = messages_prepare(messages)
        session_id = create_session(request)
        if not session_id:
            raise HTTPException(status_code=401, detail="invalid token.")
        pow_resp = get_pow_response(request)
        if not pow_resp:
            raise HTTPException(
                status_code=401,
                detail="Failed to get PoW (invalid token or unknown error).",
            )
        headers = {**get_auth_headers(request), "x-ds-pow-response": pow_resp}
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": final_prompt,
            "ref_file_ids": [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
        }

        deepseek_resp = call_completion_endpoint(payload, headers, max_attempts=3)
        if not deepseek_resp:
            raise HTTPException(status_code=500, detail="Failed to get completion.")
        created_time = int(time.time())
        completion_id = f"{session_id}"

        # 流式响应（SSE）或普通响应
        if bool(req_data.get("stream", False)):
            if deepseek_resp.status_code != 200:
                deepseek_resp.close()
                return JSONResponse(
                    content=deepseek_resp.content, status_code=deepseek_resp.status_code
                )

            def sse_stream():
                try:
                    final_text = ""
                    final_thinking = ""
                    first_chunk_sent = False
                    result_queue = queue.Queue()
                    last_send_time = time.time()
                    citation_map = {}  # 用于存储引用链接的字典

                    def process_data():
                        ptype = "text"
                        try:
                            for raw_line in deepseek_resp.iter_lines():
                                try:
                                    line = raw_line.decode("utf-8")
                                except Exception as e:
                                    logger.warning(f"[sse_stream] 解码失败: {e}")
                                    # 根据当前模式决定错误消息类型
                                    error_type = "thinking" if ptype == "thinking" else "text"
                                    busy_content_str = f'{{"choices":[{{"index":0,"delta":{{"content":"解码失败，请稍候再试","type":"{error_type}"}}}}],"model":"","chunk_token_usage":1,"created":0,"message_id":-1,"parent_id":-1}}'
                                    try:
                                        busy_content = json.loads(busy_content_str)
                                        result_queue.put(busy_content)
                                    except json.JSONDecodeError:
                                        # 如果JSON解析也失败，创建最基本的错误响应
                                        result_queue.put({"choices": [{"index": 0, "delta": {"content": "解码失败", "type": "text"}}]})
                                    result_queue.put(None)
                                    break
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    data_str = line[5:].strip()
                                    if data_str == "[DONE]":
                                        result_queue.put(None)  # 结束信号
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                        
                                        if "v" in chunk:
                                            v_value = chunk["v"]
                                            
                                            # 构造新的 delta 格式的 chunk
                                            content = ""

                                            if "p" in chunk and chunk.get("p") == "response/search_status":
                                                continue
                                                
                                            if "p" in chunk and chunk.get("p") == "response/thinking_content":
                                                ptype = "thinking"
                                            elif "p" in chunk and chunk.get("p") == "response/content":
                                                ptype = "text"

                                            # 处理文本内容
                                            if isinstance(v_value, str):
                                                content = v_value
                                            # 处理数组更新如状态变更
                                            elif isinstance(v_value, list):
                                                for item in v_value:
                                                    if item.get("p") == "status" and item.get("v") == "FINISHED":
                                                        # 最终完成信号
                                                        result_queue.put({"choices": [{"index": 0, "finish_reason": "stop"}]})
                                                        result_queue.put(None)
                                                        return
                                                continue
                                            
                                            # 构造兼容原逻辑的 chunk
                                            unified_chunk = {
                                                "choices": [{
                                                    "index": 0,
                                                    "delta": {
                                                        "content": content,
                                                        "type": ptype
                                                    }
                                                }],
                                                "model": "",
                                                "chunk_token_usage": len(content) // 4,  # 简单估算token数
                                                "created": 0,
                                                "message_id": -1,
                                                "parent_id": -1
                                            }
                    
                                            result_queue.put(unified_chunk)
                                    except Exception as e:
                                        logger.warning(
                                            f"[sse_stream] 无法解析: {data_str}, 错误: {e}"
                                        )
                                        # 根据当前模式决定错误消息类型
                                        error_type = "thinking" if ptype == "thinking" else "text"
                                        busy_content_str = f'{{"choices":[{{"index":0,"delta":{{"content":"解析失败，请稍候再试","type":"{error_type}"}}}}],"model":"","chunk_token_usage":1,"created":0,"message_id":-1,"parent_id":-1}}'
                                        try:
                                            busy_content = json.loads(busy_content_str)
                                            result_queue.put(busy_content)
                                        except json.JSONDecodeError:
                                            # 如果JSON解析也失败，创建最基本的错误响应
                                            result_queue.put({"choices": [{"index": 0, "delta": {"content": "解析失败", "type": "text"}}]})
                                        result_queue.put(None)
                                        break
                        except Exception as e:
                            logger.warning(f"[sse_stream] 错误: {e}")
                            # 创建基本的错误响应，不依赖JSON解析
                            try:
                                error_response = {"choices": [{"index": 0, "delta": {"content": "服务器错误，请稍候再试", "type": "text"}}]}
                                result_queue.put(error_response)
                            except Exception:
                                # 最终备选方案
                                pass
                            result_queue.put(None)
                            # raise HTTPException(
                                # status_code=500, detail="Server is error."
                            # )
                        finally:
                            deepseek_resp.close()

                    process_thread = threading.Thread(target=process_data)
                    process_thread.start()

                    while True:
                        current_time = time.time()
                        if current_time - last_send_time >= KEEP_ALIVE_TIMEOUT:

                            yield ": keep-alive\n\n"
                            last_send_time = current_time
                            continue
                        try:
                            chunk = result_queue.get(timeout=0.05)
                            if chunk is None:
                                # 发送最终统计信息
                                prompt_tokens = len(final_prompt) // 4  # 简单估算token数
                                thinking_tokens = len(final_thinking) // 4  # 简单估算token数
                                completion_tokens = len(final_text) // 4  # 简单估算token数
                                usage = {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": thinking_tokens + completion_tokens,
                                    "total_tokens": prompt_tokens + thinking_tokens + completion_tokens,
                                    "completion_tokens_details": {
                                        "reasoning_tokens": thinking_tokens
                                    },
                                }
                                finish_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [
                                        {
                                            "delta": {},
                                            "index": 0,
                                            "finish_reason": "stop",
                                        }
                                    ],
                                    "usage": usage,
                                }
                                yield f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                last_send_time = current_time
                                break
                            new_choices = []
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                ctype = delta.get("type")
                                ctext = delta.get("content", "")
                                if (
                                    choice
                                    .get("finish_reason")
                                    == "backend_busy"
                                ):
                                    ctext = '服务器繁忙，请稍候再试'
                                if search_enabled and ctext.startswith("[citation:"):
                                    ctext = ""
                                if ctype == "thinking":
                                    if thinking_enabled:
                                        final_thinking += ctext
                                elif ctype == "text":
                                    final_text += ctext
                                delta_obj = {}
                                if not first_chunk_sent:
                                    delta_obj["role"] = "assistant"
                                    first_chunk_sent = True
                                if ctype == "thinking":
                                    if thinking_enabled:
                                        delta_obj["reasoning_content"] = ctext
                                elif ctype == "text":
                                    delta_obj["content"] = ctext
                                if delta_obj:
                                    new_choices.append(
                                        {
                                            "delta": delta_obj,
                                            "index": choice.get("index", 0),
                                        }
                                    )
                            if new_choices:
                                out_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": new_choices,
                                }
                                yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                last_send_time = current_time
                        except queue.Empty:
                            continue
                except Exception as e:
                    logger.error(f"[sse_stream] 异常: {e}")
                finally:
                    if getattr(request.state, "use_config_token", False) and hasattr(
                        request.state, "account"
                    ):
                        release_account(request.state.account)

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={"Content-Type": "text/event-stream"},
            )
        else:
            # 非流式响应处理
            think_list = []
            text_list = []
            result = None
            citation_map = {}

            data_queue = queue.Queue()

            def collect_data():
                nonlocal result
                ptype = "text"
                try:
                    for raw_line in deepseek_resp.iter_lines():
                        try:
                            line = raw_line.decode("utf-8")
                        except Exception as e:
                            logger.warning(f"[chat_completions] 解码失败: {e}")
                            # 根据当前处理类型添加错误消息
                            if ptype == "thinking":
                                think_list.append('解码失败，请稍候再试')
                            else:
                                text_list.append('解码失败，请稍候再试')
                            data_queue.put(None)
                            break
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                data_queue.put(None)
                                break
                            try:
                                chunk = json.loads(data_str)
            
                                # 提取 v 字段
                                if "v" in chunk:
                                    v_value = chunk["v"]
                                    
                                    if "p" in chunk and chunk.get("p") == "response/search_status":
                                        continue
                                                
                                    if "p" in chunk and chunk.get("p") == "response/thinking_content":
                                        ptype = "thinking"
                                    elif "p" in chunk and chunk.get("p") == "response/content":
                                        ptype = "text"
            
                                    # 处理字符串形式的 v 值（即文本内容）
                                    if isinstance(v_value, str):
                                        if search_enabled and v_value.startswith("[citation:"):
                                            continue  # 跳过 citation 内容
                                        if ptype == "thinking":
                                            think_list.append(v_value)
                                        else:
                                            text_list.append(v_value)
            
                                    # 处理数组更新如状态变更
                                    elif isinstance(v_value, list):
                                        for item in v_value:
                                            if item.get("p") == "status" and item.get("v") == "FINISHED":
                                                # 构建最终结果
                                                final_reasoning = "".join(think_list)
                                                final_content = "".join(text_list)
                                                prompt_tokens = len(final_prompt) // 4  # 简单估算token数
                                                reasoning_tokens = len(final_reasoning) // 4  # 简单估算token数
                                                completion_tokens = len(final_content) // 4  # 简单估算token数
                                                result = {
                                                    "id": completion_id,
                                                    "object": "chat.completion",
                                                    "created": created_time,
                                                    "model": model,
                                                    "choices": [
                                                        {
                                                            "index": 0,
                                                            "message": {
                                                                "role": "assistant",
                                                                "content": final_content,
                                                                "reasoning_content": final_reasoning,
                                                            },
                                                            "finish_reason": "stop",
                                                        }
                                                    ],
                                                    "usage": {
                                                        "prompt_tokens": prompt_tokens,
                                                        "completion_tokens": reasoning_tokens + completion_tokens,
                                                        "total_tokens": prompt_tokens + reasoning_tokens + completion_tokens,
                                                        "completion_tokens_details": {
                                                            "reasoning_tokens": reasoning_tokens
                                                        },
                                                    },
                                                }
                                                data_queue.put("DONE")
                                                return  # 提前返回，结束函数
            
                            except Exception as e:
                                logger.warning(f"[collect_data] 无法解析: {data_str}, 错误: {e}")
                                # 根据当前处理类型添加错误消息
                                if ptype == "thinking":
                                    think_list.append('解析失败，请稍候再试')
                                else:
                                    text_list.append('解析失败，请稍候再试')
                                data_queue.put(None)
                                break
                except Exception as e:
                    logger.warning(f"[collect_data] 错误: {e}")
                    # 根据当前处理类型添加错误消息
                    if ptype == "thinking":
                        think_list.append('处理失败，请稍候再试')
                    else:
                        text_list.append('处理失败，请稍候再试')
                    data_queue.put(None)
                finally:
                    deepseek_resp.close()
                    if result is None:
                        # 如果没有提前构造 result，则构造默认结果
                        final_content = "".join(text_list)
                        final_reasoning = "".join(think_list)  # 修复：应该使用think_list而不是text_list
                        prompt_tokens = len(final_prompt) // 4  # 简单估算token数
                        reasoning_tokens = len(final_reasoning) // 4  # 简单估算token数
                        completion_tokens = len(final_content) // 4  # 简单估算token数
                        result = {
                            "id": completion_id,
                            "object": "chat.completion",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": final_content,
                                        "reasoning_content": final_reasoning,
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": reasoning_tokens + completion_tokens,
                                "total_tokens": prompt_tokens + reasoning_tokens + completion_tokens,
                            },
                        }
                    data_queue.put("DONE")

            collect_thread = threading.Thread(target=collect_data)
            collect_thread.start()

            def generate():
                last_send_time = time.time()
                while True:
                    current_time = time.time()
                    if current_time - last_send_time >= KEEP_ALIVE_TIMEOUT:

                        yield ""
                        last_send_time = current_time
                    if not collect_thread.is_alive() and result is not None:
                        yield json.dumps(result)
                        break
                    time.sleep(0.1)

            return StreamingResponse(generate(), media_type="application/json")
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    except Exception as exc:
        logger.error(f"[chat_completions] 未知异常: {exc}")
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})
    finally:
        if getattr(request.state, "use_config_token", False) and hasattr(
            request.state, "account"
        ):
            release_account(request.state.account)


# ----------------------------------------------------------------------
# Claude 路由：/anthropic/v1/messages
# ----------------------------------------------------------------------
@app.post("/anthropic/v1/messages")
async def claude_messages(request: Request):
    try:
        # 处理 token 相关逻辑，若认证失败则直接返回错误响应
        try:
            determine_claude_mode_and_token(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code, content={"error": exc.detail}
            )
        except Exception as exc:
            logger.error(f"[claude_messages] determine_claude_mode_and_token 异常: {exc}")
            return JSONResponse(
                status_code=500, content={"error": "Claude authentication failed."}
            )

        req_data = await request.json()
        model = req_data.get("model")
        messages = req_data.get("messages", [])
        
        if not model or not messages:
            raise HTTPException(
                status_code=400, detail="Request must include 'model' and 'messages'."
            )
        
        # 标准化消息内容 - 确保Claude Code兼容性
        normalized_messages = []
        for message in messages:
            normalized_message = message.copy()
            if isinstance(message.get("content"), list):
                # 将数组内容转换为单一字符串 - 改进版本
                content_parts = []
                for content_block in message["content"]:
                    if content_block.get("type") == "text" and "text" in content_block:
                        content_parts.append(content_block["text"])
                    elif content_block.get("type") == "tool_result":
                        # 保持工具结果格式不变，但提取内容用于处理
                        if "content" in content_block:
                            content_parts.append(str(content_block["content"]))
                # 确保内容非空，避免空字符串导致的问题
                if content_parts:
                    normalized_message["content"] = "\n".join(content_parts)
                elif isinstance(message.get("content"), list) and message["content"]:
                    # 如果没有提取到文本内容，保持原始格式
                    normalized_message["content"] = message["content"]
                else:
                    normalized_message["content"] = ""
            normalized_messages.append(normalized_message)
        
        # 处理工具使用请求
        tools_requested = req_data.get("tools") or []
        has_tools = len(tools_requested) > 0
        
        # 检查是否包含工具结果（tool_result）
        has_tool_result = False
        for message in messages:
            if isinstance(message.get("content"), list):
                for content_block in message["content"]:
                    if content_block.get("type") == "tool_result":
                        has_tool_result = True
                        break

        # 处理Claude格式请求（使用标准化后的消息）
        payload = req_data.copy()
        payload["messages"] = normalized_messages.copy()
        
        # 如果有工具定义，添加工具使用指导的系统消息
        if has_tools and not any(m.get("role") == "system" for m in payload["messages"]):
            tool_schemas = []
            for tool in tools_requested:
                tool_name = tool.get('name', 'unknown')
                tool_desc = tool.get('description', 'No description available')
                schema = tool.get('input_schema', {})
                
                tool_info = f"Tool: {tool_name}\nDescription: {tool_desc}"
                if 'properties' in schema:
                    props = []
                    required = schema.get('required', [])
                    for prop_name, prop_info in schema['properties'].items():
                        prop_type = prop_info.get('type', 'string')
                        is_req = ' (required)' if prop_name in required else ''
                        props.append(f"  - {prop_name}: {prop_type}{is_req}")
                    if props:
                        tool_info += f"\nParameters:\n{chr(10).join(props)}"
                tool_schemas.append(tool_info)
            
            system_message = {
                "role": "system",
                "content": f"""You are Claude, a helpful AI assistant. You have access to these tools:

{chr(10).join(tool_schemas)}

When you need to use tools, you can call multiple tools in a single response. Use this format:

{{"tool_calls": [
  {{"name": "tool1", "input": {{"param": "value"}}}},
  {{"name": "tool2", "input": {{"param": "value"}}}}
]}}

IMPORTANT: You can call multiple tools in ONE response. If you need to:
1. Create a directory - include that in tool_calls
2. Write a file - include that in the SAME tool_calls array
3. Run a command - include that in the SAME tool_calls array

Example of multiple tool calls in one response:
{{"tool_calls": [
  {{"name": "str_replace_editor", "input": {{"command": "create", "path": "pp1/hello.py", "file_text": "print('Hello, World!')"}}}},
  {{"name": "Bash", "input": {{"command": "python pp1/hello.py"}}}}
]}}

Examples:
- For TodoWrite: {{"name": "TodoWrite", "input": {{"todos": [{{"content": "task", "status": "pending", "activeForm": "doing task"}}]}}}}
- For str_replace_editor: {{"name": "str_replace_editor", "input": {{"command": "create", "path": "file.py", "file_text": "code"}}}}
- For Bash: {{"name": "Bash", "input": {{"command": "cd /path && python file.py"}}}}

Remember: Output ONLY the JSON, no other text. The response must start with {{ and end with ]}}"""
            }
            payload["messages"].insert(0, system_message)

        deepseek_resp = await call_claude_via_openai(request, payload)
        if not deepseek_resp:
            raise HTTPException(status_code=500, detail="Failed to get Claude response.")

        created_time = int(time.time())
        
        # 处理响应
        if deepseek_resp.status_code != 200:
            deepseek_resp.close()
            return JSONResponse(
                status_code=500, 
                content={"error": {"type": "api_error", "message": "Failed to get response"}}
            )

        # 流式响应或普通响应
        if bool(req_data.get("stream", False)):
            def claude_sse_stream():
                try:
                    message_id = f"msg_{int(time.time())}_{random.randint(1000, 9999)}"
                    input_tokens = sum(len(str(m.get("content", ""))) for m in messages) // 4
                    output_tokens = 0
                    
                    # 收集所有响应内容
                    full_response_text = ""
                    response_completed = False
                    
                    # 解析DeepSeek流式响应
                    for line in deepseek_resp.iter_lines():
                        if not line:
                            continue
                        try:
                            line_str = line.decode('utf-8')
                        except Exception:
                            continue
                            
                        if line_str.startswith('data:'):
                            data_str = line_str[5:].strip()
                            if data_str == '[DONE]':
                                response_completed = True
                                break
                                
                            try:
                                chunk = json.loads(data_str)
                                if "v" in chunk and isinstance(chunk["v"], str):
                                    full_response_text += chunk["v"]
                                elif "v" in chunk and isinstance(chunk["v"], list):
                                    # 检查完成状态
                                    for item in chunk["v"]:
                                        if item.get("p") == "status" and item.get("v") == "FINISHED":
                                            response_completed = True
                                            break
                            except (json.JSONDecodeError, KeyError):
                                continue
                    
                    # 现在一次性发送Claude格式的事件
                    
                    # 1. message_start
                    message_start = {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": input_tokens, "output_tokens": 0}
                        }
                    }
                    yield f"data: {json.dumps(message_start)}\n\n"
                    
                    # 2. 检查是否有工具调用 - 改进的检测逻辑
                    detected_tools = []
                    
                    # 清理响应文本
                    cleaned_response = full_response_text.strip()
                    
                    # 记录原始响应用于调试
                    logger.debug(f"[Tool Detection] Raw response: {cleaned_response[:500] if cleaned_response else 'Empty'}")
                    
                    # 尝试多种工具调用检测方法
                    detected_tools = []
                    tool_detected = False
                    
                    # 方法1: 检测完整的JSON格式
                    if cleaned_response.startswith('{"tool_calls":') and cleaned_response.endswith(']}'):
                        logger.info(f"[Tool Detection] Method 1: Found tool calls JSON")
                        try:
                            tool_data = json.loads(cleaned_response)
                            for tool_call in tool_data.get('tool_calls', []):
                                tool_name = tool_call.get('name')
                                tool_input = tool_call.get('input', {})
                                
                                # 检查是否是有效的工具名称
                                if any(tool.get('name') == tool_name for tool in tools_requested):
                                    detected_tools.append({
                                        'name': tool_name,
                                        'input': tool_input
                                    })
                                    tool_detected = True
                        except json.JSONDecodeError:
                            pass
                    
                    # 方法2: 使用正则表达式检测嵌入的JSON
                    if not tool_detected:
                        tool_call_pattern = r'\{\s*["\']tool_calls["\']\s*:\s*\[(.*?)\]\s*\}'
                        matches = re.findall(tool_call_pattern, cleaned_response, re.DOTALL)
                        
                        for match in matches:
                            try:
                                # 尝试解析工具调用JSON
                                tool_calls_json = f'{{"tool_calls": [{match}]}}'
                                tool_data = json.loads(tool_calls_json)
                                
                                for tool_call in tool_data.get('tool_calls', []):
                                    tool_name = tool_call.get('name')
                                    tool_input = tool_call.get('input', {})
                                    
                                    # 检查是否是有效的工具名称
                                    if any(tool.get('name') == tool_name for tool in tools_requested):
                                        detected_tools.append({
                                            'name': tool_name,
                                            'input': tool_input
                                        })
                                        tool_detected = True
                            except json.JSONDecodeError:
                                continue
                    
                    content_index = 0
                    if detected_tools:
                        # 有工具调用
                        stop_reason = "tool_use"
                        for tool_info in detected_tools:
                            tool_use_id = f"toolu_{int(time.time())}_{random.randint(1000, 9999)}_{content_index}"
                            tool_name = tool_info['name']
                            tool_input = tool_info['input']
                            
                            # content_block_start
                            yield f"data: {json.dumps({'type': 'content_block_start', 'index': content_index, 'content_block': {'type': 'tool_use', 'id': tool_use_id, 'name': tool_name, 'input': tool_input}})}\n\n"
                            
                            # content_block_stop
                            yield f"data: {json.dumps({'type': 'content_block_stop', 'index': content_index})}\n\n"

                            content_index += 1
                            output_tokens += len(str(tool_input)) // 4
                    else:
                        # 没有工具调用，普通文本响应
                        stop_reason = "end_turn"
                        if full_response_text:
                            yield f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': full_response_text}})}\n\n"
                            yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            output_tokens += len(full_response_text) // 4

                    # 3. message_delta 和 message_stop
                    yield f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}})}\n\n"
                    yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"
                        
                except Exception as e:
                    logger.error(f"[claude_sse_stream] 异常: {e}")
                    error_event = {
                        "type": "error",
                        "error": {"type": "api_error", "message": f"Stream processing error: {str(e)}"}
                    }
                    yield f"data: {json.dumps(error_event)}\n\n"
                finally:
                    try:
                        deepseek_resp.close()
                    except Exception:
                        pass
                    # 释放账号资源 
                    if getattr(request.state, "use_config_token", False) and hasattr(
                        request.state, "account"
                    ):
                        release_account(request.state.account)

            return StreamingResponse(
                claude_sse_stream(),
                media_type="text/event-stream",
                headers={"Content-Type": "text/event-stream"},
            )
        else:
            # 非流式响应处理 - 添加工具调用支持
            try:
                final_content = ""
                final_reasoning = ""
                
                for line in deepseek_resp.iter_lines():
                    if not line:
                        continue
                    
                    try:
                        line_str = line.decode('utf-8')
                    except Exception as e:
                        logger.warning(f"[claude_messages] 行解码失败: {e}")
                        continue
                        
                    if line_str.startswith('data:'):
                        data_str = line_str[5:].strip()
                        if data_str == '[DONE]':
                            break
                        
                        try:
                            chunk = json.loads(data_str)
                            
                            # 使用DeepSeek的响应格式解析 - 提取 v 字段
                            if "v" in chunk:
                                v_value = chunk["v"]
                                
                                # 跳过搜索状态
                                if "p" in chunk and chunk.get("p") == "response/search_status":
                                    continue
                                    
                                # 判断内容类型
                                ptype = "text"
                                if "p" in chunk and chunk.get("p") == "response/thinking_content":
                                    ptype = "thinking"
                                elif "p" in chunk and chunk.get("p") == "response/content":
                                    ptype = "text"
                                
                                # 处理字符串形式的 v 值（即文本内容）
                                if isinstance(v_value, str):
                                    if ptype == "thinking":
                                        final_reasoning += v_value
                                    else:
                                        final_content += v_value
                                        
                                # 处理数组更新如状态变更
                                elif isinstance(v_value, list):
                                    for item in v_value:
                                        if item.get("p") == "status" and item.get("v") == "FINISHED":
                                            # 完成标志
                                            break
                                            
                        except json.JSONDecodeError as e:
                            logger.warning(f"[claude_messages] JSON解析失败: {e}, data: {data_str}")
                            continue
                        except Exception as e:
                            logger.warning(f"[claude_messages] chunk处理失败: {e}")
                            continue
                
                try:
                    deepseek_resp.close()
                except Exception as e:
                    logger.warning(f"[claude_messages] 关闭响应异常: {e}")
                
                # 检查是否包含工具调用 - 改进的检测逻辑
                detected_tools = []
                
                # 清理响应文本
                cleaned_content = final_content.strip()
                
                # 尝试多种工具调用检测方法
                tool_detected = False
                
                # 方法1: 检测完整的JSON格式
                if cleaned_content.startswith('{"tool_calls":') and cleaned_content.endswith(']}'):
                    try:
                        tool_data = json.loads(cleaned_content)
                        for tool_call in tool_data.get('tool_calls', []):
                            tool_name = tool_call.get('name')
                            tool_input = tool_call.get('input', {})
                            
                            # 检查是否是有效的工具名称
                            if any(tool.get('name') == tool_name for tool in tools_requested):
                                detected_tools.append({
                                    'name': tool_name,
                                    'input': tool_input
                                })
                                tool_detected = True
                    except json.JSONDecodeError:
                        pass
                
                # 方法2: 使用正则表达式检测嵌入的JSON
                if not tool_detected:
                    tool_call_pattern = r'\{\s*["\']tool_calls["\']\s*:\s*\[(.*?)\]\s*\}'
                    matches = re.findall(tool_call_pattern, cleaned_content, re.DOTALL)
                    
                    for match in matches:
                        try:
                            # 尝试解析工具调用JSON
                            tool_calls_json = f'{{"tool_calls": [{match}]}}'
                            tool_data = json.loads(tool_calls_json)
                            
                            for tool_call in tool_data.get('tool_calls', []):
                                tool_name = tool_call.get('name')
                                tool_input = tool_call.get('input', {})
                                
                                # 检查是否是有效的工具名称
                                if any(tool.get('name') == tool_name for tool in tools_requested):
                                    detected_tools.append({
                                        'name': tool_name,
                                        'input': tool_input
                                    })
                                    tool_detected = True
                        except json.JSONDecodeError:
                            continue
                
                # 构造标准的Anthropic Messages API响应格式
                claude_response = {
                    "id": f"msg_{int(time.time())}_{random.randint(1000, 9999)}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": "tool_use" if detected_tools else "end_turn",
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": len(str(normalized_messages)) // 4,
                        "output_tokens": (len(final_content) + len(final_reasoning)) // 4
                    }
                }
                
                # 如果有推理内容，添加思考块
                if final_reasoning:
                    claude_response["content"].append({
                        "type": "thinking",
                        "thinking": final_reasoning
                    })
                
                # 处理工具调用
                if detected_tools:
                    for i, tool_info in enumerate(detected_tools):
                        tool_use_id = f"toolu_{int(time.time())}_{random.randint(1000, 9999)}_{i}"
                        tool_name = tool_info['name']
                        tool_input = tool_info['input']
                        
                        claude_response["content"].append({
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": tool_name,
                            "input": tool_input
                        })
                else:
                    # 没有工具调用，添加普通文本内容
                    if final_content or not final_reasoning:
                        claude_response["content"].append({
                            "type": "text",
                            "text": final_content or "抱歉，没有生成有效的响应内容。"
                        })
                
                return JSONResponse(content=claude_response, status_code=200)
                
            except Exception as e:
                logger.error(f"[claude_messages] 非流式响应处理异常: {e}")
                try:
                    deepseek_resp.close()
                except Exception as close_e:
                    logger.warning(f"[claude_messages] 关闭响应异常2: {close_e}")
                return JSONResponse(
                    status_code=500, 
                    content={"error": {"type": "api_error", "message": "Response processing error"}}
                )

    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": {"type": "invalid_request_error", "message": exc.detail}})
    except Exception as exc:
        logger.error(f"[claude_messages] 未知异常: {exc}")
        return JSONResponse(status_code=500, content={"error": {"type": "api_error", "message": "Internal Server Error"}})
    finally:
        # 释放账号资源
        if getattr(request.state, "use_config_token", False) and hasattr(
            request.state, "account"
        ):
            release_account(request.state.account)


# ----------------------------------------------------------------------
# Claude 路由：/anthropic/v1/messages/count_tokens
# ----------------------------------------------------------------------
@app.post("/anthropic/v1/messages/count_tokens")
async def claude_count_tokens(request: Request):
    try:
        # 处理 token 相关逻辑，若认证失败则直接返回错误响应
        try:
            determine_claude_mode_and_token(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code, content={"error": exc.detail}
            )
        except Exception as exc:
            logger.error(f"[claude_count_tokens] determine_claude_mode_and_token 异常: {exc}")
            return JSONResponse(
                status_code=500, content={"error": "Claude authentication failed."}
            )

        req_data = await request.json()
        model = req_data.get("model")
        messages = req_data.get("messages", [])
        system = req_data.get("system", "")
        
        if not model or not messages:
            raise HTTPException(
                status_code=400, detail="Request must include 'model' and 'messages'."
            )
        
        # 计算输入token数量
        def estimate_tokens(text):
            """简单的token估算，约4个字符=1个token"""
            if isinstance(text, str):
                return len(text) // 4
            elif isinstance(text, list):
                return sum(estimate_tokens(item.get("text", "")) if isinstance(item, dict) else estimate_tokens(str(item)) for item in text)
            else:
                return len(str(text)) // 4
        
        # 计算消息的token数量
        input_tokens = 0
        
        # 添加系统消息的token
        if system:
            input_tokens += estimate_tokens(system)
            
        # 添加消息列表的token
        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")
            
            # 角色标记大约占用2个token
            input_tokens += 2
            
            # 内容token计算
            if isinstance(content, list):
                for content_block in content:
                    if isinstance(content_block, dict):
                        if content_block.get("type") == "text":
                            input_tokens += estimate_tokens(content_block.get("text", ""))
                        elif content_block.get("type") == "tool_result":
                            input_tokens += estimate_tokens(content_block.get("content", ""))
                        else:
                            # 其他类型的内容块
                            input_tokens += estimate_tokens(str(content_block))
                    else:
                        input_tokens += estimate_tokens(str(content_block))
            else:
                input_tokens += estimate_tokens(content)
        
        # 处理工具定义
        tools = req_data.get("tools", [])
        if tools:
            for tool in tools:
                # 工具名称和描述
                input_tokens += estimate_tokens(tool.get("name", ""))
                input_tokens += estimate_tokens(tool.get("description", ""))
                
                # 工具参数schema
                input_schema = tool.get("input_schema", {})
                input_tokens += estimate_tokens(json.dumps(input_schema, ensure_ascii=False))
        
        # 构造响应
        response = {
            "input_tokens": max(1, input_tokens)  # 至少1个token
        }
        
        return JSONResponse(content=response, status_code=200)
        
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": {"type": "invalid_request_error", "message": exc.detail}})
    except Exception as exc:
        logger.error(f"[claude_count_tokens] 未知异常: {exc}")
        return JSONResponse(status_code=500, content={"error": {"type": "api_error", "message": "Internal Server Error"}})


# ----------------------------------------------------------------------
# (11) 路由：/
# ----------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("welcome.html", {"request": request})


# ----------------------------------------------------------------------
# 启动 FastAPI 应用
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)