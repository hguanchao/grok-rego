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
# 创建邮箱时避开 xAI 已判 invalid 的后缀；进程内缓存，ban 后立刻失效。
_banned_cache: set[str] | None = None
_banned_lock = threading.Lock()
_CREATE_TRIES = 16
# 域名不合格后隔 2 秒再请求，避免连打创建接口触发上游 HTTP 429。
_CREATE_RETRY_INTERVAL = 2.0


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
    available = [
        d for d in domains if not _host_banned(d) and _host_allowed(d)
    ]
    if not available:
        if all(_host_banned(d) for d in domains):
            raise RuntimeError(
                "cf 配置的根域名均已拉黑: " + ", ".join(domains)
            )
        raise RuntimeError(
            "cf 配置的根域名均不在白名单: " + ", ".join(domains)
        )
    mode = (config.CF_DOMAIN_MODE or "random").strip().lower()
    if mode == "poll":
        with _DOMAIN_LOCK:
            domain = available[_domain_index % len(available)]
            _domain_index += 1
    else:
        if mode != "random":
            logger.warning(f"[临时邮箱] 未知 cf_domain_mode={mode!r}，回退 random")
        domain = random.choice(available)
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


def mail_host(address: str) -> str:
    """从邮箱取出 @ 后的主机名。"""
    raw = str(address or "").strip().lower()
    if "@" not in raw:
        return ""
    return raw.rsplit("@", 1)[-1].strip().rstrip(".")


# 常见多段公共后缀：拉黑时升到 registrable domain，
# 例如 a.shop.co.uk → shop.co.uk，而不是只禁随机子域。
_MULTI_PART_PUBLIC_SUFFIXES = (
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.jp",
    "ne.jp",
    "or.jp",
    "com.br",
    "com.cn",
    "net.cn",
    "org.cn",
    "co.nz",
    "co.kr",
    "com.sg",
    "com.hk",
    "com.tw",
)


