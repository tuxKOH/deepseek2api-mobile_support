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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# -------------------------- 配置 --------------------------
DEBUG = False  # 設為 True 開啟詳細日誌

LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")

app = FastAPI()

# 自定義路由，簡化日誌
class LoggingRoute(APIRoute):
    def get_route_handler(self):
        original_route_handler = super().get_route_handler()
        async def log_route_handler(request: Request) -> Response:
            request_id = str(uuid.uuid4())[:8]
            request.state.request_id = request_id

            body = await request.body()
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive

            if DEBUG:
                logger.debug(f"[{request_id}] {request.method} {request.url.path}")
                if body:
                    try:
                        body_json = json.loads(body)
                        logger.debug(f"[{request_id}] Body: {json.dumps(body_json, ensure_ascii=False)[:500]}")
                    except:
                        logger.debug(f"[{request_id}] Body: {body[:500]}")

            start_time = time.time()
            response = await original_route_handler(request)
            process_time = (time.time() - start_time) * 1000
            
            if response.status_code >= 400 or DEBUG:
                logger.info(f"[{request_id}] {request.method} {request.url.path} -> {response.status_code} ({process_time:.2f}ms)")
            
            return response
        return log_route_handler

app.router.route_class = LoggingRoute

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

templates = Jinja2Templates(directory="templates")

# -------------------------- 配置讀寫 --------------------------
CONFIG_PATH = "config.json"

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"無法讀取配置文件: {e}")
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入配置文件失敗: {e}")

CONFIG = load_config()

# -------------------------- 賬號管理 --------------------------
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
            logger.info(f"已加載 {len(self.accounts)} 個賬號")

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
                logger.warning("沒有可用的賬號")
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
            if DEBUG:
                logger.debug(f"選擇賬號: {acc_id}")
            return account

    def release_account(self, account):
        if not account:
            return
        acc_id = get_account_identifier(account)
        if acc_id and acc_id in self.in_use:
            self.in_use.remove(acc_id)

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
            logger.warning(f"賬號暫時禁用5分鐘: {acc_id}")

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

init_account_queue()

# -------------------------- DeepSeek 常量 --------------------------
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

# -------------------------- Selenium 登入 --------------------------
def login_deepseek_with_selenium(email, password):
    """使用 Selenium 直接 API 登入獲取 token"""
    
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36')
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    service = Service('/data/data/com.termux/files/usr/bin/chromedriver')
    
    driver = None
    try:
        if DEBUG:
            logger.debug("啟動瀏覽器...")
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        driver.get("https://chat.deepseek.com/")
        time.sleep(2)
        
        if DEBUG:
            logger.debug("直接 API 登入...")
        
        token = driver.execute_script(f"""
            return new Promise((resolve, reject) => {{
                fetch('https://chat.deepseek.com/api/v0/users/login', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'x-client-platform': 'web',
                        'x-client-version': '1.8.0',
                    }},
                    body: JSON.stringify({{
                        email: '{email}',
                        password: '{password}',
                        device_id: 'selenium_login',
                        os: 'android'
                    }})
                }})
                .then(response => response.json())
                .then(data => {{
                    let token = data?.data?.biz_data?.user?.token;
                    resolve(token || '');
                }})
                .catch(error => {{
                    console.error('Login error:', error);
                    resolve('');
                }});
            }});
        """)
        
        if not token or len(token) < 10:
            raise Exception("無法獲取 token")
        
        logger.info(f"登入成功: {token[:20]}...")
        return token
        
    except Exception as e:
        logger.error(f"Selenium 登入失敗: {e}")
        raise
        
    finally:
        if driver:
            driver.quit()


