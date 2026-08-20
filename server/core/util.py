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
import time

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


def elapsed_label(t0: float) -> str:
    """步骤耗时文案：1.2s（从 t0=time.monotonic() 起算）。"""
    return f"{max(0.0, time.monotonic() - t0):.1f}s"


def mask_email(value: str | None) -> str:
    """脱敏邮箱账号，供日志输出使用。"""
    raw = str(value or "").strip()
    local, separator, domain = raw.partition("@")
    if not separator:
        return "***"
    local_masked = f"{local[:2]}***" if local else "***"
    domain_name, dot, suffix = domain.partition(".")
    domain_masked = f"{domain_name[:1]}***" if domain_name else "***"
    return f"{local_masked}@{domain_masked}{dot}{suffix}"


def decode_jwt_exp(token: str | None) -> int | None:
    """解码 JWT payload 提取 exp 时间戳；解析失败返回 None。"""
    if not token:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return payload.get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
