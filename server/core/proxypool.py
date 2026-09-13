"""出口代理池：注册粘性绑定 + 网关轮询，失败短冷却。

配置源是 ``config.PROXIES``。线程内 ``bind()`` 后 ``current()`` 固定同一条，
保证同一注册流程浏览器 / 邮件 / OAuth 出口一致；网关不 bind，每次 ``pick()``。

本机出口（回环地址，如 ``http://127.0.0.1:7890``）是本地代理客户端而非远端出口，
**不参与任何冷却与降智排除**：它一旦被冻结，整条链路就没有出口可用了。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from core import config
from core.logger import logger
from core.util import proxy_endpoint_ready

_COOL_SEC = 60.0
# 降智升级与账号同口径：第 2 次冷却时长（内存，重启清空）
_QUALITY_COOL_SEC = 12 * 3600.0

_local_cache: dict[str, bool] = {}
_local_lock = threading.Lock()

_lock = threading.Lock()
_seq = 0
_cool: dict[str, float] = {}
_quality_cool: dict[str, float] = {}
_quality_strikes: dict[str, int] = {}
_quality_disabled: set[str] = set()
# 本机出口降智只提示一次，避免高频日志刷屏
_local_warned: set[str] = set()
_tls = threading.local()


def parse_proxy_list(raw: Any) -> list[str]:
    """把字符串或数组收成去重后的代理 URL 列表（支持换行 / 逗号 / 分号）。"""
    parts: list[str]
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(item) for item in raw]
    elif isinstance(raw, str):
        parts = raw.replace(";", "\n").replace(",", "\n").splitlines()
    else:
        parts = str(raw).splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        url = part.strip()
        if not url or url.startswith("#"):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def redact(url: str) -> str:
    """日志用：把 user:pass 里的密码打成 ***。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    if parsed.password is None:
        return raw
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = parsed.username or ""
    auth = f"{user}:***@" if user else "***@"
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}{auth}{host}{parsed.path or ''}"


def urls() -> list[str]:
    """当前配置里的代理列表（拷贝）。"""
    return [u for u in (getattr(config, "PROXIES", None) or []) if u]


def is_local(url: str) -> bool:
    """是否本机出口（回环地址）。

    判定只认 host，不看端口：``127.0.0.0/8``、``localhost``、``::1`` 都算本机。
    结果带缓存——``pick()`` 每次都会调用它，配置里的代理 URL 是有限集合。
    """
    raw = str(url or "").strip()
    if not raw:
        return False
    with _local_lock:
        hit = _local_cache.get(raw)
    if hit is not None:
        return hit
    host = (urlsplit(raw if "://" in raw else f"//{raw}").hostname or "").strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        local = True
    else:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
    with _local_lock:
        _local_cache[raw] = local
    return local


def _blocked(url: str, now: float) -> bool:
    """网络冷却 / 降智冷却 / 降智排除，pick 时跳过。本机出口永不跳过。"""
    if is_local(url):
        return False
    if url in _quality_disabled:
        return True
    if _cool.get(url, 0) > now:
        return True
    if _quality_cool.get(url, 0) > now:
        return True
    return False


def has_other(exclude: str) -> bool:
    """是否还有可选用的其它代理（跳过排除中 / 降智冷却中的）。"""
    now = time.monotonic()
    with _lock:
        return any(u != exclude and not _blocked(u, now) for u in urls())


def pick(*, exclude: str = "") -> str:
    """轮询一条未冷却代理；全部冷却时仍返回一条，避免网关断流。"""
    pool = urls()
    if not pool:
        return ""
    now = time.monotonic()
    with _lock:
        live = [u for u in pool if not _blocked(u, now)]
        if exclude:
            filtered = [u for u in live if u != exclude]
            if filtered:
                live = filtered
        if not live:
            live = [u for u in pool if u != exclude] or pool
        global _seq
        chosen = live[_seq % len(live)]
        _seq += 1
        return chosen


def bind(url: str | None = None) -> str:
    """把当前线程钉在一条代理上（注册一轮）。"""
    chosen = url if url else pick()
    _tls.url = chosen
    return chosen


