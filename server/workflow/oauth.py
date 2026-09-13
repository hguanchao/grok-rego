"""SSO 协议级自动认证客户端。

借鉴 acorn 项目的「SSO cookie + device flow 协议级 approve」思路重写实现，
适配 grok-rego 业务上下文：

- sso_cookie 为字符串（sso 会话凭证 JWT 原值；sso 与 sso-rw value 相同）
- 使用 curl_cffi impersonate="chrome" 模拟浏览器 TLS 指纹
- 三级降级路径：协议级 device（首选，全自动）→ device code 人工兜底
- 全程无浏览器、无人工干预，认证失败返回明确原因

路径说明：
  协议级 device：注入 sso cookie → 校验会话有效 → 请求 device_code
    → POST /oauth2/device/verify（模拟用户访问授权页）
    → POST /oauth2/device/approve（模拟用户点击 Allow）
    → 轮询 /oauth2/token 直到拿到 access_token
"""
import json
import time
from typing import Any

from curl_cffi import requests

from core.config import OAUTH2_CLIENT_ID, OAUTH2_ISSUER, OAUTH2_SCOPES
from core.logger import logger
from core.util import curl_error_code, proxy_endpoint_ready

# ─── OAuth2 端点与协议常量 ───────────────────────────────
_DEVICE_CODE_URL = f"{OAUTH2_ISSUER}/oauth2/device/code"
_DEVICE_VERIFY_URL = f"{OAUTH2_ISSUER}/oauth2/device/verify"
_DEVICE_APPROVE_URL = f"{OAUTH2_ISSUER}/oauth2/device/approve"
_TOKEN_ENDPOINT = f"{OAUTH2_ISSUER}/oauth2/token"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_REFERRER = "grok-build"
_SCOPE_STR = " ".join(OAUTH2_SCOPES)

# 协议级 approve 的宽限时间（invalid_grant 短暂重试窗口）与总轮询上限
_PROTOCOL_GRACE = 8.0
_POLL_DEADLINE = 120.0
_REFRESH_RETRY_DELAYS = (1.0, 2.0)
# 仅连接建立前失败可安全重试；56 属于响应接收阶段，refresh_token 可能已轮换，禁止盲重试。
_SAFE_REFRESH_RETRY_CODES = {5, 6, 7}

# 客户端版本头（对齐 grok-cli）
_CLIENT_VERSION = "1.0.3"
_CLIENT_SURFACE = "cli"

# 表单请求通用头
_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml",
    "Origin": "https://accounts.x.ai",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}


def _version_headers() -> dict[str, str]:
    """设备授权码请求用的版本标识头。"""
    return {
        "x-grok-client-version": _CLIENT_VERSION,
        "x-grok-client-surface": _CLIENT_SURFACE,
    }


def _token_ua() -> str:
    """token 端点 User-Agent（对齐 grok-shell 风格）。"""
    return f"grok-shell/{_CLIENT_VERSION}"


def _chrome_session(proxy: str = "") -> requests.Session:
    """创建带 chrome TLS 指纹的会话；附代理。"""
    session = requests.Session(impersonate="chrome")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


# sso cookie 注入域：xAI 全家域（固定写死，sso 与 sso-rw value 相同）
_SSO_COOKIE_DOMAINS = (
    ".x.ai", "accounts.x.ai", "auth.x.ai",
    ".grok.com", "grok.com",
)


def _extract_sso_value(sso_cookie: Any) -> str:
    """提取 sso 会话凭证原值。

    新格式：sso_cookie 为字符串（sso 会话凭证 JWT 原值），直接返回。
    兼容历史数组格式 [{name, value, ...}]：优先取 sso-rw 的 value。
    """
    if not sso_cookie:
        return ""
    # 字符串：直接当作 sso 凭证原值
    if isinstance(sso_cookie, str):
        return sso_cookie.strip()
    # 兼容历史 list 格式
    if isinstance(sso_cookie, list):
        for cookie in sso_cookie:
            if isinstance(cookie, dict) and str(cookie.get("name") or "").lower() == "sso-rw":
                return str(cookie.get("value") or "").strip()
        for cookie in sso_cookie:
            if isinstance(cookie, dict) and str(cookie.get("name") or "").lower() == "sso":
                return str(cookie.get("value") or "").strip()
    return ""


