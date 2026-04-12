import base64
import heapq
import json
import logging
import queue
import random
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from powc import compute_pow_answer
import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

# -------------------------- 日志配置 --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")

app = FastAPI()

# 自定义路由类，用于记录请求/响应日志
class LoggingRoute(APIRoute):
    def get_route_handler(self):
        original_route_handler = super().get_route_handler()
        async def log_route_handler(request: Request) -> Response:
            request_id = str(uuid.uuid4())[:8]
            request.state.request_id = request_id

            # 读取请求体（保留以便后续使用）
            body = await request.body()
            # 重新设置 body，因为 body 已被消费
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive

            logger.info(f"[{request_id}] {request.method} {request.url.path}")

            # 记录关键请求头
            headers_to_log = {k: v for k, v in request.headers.items() if k.lower() in ["authorization", "content-type"]}
            logger.info(f"[{request_id}] Headers: {headers_to_log}")

            if body:
                try:
                    body_json = json.loads(body)
                    logger.info(f"[{request_id}] Body: {json.dumps(body_json, ensure_ascii=False)[:2000]}")
                except:
                    logger.info(f"[{request_id}] Body: {body[:1000]}")

            start_time = time.time()
            response = await original_route_handler(request)
            process_time = (time.time() - start_time) * 1000
            logger.info(f"[{request_id}] Response: {response.status_code} ({process_time:.2f}ms)")

            return response

        return log_route_handler

app.router.route_class = LoggingRoute

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

templates = Jinja2Templates(directory="templates")

# ----------------------------------------------------------------------
# (1) 配置文件的读写函数
# ----------------------------------------------------------------------
CONFIG_PATH = "config.json"

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[load_config] 无法读取配置文件: {e}")
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[save_config] 写入 config.json 失败: {e}")

CONFIG = load_config()

# -------------------------- 全局账号队列 --------------------------

def get_account_identifier(account):
    return account.get("email", "").strip() or account.get("mobile", "").strip()

class AccountManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.accounts = {}
        self.usage_heap = []
        self.in_use = set()
        self.last_used_time = {}

    def load_accounts(self, accounts_list):
        with self.lock:
            self.accounts.clear()
            self.usage_heap.clear()
            self.in_use.clear()
            self.last_used_time.clear()
            for acc in accounts_list:
                acc_id = get_account_identifier(acc)
                if acc_id:
                    self.accounts[acc_id] = acc
                    last_time = 0
                    self.last_used_time[acc_id] = last_time
                    heapq.heappush(self.usage_heap, (last_time, acc_id))
            logger.info(f"[AccountManager] 已加载 {len(self.accounts)} 个账号")

    def get_next_account(self, exclude_ids=None):
        if exclude_ids is None:
            exclude_ids = []
        with self.lock:
            available_accounts = []
            temp_heap = []
            while self.usage_heap:
                last_time, acc_id = heapq.heappop(self.usage_heap)
                if acc_id not in self.in_use and acc_id not in exclude_ids:
                    available_accounts.append((last_time, acc_id))
                else:
                    temp_heap.append((last_time, acc_id))

            for item in temp_heap:
                heapq.heappush(self.usage_heap, item)

            if not available_accounts:
                logger.warning("[AccountManager] 没有可用的账号")
                return None

            available_accounts.sort()
            last_time, acc_id = available_accounts[0]
            for item in available_accounts[1:]:
                heapq.heappush(self.usage_heap, item)

            self.in_use.add(acc_id)
            current_time = time.time()
            self.last_used_time[acc_id] = current_time
            heapq.heappush(self.usage_heap, (current_time, acc_id))

            account = self.accounts.get(acc_id)
            logger.info(f"[AccountManager] 选择账号: {acc_id}")
            return account

    def release_account(self, account):
        if not account:
            return
        acc_id = get_account_identifier(account)
        if acc_id and acc_id in self.in_use:
            self.in_use.remove(acc_id)
            logger.debug(f"[AccountManager] 释放账号: {acc_id}")

    def mark_account_failed(self, account):
        if not account:
            return
        acc_id = get_account_identifier(account)
        if acc_id:
            if acc_id in self.in_use:
                self.in_use.remove(acc_id)
            future_time = time.time() + 300
            self.last_used_time[acc_id] = future_time
            self.usage_heap = [(t, i) for t, i in self.usage_heap if i != acc_id]
            heapq.heappush(self.usage_heap, (future_time, acc_id))
            logger.warning(f"[AccountManager] 账号标记失败，5分钟后重试: {acc_id}")

    def get_stats(self):
        with self.lock:
            total = len(self.accounts)
            in_use_count = len(self.in_use)
            available = total - in_use_count
            return {"total_accounts": total, "in_use": in_use_count, "available": available}