def unbind() -> None:
    """解开当前线程的粘性绑定。"""
    _tls.url = None


def current() -> str:
    """绑定中返回绑定值；否则 pick 一条。"""
    bound = getattr(_tls, "url", None)
    if isinstance(bound, str) and bound:
        return bound
    return pick()


def mark_fail(url: str, seconds: float = _COOL_SEC) -> None:
    """网络失败短冷却，后续 pick 优先跳过。与降智升级制分开。

    本机出口不记冷却：本地代理客户端偶发抖动，不该让池子失去唯一可靠出口。
    """
    if not url or is_local(url):
        return
    with _lock:
        _cool[url] = time.monotonic() + max(0.0, float(seconds))


def mark_quality_hit(url: str, *, cooldown_sec: float = _QUALITY_COOL_SEC) -> str:
    """出口 IP 降智升级，口径对齐账号：观察 → 冷却 12h → 排除。

    返回 observed / cooled / disabled / unchanged / local。内存态，重启清空。
    本机出口返回 local 且不记任何状态：它代表的是本地代理客户端而非供应商出口，
    把它冷却或排除等于自断出口（远端全挂时唯一能用的就是它）。
    """
    if not url:
        return "unchanged"
    if is_local(url):
        with _lock:
            first = url not in _local_warned
            _local_warned.add(url)
        if first:
            logger.warning(
                f"[代理池] 降智命中本机出口 {redact(url)}，不记冷却（本机出口常驻可用）"
            )
        return "local"
    now = time.monotonic()
    with _lock:
        if url in _quality_disabled:
            return "unchanged"
        if _quality_cool.get(url, 0) > now:
            return "unchanged"
        n = _quality_strikes.get(url, 0) + 1
        _quality_strikes[url] = n
        shown = redact(url)
        if n >= 3:
            _quality_disabled.add(url)
            _quality_cool.pop(url, None)
            logger.warning(
                f"[代理池] 降智第 {n} 次命中 {shown} 长期排除（重启或改配置可恢复）"
            )
            return "disabled"
        if n >= 2:
            _quality_cool[url] = now + max(0.0, float(cooldown_sec))
            hours = max(0.0, float(cooldown_sec)) / 3600.0
            logger.warning(
                f"[代理池] 降智第 {n} 次命中 {shown} 冷却 {hours:g}h"
            )
            return "cooled"
        logger.warning(
            f"[代理池] 降智首次命中 {shown}（仅记录观察，再次命中才冷却）"
        )
        return "observed"


def status_label(url: str) -> str:
    """日志片段：脱敏 URL + TCP 探活。"""
    if not url:
        return "none"
    ready = "ready" if proxy_endpoint_ready(url, timeout=0.2) else "down"
    return f"{redact(url)}={ready}"


def snapshot() -> dict[str, Any]:
    """管理面运行态：条数、冷却、降智升级、脱敏展示。"""
    now = time.monotonic()
    items: list[dict[str, Any]] = []
    cooling = 0
    disabled = 0
    with _lock:
        for url in urls():
            local = is_local(url)
            net_until = _cool.get(url, 0.0)
            q_until = _quality_cool.get(url, 0.0)
            until = max(net_until, q_until)
            # 本机出口恒为正常：即使内部字典被写入（如单测直接改私有态）也不上报异常
            cool = until > now and not local
            is_disabled = url in _quality_disabled and not local
            if cool:
                cooling += 1
            if is_disabled:
                disabled += 1
            items.append(
                {
                    "display": redact(url),
                    "local": local,
                    "cooling": cool,
                    "cool_left_sec": max(0, int(until - now)) if cool else 0,
                    "strikes": 0 if local else _quality_strikes.get(url, 0),
                    "disabled": is_disabled,
                }
            )
    return {
        "total": len(items),
        "cooling": cooling,
        "disabled": disabled,
        "items": items,
    }


def reset() -> None:
    """单测用：清轮询下标、冷却、降智升级与线程绑定。"""
    global _seq
    with _lock:
        _seq = 0
        _cool.clear()
        _quality_cool.clear()
        _quality_strikes.clear()
        _quality_disabled.clear()
        _local_warned.clear()
    unbind()