def _build_sso_session(sso_value: str, proxy: str = "") -> requests.Session | None:
    """注入 sso cookie 建立会话；sso 与 sso-rw value 相同，固定覆盖全家域。"""
    if not sso_value:
        return None
    session = _chrome_session(proxy)
    for domain in _SSO_COOKIE_DOMAINS:
        session.cookies.set("sso", sso_value, domain=domain)
        session.cookies.set("sso-rw", sso_value, domain=domain)
    return session


def _validate_session(session: requests.Session) -> bool:
    """校验 sso 会话是否有效：访问 accounts.x.ai 首页不应跳转登录页。"""
    try:
        r = session.get(
            "https://accounts.x.ai/",
            impersonate="chrome",
            timeout=15,
            allow_redirects=True,
        )
        url = str(r.url or "")
        # 跳转到 sign-in / sign-up 表示 sso 已失效
        if "sign-in" in url or "sign-up" in url or r.status_code == 401:
            logger.warning("[认证] sso cookie 已失效（跳转登录页）")
            return False
        return True
    except Exception as exc:
        logger.warning(f"[认证] sso 会话校验网络异常: {type(exc).__name__}: {exc}")
        return False


def _request_device_code(session: requests.Session) -> dict[str, Any] | None:
    """请求设备授权码，返回 {device_code, user_code, interval, open_url} 或 None。"""
    try:
        r = session.post(
            _DEVICE_CODE_URL,
            data={
                "client_id": OAUTH2_CLIENT_ID,
                "scope": " ".join(OAUTH2_SCOPES),
                "referrer": _REFERRER,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                **_version_headers(),
            },
            impersonate="chrome",
            timeout=20,
        )
    except Exception as exc:
        logger.error(f"[认证] 请求设备授权码网络异常: {type(exc).__name__}: {exc}")
        return None
    if not (200 <= r.status_code < 300):
        logger.error(f"[认证] 请求设备授权码失败: HTTP {r.status_code}")
        return None
    try:
        device = r.json()
    except Exception:
        logger.error("[认证] 设备授权码响应非 JSON")
        return None
    device_code = str(device.get("device_code") or "")
    user_code = str(device.get("user_code") or "")
    if not device_code or not user_code:
        logger.error("[认证] 设备授权码响应缺少 device_code / user_code")
        return None
    try:
        interval = max(1, int(device.get("interval") or 2))
    except (TypeError, ValueError):
        interval = 2
    open_url = (
        device.get("verification_uri_complete")
        or (
            f"{device.get('verification_uri')}?user_code={user_code}"
            if device.get("verification_uri")
            else ""
        )
        or ""
    )
    return {
        "device_code": device_code,
        "user_code": user_code,
        "interval": interval,
        "open_url": open_url,
    }