account_manager = AccountManager()

def init_account_queue():
    accounts = CONFIG.get("accounts", [])[:]
    account_manager.load_accounts(accounts)
    random.shuffle(accounts)

init_account_queue()

# ----------------------------------------------------------------------
# (2) DeepSeek 相关常量（Expert 模式）
# ----------------------------------------------------------------------
DEEPSEEK_HOST = "chat.deepseek.com"
DEEPSEEK_LOGIN_URL = f"https://{DEEPSEEK_HOST}/api/v0/users/login"
DEEPSEEK_CREATE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/create"
DEEPSEEK_CREATE_POW_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/create_pow_challenge"
DEEPSEEK_COMPLETION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/completion"

BASE_HEADERS = {
    "Host": "chat.deepseek.com",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-client-platform": "web",
    "x-client-version": "1.8.0",
    "x-client-locale": "en_US",
    "x-app-version": "20241129.1",
    "x-client-timezone-offset": "28800",
    "accept-charset": "UTF-8",
}

WASM_PATH = "sha3_wasm_bg.7b9ca65ddd.wasm"

# ----------------------------------------------------------------------
# (3) 登录函数
# ----------------------------------------------------------------------
def login_deepseek_via_account(account):
    email = account.get("email", "").strip()
    mobile = account.get("mobile", "").strip()
    password = account.get("password", "").strip()

    if not password or (not email and not mobile):
        raise HTTPException(status_code=400, detail="账号缺少必要信息")

    if email:
        payload = {"email": email, "password": password, "device_id": "deepseek_to_api", "os": "android"}
    else:
        payload = {"mobile": mobile, "area_code": None, "password": password, "device_id": "deepseek_to_api", "os": "android"}

    try:
        resp = requests.post(DEEPSEEK_LOGIN_URL, headers=BASE_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] 登录异常: {e}")
        raise HTTPException(status_code=500, detail="Account login failed")

    if data.get("data", {}).get("biz_data", {}).get("user") is None:
        raise HTTPException(status_code=500, detail="登录响应格式错误")

    new_token = data["data"]["biz_data"]["user"].get("token")
    if not new_token:
        raise HTTPException(status_code=500, detail="登录响应缺少 token")

    account["token"] = new_token

    # 更新配置
    accounts = CONFIG.get("accounts", [])
    acc_id = get_account_identifier(account)
    for i, acc in enumerate(accounts):
        if get_account_identifier(acc) == acc_id:
            accounts[i] = account
            break
    CONFIG["accounts"] = accounts
    save_config(CONFIG)

    return new_token

def update_account_in_config(updated_account):
    # 此函数已被 login_deepseek_via_account 内部调用，保留占位
    pass

def choose_new_account(exclude_ids=None):
    return account_manager.get_next_account(exclude_ids)

def release_account(account):
    account_manager.release_account(account)

def mark_account_failed(account):
    account_manager.mark_account_failed(account)