def _canonical_ban_host(host: str) -> str:
    """完整二级域名（registrable domain），白名单 / 黑名单同一粒度。

    ``d2.inovel26.com`` / ``ss.imagesthere.com`` → ``inovel26.com`` / ``imagesthere.com``。
    命中已配置的 cf 根域时升到该根域（``ab.feqz.help`` → ``feqz.help``）。
    ``a.b.co.uk`` 一类多段公共后缀升到 ``b.co.uk``。
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    matched = ""
    for root in config.CF_DOMAINS:
        root = str(root or "").strip().lower().rstrip(".")
        if root and (host == root or host.endswith("." + root)):
            if len(root) > len(matched):
                matched = root
    if matched:
        return matched
    labels = [part for part in host.split(".") if part]
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    for public in _MULTI_PART_PUBLIC_SUFFIXES:
        if host == public or host.endswith("." + public):
            public_labels = public.split(".")
            need = len(public_labels) + 1
            if len(labels) >= need:
                return ".".join(labels[-need:])
            return host
    return suffix


def ban_rejected_address(address: str, reason: str = "xAI invalid") -> str:
    """把被 xAI 拒绝的完整二级域名写入黑名单，返回拉黑的域名。"""
    host = _canonical_ban_host(mail_host(address))
    if not host:
        return ""
    from db import ban_mail_domain

    added = ban_mail_domain(host, reason=reason)
    global _banned_cache
    snapshot: set[str] | None = None
    with _banned_lock:
        cached = _banned_cache
    if cached is None:
        from db import list_banned_mail_domains

        snapshot = set(list_banned_mail_domains())
    with _banned_lock:
        if _banned_cache is None:
            _banned_cache = snapshot if snapshot is not None else set()
        _banned_cache.add(host)
    if added:
        logger.warning(f"[临时邮箱] 已拉黑二级域名 {host}（{address}）")
    else:
        logger.debug(f"[临时邮箱] 二级域名已在黑名单: {host}")
    return host


def invalidate_banned_cache() -> None:
    """黑名单被整表替换后丢掉进程内缓存。"""
    global _banned_cache
    with _banned_lock:
        _banned_cache = None


def canonicalize_mail_domains(raw: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """去重保序，白名单 / 黑名单只记录完整二级域名。"""
    hosts: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        host = _canonical_ban_host(str(item or "").strip().lower().rstrip("."))
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


def _banned_hosts() -> set[str]:
    global _banned_cache
    with _banned_lock:
        if _banned_cache is not None:
            return set(canonicalize_mail_domains(_banned_cache))
    from db import list_banned_mail_domains

    hosts = set(list_banned_mail_domains())
    with _banned_lock:
        _banned_cache = set(hosts)
        return set(_banned_cache)


def _sld_in(host: str, domains: list[str] | set[str]) -> bool:
    """host 的完整二级域名是否落在名单里（名单项同样先升到二级）。"""
    sld = _canonical_ban_host(host)
    if not sld:
        return False
    wanted = set(canonicalize_mail_domains(domains))
    return sld in wanted


def _host_banned(host: str) -> bool:
    return _sld_in(host, _banned_hosts())


def _allowed_hosts() -> list[str]:
    return canonicalize_mail_domains(config.MAIL_DOMAIN_WHITELIST)


def _host_allowed(host: str) -> bool:
    """空白名单不限制；非空则完整二级域名必须命中白名单。"""
    allowed = _allowed_hosts()
    if not allowed:
        return True
    return _sld_in(host, allowed)


def _pause_before_recreate(provider: str, attempt: int) -> None:
    """下一次创建前等待，避免连打上游触发 HTTP 429。已取消则中止。"""
    if attempt >= _CREATE_TRIES:
        return
    logger.debug(
        f"[临时邮箱] {provider} {_CREATE_RETRY_INTERVAL:.0f}s 后重创 "
        f"({attempt}/{_CREATE_TRIES})"
    )
    if wait_or_cancel(_CREATE_RETRY_INTERVAL):
        raise RuntimeError(f"{provider} 重创邮箱已取消")


def _create_until_acceptable(
    provider: str,
    create_once: Callable[[], tuple[str, str]],
    *,
    skip_ban: bool = False,
) -> tuple[str, str]:
    """创建邮箱；二级域名命中黑名单或不在白名单则隔 2 秒丢弃重创。"""
    last_address = ""
    last_reason = ""
    last_sld = ""
    for attempt in range(1, _CREATE_TRIES + 1):
        address, token = create_once()
        last_address = address
        host = mail_host(address)
        last_sld = _canonical_ban_host(host)
        if not skip_ban and _host_banned(host):
            last_reason = "banned"
            logger.warning(
                f"[临时邮箱] {address} 二级域名 {last_sld} 已拉黑，"
                f"丢弃重创 ({attempt}/{_CREATE_TRIES})"
            )
            _pause_before_recreate(provider, attempt)
            continue
        if not _host_allowed(host):
            last_reason = "whitelist"
            logger.warning(
                f"[临时邮箱] {address} 二级域名 {last_sld} 不在白名单，"
                f"丢弃重创 ({attempt}/{_CREATE_TRIES})"
            )
            if skip_ban:
                break
            _pause_before_recreate(provider, attempt)
            continue
        return address, token
    last_note = f"（最后一次 {last_address}）" if last_address else ""
    sld_note = f" {last_sld}" if last_sld else ""
    if last_reason == "whitelist":
        raise RuntimeError(
            f"{provider} 抽到非白名单二级域名{sld_note}{last_note}"
        )
    raise RuntimeError(
        f"{provider} 连续 {_CREATE_TRIES} 次抽到已拉黑二级域名{sld_note}{last_note}"
    )


def _cf_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（子域 2–4 位）；二级域名命中黑名单或不在白名单则丢弃重创。"""
    if not config.CF_API_BASE:
        raise RuntimeError("cf_api_base 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(
            random.choices(string.ascii_lowercase, k=random.randint(6, 10))
        )
    address, jwt = _create_until_acceptable("cf", lambda: _cf_create_once(local))
    logger.debug(f"[临时邮箱] 已创建: {address}")
    return address, jwt


def _yyds_create_temp_email(local_part: str = "") -> tuple[str, str]:
    """创建临时邮箱（API key 默认域）；二级域名命中黑名单或不在白名单则丢弃重创。"""
    if not (config.YYDS_API_KEY or "").strip():
        raise RuntimeError("yyds_api_key 未配置")
    local = (local_part or "").strip().lower()
    if not local:
        local = "".join(random.choices(string.ascii_lowercase, k=random.randint(6, 10)))
    address, token = _create_until_acceptable(
        "yyds", lambda: _yyds_create_once(local)
    )
    logger.debug(f"[临时邮箱] 已创建 (yyds): {address}")
    return address, token


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
    """创建临时邮箱；二级域名命中黑名单或不在白名单则丢弃重创。"""
    custom = (config.TEMPMAIL_DOMAIN or "").strip()
    address, token = _create_until_acceptable(
        "tempmail",
        lambda: _tempmail_create_once(local_part),
        skip_ban=bool(custom),
    )
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
