"""临时邮箱：Cloudflare Worker / YYDS / TempMail.lol。"""

from __future__ import annotations

import random
import string
import threading
import time
from collections.abc import Callable
from typing import Any

from curl_cffi import requests

from core import config
from core.logger import logger
from core.util import elapsed_label, extract_verification_code, wait_or_cancel

_DOMAIN_LOCK = threading.Lock()
_domain_index = 0


def _proxies() -> dict[str, str] | None:
    """按当前配置返回 curl_cffi proxies；避免 from-import 快照过期。"""
    from core import proxypool

    proxy = proxypool.current()
    return {"http": proxy, "https": proxy} if proxy else None


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
    logger.debug(
        f"[临时邮箱] 选用根域名: {domain} (mode={mode}, pool={len(domains)})"
    )
    return domain


def _random_subdomain() -> str:
    """2–4 位小写字母子域。"""
    return "".join(random.choices(string.ascii_lowercase, k=random.randint(2, 4)))


def _cf_create_once(local: str) -> tuple[str, str]:
    """向 CF 邮箱服务申请一次地址，返回 (address, jwt)。"""
    root_domain = _pick_domain()
    sub = _random_subdomain()
    payload: dict[str, Any] = {
        "name": local,
        "domain": f"{sub}.{root_domain}",
        "enableRandomSubdomain": False,
    }
    if config.CF_API_KEY:
        payload["api_key"] = config.CF_API_KEY
        logger.debug("[临时邮箱] 管理员 API 模式创建邮箱")
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
    return address, jwt


def _cf_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（子域 2–4 位）。"""
    if not config.CF_API_BASE:
        raise RuntimeError("cf_api_base 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(6, 10))
        )
    address, jwt = _cf_create_once(local)
    logger.debug(f"[临时邮箱] 已创建: {address}")
    return address, jwt


def _cf_poll_for_code(
    jwt: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """轮询邮箱，提取验证码（格式如 ABC-XYZ 或纯数字）。

    stop_when：页面已进入下一步时返回 True，立即结束轮询避免空等。
    """
    headers = {"Authorization": f"Bearer {jwt}"}
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")

    seen_ids: set[Any] = set()
    elapsed = 0
    while elapsed < timeout:
        if stop_when is not None and stop_when():
            logger.debug(f"[邮件] 停止等验证码  · {elapsed_label(t0)}")
            return None
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

        if wait_or_cancel(interval):
            logger.debug(f"[邮件] 已取消，停止等验证码  · {elapsed_label(t0)}")
            return None
        elapsed += interval

    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


"""
yyds 邮箱服务商实现（YYDS Mail API）。

