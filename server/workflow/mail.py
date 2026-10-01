"""临时邮箱：Cloudflare Worker / YYDS / TempMail.lol / Tempyard。

创建与收信都按 config.MAIL_PROVIDER 分发。Cloudflare 与 Tempyard 共用
cloudflare_temp_email 的 new_address / parsed_mails；差别是 Cloudflare
在根域名上再套随机子域，Tempyard 的域名本身就是完整收信域。
"""

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
_TEMPYARD_DOMAIN_LOCK = threading.Lock()
_tempyard_domain_index = 0
_TEMPYARD_SETTINGS_LOCK = threading.Lock()
_tempyard_settings: dict[str, Any] | None = None
_tempyard_settings_at = 0.0
_TEMPYARD_SETTINGS_TTL = 300.0


def _proxies() -> dict[str, str] | None:
    """按当前配置返回 curl_cffi proxies；避免 from-import 快照过期。"""
    from core import proxypool

    proxy = proxypool.current()
    return {"http": proxy, "https": proxy} if proxy else None


def _provider_name() -> str:
    return (config.MAIL_PROVIDER or "cf").strip().lower()


def _random_local(length: tuple[int, int] = (6, 10)) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=random.randint(*length)))


def _local_or_random(local_part: str) -> str:
    local = (local_part or "").strip().lower()
    return local or _random_local()


def _pick_from(
    domains: list[str],
    mode: str,
    index_name: str,
    lock: threading.Lock,
    label: str,
) -> str:
    """按 poll / random 从域名列表取一个，poll 的游标存在模块全局。"""
    chosen = (mode or "random").strip().lower()
    if chosen == "poll":
        with lock:
            index = globals()[index_name]
            domain = domains[index % len(domains)]
            globals()[index_name] = index + 1
        return domain
    if chosen != "random":
        logger.warning(f"[临时邮箱] 未知 {label}={chosen!r}，回退 random")
    return random.choice(domains)


def _request_headers(
    extra: dict[str, str] | None,
    *,
    json_body: bool,
) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    return headers


def _json_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    resp = requests.post(
        url,
        json=payload,
        headers=_request_headers(headers, json_body=True),
        timeout=timeout,
        proxies=_proxies(),
        impersonate="chrome",
    )
    resp.raise_for_status()
    return resp.json()


def _json_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    resp = requests.get(
        url,
        headers=_request_headers(headers, json_body=False),
        timeout=timeout,
        proxies=_proxies(),
        impersonate="chrome",
    )
    resp.raise_for_status()
    return resp.json()


def _address_jwt(data: Any, label: str) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} 创建邮箱响应异常: {data}")
    address = str(data.get("address") or "")
    token = str(data.get("jwt") or data.get("token") or "")
    if not address or not token:
        raise RuntimeError(f"{label} 创建邮箱响应缺少 address/token: {data}")
    return address, token


def _note_code(code: str, sender: str, t0: float) -> str:
    from_part = f"（来自 {sender}）" if sender else ""
    logger.debug(f"[邮件] 已提取验证码 {code}{from_part}  · {elapsed_label(t0)}")
    return code


def _poll_loop(
    timeout: int,
    interval: int,
    stop_when: Callable[[], bool] | None,
    fetch: Callable[[], str | None],
    *,
    charge_round: bool = False,
) -> str | None:
    """通用轮询。fetch 返回验证码、None（继续）、或抛异常（记日志后继续）。

    charge_round：把本轮耗时计入超时（YYDS 长轮询本身会挂起数秒）。
    """
    t0 = time.monotonic()
    logger.debug(f"[邮件] 开始轮询，等待验证码邮件（最多 {timeout} 秒）...")
    elapsed = 0.0
    while elapsed < timeout:
        if stop_when is not None and stop_when():
            logger.debug(f"[邮件] 停止等验证码  · {elapsed_label(t0)}")
            return None
        round_start = time.time()
        try:
            code = fetch()
        except Exception as exc:
            logger.debug(f"[邮件] 轮询出错: {exc}")
            code = None
        else:
            # 空串表示收件箱已失效，立刻结束；None 表示这轮没有验证码。
            if code is not None:
                return code or None
        if wait_or_cancel(interval):
            logger.debug(f"[邮件] 已取消，停止等验证码  · {elapsed_label(t0)}")
            return None
        elapsed += interval + (time.time() - round_start if charge_round else 0)
    logger.error(f"[邮件] 等待验证码超时（已等 {timeout}s）  · {elapsed_label(t0)}")
    return None


