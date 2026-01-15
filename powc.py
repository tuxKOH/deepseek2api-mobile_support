import math
import struct
from pathlib import Path

from wasm3 import Environment

try:
    from wasm3 import Wasm3Error
except ImportError:
    try:
        from wasm3 import ResultError as Wasm3Error
    except ImportError:
        class Wasm3Error(Exception):
            pass


def compute_pow_answer(
    algorithm: str,
    challenge_str: str,
    salt: str,
    difficulty: int,
    expire_at: int,
    signature: str,
    target_path: str,
    wasm_path: str,
    max_time: float | None = None,
) -> int | None:
    """
    使用 wasm3 執行 DeepSeekHash PoW，若找到答案則回傳整數，否則回傳 None。
    """
    if algorithm != "DeepSeekHashV1":
        raise ValueError(f"不支援的算法：{algorithm}")

    wasm_file = Path(wasm_path)
    if not wasm_file.is_file():
        raise RuntimeError(f"載入 wasm 檔失敗：{wasm_path}，錯誤：檔案不存在")

    try:
        wasm_bytes = wasm_file.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"載入 wasm 檔失敗：{wasm_path}，錯誤：{exc}") from exc

    try:
        env = Environment()
        runtime = env.new_runtime(64 * 1024)
        module = env.parse_module(wasm_bytes)
        runtime.load(module)
        memory = runtime.get_memory(0)
    except Wasm3Error as exc:
        raise RuntimeError(f"初始化 wasm3 失敗：{exc}") from exc

    def refresh_memory():
        nonlocal memory
        memory = runtime.get_memory(0)

    def ensure_memory_capacity(end: int):
        nonlocal memory
        if end <= len(memory):
            return
        refresh_memory()
        if end <= len(memory):
            return
        raise RuntimeError(
            f"wasm 記憶體不足：offset={end}, mem_size={len(memory)}"
        )

    def get_export(name: str):
        try:
            return runtime.find_function(name)
        except Wasm3Error as exc:
            raise RuntimeError(f"缺少 wasm 導出函式：{name}") from exc

    add_to_stack = get_export("__wbindgen_add_to_stack_pointer")
    alloc = get_export("__wbindgen_export_0")
    wasm_solve = get_export("wasm_solve")

    def write_memory(offset: int, data: bytes):
        end = offset + len(data)
        ensure_memory_capacity(end)
        memory[offset:end] = data

    def read_memory(offset: int, size: int) -> bytes:
        end = offset + size
        ensure_memory_capacity(end)
        return bytes(memory[offset:end])

    def encode_string(text: str) -> tuple[int, int]:
        data = text.encode("utf-8")
        length = len(data)
        try:
            ptr = alloc(length, 1)
        except Wasm3Error as exc:
            raise RuntimeError(f"申請 wasm 記憶體失敗：{exc}") from exc
        refresh_memory()
        write_memory(ptr, data)
        return ptr, length

    prefix = f"{salt}_{expire_at}_"
    stack_frame = 16

    try:
        retptr = add_to_stack(-stack_frame)
    except Wasm3Error as exc:
        raise RuntimeError(f"調整 wasm 堆疊指標失敗：{exc}") from exc

    try:
        ptr_challenge, len_challenge = encode_string(challenge_str)
        ptr_prefix, len_prefix = encode_string(prefix)

        try:
            wasm_solve(
                retptr,
                ptr_challenge,
                len_challenge,
                ptr_prefix,
                len_prefix,
                float(difficulty),
            )
        except Wasm3Error as exc:
            raise RuntimeError(f"呼叫 wasm_solve 失敗：{exc}") from exc

        refresh_memory()

        status_bytes = read_memory(retptr, 4)
        if len(status_bytes) != 4:
            raise RuntimeError("讀取狀態位元組失敗")
        status = struct.unpack("<i", status_bytes)[0]

        value_bytes = read_memory(retptr + 8, 8)
        if len(value_bytes) != 8:
            raise RuntimeError("讀取結果位元組失敗")
        value = struct.unpack("<d", value_bytes)[0]
    finally:
        try:
            add_to_stack(stack_frame)
        except Wasm3Error as exc:
            raise RuntimeError(f"恢復 wasm 堆疊指標失敗：{exc}") from exc

    if status == 0:
        return None

    if not math.isfinite(value) or not value.is_integer():
        raise RuntimeError(f"wasm 回傳非常規整數：{value}")

    return int(value)