Base URL: https://maliapi.215.im/v1，通过 X-API-Key 认证（AC- 前缀）。
创建临时邮箱后返回 temp token，轮询 /v1/messages/next 长轮询取验证码。
"""


def _yyds_create_once(local: str) -> tuple[str, str]:
    """向 YYDS 申请一次邮箱，返回 (address, temp_token)。"""
    base = (config.YYDS_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("yyds_api_base 未配置")
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
    return address, data["token"]


def _yyds_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（API key 默认域）。"""
    if not (config.YYDS_API_KEY or "").strip():
        raise RuntimeError("yyds_api_key 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    address, token = _yyds_create_once(local)
    logger.debug(f"[临时邮箱] 已创建 (yyds): {address}")
    return address, token


def _yyds_poll_for_code(
    temp_token: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """长轮询 /v1/messages/next 取未读邮件，提取验证码（含服务端 verificationCode）。

    stop_when：页面已进入下一步时返回 True，立即结束轮询避免空等。
    """
    headers = {"Authorization": f"Bearer {temp_token}"}
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")

    elapsed = 0
    while elapsed < timeout:
        if stop_when is not None and stop_when():
            logger.debug(f"[邮件] 停止等验证码  · {elapsed_label(t0)}")
            return None
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

        if wait_or_cancel(interval):
            logger.debug(f"[邮件] 已取消，停止等验证码  · {elapsed_label(t0)}")
            return None
        elapsed += interval + (time.time() - round_start)

    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


"""
TempMail.lol 邮箱服务商实现（https://tempmail.lol/zh/api）。

Base URL 默认 https://api.tempmail.lol/v2。免费档无需 API Key；
Plus/Ultra 密钥走 Authorization: Bearer。创建收件箱返回 token，
轮询 GET /inbox?token= 取邮件。
"""


def _tempmail_headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    api_key = (config.TEMPMAIL_API_KEY or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _tempmail_base() -> str:
    base = (config.TEMPMAIL_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("tempmail_api_base 未配置")
    return base


def _tempmail_create_once(local_part: str = "") -> tuple[str, str]:
    """向 TempMail 申请一次收件箱，返回 (address, token)。"""
    payload: dict[str, Any] = {}
    local = (local_part or "").strip().lower()
    if local:
        payload["prefix"] = local
    domain = (config.TEMPMAIL_DOMAIN or "").strip()
    if domain:
        payload["domain"] = domain
    else:
        # 自定义域未配时走社区域，公共免费域更容易被 xAI 判 invalid
        payload["community"] = True
    resp = requests.post(
        f"{_tempmail_base()}/inbox/create",
        json=payload,
        headers=_tempmail_headers(json_body=True),
        timeout=30,
        proxies=_proxies(),
        impersonate="chrome",
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    address = data.get("address") or ""
    token = data.get("token") or ""
    if not address or not token:
        raise RuntimeError(f"tempmail 创建邮箱响应缺少 address/token: {data}")
    return address, token


def _tempmail_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱。"""
    address, token = _tempmail_create_once(local_part)
    logger.debug(f"[临时邮箱] 已创建 (tempmail): {address}")
    return address, token


def _tempmail_poll_for_code(
    inbox_token: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """轮询 GET /inbox?token= 取邮件，提取验证码。

    stop_when：页面已进入下一步时返回 True，立即结束轮询避免空等。
    """
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")

    seen_keys: set[tuple[Any, ...]] = set()
    elapsed = 0
    while elapsed < timeout:
        if stop_when is not None and stop_when():
            logger.debug(f"[邮件] 停止等验证码  · {elapsed_label(t0)}")
            return None
        try:
            resp = requests.get(
                f"{_tempmail_base()}/inbox",
                params={"token": inbox_token},
                headers=_tempmail_headers(),
                timeout=15,
                proxies=_proxies(),
                impersonate="chrome",
            )
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                if data.get("expired") is True:
                    logger.error(
                        f"[邮件] TempMail 收件箱已过期  · {elapsed_label(t0)}"
                    )
                    return None
                for mail in data.get("emails") or []:
                    key = (
                        mail.get("date"),
                        mail.get("from") or mail.get("sender") or "",
                        mail.get("subject") or "",
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    subject = mail.get("subject") or ""
                    text = mail.get("body") or mail.get("text") or ""
                    html = mail.get("html") or ""
                    sender = mail.get("from") or mail.get("sender") or ""
                    logger.debug(
                        f"[邮件] 收到邮件: from={sender}, subject={subject}"
                    )
                    code = extract_verification_code(subject, f"{text}\n{html}")
                    if code:
                        from_part = f"（来自 {sender}）" if sender else ""
                        logger.debug(
                            f"[邮件] 已提取验证码 {code}{from_part}  · {elapsed_label(t0)}"
                        )
                        return code
                    logger.debug(
                        f"[邮件] 邮件中未找到验证码格式, "
                        f"subject={subject}, text={str(text)[:200]}"
                    )
            else:
                logger.debug(
                    f"[邮件] 轮询响应异常: HTTP {resp.status_code} {resp.text[:200]}"
                )
        except Exception as e:
            logger.debug(f"[邮件] 轮询出错: {e}")

        if wait_or_cancel(interval):
            logger.debug(f"[邮件] 已取消，停止等验证码  · {elapsed_label(t0)}")
            return None
        elapsed += interval

    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


def _provider_name() -> str:
    return (config.MAIL_PROVIDER or "cf").strip().lower()


def create_temp_email(local_part: str = "") -> tuple[str, str]:
    """按 MAIL_PROVIDER 创建临时邮箱，返回 (address, token_or_jwt)。

    local_part：可选本地部分（cf / tempmail 支持指定；yyds 忽略，由服务端生成）。
    """
    provider_name = _provider_name()
    if provider_name == "yyds":
        return _yyds_create_temp_email()
    if provider_name == "tempmail":
        return _tempmail_create_temp_email(local_part)
    return _cf_create_temp_email(local_part)


def poll_for_code(
    email_or_jwt: str,
    token: str | None = None,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """按 MAIL_PROVIDER 轮询验证码。

    兼容两种调用（凭据均取 token，缺省回退第一参）：
    - cf：poll_for_code(jwt) 或 poll_for_code(email, jwt)（第二参为 jwt）
    - yyds：poll_for_code(temp_token) 或 poll_for_code(email, temp_token)
    - tempmail：poll_for_code(inbox_token) 或 poll_for_code(email, inbox_token)
    stop_when：页面已进入下一步时提前结束，避免空等上一步。
    """
    provider_name = _provider_name()
    # 优先用 token（jwt / temp_token / inbox_token），否则 email_or_jwt 本身就是凭据
    credential = (token or email_or_jwt or "").strip()
    if provider_name == "yyds":
        return _yyds_poll_for_code(
            credential, timeout=timeout, interval=interval, stop_when=stop_when
        )
    if provider_name == "tempmail":
        return _tempmail_poll_for_code(
            credential, timeout=timeout, interval=interval, stop_when=stop_when
        )
    return _cf_poll_for_code(
        credential, timeout=timeout, interval=interval, stop_when=stop_when
    )