def _pick_domain() -> str:
    """按 cf_domain_mode 选择根域名: poll 轮询 / random 随机。"""
    domains = list(config.CF_DOMAINS)
    if not domains:
        raise RuntimeError(
            "cf_domains 为空，无法创建临时邮箱；请在 config.json 配置逗号分隔根域名"
        )
    domain = _pick_from(
        domains, config.CF_DOMAIN_MODE, "_domain_index", _DOMAIN_LOCK, "cf_domain_mode"
    )
    logger.debug(
        f"[临时邮箱] 选用根域名: {domain} "
        f"(mode={config.CF_DOMAIN_MODE}, pool={len(domains)})"
    )
    return domain


def _random_subdomain() -> str:
    """2–4 位小写字母子域。"""
    return _random_local((2, 4))


def _cf_api_base() -> str:
    base = (config.CF_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("cf_api_base 未配置")
    return base


def _worker_create(base: str, payload: dict[str, Any], label: str) -> tuple[str, str]:
    """cloudflare_temp_email：POST /api/new_address → (address, jwt)。"""
    data = _json_post(f"{base}/api/new_address", payload)
    return _address_jwt(data, label)


def _cf_create_once(local: str) -> tuple[str, str]:
    """向 CF 邮箱服务申请一次地址，返回 (address, jwt)。"""
    root_domain = _pick_domain()
    payload: dict[str, Any] = {
        "name": local,
        "domain": f"{_random_subdomain()}.{root_domain}",
        "enableRandomSubdomain": False,
    }
    if config.CF_API_KEY:
        payload["api_key"] = config.CF_API_KEY
        logger.debug("[临时邮箱] 管理员 API 模式创建邮箱")
    return _worker_create(_cf_api_base(), payload, "cf")


def _cf_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（子域 2–4 位）。"""
    address, jwt = _cf_create_once(_local_or_random(local_part))
    logger.debug(f"[临时邮箱] 已创建: {address}")
    return address, jwt


def _mails_from_worker(base: str, jwt: str, t0: float, seen_ids: set[Any]) -> str | None:
    """读 parsed_mails。无码返回 None；命中返回验证码。"""
    resp = requests.get(
        f"{base.rstrip('/')}/api/parsed_mails?limit=20&offset=0",
        headers={"Authorization": f"Bearer {jwt}", "Accept": "application/json"},
        timeout=15,
        proxies=_proxies(),
        impersonate="chrome",
    )
    if resp.status_code != 200:
        logger.debug(f"[邮件] 轮询响应异常: HTTP {resp.status_code} {resp.text[:200]}")
        return None
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
            return _note_code(code, sender, t0)
        logger.debug(
            f"[邮件] 邮件中未找到验证码格式, subject={subject}, text={text[:200]}"
        )
    return None


def _cf_poll_for_code(
    jwt: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
    base_url: str | None = None,
) -> str | None:
    """轮询 Cloudflare / Tempyard 收件箱，提取验证码。"""
    base = (base_url if base_url is not None else config.CF_API_BASE).rstrip("/")
    t0 = time.monotonic()
    seen_ids: set[Any] = set()

    def fetch() -> str | None:
        return _mails_from_worker(base, jwt, t0, seen_ids)

    return _poll_loop(timeout, interval, stop_when, fetch)


def _yyds_base() -> str:
    base = (config.YYDS_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("yyds_api_base 未配置")
    return base


def _yyds_create_once(local: str) -> tuple[str, str]:
    """向 YYDS 申请一次邮箱，返回 (address, temp_token)。"""
    data = _json_post(
        f"{_yyds_base()}/accounts",
        {"localPart": local},
        headers={"X-API-Key": config.YYDS_API_KEY},
    )
    body = data.get("data", {}) if isinstance(data, dict) else {}
    address = body.get("address")
    if not address:
        raise RuntimeError(f"yyds 创建邮箱失败，响应缺少 address: {body}")
    return address, body["token"]


def _yyds_create_temp_email(_local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（API key 默认域）。本地部分由服务端生成，入参忽略。"""
    if not (config.YYDS_API_KEY or "").strip():
        raise RuntimeError("yyds_api_key 未配置")
    address, token = _yyds_create_once(_random_local())
    logger.debug(f"[临时邮箱] 已创建 (yyds): {address}")
    return address, token


def _yyds_poll_for_code(
    temp_token: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """长轮询 /messages/next 取未读邮件，提取验证码（含服务端 verificationCode）。"""
    t0 = time.monotonic()

    def fetch() -> str | None:
        resp = requests.get(
            f"{_yyds_base()}/messages/next?wait=5",
            headers={"Authorization": f"Bearer {temp_token}", "Accept": "application/json"},
            timeout=10,
            proxies=_proxies(),
            impersonate="chrome",
        )
        if resp.status_code == 204:
            logger.debug("[邮件] 暂无新邮件，继续等待")
            return None
        if resp.status_code != 200:
            logger.debug(
                f"[邮件] 轮询响应异常: HTTP {resp.status_code} {resp.text[:200]}"
            )
            return None
        message: dict[str, Any] = resp.json().get("data", {}).get("message", {})
        subject = message.get("subject", "")
        text = message.get("text", "")
        sender = (message.get("from") or {}).get("address", "") or ""
        logger.debug(f"[邮件] 收到邮件: from={sender or '?'}, subject={subject}")
        code = message.get("verificationCode") or extract_verification_code(subject, text)
        if code:
            return _note_code(code, sender, t0)
        logger.debug(
            f"[邮件] 邮件中未找到验证码格式, subject={subject}, text={text[:200]}"
        )
        return None

    return _poll_loop(timeout, interval, stop_when, fetch, charge_round=True)


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
    """向 TempMail.lol 申请一次收件箱，返回 (address, token)。

    免费档无需 API Key。未配自定义域时走社区域。
    """
    payload: dict[str, Any] = {}
    local = (local_part or "").strip().lower()
    if local:
        payload["prefix"] = local
    domain = (config.TEMPMAIL_DOMAIN or "").strip()
    if domain:
        payload["domain"] = domain
    else:
        payload["community"] = True
    data = _json_post(
        f"{_tempmail_base()}/inbox/create",
        payload,
        headers=_tempmail_headers(),
    )
    return _address_jwt(data, "tempmail")


def _tempmail_create_temp_email(local_part: str = "") -> tuple[str, str]:
    address, token = _tempmail_create_once(local_part)
    logger.debug(f"[临时邮箱] 已创建 (tempmail): {address}")
    return address, token


def _tempmail_poll_for_code(
    inbox_token: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """轮询 GET /inbox?token= 取邮件，提取验证码。"""
    t0 = time.monotonic()
    seen_keys: set[tuple[Any, ...]] = set()

    def fetch() -> str | None:
        resp = requests.get(
            f"{_tempmail_base()}/inbox",
            params={"token": inbox_token},
            headers=_tempmail_headers(),
            timeout=15,
            proxies=_proxies(),
            impersonate="chrome",
        )
        if resp.status_code != 200:
            logger.debug(
                f"[邮件] 轮询响应异常: HTTP {resp.status_code} {resp.text[:200]}"
            )
            return None
        data: dict[str, Any] = resp.json()
        if data.get("expired") is True:
            logger.error(f"[邮件] TempMail 收件箱已过期  · {elapsed_label(t0)}")
            return ""
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
            logger.debug(f"[邮件] 收到邮件: from={sender}, subject={subject}")
            code = extract_verification_code(subject, f"{text}\n{html}")
            if code:
                return _note_code(code, sender, t0)
            logger.debug(
                f"[邮件] 邮件中未找到验证码格式, "
                f"subject={subject}, text={str(text)[:200]}"
            )
        return None

    return _poll_loop(timeout, interval, stop_when, fetch)


def _tempyard_base() -> str:
    base = (config.TEMPYARD_API_BASE or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("tempyard_api_base 未配置")
    return base


def _tempyard_fetch_settings() -> dict[str, Any]:
    """拉取公开域名列表，短时间缓存，避免每次创建都打 settings。"""
    global _tempyard_settings, _tempyard_settings_at
    now = time.monotonic()
    cached = _tempyard_settings
    if cached is not None and now - _tempyard_settings_at < _TEMPYARD_SETTINGS_TTL:
        return cached
    with _TEMPYARD_SETTINGS_LOCK:
        now = time.monotonic()
        cached = _tempyard_settings
        if cached is not None and now - _tempyard_settings_at < _TEMPYARD_SETTINGS_TTL:
            return cached
        data = _json_get(f"{_tempyard_base()}/open_api/settings")
        if not isinstance(data, dict):
            raise RuntimeError(f"tempyard settings 响应异常: {data}")
        _tempyard_settings = data
        _tempyard_settings_at = time.monotonic()
        return data


def _tempyard_domains() -> list[str]:
    domains = list(config.TEMPYARD_DOMAINS)
    if domains:
        return domains
    data = _tempyard_fetch_settings()
    raw = data.get("domains") or data.get("defaultDomains") or []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        domain = str(item or "").strip().lower()
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    if not domains:
        raise RuntimeError(
            "tempyard 没有可用域名；请配置 tempyard_domains，或检查邮箱 API"
        )
    return domains


def _tempyard_pick_domain() -> str:
    """选一个完整收信域。配置优先；否则用服务端 domains。"""
    domains = _tempyard_domains()
    domain = _pick_from(
        domains,
        config.TEMPYARD_DOMAIN_MODE,
        "_tempyard_domain_index",
        _TEMPYARD_DOMAIN_LOCK,
        "tempyard_domain_mode",
    )
    logger.debug(
        f"[临时邮箱] 选用 Tempyard 域名: {domain} "
        f"(mode={config.TEMPYARD_DOMAIN_MODE}, pool={len(domains)})"
    )
    return domain


def _tempyard_create_once(local: str) -> tuple[str, str]:
    """向 Tempyard 申请一次地址。域名不再套随机子域。"""
    return _worker_create(
        _tempyard_base(),
        {
            "name": local,
            "domain": _tempyard_pick_domain(),
            "enableRandomSubdomain": False,
        },
        "tempyard",
    )


def _tempyard_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建 Tempyard 邮箱。本地部分由服务端按需加前缀，这里只给随机名。"""
    address, jwt = _tempyard_create_once(_local_or_random(local_part))
    logger.debug(f"[临时邮箱] 已创建 (tempyard): {address}")
    return address, jwt


def _tempyard_poll_for_code(
    jwt: str,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """轮询 Tempyard 收件箱。响应格式与 Cloudflare 邮箱相同。"""
    return _cf_poll_for_code(
        jwt,
        timeout=timeout,
        interval=interval,
        stop_when=stop_when,
        base_url=_tempyard_base(),
    )


_CREATORS = {
    "yyds": lambda local: _yyds_create_temp_email(local),
    "tempmail": _tempmail_create_temp_email,
    "tempyard": _tempyard_create_temp_email,
    "cf": _cf_create_temp_email,
}
_POLLERS = {
    "yyds": _yyds_poll_for_code,
    "tempmail": _tempmail_poll_for_code,
    "tempyard": _tempyard_poll_for_code,
    "cf": _cf_poll_for_code,
}


def create_temp_email(local_part: str = "") -> tuple[str, str]:
    """按 MAIL_PROVIDER 创建临时邮箱，返回 (address, token_or_jwt)。"""
    create = _CREATORS.get(_provider_name(), _cf_create_temp_email)
    return create(local_part)


def poll_for_code(
    email_or_jwt: str,
    token: str | None = None,
    timeout: int = 120,
    interval: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> str | None:
    """按 MAIL_PROVIDER 轮询验证码。

    凭据优先用 token，缺省回退第一参。stop_when 为真时立即结束，避免空等上一步。
    """
    credential = (token or email_or_jwt or "").strip()
    poll = _POLLERS.get(_provider_name(), _cf_poll_for_code)
    return poll(credential, timeout=timeout, interval=interval, stop_when=stop_when)
