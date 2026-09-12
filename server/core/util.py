"""时间与通用工具。"""

"""
北京时间（UTC+8）时间戳工具。

全项目统一使用北京时间：数据库、任务日志、状态时间戳均由此模块生成，
避免因服务器系统时区导致时间显示错误。
"""

from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))
_STR_FORMAT = "%Y-%m-%d %H:%M:%S"
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


def now_dt() -> datetime:
    """返回当前北京时间 aware datetime。"""
    return datetime.now(BEIJING_TZ)


def now_str() -> str:
    """返回当前北京时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return now_dt().strftime(_STR_FORMAT)


def now_iso() -> str:
    """返回当前北京时间 ISO 字符串（YYYY-MM-DDTHH:MM:SS，无时区后缀）。"""
    return now_dt().strftime(_ISO_FORMAT)


def now_iso_tz() -> str:
    """返回当前北京时间 ISO 字符串（YYYY-MM-DDTHH:MM:SS+08:00，带时区偏移）。

    落库推荐使用此格式：带时区标记可供前端/下游直接解析，避免歧义。
    """
    return now_dt().isoformat(timespec="seconds")


def iso_after_hours(hours: float) -> str:
    """返回当前北京时间 + hours 小时的 ISO 字符串（与 now_iso_tz 同格式）。

    同格式保证落库后可直接用字符串比较判断是否过期。
    """
    return (now_dt() + timedelta(hours=hours)).isoformat(timespec="seconds")


def format_exp(ts: int | None) -> str:
    """Unix 秒时间戳 → 北京时间 YYYY-MM-DD HH:MM；无效返回「未知」。"""
    if not ts:
        return "未知"
    try:
        return datetime.fromtimestamp(int(ts), tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"

"""
验证码提取工具。

从邮件主题/正文中提取注册验证码（ABC-XYZ 格式或纯数字），供各邮箱服务商复用。
"""

import base64
import json
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit

# 验证码正则：格式如 OE5-SDO
_CODE_PATTERN = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")
# 兜底：纯数字验证码
_DIGIT_PATTERN = re.compile(r"\b(\d{4,8})\b")


def extract_verification_code(subject: str, text: str) -> str | None:
    """从邮件主题和正文中提取验证码（ABC-XYZ 格式或纯数字）。"""
    for content in (subject, text):
        match = _CODE_PATTERN.search(content)
        if match:
            return match.group(1)
    digit_match = _DIGIT_PATTERN.search(text)
    if digit_match:
        return digit_match.group(1)
    return None


def curl_error_code(exc: BaseException) -> int | None:
    """提取 curl_cffi 异常中的 libcurl 错误码。"""
    value = getattr(exc, "code", None)
    try:
        if value is not None and int(value) > 0:
            return int(value)
    except (TypeError, ValueError):
        pass
    match = re.search(r"curl:\s*\((\d+)\)", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def proxy_endpoint_ready(proxy: str | None, timeout: float = 1.0) -> bool:
    """仅检查代理 TCP 端点是否可连接，不发起任何上游请求。"""
    raw = str(proxy or "").strip()
    if not raw:
        return True
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    if not parsed.hostname:
        return False
    try:
        scheme = parsed.scheme.lower()
        if scheme in {"socks4", "socks5", "socks5h"}:
            default_port = 1080
        else:
            default_port = 443 if scheme == "https" else 80
        port = parsed.port or default_port
    except ValueError:
        return False
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def elapsed_label(t0: float) -> str:
    """步骤耗时文案：1.2s（从 t0=time.monotonic() 起算）。"""
    return f"{max(0.0, time.monotonic() - t0):.1f}s"


# 号池批量任务：20 个 worker 同时跑，每个 worker 做完一个号再隔 1 秒接下一个。
ACCOUNT_WORKERS = 20
ACCOUNT_WORKER_GAP_SEC = 1.0


def run_account_workers(
    items: list[Any],
    work: Callable[[Any], Any],
    *,
    workers: int = ACCOUNT_WORKERS,
    gap_sec: float = ACCOUNT_WORKER_GAP_SEC,
    thread_name_prefix: str = "号池",
    should_stop: Callable[[], bool] | None = None,
    on_complete: Callable[[int, Any, Any], None] | None = None,
) -> list[Any]:
    """把账号列表切成最多 `workers` 条链，每条链串行处理，号与号之间固定间隔。

    单个账号完成后立即调用 `on_complete(index, item, result)`；返回与 `items`
    等长的结果列表，某条失败写入异常对象，取消未跑的写入 None。
    """
    if not items:
        return []
    n = len(items)
    worker_n = max(1, min(int(workers), n))
    results: list[Any] = [None] * n

    def chain(start: int) -> None:
        first = True
        for index in range(start, n, worker_n):
            if should_stop is not None and should_stop():
                return
            if not first and gap_sec > 0:
                time.sleep(gap_sec)
            first = False
            try:
                results[index] = work(items[index])
            except Exception as exc:
                results[index] = exc
            if on_complete is not None:
                try:
                    on_complete(index, items[index], results[index])
                except Exception:
                    # 完成回调属于结果消费阶段；异常不能覆盖真实业务结果或中断后续账号。
                    pass

    with ThreadPoolExecutor(max_workers=worker_n, thread_name_prefix=thread_name_prefix) as pool:
        futs = [pool.submit(chain, start) for start in range(worker_n)]
        for fut in as_completed(futs):
            fut.result()
    return results


def decode_jwt_exp(token: str | None) -> int | None:
    """解码 JWT payload 提取 exp 时间戳；解析失败返回 None。"""
    if not token:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return payload.get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None