def login_deepseek_via_account(account):
    """登入並獲取 token"""
    email = account.get("email", "").strip()
    mobile = account.get("mobile", "").strip()
    password = account.get("password", "").strip()

    if not password or (not email and not mobile):
        raise HTTPException(status_code=400, detail="賬號缺少必要信息")

    # 嘗試使用現有 token
    existing_token = account.get("token", "")
    if existing_token:
        try:
            test_resp = requests.get(
                "https://chat.deepseek.com/api/v0/user/me",
                headers={**BASE_HEADERS, "Authorization": f"Bearer {existing_token}"},
                timeout=10
            )
            if test_resp.status_code == 200:
                if DEBUG:
                    logger.debug("現有 token 有效")
                return existing_token
        except:
            pass

    # Token 過期或不存在，使用 Selenium 登入
    try:
        new_token = login_deepseek_with_selenium(email or mobile, password)
        
        account["token"] = new_token
        
        # 更新配置文件
        accounts = CONFIG.get("accounts", [])
        acc_id = get_account_identifier(account)
        for i, acc in enumerate(accounts):
            if get_account_identifier(acc) == acc_id:
                accounts[i] = account
                break
        CONFIG["accounts"] = accounts
        save_config(CONFIG)
        
        logger.info("Token 已更新並保存")
        return new_token
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"登入失敗: {e}")
        raise HTTPException(status_code=500, detail="Account login failed")


def choose_new_account(exclude_ids=None):
    return account_manager.get_next_account(exclude_ids)

def release_account(account):
    account_manager.release_account(account)

def mark_account_failed(account):
    account_manager.mark_account_failed(account)