# ----------------------------------------------------------------------
# (4) 模式判断
# ----------------------------------------------------------------------
def determine_mode_and_token(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    caller_key = auth_header.replace("Bearer ", "", 1).strip()
    config_keys = CONFIG.get("keys", [])

    if caller_key in config_keys:
        request.state.use_config_token = True
        request.state.tried_accounts = []
        selected_account = choose_new_account()
        if not selected_account:
            raise HTTPException(status_code=429, detail="No available accounts")
        if not selected_account.get("token", "").strip():
            login_deepseek_via_account(selected_account)
        request.state.deepseek_token = selected_account.get("token")
        request.state.account = selected_account
    else:
        request.state.use_config_token = False
        request.state.deepseek_token = caller_key

def get_auth_headers(request: Request):
    return {**BASE_HEADERS, "authorization": f"Bearer {request.state.deepseek_token}"}

# ----------------------------------------------------------------------
# (5) 会话创建 (兼容新结构)
# ----------------------------------------------------------------------
def create_session(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            headers = get_auth_headers(request)
            session_resp = requests.post(DEEPSEEK_CREATE_SESSION_URL, headers=headers, json={})
            session_resp.raise_for_status()
            session_data = session_resp.json()
            session_id = session_data["data"]["biz_data"]["chat_session"]["id"]
            logger.info(f"[{request.state.request_id}] 创建会话成功: {session_id}")
            return session_id
        except Exception as e:
            attempts += 1
            logger.warning(f"[{request.state.request_id}] 创建会话失败(尝试 {attempts}/{max_attempts}): {e}")
            if attempts < max_attempts:
                if request.state.use_config_token and hasattr(request.state, "account"):
                    mark_account_failed(request.state.account)
                    selected_account = choose_new_account(request.state.tried_accounts if hasattr(request.state, "tried_accounts") else [])
                    if not selected_account:
                        raise HTTPException(status_code=429, detail="No available accounts after retries")
                    request.state.tried_accounts.append(get_account_identifier(selected_account))
                    if not selected_account.get("token", "").strip():
                        login_deepseek_via_account(selected_account)
                    request.state.deepseek_token = selected_account.get("token")
                    request.state.account = selected_account
                time.sleep(1)
            else:
                raise HTTPException(status_code=500, detail="Failed to create session after retries")

# ----------------------------------------------------------------------
# (6) PoW 挑战
# ----------------------------------------------------------------------
def solve_pow_challenge(challenge: str, prefix: str) -> str:
    """简单实现: 尝试找到合适的 suffix 使得 SHA3-256(prefix + suffix) 以 challenge 开头"""
    # 这里使用纯 Python 实现简化版本（实际可能需要调用 WASM）
    for i in range(100000):
        suffix = str(i)
        # 实际应计算 SHA3-256，这里简化返回固定值
        # 用户需要自行实现 WASM 加载逻辑
        pass
    return "0"

def get_pow_params(request: Request, session_id: str):
    headers = get_auth_headers(request)
    pow_payload = {"target_path": "/api/v0/chat/completion"}
    try:
        pow_resp = requests.post(DEEPSEEK_CREATE_POW_URL, headers=headers, json=pow_payload)
        pow_resp.raise_for_status()
        pow_data = pow_resp.json()
        
        challenge_data = pow_data["data"]["biz_data"]["challenge"]
        algorithm = challenge_data["algorithm"]
        challenge = challenge_data["challenge"]
        salt = challenge_data["salt"]
        signature = challenge_data["signature"]
        difficulty = challenge_data["difficulty"]
        expire_at = challenge_data["expire_at"]
        target_path = challenge_data["target_path"]
        
        logger.info(f"[{request.state.request_id}] PoW 算法: {algorithm}, 难度: {difficulty}")
        
        # 计算答案
        answer = compute_pow_answer(
            algorithm=algorithm,
            challenge_str=challenge,
            salt=salt,
            difficulty=difficulty,
            expire_at=expire_at,
            signature=signature,
            target_path=target_path,
            wasm_path=WASM_PATH,
            max_time=10.0
        )
        
        if answer is None:
            logger.warning(f"[{request.state.request_id}] PoW 计算失败")
            raise HTTPException(status_code=500, detail="PoW computation failed")
            
        logger.info(f"[{request.state.request_id}] PoW 答案: {answer}")
        
        # 构建 PoW 响应对象
        pow_response = {
            "algorithm": algorithm,
            "challenge": challenge,
            "salt": salt,
            "answer": answer,
            "signature": signature,
            "target_path": target_path
        }
        
        # Base64 编码
        import base64
        pow_response_b64 = base64.b64encode(
            json.dumps(pow_response, separators=(',', ':')).encode()
        ).decode()
        
        return pow_response_b64
        
    except Exception as e:
        logger.error(f"[{request.state.request_id}] 获取 PoW 参数失败: {e}")
        raise HTTPException(status_code=500, detail="Failed to get PoW parameters")

# ----------------------------------------------------------------------
# (7) 消息预处理
# ----------------------------------------------------------------------
def build_prompt(messages: list) -> str:
    processed = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if item.get("type") == "text"]
            text = "\n".join(texts)
        else:
            text = str(content)
        processed.append({"role": role, "text": text})

    if not processed:
        return ""

    merged = [processed[0]]
    for msg in processed[1:]:
        if msg["role"] == merged[-1]["role"]:
            merged[-1]["text"] += "\n\n" + msg["text"]
        else:
            merged.append(msg)

    parts = []
    for idx, block in enumerate(merged):
        role = block["role"]
        text = block["text"]
        if role == "assistant":
            parts.append(f"{text}")
        elif role in ("user", "system"):
            if idx > 0:
                parts.append(f"{text}")
            else:
                parts.append(text)
        else:
            parts.append(text)

    final_prompt = "".join(parts)
    final_prompt = re.sub(r"!\[(.*?)\]\((.*?)\)", r"[\1](\2)", final_prompt)
    return final_prompt

# ----------------------------------------------------------------------
# (8) 对话接口重试
# ----------------------------------------------------------------------
def call_completion_endpoint(payload, headers, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            resp = requests.post(DEEPSEEK_COMPLETION_URL, headers=headers, json=payload, stream=True)
            resp.raise_for_status()
            return resp
        except Exception as e:
            attempts += 1
            logger.warning(f"[call_completion_endpoint] 请求失败(尝试 {attempts}/{max_attempts}): {e}")
            if attempts >= max_attempts:
                raise

# ----------------------------------------------------------------------
# (9) SSE 流解析
# ----------------------------------------------------------------------
def parse_sse_stream(resp, result_queue: queue.Queue):
    """解析 SSE 流，将解析后的数据放入队列"""
    buf = deque()
    try:
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8")
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    result_queue.put(None)  # 结束信号
                    break
                try:
                    chunk = json.loads(data_str)
                    result_queue.put(chunk)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error(f"[parse_sse_stream] 异常: {e}")
        result_queue.put(None)

# ----------------------------------------------------------------------
# (10) 主接口: /v1/chat/completions
# ----------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    account_released = False

    try:
        # 判断模式
        determine_mode_and_token(request)

        body = await request.json()
        model = body.get("model", "deepseek-chat")
        messages = body.get("messages", [])
        stream = body.get("stream", False)
        max_tokens = body.get("max_tokens", 4096)

        # 判断是否为推理模式
        internal_thinking = model == "deepseek-reasoner"

        if not messages:
            raise HTTPException(status_code=400, detail="Missing messages")

        # 创建会话
        session_id = create_session(request)

        # 获取 PoW 响应（Base64编码的JSON）
        pow_response_b64 = get_pow_params(request, session_id)

        # 构建消息
        final_prompt = build_prompt(messages)

        # 构建请求体
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": final_prompt,
            "ref_file_ids": [],
            "thinking_enabled": internal_thinking,
            "search_enabled": False,
            "preempt": False
        }

        headers = get_auth_headers(request)
        headers["x-ds-pow-response"] = pow_response_b64

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_time = int(time.time())

        deepseek_resp = call_completion_endpoint(payload, headers)

        # -------------------- 流式响应 --------------------
        if stream:
            result_queue = queue.Queue()
            parse_thread = threading.Thread(target=parse_sse_stream, args=(deepseek_resp, result_queue))
            parse_thread.start()

            def sse_stream():
                nonlocal account_released
                KEEP_ALIVE_TIMEOUT = 15
                FINISHED_DELAY = 0.5
                last_send_time = time.time()
                pending_finished = None
                pending_finished_time = 0
                first_chunk_sent = False

                final_text = ""
                final_thinking = ""
                
                chunk_count = 0
                message_complete = False

                try:
                    while True:
                        current_time = time.time()
                        if current_time - last_send_time >= KEEP_ALIVE_TIMEOUT:
                            yield ": keep-alive\n\n"
                            last_send_time = current_time
                            continue

                        if pending_finished is not None:
                            if time.time() - pending_finished_time >= FINISHED_DELAY:
                                yield "data: [DONE]\n\n"
                                pending_finished = None
                                break

                        try:
                            chunk = result_queue.get(timeout=0.05)

                            if chunk is None:
                                # 流结束，发送完整的消息
                                prompt_tokens = len(final_prompt) // 4
                                thinking_tokens = len(final_thinking) // 4
                                completion_tokens = len(final_text) // 4

                                usage = {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": thinking_tokens + completion_tokens,
                                    "total_tokens": prompt_tokens + thinking_tokens + completion_tokens,
                                    "completion_tokens_details": {"reasoning_tokens": thinking_tokens},
                                }

                                # 发送最终 usage 和 finish
                                finish_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
                                    "usage": usage,
                                }
                                yield f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                break

                            chunk_count += 1
                            v_value = chunk.get("v", "")
                            p_value = chunk.get("p", "")

                            # 记录前10个 chunk
                            if chunk_count <= 10:
                                logger.info(f"[{request_id}] Chunk #{chunk_count}: p={p_value}, v_type={type(v_value)}")

                            # 处理新的响应格式
                            if isinstance(v_value, dict) and "response" in v_value:
                                response_data = v_value["response"]
                                
                                # 提取 fragments 中的内容
                                fragments = response_data.get("fragments", [])
                                for fragment in fragments:
                                    if fragment.get("type") == "RESPONSE":
                                        content = fragment.get("content", "")
                                        if content and not message_complete:
                                            final_text = content
                                            message_complete = True
                                            
                                            # 发送内容 chunk
                                            delta_obj = {"role": "assistant", "content": content}
                                            out_chunk = {
                                                "id": completion_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_time,
                                                "model": model,
                                                "choices": [{"delta": delta_obj, "index": 0}],
                                            }
                                            yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                            last_send_time = current_time
                                            first_chunk_sent = True
                                            
                                            logger.info(f"[{request_id}] 提取内容: {repr(content)}")
                                
                                continue

                            # 处理状态更新
                            if p_value == "response/status" and v_value == "FINISHED":
                                pending_finished = chunk
                                pending_finished_time = time.time()
                                continue

                            # 处理旧的格式（兼容性）
                            if v_value == "FINISHED":
                                pending_finished = chunk
                                pending_finished_time = time.time()
                                continue

                            if p_value == "response/thinking_content" and isinstance(v_value, str):
                                final_thinking += v_value
                            elif p_value == "response/content" and isinstance(v_value, str):
                                if not final_text:
                                    logger.info(f"[{request_id}] 第一个内容chunk: {repr(v_value)}")
                                final_text += v_value
                                
                                delta_obj = {}
                                if not first_chunk_sent:
                                    delta_obj["role"] = "assistant"
                                    first_chunk_sent = True
                                
                                if v_value:
                                    delta_obj["content"] = v_value
                                    
                                if delta_obj:
                                    out_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [{"delta": delta_obj, "index": 0}],
                                    }
                                    yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                    last_send_time = current_time

                        except queue.Empty:
                            continue

                except Exception as e:
                    logger.error(f"[sse_stream] 异常: {e}")
                finally:
                    if request.state.use_config_token and hasattr(request.state, "account") and not account_released:
                        release_account(request.state.account)
                        account_released = True

            return StreamingResponse(sse_stream(), media_type="text/event-stream", headers={"Content-Type": "text/event-stream"})

        # -------------------- 非流式响应 --------------------
        else:
            final_content = ""
            final_reasoning = ""
            
            for raw_line in deepseek_resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8")
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        v_value = chunk.get("v", "")
                        p_value = chunk.get("p", "")
                        
                        # 处理新的响应格式
                        if isinstance(v_value, dict) and "response" in v_value:
                            fragments = v_value["response"].get("fragments", [])
                            for fragment in fragments:
                                if fragment.get("type") == "RESPONSE":
                                    final_content = fragment.get("content", "")
                                    break
                        
                        # 兼容旧格式
                        elif p_value == "response/thinking_content" and isinstance(v_value, str):
                            final_reasoning += v_value
                        elif p_value == "response/content" and isinstance(v_value, str):
                            final_content += v_value
                            
                    except Exception as e:
                        logger.warning(f"[{request_id}] 解析非流式 chunk 失败: {e}")

            deepseek_resp.close()

            prompt_tokens = len(final_prompt) // 4
            reasoning_tokens = len(final_reasoning) // 4
            completion_tokens = len(final_content) // 4

            # 构建标准 OpenAI 格式响应
            message = {"role": "assistant", "content": final_content}
            if final_reasoning and internal_thinking:
                message["reasoning_content"] = final_reasoning

            result = {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_time,
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": reasoning_tokens + completion_tokens,
                    "total_tokens": prompt_tokens + reasoning_tokens + completion_tokens,
                }
            }

            logger.info(f"[{request_id}] 响应完成, content长度={len(final_content)}, reasoning长度={len(final_reasoning)}")
            return JSONResponse(content=result)

    except HTTPException as exc:
        logger.error(f"[{request_id}] HTTPException: {exc.status_code} - {exc.detail}")
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    except Exception as exc:
        logger.error(f"[{request_id}] 未知异常: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})
    finally:
        if not account_released and request.state.use_config_token and hasattr(request.state, "account"):
            release_account(request.state.account)
            account_released = True

# ----------------------------------------------------------------------
# (11) 其他路由
# ----------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("welcome.html", {"request": request})

@app.get("/admin/account_stats")
def account_stats():
    return JSONResponse(content=account_manager.get_stats())

# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)
