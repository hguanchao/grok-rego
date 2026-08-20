"""临时邮箱：Cloudflare Worker / YYDS。"""

from __future__ import annotations

import random
import string
import threading
import time
from typing import Any

from curl_cffi import requests

from core import config
from core.logger import logger
from core.util import elapsed_label, extract_verification_code

_DOMAIN_LOCK = threading.Lock()
_domain_index = 0


def _proxies() -> dict[str, str] | None:
    """按当前配置返回 curl_cffi proxies；避免 from-import 快照过期。"""
    return {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None


def _pick_domain() -> str:
    """按 cf_domain_mode 选择根域名: poll 轮询 / random 随机。

    必须通过 config 模块属性读取，禁止 from-import 常量快照，
    否则 load_config 重绑 CF_DOMAIN_MODE / CF_DOMAINS 后本模块仍用旧值。
    """
    global _domain_index
    domains = list(config.CF_DOMAINS)
    if not domains:
        raise RuntimeError(
            "cf_domains 为空，无法创建临时邮箱；请在 config.json 配置逗号分隔根域名"
        )
    mode = (config.CF_DOMAIN_MODE or "random").strip().lower()
    if mode == "poll":
        with _DOMAIN_LOCK:
            domain = domains[_domain_index % len(domains)]
            _domain_index += 1
    else:
        if mode != "random":
            logger.warning(f"[临时邮箱] 未知 cf_domain_mode={mode!r}，回退 random")
        domain = random.choice(domains)
    logger.info(
        f"[临时邮箱] 选用根域名: {domain} (mode={mode}, pool={len(domains)})"
    )
    return domain


def _random_subdomain() -> str:
    """2–4 位小写字母子域。"""
    return "".join(random.choices(string.ascii_lowercase, k=random.randint(2, 4)))


def _cf_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（子域 2–4 位），返回 (address, jwt)。"""
    if not config.CF_API_BASE:
        raise RuntimeError("cf_api_base 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(6, 10))
        )
    root_domain = _pick_domain()
    sub = _random_subdomain()
    payload: dict[str, Any] = {
        "name": local,
        "domain": f"{sub}.{root_domain}",
        "enableRandomSubdomain": False,
    }
    if config.CF_API_KEY:
        payload["api_key"] = config.CF_API_KEY
        logger.info("[临时邮箱] 管理员 API 模式创建邮箱")
    resp = requests.post(
        f"{config.CF_API_BASE}/api/new_address",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
        proxies=_proxies(),
        impersonate="chrome",
    )
    resp.raise_for_status()
    data = resp.json()
    address = data.get("address") or ""
    jwt = data.get("jwt") or ""
    if not address or not jwt:
        raise RuntimeError(f"cf 创建邮箱响应缺少 address/jwt: {data}")
    # 最终地址带随机子域，日志同时打出请求的根域名便于核对 poll/random
    logger.debug(f"[临时邮箱] 已创建: {address} (root={root_domain}, sub={sub})")
    return address, jwt


def _cf_poll_for_code(jwt: str, timeout: int = 120, interval: int = 3) -> str | None:
    """轮询邮箱，提取验证码（格式如 ABC-XYZ 或纯数字）。"""
    headers = {"Authorization": f"Bearer {jwt}"}
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")

    seen_ids: set[Any] = set()
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = requests.get(
                f"{config.CF_API_BASE}/api/parsed_mails?limit=20&offset=0",
                headers=headers,
                timeout=15,
                proxies=_proxies(),
                impersonate="chrome",
            )
            if resp.status_code == 200:
                for mail in resp.json().get("results", []):
                    if mail["id"] in seen_ids:
                        continue
                    seen_ids.add(mail["id"])
                    subject = mail.get("subject", "")
                    text = mail.get("text", "")
                    sender = mail.get("source", "") or ""
                    logger.debug(f"[邮件] 收到邮件: from={sender}, subject={subject}")

                    code = extract_verification_code(subject, text)
                    if code:
                        from_part = f"（来自 {sender}）" if sender else ""
                        logger.debug(
                            f"[邮件] 已提取验证码 {code}{from_part}  · {elapsed_label(t0)}"
                        )
                        return code

                    logger.debug(
                        f"[邮件] 邮件中未找到验证码格式, "
                        f"subject={subject}, text={text[:200]}"
                    )
        except Exception as e:
            logger.debug(f"[邮件] 轮询出错: {e}")

        time.sleep(interval)
        elapsed += interval

    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


"""
yyds 邮箱服务商实现（YYDS Mail API）。

Base URL: https://maliapi.215.im/v1，通过 X-API-Key 认证（AC- 前缀）。
创建临时邮箱后返回 temp token，轮询 /v1/messages/next 长轮询取验证码。
"""





def _proxies() -> dict[str, str] | None:
    """按当前配置返回代理；避免 from-import 快照过期。"""
    proxy = config.PROXY
    return {"http": proxy, "https": proxy} if proxy else None


def _yyds_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（API key 默认域），返回 (address, temp_token)。"""
    if not (config.YYDS_API_KEY or "").strip():
        raise RuntimeError("yyds_api_key 未配置")
    base = (config.YYDS_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("yyds_api_base 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    resp = requests.post(
        f"{base}/accounts",
        json={"localPart": local},
        headers={
            "X-API-Key": config.YYDS_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=30,
        proxies=_proxies(),
        impersonate="chrome",
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json().get("data", {})
    address = data.get("address")
    if not address:
        raise RuntimeError(f"yyds 创建邮箱失败，响应缺少 address: {data}")
    logger.debug(f"[临时邮箱] 已创建 (yyds): {address}")
    return address, data["token"]


def _yyds_poll_for_code(temp_token: str, timeout: int = 120, interval: int = 3) -> str | None:
    """长轮询 /v1/messages/next 取未读邮件，提取验证码（含服务端 verificationCode）。"""
    headers = {"Authorization": f"Bearer {temp_token}"}
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")

    elapsed = 0
    while elapsed < timeout:
        round_start = time.time()
        try:
            base = (config.YYDS_API_BASE or "").strip().rstrip("/")
            resp = requests.get(
                f"{base}/messages/next?wait=5",
                headers=headers,
                timeout=10,
                proxies=_proxies(),
                impersonate="chrome",
            )
            if resp.status_code == 204:
                logger.debug("[邮件] 暂无新邮件，继续等待")
            elif resp.status_code == 200:
                message: dict[str, Any] = resp.json().get("data", {}).get("message", {})
                subject = message.get("subject", "")
                text = message.get("text", "")
                server_code = message.get("verificationCode") or ""
                sender = (message.get("from") or {}).get("address", "") or ""
                logger.debug(f"[邮件] 收到邮件: from={sender or '?'}, subject={subject}")

                code = server_code or extract_verification_code(subject, text)
                if code:
                    from_part = f"（来自 {sender}）" if sender else ""
                    logger.debug(
                        f"[邮件] 已提取验证码 {code}{from_part}  · {elapsed_label(t0)}"
                    )
                    return code
                logger.debug(
                    f"[邮件] 邮件中未找到验证码格式, subject={subject}, text={text[:200]}"
                )
            else:
                logger.debug(
                    f"[邮件] 轮询响应异常: HTTP {resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            logger.debug(f"[邮件] 轮询出错: {e}")

        time.sleep(interval)
        elapsed += interval + (time.time() - round_start)

    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


def create_temp_email(local_part: str = "") -> tuple[str, str]:
    """按 MAIL_PROVIDER 创建临时邮箱，返回 (address, token_or_jwt)。

    local_part：可选本地部分（cf 支持指定；yyds 忽略，由服务端生成）。
    """
    provider_name = (config.MAIL_PROVIDER or "cf").strip().lower()
    if provider_name == "yyds":
        return _yyds_create_temp_email()
    return _cf_create_temp_email(local_part)


def poll_for_code(
    email_or_jwt: str,
    token: str | None = None,
    timeout: int = 120,
    interval: int = 3,
) -> str | None:
    """按 MAIL_PROVIDER 轮询验证码。

    兼容两种调用：
    - cf：poll_for_code(jwt) 或 poll_for_code(email, jwt)（第二参为 jwt）
    - yyds：poll_for_code(email, token)
    """
    provider_name = (config.MAIL_PROVIDER or "cf").strip().lower()
    if provider_name == "yyds":
        return _yyds_poll_for_code(
            email_or_jwt, token or "", timeout=timeout, interval=interval
        )
    # cf：优先用 token(jwt)，否则 email_or_jwt 本身就是 jwt
    jwt = (token or email_or_jwt or "").strip()
    return _cf_poll_for_code(jwt, timeout=timeout, interval=interval)