# -------------------------- 模式判斷 --------------------------
def determine_mode_and_token(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")

    caller_key = auth_header.replace("Bearer ", "", 1).strip()
    config_keys = CONFIG.get("keys", [])

    if caller_key in config_keys:
        request.state.use_config_token = True
        request.state.tried_accounts = []
        selected_account = choose_new_account()
        if not selected_account:
            raise HTTPException(status_code=429, detail="No available accounts")
        
        # 確保有 token
        if not selected_account.get("token", "").strip():
            login_deepseek_via_account(selected_account)
        else:
            # 驗證現有 token
            try:
                test_resp = requests.get(
                    "https://chat.deepseek.com/api/v0/user/me",
                    headers={**BASE_HEADERS, "Authorization": f"Bearer {selected_account.get('token')}"},
                    timeout=10
                )
                if test_resp.status_code != 200:
                    login_deepseek_via_account(selected_account)
            except:
                login_deepseek_via_account(selected_account)
        
        request.state.deepseek_token = selected_account.get("token")
        request.state.account = selected_account
    else:
        # 用戶直接提供 token，驗證有效性
        try:
            test_resp = requests.get(
                "https://chat.deepseek.com/api/v0/user/me",
                headers={**BASE_HEADERS, "Authorization": f"Bearer {caller_key}"},
                timeout=10
            )
            if test_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid or expired token")
        except HTTPException:
            raise
        except:
            raise HTTPException(status_code=401, detail="Token validation failed")
        
        request.state.use_config_token = False
        request.state.deepseek_token = caller_key

def get_auth_headers(request: Request):
    return {**BASE_HEADERS, "authorization": f"Bearer {request.state.deepseek_token}"}

# -------------------------- 會話創建 --------------------------
def create_session(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            headers = get_auth_headers(request)
            session_resp = requests.post(DEEPSEEK_CREATE_SESSION_URL, headers=headers, json={})
            session_resp.raise_for_status()
            session_data = session_resp.json()
            session_id = session_data["data"]["biz_data"]["chat_session"]["id"]
            if DEBUG:
                logger.debug(f"創建會話成功: {session_id}")
            return session_id
        except Exception as e:
            attempts += 1
            if DEBUG:
                logger.warning(f"創建會話失敗 (嘗試 {attempts}/{max_attempts}): {e}")
            if attempts < max_attempts:
                if request.state.use_config_token and hasattr(request.state, "account"):
                    mark_account_failed(request.state.account)
                    selected_account = choose_new_account(
                        request.state.tried_accounts if hasattr(request.state, "tried_accounts") else []
                    )
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

# -------------------------- PoW --------------------------
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
        
        if DEBUG:
            logger.debug(f"PoW 算法: {algorithm}, 難度: {difficulty}")
        
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
            raise HTTPException(status_code=500, detail="PoW computation failed")
        
        pow_response = {
            "algorithm": algorithm,
            "challenge": challenge,
            "salt": salt,
            "answer": answer,
            "signature": signature,
            "target_path": target_path
        }
        
        pow_response_b64 = base64.b64encode(
            json.dumps(pow_response, separators=(',', ':')).encode()
        ).decode()
        
        return pow_response_b64
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"獲取 PoW 參數失敗: {e}")
        raise HTTPException(status_code=500, detail="Failed to get PoW parameters")

# -------------------------- 消息預處理 --------------------------
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
    for block in merged:
        role = block["role"]
        text = block["text"]
        parts.append(f"<｜{role}｜>\n{text}")

    final_prompt = "".join(parts)
    final_prompt = re.sub(r"!\[(.*?)\]\((.*?)\)", r"[\1](\2)", final_prompt)
    return final_prompt

# -------------------------- 請求重試 --------------------------
def call_completion_endpoint(payload, headers, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            resp = requests.post(DEEPSEEK_COMPLETION_URL, headers=headers, json=payload, stream=True)
            resp.raise_for_status()
            return resp
        except Exception as e:
            attempts += 1
            if DEBUG:
                logger.warning(f"請求失敗 (嘗試 {attempts}/{max_attempts}): {e}")
            if attempts >= max_attempts:
                raise

# -------------------------- SSE 解析 --------------------------
def parse_sse_stream(resp, result_queue: queue.Queue):
    try:
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8")
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    result_queue.put(None)
                    return
                try:
                    chunk = json.loads(data_str)
                    result_queue.put(chunk)
                except json.JSONDecodeError:
                    continue
        result_queue.put(None)
    except Exception as e:
        logger.error(f"SSE 解析異常: {e}")
        result_queue.put(None)

# -------------------------- 主接口 --------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_id = getattr(request.state, "request_id", "unknown")
    account_released = False

    try:
        determine_mode_and_token(request)

        body = await request.json()
        model = body.get("model", "deepseek-chat")
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        # 模型配置
        if model == "deepseek-v4-flash":
            model_type = "default"
            auto_search = False
        elif model == "deepseek-v4-pro":
            model_type = "expert"
            auto_search = False
        elif model == "deepseek-vision":
            model_type = "vision"
            auto_search = False
        elif model == "deepseek-v4-flash-searching":
            model_type = "default"
            auto_search = True
        elif model == "deepseek-v4-pro-searching":
            model_type = "expert"
            auto_search = True
        elif model == "deepseek-vision-searching":
            model_type = "vision"
            auto_search = True
        else:
            model_type = "expert"
            auto_search = False

        internal_thinking = (model == "deepseek-reasoner")

        if "thinking" in body:
            thinking_cfg = body["thinking"]
            if isinstance(thinking_cfg, dict) and "type" in thinking_cfg:
                if thinking_cfg["type"] == "enabled":
                    internal_thinking = True
                elif thinking_cfg["type"] == "disabled":
                    internal_thinking = False

        if not messages:
            raise HTTPException(status_code=400, detail="Missing messages")

        session_id = create_session(request)
        pow_response_b64 = get_pow_params(request, session_id)
        final_prompt = build_prompt(messages)

        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": final_prompt,
            "ref_file_ids": [],
            "thinking_enabled": internal_thinking,
            "search_enabled": auto_search,
            "model_type": model_type,
            "preempt": False
        }

        headers = get_auth_headers(request)
        headers["x-ds-pow-response"] = pow_response_b64

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_time = int(time.time())

        deepseek_resp = call_completion_endpoint(payload, headers)

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
                thinking_phase = internal_thinking
                thinking_text_started = False
                
                # 调试计数器
                chunk_count = 0
            
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
                            chunk_count += 1
            
                            if chunk is None:
                                logger.debug(f"[DEBUG] 收到结束标记，总区块数: {chunk_count}")
                                prompt_tokens = len(final_prompt) // 4
                                thinking_tokens = len(final_thinking) // 4
                                completion_tokens = len(final_text) // 4
            
                                usage = {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": thinking_tokens + completion_tokens,
                                    "total_tokens": prompt_tokens + thinking_tokens + completion_tokens,
                                    "completion_tokens_details": {"reasoning_tokens": thinking_tokens},
                                }
            
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
            
                            v_value = chunk.get("v", "")
                            p_value = chunk.get("p", "")
                            
                            # 调试输出每个区块的详细信息
                            logger.debug(f"[DEBUG] Chunk #{chunk_count}:")
                            logger.debug(f"  p_value (路径): {repr(p_value)}")
                            if isinstance(v_value, str):
                                display_value = v_value[:100] + "..." if len(v_value) > 100 else v_value
                                logger.debug(f"  v_value (内容): {repr(display_value)}")
                            elif isinstance(v_value, dict):
                                # 输出更完整的字典内容（限制长度）
                                dict_str = json.dumps(v_value, ensure_ascii=False)
                                if len(dict_str) > 500:
                                    dict_str = dict_str[:500] + "..."
                                logger.debug(f"  v_value (字典): {dict_str}")
                            else:
                                logger.debug(f"  v_value: {repr(v_value)}")
                            is_content_path = p_value and ("response/fragments/" in p_value or p_value == "response/content")
                            is_empty_p_with_content = not p_value and v_value not in ["", "FINISHED"]
                            logger.debug(f"  is_content_path: {is_content_path}, is_empty_p_with_content: {is_empty_p_with_content}")
                            logger.debug(f"  thinking_phase: {thinking_phase}, thinking_text_started: {thinking_text_started}")
            
                            # ========== 处理包含 response 对象的字典 ==========
                            if isinstance(v_value, dict) and "response" in v_value:
                                logger.debug(f"  [处理] 完整 response 对象")
                                response_data = v_value["response"]
                                # 提取 fragments 列表
                                fragments = response_data.get("fragments", [])
                                for fragment in fragments:
                                    frag_type = fragment.get("type")
                                    content = fragment.get("content", "")
                                    if frag_type == "RESPONSE":
                                        if content:
                                            logger.debug(f"  [RESPONSE片段] 输出内容: {repr(content)}")
                                            # 遇到 RESPONSE 类型，思考阶段结束
                                            if thinking_phase:
                                                thinking_phase = False
                                            final_text += content
                                            delta_obj = {}
                                            if not first_chunk_sent:
                                                delta_obj["role"] = "assistant"
                                                first_chunk_sent = True
                                            delta_obj["content"] = content
                                            out_chunk = {
                                                "id": completion_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_time,
                                                "model": model,
                                                "choices": [{"delta": delta_obj, "index": 0}],
                                            }
                                            yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                            last_send_time = current_time
                                    elif frag_type in ("THINK", "THINKING", "REASONING"):
                                        # 处理思考片段
                                        if content and thinking_phase:
                                            logger.debug(f"  [THINKING片段] 输出思考内容: {repr(content)}")
                                            final_thinking += content
                                            if internal_thinking:
                                                delta_obj = {}
                                                if not first_chunk_sent:
                                                    delta_obj["role"] = "assistant"
                                                    first_chunk_sent = True
                                                delta_obj["reasoning_content"] = content
                                                out_chunk = {
                                                    "id": completion_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": created_time,
                                                    "model": model,
                                                    "choices": [{"delta": delta_obj, "index": 0}],
                                                }
                                                yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                                last_send_time = current_time
                                            thinking_text_started = True
                                continue
            
                            # ========== 处理 response/fragments 数组（最终完整片段） ==========
                            if p_value == "response/fragments" and isinstance(v_value, list):
                                logger.debug(f"  [处理] response/fragments 数组，长度: {len(v_value)}")
                                for fragment in v_value:
                                    if isinstance(fragment, dict):
                                        frag_type = fragment.get("type")
                                        content = fragment.get("content", "")
                                        if frag_type == "RESPONSE":
                                            if content:
                                                logger.debug(f"  [完整片段] 输出内容: {repr(content)}")
                                                # 遇到 RESPONSE 类型，思考阶段结束
                                                if thinking_phase:
                                                    thinking_phase = False
                                                final_text += content
                                                delta_obj = {}
                                                if not first_chunk_sent:
                                                    delta_obj["role"] = "assistant"
                                                    first_chunk_sent = True
                                                delta_obj["content"] = content
                                                out_chunk = {
                                                    "id": completion_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": created_time,
                                                    "model": model,
                                                    "choices": [{"delta": delta_obj, "index": 0}],
                                                }
                                                yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                                last_send_time = current_time
                                        elif frag_type in ("THINK", "THINKING", "REASONING"):
                                            if content and thinking_phase:
                                                logger.debug(f"  [完整数组思考片段] 输出思考内容: {repr(content)}")
                                                final_thinking += content
                                                if internal_thinking:
                                                    delta_obj = {}
                                                    if not first_chunk_sent:
                                                        delta_obj["role"] = "assistant"
                                                        first_chunk_sent = True
                                                    delta_obj["reasoning_content"] = content
                                                    out_chunk = {
                                                        "id": completion_id,
                                                        "object": "chat.completion.chunk",
                                                        "created": created_time,
                                                        "model": model,
                                                        "choices": [{"delta": delta_obj, "index": 0}],
                                                    }
                                                    yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                                    last_send_time = current_time
                                                thinking_text_started = True
                                continue
            
                            # 處理流式内容（逐 token）
                            if isinstance(v_value, str) and v_value:
                                is_content_path = p_value and ("response/fragments/" in p_value or p_value == "response/content")
                                is_empty_p_with_content = not p_value and v_value not in ["", "FINISHED"]
                                
                                if thinking_phase:
                                    # 思考阶段的内容
                                    if is_content_path or is_empty_p_with_content:
                                        logger.debug(f"  [思考] 思考内容 token: {repr(v_value[:50])}")
                                        final_thinking += v_value
                                        if internal_thinking:
                                            delta_obj = {}
                                            if not first_chunk_sent:
                                                delta_obj["role"] = "assistant"
                                                first_chunk_sent = True
                                            delta_obj["reasoning_content"] = v_value
                                            out_chunk = {
                                                "id": completion_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_time,
                                                "model": model,
                                                "choices": [{"delta": delta_obj, "index": 0}],
                                            }
                                            yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                            last_send_time = current_time
                                        thinking_text_started = True
                                        continue
                                else:
                                    # 正常内容阶段
                                    if is_content_path or is_empty_p_with_content:
                                        logger.debug(f"  [正常内容] 输出: {repr(v_value[:50])}")
                                        final_text += v_value
                                        
                                        delta_obj = {}
                                        if not first_chunk_sent:
                                            delta_obj["role"] = "assistant"
                                            first_chunk_sent = True
                                        
                                        delta_obj["content"] = v_value
                                        out_chunk = {
                                            "id": completion_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_time,
                                            "model": model,
                                            "choices": [{"delta": delta_obj, "index": 0}],
                                        }
                                        yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                        last_send_time = current_time
                                        continue
            
                            # 处理 thinking_content（兼容旧格式）
                            if p_value == "response/thinking_content" and isinstance(v_value, str):
                                logger.debug(f"  [旧格式思考内容]: {repr(v_value[:50])}")
                                final_thinking += v_value
                                if internal_thinking and v_value:
                                    delta_obj = {}
                                    if not first_chunk_sent:
                                        delta_obj["role"] = "assistant"
                                        first_chunk_sent = True
                                    
                                    delta_obj["reasoning_content"] = v_value
                                    out_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [{"delta": delta_obj, "index": 0}],
                                    }
                                    yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                    last_send_time = current_time
                                continue
            
                        except queue.Empty:
                            continue
            
                except Exception as e:
                    logger.error(f"SSE 流異常: {e}")
                finally:
                    if request.state.use_config_token and hasattr(request.state, "account") and not account_released:
                        release_account(request.state.account)
                        account_released = True
                    logger.debug(f"[DEBUG] 流结束 - 思考内容长度: {len(final_thinking)}, 正常内容长度: {len(final_text)}")
                        
            return StreamingResponse(sse_stream(), media_type="text/event-stream", headers={"Content-Type": "text/event-stream"})

        # 非流式響應
        else:
            result_queue = queue.Queue()
            parse_thread = threading.Thread(target=parse_sse_stream, args=(deepseek_resp, result_queue))
            parse_thread.start()

            thinking_phase = internal_thinking
            thinking_text_started = False
            final_content = ""
            final_reasoning = ""

            try:
                while True:
                    try:
                        chunk = result_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if chunk is None:
                        break

                    v_value = chunk.get("v", "")
                    p_value = chunk.get("p", "")

                    # 处理包含 response 对象的字典（非流式）
                    if isinstance(v_value, dict) and "response" in v_value:
                        response_data = v_value["response"]
                        fragments = response_data.get("fragments", [])
                        for fragment in fragments:
                            frag_type = fragment.get("type")
                            content = fragment.get("content", "")
                            if frag_type == "RESPONSE":
                                if thinking_phase:
                                    thinking_phase = False
                                final_content = content
                            elif frag_type in ("THINK", "THINKING", "REASONING"):
                                if thinking_phase:
                                    final_reasoning += content
                                    thinking_text_started = True
                        continue

                    # 处理 response/fragments 数组（非流式）
                    if p_value == "response/fragments" and isinstance(v_value, list):
                        for fragment in v_value:
                            if isinstance(fragment, dict):
                                frag_type = fragment.get("type")
                                content = fragment.get("content", "")
                                if frag_type == "RESPONSE":
                                    if thinking_phase:
                                        thinking_phase = False
                                    final_content += content
                                elif frag_type in ("THINK", "THINKING", "REASONING"):
                                    if thinking_phase:
                                        final_reasoning += content
                                        thinking_text_started = True
                        continue

                    # 处理流式内容（非流式模式下累积）
                    if isinstance(v_value, str) and v_value:
                        is_content_path = p_value and ("response/fragments/" in p_value or p_value == "response/content")
                        is_empty_p_with_content = not p_value and v_value not in ["", "FINISHED"]
                        
                        if thinking_phase:
                            if is_content_path or is_empty_p_with_content:
                                final_reasoning += v_value
                                thinking_text_started = True
                        else:
                            if is_content_path or is_empty_p_with_content:
                                final_content += v_value

            finally:
                deepseek_resp.close()

            prompt_tokens = len(final_prompt) // 4
            reasoning_tokens = len(final_reasoning) // 4
            completion_tokens = len(final_content) // 4

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

            return JSONResponse(content=result)

    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    except Exception as exc:
        logger.error(f"未知異常: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})
    finally:
        if not account_released and request.state.use_config_token and hasattr(request.state, "account"):
            release_account(request.state.account)

# -------------------------- 其他路由 --------------------------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("welcome.html", {"request": request})

@app.get("/admin/account_stats")
def account_stats():
    return JSONResponse(content=account_manager.get_stats())

@app.get("/v1/models")
def list_models():
    models = [
        {"id": "deepseek-chat", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-reasoner", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-vision", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash-searching", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro-searching", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
        {"id": "deepseek-vision-searching", "object": "model", "created": 1700000000, "owned_by": "deepseek"},
    ]
    return JSONResponse(content={"object": "list", "data": models})

# -------------------------- 啟動 --------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)