def _protocol_approve(
    session: requests.Session, device: dict[str, Any], principal_id: str
) -> bool:
    """协议级完成 device 授权：verify → approve，全程 HTTP 模拟，无浏览器。

    返回 True 表示授权流程已走完（可进入 token 轮询）。
    """
    user_code = device["user_code"]
    open_url = device.get("open_url", "")

    # 1. 访问授权页（带 user_code 的 verify URL）
    if open_url and open_url.startswith("https://"):
        try:
            session.get(
                open_url,
                impersonate="chrome",
                timeout=15,
                allow_redirects=True,
            )
        except Exception as exc:
            logger.warning(f"[认证] 访问授权页异常（继续尝试）: {exc}")

    # 2. POST verify：提交 user_code，进入授权确认页
    logger.info(f"[认证] 提交 device verify  user_code={user_code}")
    try:
        r = session.post(
            _DEVICE_VERIFY_URL,
            data={"user_code": user_code},
            headers={**_FORM_HEADERS, "Referer": open_url or "https://accounts.x.ai/"},
            impersonate="chrome",
            timeout=20,
            allow_redirects=True,
        )
    except Exception as exc:
        logger.error(f"[认证] verify 请求异常: {type(exc).__name__}: {exc}")
        return False

    url = str(r.url or "")
    # 登录会话失效：verify 被拒
    if "sign-in" in url or r.status_code in (401, 403):
        logger.warning("[认证] device 校验被拒（登录会话失效）")
        return False

    # 已直接到 done 页（会话记忆了之前的授权）
    done = "/oauth2/device/done" in url.lower()
    if done:
        logger.info("[认证] verify 已直接到达 done 页（会话记忆授权）")
        return True

    # 3. POST approve：模拟用户点击 Allow
    logger.info(f"[认证] 提交 device approve  principal_id={principal_id}")
    try:
        r = session.post(
            _DEVICE_APPROVE_URL,
            data={
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": principal_id,
            },
            headers={**_FORM_HEADERS, "Referer": url or "https://accounts.x.ai/"},
            impersonate="chrome",
            timeout=20,
            allow_redirects=True,
        )
    except Exception as exc:
        logger.error(f"[认证] approve 请求异常: {type(exc).__name__}: {exc}")
        return False

    if "/oauth2/device/done" not in str(r.url or "").lower():
        logger.warning(f"[认证] device 批准未完成（HTTP {r.status_code} url={r.url}）")
        return False
    logger.success("[认证] device 批准完成")
    return True


def _poll_device_token(
    device: dict[str, Any], grace: float = _PROTOCOL_GRACE
) -> dict[str, Any] | None:
    """轮询 token 端点直到拿到 access_token 或终态错误。

    grace：invalid_grant 的宽限重试窗口（授权流程刚走完，上游可能短暂未就绪）。
    """
    started = time.time()
    grace_deadline = started + grace
    poll_deadline = started + _POLL_DEADLINE
    interval = max(1, int(device.get("interval") or 2))
    from core import proxypool

    session = _chrome_session(proxypool.current())
    round_n = 0

    while time.time() < poll_deadline:
        try:
            r = session.post(
                _TOKEN_ENDPOINT,
                data={
                    "grant_type": _DEVICE_GRANT,
                    "device_code": device["device_code"],
                    "client_id": OAUTH2_CLIENT_ID,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": _token_ua(),
                    **_version_headers(),
                },
                impersonate="chrome",
                timeout=8,
            )
        except Exception as exc:
            logger.debug(f"[认证] token 轮询网络异常（继续）: {exc}")
            time.sleep(interval)
            continue

        try:
            payload = r.json() if hasattr(r, "json") else {}
        except Exception:
            logger.debug(f"[认证] token 轮询响应非 JSON: HTTP {r.status_code}")
            time.sleep(interval)
            continue

        if 200 <= r.status_code < 300 and payload.get("access_token"):
            logger.info("[认证] Token 交换成功")
            return payload

        err = str(payload.get("error") or "")
        if err == "authorization_pending":
            round_n += 1
            if round_n == 1 or round_n % 5 == 0:
                logger.debug(f"[认证] 等待授权完成: round={round_n} interval={interval}s")
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            logger.debug(f"[认证] 轮询降速: interval={interval}s")
            time.sleep(interval)
            continue
        if err == "invalid_grant" and time.time() < grace_deadline:
            time.sleep(interval)
            continue
        if err in ("expired_token", "access_denied", "invalid_grant"):
            logger.warning(f"[认证] 授权已终止: error={err}")
            return None
        logger.warning(f"[认证] token 响应异常: HTTP {r.status_code} error={err or 'unknown'}")
        time.sleep(interval)

    logger.warning("[认证] 等待 Token 交换超时")
    return None


