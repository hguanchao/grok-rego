"""
Grok 号池网关。

把本机 `/grok/v1/*` 转发到 `https://cli-chat-proxy.grok.com/v1`，
用号池账号的 access_token 鉴权。选号：请求头 `X-Account-Id` 或查询参数 `account_id`。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from core import config
from core.config import UPSTREAM_BASE
from core.logger import logger
from core.util import curl_error_code, mask_email, now_iso_tz, proxy_endpoint_ready
from db import get_account_by_id, insert_usage
from gateway import egress
from gateway.usage import StreamUsageAccumulator, extract_nonstream

GROK_BASE = UPSTREAM_BASE.rstrip("/")
_CLIENT_VERSION = "1.0.0"
_GROK_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": _CLIENT_VERSION,
    "x-authenticateresponse": "authenticate-response",
    "User-Agent": f"grok-shell/{_CLIENT_VERSION} (windows; x86_64)",
}

_MAX_BODY = 32 * 1024 * 1024
_LOG_CAP = 200
_CONNECT_TIMEOUT = 20.0
_READ_TIMEOUT = 300.0
_UPSTREAM_RETRY_CODES = {5, 6, 7, 18, 28, 35, 52, 56}
_UPSTREAM_RETRY_DELAY = 0.5
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
    "expect",
}

_lock = threading.Lock()
_seq = 0
_logs: deque[dict[str, Any]] = deque(maxlen=_LOG_CAP)
_stats: dict[str, Any] = {
    "total": 0,
    "ok": 0,
    "error": 0,
    "streaming": 0,
    "bytes_in": 0,
    "bytes_out": 0,
    "last_status": None,
    "last_path": None,
    "last_at": None,
    "last_error": None,
    "last_account": None,
}


def _next_id() -> int:
    global _seq
    _seq += 1
    return _seq


def _record(
    *,
    method: str,
    path: str,
    model: str | None,
    stream: bool,
    status: int,
    ms: int,
    bytes_in: int,
    bytes_out: int,
    error: str | None,
    account_id: int | None = None,
    account: str = "",
    account_email: str | None = None,
    usage: dict[str, int] | None = None,
    ip: str | None = None,
    client_ua: str | None = None,
) -> None:
    """写入环形日志、累加计数，并落库一条用量记录（含号池账号归属）。"""
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    entry = {
        "id": _next_id(),
        "ts": now_iso_tz(),
        "method": method,
        "path": path,
        "model": model or "",
        "stream": stream,
        "status": status,
        "ms": ms,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "error": error or "",
        "account_id": account_id or 0,
        "account": account,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cache_tokens": usage.get("cache_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
    }
    with _lock:
        _logs.appendleft(entry)
        _stats["total"] += 1
        if 200 <= status < 400:
            _stats["ok"] += 1
        else:
            _stats["error"] += 1
        if stream:
            _stats["streaming"] += 1
        _stats["bytes_in"] += bytes_in
        _stats["bytes_out"] += bytes_out
        _stats["last_status"] = status
        _stats["last_path"] = path
        _stats["last_at"] = entry["ts"]
        _stats["last_error"] = error or ""
        _stats["last_account"] = account
    try:
        insert_usage(
            ip=ip,
            client_ua=client_ua,
            endpoint=path.split("?", 1)[0],
            model=model or "",
            stream=stream,
            account_id=account_id,
            account_email=account_email or (account if account and "@" in account else None),
            status=1 if 200 <= status < 400 else 0,
            reason=error or None,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cache_tokens=usage.get("cache_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
        )
    except Exception:
        # 落库失败绝不阻塞网关转发
        logger.exception("[Grok网关] 用量落库失败")


def snapshot() -> dict[str, Any]:
    """管理面快照：接入信息 + 计数 + 最近请求。"""
    with _lock:
        stats = dict(_stats)
        logs = list(_logs)
    return {
        "upstream": GROK_BASE,
        "client_base": f"http://{config.API_HOST}:{config.API_PORT}/grok/v1",
        "base_path": "/grok/v1",
        "select": "X-Account-Id 或 ?account_id=",
        "proxy": str(config.PROXY or "").strip(),
        "stats": stats,
        "logs": logs,
    }


def _upstream_url(path: str, query: str) -> str:
    prefix = "/grok/v1"
    suffix = path[len(prefix):] if path.startswith(prefix) else path
    url = GROK_BASE + (suffix or "")
    if query:
        url = f"{url}?{query}"
    return url


def _extract_meta(body: bytes) -> tuple[str | None, bool]:
    if not body:
        return None, False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, False
    if not isinstance(payload, dict):
        return None, False
    model = payload.get("model")
    model_s = str(model).strip() if isinstance(model, str) else None
    return model_s or None, bool(payload.get("stream"))


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > _MAX_BODY:
        raise ValueError(f"请求体过大（>{_MAX_BODY} bytes）")
    return handler.rfile.read(length)


def _strip_account_query(query: str) -> str:
    if not query:
        return ""
    parsed = parse_qs(query, keep_blank_values=True)
    parsed.pop("account_id", None)
    return urlencode(parsed, doseq=True)


def _resolve_account(handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any] | None, str]:
    """从 X-Account-Id 或 ?account_id= 解析号池账号。"""
    raw = str(handler.headers.get("X-Account-Id") or "").strip()
    if not raw:
        parsed = urlparse(handler.path)
        raw = str((parse_qs(parsed.query).get("account_id") or [""])[0]).strip()
    if not raw.isdigit():
        return None, "缺少账号：设置请求头 X-Account-Id 或查询参数 account_id"
    acc = get_account_by_id(int(raw))
    if not acc:
        return None, f"账号不存在: {raw}"
    if not str(acc.get("access_token") or "").strip():
        return None, f"账号未认证: {acc.get('email') or raw}"
    return acc, ""


def _forward_headers(incoming: Any, token: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in incoming.items():
        low = key.lower()
        if low in _HOP_BY_HOP or low in {
            "authorization",
            "x-account-id",
            "x-xai-token-auth",
            "x-grok-client-version",
            "x-authenticateresponse",
            "user-agent",
        }:
            continue
        out[key] = value
    out.update(_GROK_HEADERS)
    out["Authorization"] = f"Bearer {token}"
    return out


def _response_headers(upstream: requests.Response) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for key, value in upstream.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        headers.append((key, value))
    return headers


def _send_cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Api-Key, X-Account-Id, HTTP-Referer, X-Title, OpenAI-Beta",
    )
    handler.send_header("Access-Control-Expose-Headers", "*")


def _send_bytes(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
    extra: list[tuple[str, str]] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    _send_cors(handler)
    if extra:
        for key, value in extra:
            if key.lower() in {"content-type", "content-length"}:
                continue
            handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
        handler.wfile.flush()


def _openai_error(message: str, status: int = 502) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": "gateway_error", "code": status}},
        ensure_ascii=False,
    ).encode("utf-8")


def _proxy_kwargs() -> dict[str, Any]:
    proxy = str(config.PROXY or "").strip()
    kwargs: dict[str, Any] = {
        "timeout": (_CONNECT_TIMEOUT, _READ_TIMEOUT),
        "allow_redirects": False,
        "verify": True,
        "impersonate": "chrome",
    }
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def probe(account_id: int) -> dict[str, Any]:
    """用指定号池账号探测上游 GET /billing。"""
    acc = get_account_by_id(int(account_id))
    if not acc:
        return {"ok": False, "status": 0, "ms": 0, "error": f"账号不存在: {account_id}"}
    token = str(acc.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "status": 0, "ms": 0, "error": "账号未认证"}
    t0 = time.monotonic()
    url = f"{GROK_BASE}/billing?format=credits"
    headers = dict(_GROK_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = "application/json"
    try:
        resp = requests.get(url, headers=headers, **_proxy_kwargs())
        elapsed = int((time.monotonic() - t0) * 1000)
        ok = 200 <= resp.status_code < 300
        text = (resp.text or "")[:400]
        return {
            "ok": ok,
            "status": resp.status_code,
            "ms": elapsed,
            "account_id": acc["id"],
            "email": acc.get("email") or "",
            "error": "" if ok else text,
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status": 0,
            "ms": elapsed,
            "account_id": acc["id"],
            "email": acc.get("email") or "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def proxy(handler: BaseHTTPRequestHandler, method: str, path: str) -> None:
    """把当前请求用号池账号 token 转发到 Grok 上游。"""
    parsed = urlparse(handler.path)
    try:
        body = _read_body(handler)
    except ValueError as exc:
        _send_bytes(
            handler,
            413,
            _openai_error(str(exc), 413),
            "application/json; charset=utf-8",
        )
        return

    acc, acc_err = _resolve_account(handler)
    if acc is None:
        _send_bytes(
            handler,
            400,
            _openai_error(acc_err, 400),
            "application/json; charset=utf-8",
        )
        return

    model, stream_flag = _extract_meta(body)
    url = _upstream_url(path, _strip_account_query(parsed.query))
    token = str(acc.get("access_token") or "")
    headers = _forward_headers(handler.headers, token)
    t0 = time.monotonic()
    bytes_out = 0
    status = 502
    err: str | None = None
    upstream: requests.Response | None = None
    response_started = False
    email = str(acc.get("email") or "")
    account_id = int(acc["id"])
    who = mask_email(email)
    # 用量采集：非流式整包解析 / 流式旁路累积
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    acc_us: StreamUsageAccumulator | None = None
    client_ip = egress.current_ip()  # 出口 IP（经代理探测，带缓存）
    client_ua = str(handler.headers.get("User-Agent") or "")[:255]

    try:
        is_stream = False
        raw_chunks = None
        first_raw_chunk = b""
        for request_attempt in range(2):
            try:
                upstream = requests.request(
                    method,
                    url,
                    headers=headers,
                    data=body if body else None,
                    stream=stream_flag,
                    **_proxy_kwargs(),
                )
                status = int(upstream.status_code)
                content_type = (upstream.headers.get("Content-Type") or "").lower()
                is_stream = stream_flag and status < 400 and (
                    "text/event-stream" in content_type or not content_type
                )
                if is_stream and method != "HEAD":
                    raw_chunks = iter(upstream.iter_content(chunk_size=4096))
                    for chunk in raw_chunks:
                        if chunk:
                            first_raw_chunk = chunk
                            break
                break
            except requests.RequestsError as exc:
                code = curl_error_code(exc)
                proxy = str(config.PROXY or "").strip()
                proxy_ready = proxy_endpoint_ready(proxy, timeout=0.2) if proxy else True
                can_retry = (
                    request_attempt == 0
                    and bytes_out == 0
                    and code in _UPSTREAM_RETRY_CODES
                    and (stream_flag or method in {"GET", "HEAD"})
                )
                logger.warning(
                    f"[Grok网关] 上游请求异常 curl={code or 'unknown'} "
                    f"proxy={'ready' if proxy_ready else 'down'} "
                    f"attempt={request_attempt + 1}/2 retry={can_retry}"
                )
                if not can_retry:
                    raise
                if upstream is not None:
                    upstream.close()
                    upstream = None
                time.sleep(_UPSTREAM_RETRY_DELAY)
        extra = _response_headers(upstream)
        if is_stream:
            handler.send_response(status)
            if not any(k.lower() == "content-type" for k, _ in extra):
                handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "close")
            handler.send_header("X-Accel-Buffering", "no")
            _send_cors(handler)
            for key, value in extra:
                if key.lower() in {"content-type", "cache-control", "connection"}:
                    if key.lower() == "content-type":
                        handler.send_header(key, value)
                    continue
                handler.send_header(key, value)
            handler.end_headers()
            response_started = True
            if method != "HEAD":
                acc_us = StreamUsageAccumulator()
                def _with_first_chunk():
                    if first_raw_chunk:
                        yield first_raw_chunk
                    if raw_chunks is not None:
                        yield from raw_chunks

                for chunk in _with_first_chunk():
                    if not chunk:
                        continue
                    # 旁路采样：喂入块供用量解析，不阻塞转发
                    acc_us.feed(chunk)
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    bytes_out += len(chunk)
                usage = acc_us.result()
        else:
            payload = upstream.content or b""
            bytes_out = len(payload)
            ctype = upstream.headers.get("Content-Type") or "application/json; charset=utf-8"
            # 非流式：对上游响应整包解析 usage
            if status < 400:
                usage = extract_nonstream(payload)
            _send_bytes(handler, status, payload, ctype, extra)
    except (BrokenPipeError, ConnectionError, ConnectionResetError, ConnectionAbortedError):
        err = "client_disconnected"
        logger.warning(f"[Grok网关] 客户端断开 {method} {path} {who}")
    except Exception as exc:
        code = curl_error_code(exc)
        proxy = str(config.PROXY or "").strip()
        proxy_ready = proxy_endpoint_ready(proxy, timeout=0.2) if proxy else True
        err = (
            f"{type(exc).__name__} curl={code or 'unknown'} "
            f"proxy={'ready' if proxy_ready else 'down'} bytes_out={bytes_out}: {exc}"
        )
        logger.error(f"[Grok网关] 转发失败 {method} {path} {who} {err}")
        if not response_started and not handler.wfile.closed:
            try:
                _send_bytes(
                    handler,
                    502,
                    _openai_error(err, 502),
                    "application/json; charset=utf-8",
                )
            except Exception:
                pass
        status = 502
    finally:
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        ms = int((time.monotonic() - t0) * 1000)
        _record(
            method=method,
            path=path,
            model=model,
            stream=stream_flag,
            status=status,
            ms=ms,
            bytes_in=len(body),
            bytes_out=bytes_out,
            error=err,
            account_id=account_id,
            account=who,
            account_email=email or None,
            usage=usage,
            ip=client_ip,
            client_ua=client_ua,
        )
        if err != "client_disconnected":
            logger.info(
                f"[Grok网关] {method} {path} {who} model={model or '-'} "
                f"status={status} {ms}ms in={len(body)} out={bytes_out}"
            )
