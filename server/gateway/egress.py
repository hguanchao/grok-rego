"""出口 IP 探测（带 TTL 缓存）。

网关落库的「出口 IP」：本机经出口代理访问公网时的 IP。
- 走指定/当前代理发起探测请求（api.ipify.org）；未配置代理则直连探测；
- 按代理 URL 分槽 TTL 缓存，避免每个请求都打探测服务；
- 探测失败沿用上次结果并缩短 TTL 重试，绝不阻塞转发主流程（最坏返回空串）。
"""

from __future__ import annotations

import threading
import time

from curl_cffi import requests

from core import proxypool

_TTL_OK = 600.0   # 成功结果缓存 10 分钟
_TTL_FAIL = 60.0  # 失败后 1 分钟才重试
_URL = "https://api.ipify.org/?format=json"

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}


def current_ip(proxy: str | None = None) -> str:
    """当前出口 IP；失败时尽力返回上次结果，否则空串。"""
    url = proxy if proxy is not None else proxypool.current()
    key = url or "-"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    ip = ""
    try:
        kwargs: dict[str, object] = {"timeout": (5.0, 10.0)}
        if url:
            kwargs["proxy"] = url
        resp = requests.get(_URL, **kwargs)  # type: ignore[arg-type]
        if 200 <= resp.status_code < 300:
            payload = resp.json()
            if isinstance(payload, dict):
                ip = str(payload.get("ip") or "").strip()
    except Exception:  # noqa: BLE001 —— 探测失败不影响转发
        ip = ""

    ttl = _TTL_OK if ip else _TTL_FAIL
    with _lock:
        prev = _cache.get(key, (0.0, ""))[1]
        stored = ip or prev
        _cache[key] = (now + ttl, stored)
        return stored