def _decode_jwt_subject(token: str) -> str:
    """从 JWT 中解 sub / principal_id（approve 请求需要）。失败返回空串。"""
    import base64

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        return str(payload.get("sub") or payload.get("principal_id") or "")
    except Exception:
        return ""


def auth_with_sso(sso_cookie: Any) -> tuple[dict[str, Any] | None, str]:
    """用 sso cookie 协议级自动认证，返回 (token_dict, reason)。

    token_dict 含 access_token / refresh_token / expires_in；失败返回 (None, reason)。
    全程无人工干预。
    """
    sso_value = _extract_sso_value(sso_cookie)
    if not sso_value:
        logger.warning("[认证] 缺少 sso cookie，无法自动认证")
        return None, "缺少 sso cookie，无法自动认证（需重新注册获取 SSO）"

    logger.info("[认证] SSO 协议级自动认证开始")
    from core import proxypool

    session = _build_sso_session(sso_value, proxy=proxypool.current())
    if session is None:
        logger.error("[认证] sso 会话构建失败")
        return None, "sso 会话构建失败"

    # 1. 校验会话
    if not _validate_session(session):
        logger.warning("[认证] sso cookie 已失效，认证终止")
        return None, "sso cookie 已失效（会话过期或被踢）"

    logger.info("[认证] sso 会话校验通过")
    # 2. 请求 device code
    device = _request_device_code(session)
    if device is None:
        logger.error("[认证] 请求设备授权码失败")
        return None, "请求设备授权码失败"

    logger.info(f"[认证] 设备授权码已获取  user_code={device.get('user_code')}")
    # 3. 协议级 approve（模拟用户确认授权）
    principal_id = _decode_jwt_subject(sso_value)
    if not _protocol_approve(session, device, principal_id):
        logger.error("[认证] 协议级授权确认失败")
        return None, "协议级授权确认失败（会话可能已失效）"

    logger.info("[认证] 协议级授权确认完成，开始轮询 Token")
    # 4. 轮询 token
    token = _poll_device_token(device)
    if token and token.get("access_token"):
        logger.success("[认证] Token 交换成功")
        return token, "200 Token 交换成功"
    logger.error("[认证] Token 交换超时或被拒绝")
    return None, "Token 交换超时或被拒绝"


def refresh_token(token: str) -> tuple[dict[str, Any] | None, int]:
    """用 refresh_token 换新 access_token，返回 (token_data, http_status)。

    网络异常时 http_status=0；成功时 http_status=200。
    """
    from core import proxypool

    proxy = proxypool.current()
    if proxy and not proxy_endpoint_ready(proxy):
        logger.warning("[认证] token 刷新跳过：代理未就绪")
        return None, 0

    for attempt in range(len(_REFRESH_RETRY_DELAYS) + 1):
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.post(
                _TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token,
                    "client_id": OAUTH2_CLIENT_ID,
                    "scope": _SCOPE_STR,
                },
                headers={"referrer": _REFERRER},
                proxies=proxies,
                timeout=60,
                impersonate="chrome",
            )
        except requests.RequestsError as exc:
            code = curl_error_code(exc)
            can_retry = code in _SAFE_REFRESH_RETRY_CODES and attempt < len(
                _REFRESH_RETRY_DELAYS
            )
            logger.warning(
                f"[认证] token 刷新请求异常: {type(exc).__name__} "
                f"curl={code or 'unknown'} proxy={proxypool.status_label(proxy)} "
                f"retry={can_retry}"
            )
            if proxy:
                proxypool.mark_fail(proxy)
            if not can_retry:
                return None, 0
            nxt = proxypool.pick(exclude=proxy)
            if nxt:
                proxy = nxt
            time.sleep(_REFRESH_RETRY_DELAYS[attempt])
            continue
        try:
            if resp.status_code != 200:
                logger.warning(f"[认证] token 刷新失败 status={resp.status_code}")
                return None, resp.status_code
            logger.success(f"[认证] token 刷新成功  · HTTP {resp.status_code}")
            return resp.json(), 200
        finally:
            resp.close()
    return None, 0
