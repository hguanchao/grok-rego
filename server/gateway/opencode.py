"""
OpenCode Zen 网关。

把本机 `/zen/v1/*` 透明转发到 `https://opencode.ai/zen/v1`，
固定上游鉴权与请求头，兼容 OpenAI Chat Completions / Responses / Anthropic Messages，
支持 SSE 流式。`GET /zen/v1/models` 固定返回本地 zen-models.json 清单，不请求上游。
管理面只读统计与最近请求，不落库。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests

from core import config
from core.logger import logger
from core.util import curl_error_code, now_iso_tz, proxy_endpoint_ready
from db import insert_usage
from gateway import egress
from gateway.paths import ensure_local_v1, resource_path, upstream_url
from gateway.anthropic import (
    chat_to_message,
    claude_code_model_entries,
    count_tokens,
    is_count_tokens_path,
    is_messages_path,
    iter_anthropic_sse,
    iter_responses_sse,
    messages_to_chat,
    messages_to_responses,
    responses_to_message,
    rewrite_body_model,
    rewrite_model_id,
    uses_chat_completions,
    uses_responses,
)
from gateway.usage import StreamUsageAccumulator, extract_nonstream

# 上游固定参数（产品约定，不走配置）
ZEN_BASE = "https://opencode.ai/zen/v1"
ZEN_KEY = "public"
ZEN_HEADERS = {
    "Authorization": f"Bearer {ZEN_KEY}",
    "User-Agent": "opencode/1.18.30",
    "HTTP-Referer": "https://opencode.ai/",
    "X-Title": "opencode",
    "X-Opencode-Session": "cliproxy-opencode-go-session",
}

_MAX_BODY = 32 * 1024 * 1024
_LOG_CAP = 200
_CONNECT_TIMEOUT = 20.0
_READ_TIMEOUT = 300.0
_UPSTREAM_RETRY_CODES = {5, 6, 7, 18, 28, 35, 52, 56, 92}
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
# 本地免费模型清单（key=value 均为模型 id），由上游 /models 快照固化
_MODELS_JSON = Path(__file__).resolve().parent / "zen-models.json"
_models_cache: tuple[float, int, list[dict[str, Any]]] | None = None
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
    usage: dict[str, int] | None = None,
    effort: str | None = None,
    ip: str | None = None,
    client_ua: str | None = None,
) -> None:
    """写入环形日志、累加计数，并落库一条用量记录。"""
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
    try:
        insert_usage(
            ip=ip,
            client_ua=client_ua,
            endpoint=path.split("?", 1)[0],
            model=model or "",
            stream=stream,
            status=1 if 200 <= status < 400 else 0,
            reason=error or None,
            effort=effort,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cache_tokens=usage.get("cache_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
        )
    except Exception:
        # 落库失败绝不阻塞网关转发
        logger.exception("[网关] 用量落库失败")


def snapshot() -> dict[str, Any]:
    """管理面快照：接入信息 + 计数 + 最近请求。"""
    with _lock:
        stats = dict(_stats)
        logs = list(_logs)
    return {
        "upstream": ZEN_BASE,
        "client_base": f"http://{config.API_HOST}:{config.API_PORT}/zen/v1",
        "base_path": "/zen/v1",
        "key": ZEN_KEY,
        "headers": dict(ZEN_HEADERS),
        "proxy": str(config.PROXY or "").strip(),
        "stats": stats,
        "logs": logs,
    }


def _model_entry(model_id: str, created: int) -> dict[str, Any]:
    """构造单个 OpenAI models 列表项。"""
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": "opencode",
        "display_name": model_id,
    }


def local_model_entries() -> list[dict[str, Any]]:
    """从 zen-models.json 读取免费模型清单，转成 OpenAI models 列表项。

    按 (mtime, size) 缓存：文件热更新后自动失效，无需重启服务；
    文件缺失或损坏时回退到内置 stealth 免费模型，保证端点可用。
    """
    global _models_cache
    try:
        stat = _MODELS_JSON.stat()
        if _models_cache is not None and _models_cache[:2] == (stat.st_mtime, stat.st_size):
            return _models_cache[2]
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"big-pickle": "big-pickle"}
    except OSError:
        return [_model_entry("big-pickle", int(time.time()))]
    if not isinstance(data, dict):
        data = {"big-pickle": "big-pickle"}
    created = int(stat.st_mtime)
    items = [
        _model_entry(str(mid).strip(), created)
        for mid in data
        if str(mid).strip()
    ]
    _models_cache = (stat.st_mtime, stat.st_size, items)
    return items


def _zen_models_dict() -> dict[str, str]:
    """读 zen-models.json 扁平映射表（key=cli 模型 id，value=实际上游模型 id）。"""
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _map_body_model(body: bytes) -> bytes:
    """请求体 model 查表透传：cli 传 key → zen-models.json 命中则替换为 value，未命中原样。"""
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    ident = payload["model"].strip()
    mapping = _zen_models_dict()
    mapped = mapping.get(ident)
    if mapped is None or str(mapped) == ident:
        return body
    payload["model"] = str(mapped)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _is_free_model(model_id: str) -> bool:
    """免费模型：id 以 -free 结尾，或 stealth 免费模型 big-pickle。"""
    ident = (model_id or "").strip().lower()
    return ident.endswith("-free") or ident == "big-pickle"


def _filter_free_models(raw: bytes) -> bytes:
    """OpenAI /v1/models 列表只保留免费模型。解析失败原样返回。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    items = payload.get("data")
    if not isinstance(items, list):
        return raw
    filtered: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        if not _is_free_model(model_id):
            continue
        if not item.get("display_name"):
            item = {**item, "display_name": model_id}
        filtered.append(item)
    seen = {str(item.get("id") or "") for item in filtered}
    for alias in claude_code_model_entries():
        if alias["id"] not in seen:
            filtered.append(alias)
            seen.add(alias["id"])
    payload["data"] = filtered
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _upstream_url(path: str, query: str) -> str:
    """拼 Zen 上游：base 已含 /v1，资源段再剥一层 /v1，避免 /v1/v1。"""
    return upstream_url(ZEN_BASE, path, "/zen", query)


def _chat_to_responses(src: dict[str, Any]) -> dict[str, Any]:
    """Chat Completions 体 → Responses 体（Muse 官方端点）。"""
    out: dict[str, Any] = {
        "model": src.get("model"),
        "input": src.get("messages") or src.get("input") or "",
        "stream": bool(src.get("stream")),
    }
    if src.get("max_tokens") is not None:
        out["max_output_tokens"] = src.get("max_tokens")
    if src.get("max_output_tokens") is not None:
        out["max_output_tokens"] = src.get("max_output_tokens")
    if src.get("temperature") is not None:
        out["temperature"] = src.get("temperature")
    if src.get("top_p") is not None:
        out["top_p"] = src.get("top_p")
    if src.get("tools") is not None:
        out["tools"] = src.get("tools")
    if src.get("tool_choice") is not None:
        out["tool_choice"] = src.get("tool_choice")
    reasoning = src.get("reasoning")
    if isinstance(reasoning, dict):
        out["reasoning"] = reasoning
    elif isinstance(src.get("reasoning_effort"), str) and src["reasoning_effort"].strip():
        out["reasoning"] = {"effort": src["reasoning_effort"].strip(), "summary": "concise"}
    return out


def _extract_meta(body: bytes) -> tuple[str | None, bool]:
    """从 JSON 体取出 model / stream，解析失败则忽略。"""
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


def _forward_headers(incoming: Any) -> dict[str, str]:
    """透传安全头，强制覆盖上游固定头。"""
    out: dict[str, str] = {}
    for key, value in incoming.items():
        low = key.lower()
        if low in _HOP_BY_HOP or low in {
            "authorization",
            "http-referer",
            "referer",
            "user-agent",
            "x-title",
            "x-opencode-session",
        }:
            continue
        out[key] = value
    out.update(ZEN_HEADERS)
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
        "Content-Type, Authorization, X-Api-Key, HTTP-Referer, X-Title, OpenAI-Beta, "
        "anthropic-version, anthropic-beta, x-api-key",
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


def _anthropic_error(message: str, status: int = 502) -> bytes:
    kind = "authentication_error" if status in (401, 403) else "api_error"
    return json.dumps(
        {"type": "error", "error": {"type": kind, "message": message}},
        ensure_ascii=False,
    ).encode("utf-8")


def _wrap_upstream_error(raw: bytes, anthropic: bool, status: int = 400) -> bytes:
    if not anthropic:
        return raw
    text = (raw or b"").decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error(text[:400] or "upstream error", status)
    if isinstance(obj, dict) and obj.get("type") == "error":
        return raw
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        # 保留上游真实错误语义（如 MissingSessionID），仅换 Anthropic 错误包络
        etype = str(err.get("type") or "").strip()
        msg = str(err.get("message") or err.get("type") or "upstream error")
        return _anthropic_error(f"{etype}: {msg}" if etype else msg, status)
    if isinstance(err, str) and err:
        return _anthropic_error(err, status)
    # 标准 error 结构缺失时透传原文片段，避免 CLI 只看到无信息的 "upstream error"
    return _anthropic_error(text.strip()[:400] or "upstream error", status)


def _proxy_kwargs() -> dict[str, Any]:
    proxy = str(config.PROXY or "").strip()
    kwargs: dict[str, Any] = {
        "timeout": (_CONNECT_TIMEOUT, _READ_TIMEOUT),
        "allow_redirects": False,
        "verify": True,
        # 强制 HTTP/1.1：规避 HTTP/2 帧层兼容问题（curl 92 PROTOCOL_ERROR reset）
        "http_version": "v1",
    }
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def probe() -> dict[str, Any]:
    """探测上游 GET /models，供管理面健康检查。"""
    t0 = time.monotonic()
    url = f"{ZEN_BASE}/models"
    try:
        resp = requests.get(url, headers=dict(ZEN_HEADERS), **_proxy_kwargs())
        elapsed = int((time.monotonic() - t0) * 1000)
        text = (resp.text or "")[:400]
        ok = 200 <= resp.status_code < 300
        count = 0
        if ok:
            try:
                data = json.loads(_filter_free_models(resp.content or b"{}"))
                items = data.get("data") if isinstance(data, dict) else None
                if isinstance(items, list):
                    count = len(items)
            except Exception:
                count = 0
        return {
            "ok": ok,
            "status": resp.status_code,
            "ms": elapsed,
            "models": count,
            "error": "" if ok else text,
        }
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status": 0,
            "ms": elapsed,
            "models": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _client_api_key(handler: BaseHTTPRequestHandler) -> str:
    """提取客户端携带的密钥：Authorization: Bearer <key> 或 x-api-key。"""
    auth = str(handler.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(handler.headers.get("x-api-key") or "").strip()


def _reject_unauthorized(handler: BaseHTTPRequestHandler, path: str) -> None:
    """按客户端协议风格返回 401（Anthropic Messages 走 error 包络）。"""
    anthropic = is_messages_path(path)
    if anthropic:
        payload = _anthropic_error("invalid api key", 401)
    else:
        payload = _openai_error("invalid api key", 401)
    _send_bytes(
        handler,
        401,
        payload,
        "application/json; charset=utf-8",
    )


def proxy(handler: BaseHTTPRequestHandler, method: str, path: str) -> None:
    """把当前 HTTP 请求转发到 Zen，流式响应逐块写出。"""
    parsed = urlparse(handler.path)
    query = parsed.query
    path = ensure_local_v1(path, "/zen")

    # 鉴权：配置了 gateway_api_key 时，除预检外的所有请求必须携带匹配密钥
    if config.GATEWAY_API_KEY and method != "OPTIONS":
        presented = _client_api_key(handler)
        if not secrets.compare_digest(presented, config.GATEWAY_API_KEY):
            _reject_unauthorized(handler, path)
            return

    # GET /zen/v1/models：固定返回本地清单（免费模型 + Claude Code 别名），不请求上游
    if method in ("GET", "HEAD") and resource_path(path, "/zen") == "/models":
        payload = json.dumps(
            {
                "object": "list",
                "data": [*local_model_entries(), *claude_code_model_entries()],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        t0 = time.monotonic()
        _send_bytes(handler, 200, payload, "application/json; charset=utf-8")
        _record(
            method=method,
            path=path,
            model=None,
            stream=False,
            status=200,
            ms=int((time.monotonic() - t0) * 1000),
            bytes_in=0,
            bytes_out=len(payload),
            error=None,
            ip=egress.current_ip(),
            client_ua=str(handler.headers.get("User-Agent") or "")[:255],
        )
        return

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

    body = _map_body_model(body)  # zen-models.json 查表映射：cli 传 key → value 透传上游
    body = rewrite_body_model(body)  # 未命中时 Claude Code 档位/别名改写到免费模型兜底
    model, stream_flag = _extract_meta(body)
    model = rewrite_model_id(model)
    anthropic_client = is_messages_path(path)
    translate = anthropic_client and uses_chat_completions(model)
    use_responses = uses_responses(model)  # muse 等仅支持 /responses，单独走 responses 路由
    # 推理档位：翻译路径取转换后 chat 体，直连路径读原始 reasoning_effort
    effort_flag: str | None = None

    if method in ("POST", "GET") and is_count_tokens_path(path):
        payload = count_tokens(body)
        _send_bytes(handler, 200, payload, "application/json; charset=utf-8")
        _record(
            method=method,
            path=path,
            model=model,
            stream=False,
            status=200,
            ms=0,
            bytes_in=len(body),
            bytes_out=len(payload),
            error=None,
            ip=egress.current_ip(),
            client_ua=str(handler.headers.get("User-Agent") or "")[:255],
        )
        return

    forward_path = path
    forward_body = body
    if use_responses and not translate and path.rstrip("/").endswith("/chat/completions"):
        # @ai-sdk/openai-compatible 会打 /chat/completions；Muse 官方只认 /responses
        try:
            src = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            src = {}
        if isinstance(src, dict):
            resp = _chat_to_responses(src)
            stream_flag = bool(resp.get("stream"))
            forward_body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            rr = resp.get("reasoning") if isinstance(resp.get("reasoning"), dict) else None
            if isinstance(rr, dict) and isinstance(rr.get("effort"), str):
                effort_flag = rr["effort"]
        forward_path = "/zen/v1/responses"
        query = ""
    if translate:
        try:
            src = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            src = {}
        if not isinstance(src, dict):
            src = {}
        if use_responses:
            # muse 等仅支持 /responses：把 Anthropic Messages 译成 Responses 请求体
            resp = messages_to_responses(src)
            stream_flag = bool(resp.get("stream"))
            forward_body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            forward_path = "/zen/v1/responses"
            query = ""
            effort_flag = None
            rr = resp.get("reasoning") if isinstance(resp.get("reasoning"), dict) else None
            if isinstance(rr, dict) and isinstance(rr.get("effort"), str):
                effort_flag = rr["effort"]
        else:
            chat = messages_to_chat(src)
            stream_flag = bool(chat.get("stream"))
            forward_body = json.dumps(chat, ensure_ascii=False).encode("utf-8")
            forward_path = "/zen/v1/chat/completions"
            query = ""
            effort_flag = (
                str(chat["reasoning_effort"]) if "reasoning_effort" in chat else None
            )
    else:
        try:
            raw_src = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raw_src = {}
        if isinstance(raw_src, dict) and isinstance(raw_src.get("reasoning_effort"), str):
            effort_flag = raw_src["reasoning_effort"] or None

    url = _upstream_url(forward_path, query)
    headers = _forward_headers(handler.headers)
    t0 = time.monotonic()
    bytes_out = 0
    status = 502
    err: str | None = None
    upstream: requests.Response | None = None
    response_started = False
    # 用量采集：非流式整包解析 / 流式旁路累积
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    acc: StreamUsageAccumulator | None = None
    client_ua = str(handler.headers.get("User-Agent") or "")[:255]

    try:
        # 仅客户端声明 stream 时上游才开流；否则整包读取，避免 curl_cffi
        # stream=True 下 .content 为空、Content-Length=0 把 keep-alive 打乱。
        upstream_method = method if not translate else "POST"
        is_stream = False
        raw_chunks = None
        first_raw_chunk = b""
        for request_attempt in range(2):
            try:
                upstream = requests.request(
                    upstream_method,
                    url,
                    headers=headers,
                    data=forward_body if forward_body else None,
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
                    f"[网关] 上游请求异常 curl={code or 'unknown'} "
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
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "close")
            handler.send_header("X-Accel-Buffering", "no")
            _send_cors(handler)
            for key, value in extra:
                if key.lower() in {"content-type", "cache-control", "connection"}:
                    continue
                handler.send_header(key, value)
            handler.end_headers()
            response_started = True
            if method != "HEAD":
                def _with_first_chunk():
                    if first_raw_chunk:
                        yield first_raw_chunk
                    if raw_chunks is not None:
                        yield from raw_chunks

                acc = StreamUsageAccumulator()
                if translate:
                    # 翻译模式下必须从「上游原始 OpenAI 块」采样 usage——
                    # 转换后的 Anthropic SSE 只带 output_tokens，会丢 prompt/cache/reasoning
                    def _tee(gen):
                        for c in gen:
                            if c:
                                acc.feed(c)
                            yield c

                    if use_responses:
                        chunks = iter_responses_sse(_tee(_with_first_chunk()), model)
                    else:
                        chunks = iter_anthropic_sse(_tee(_with_first_chunk()), model)

                    def _pass(gen):
                        # 翻译后的块不再重复喂 acc，仅转发
                        for c in gen:
                            if c:
                                yield c

                    chunks_iter = _pass(chunks)
                else:
                    chunks_iter = _with_first_chunk()
                for chunk in chunks_iter:
                    if not chunk:
                        continue
                    if not translate:
                        # 直连 OpenAI 兼容客户端：旁路采样原始块
                        acc.feed(chunk)
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    bytes_out += len(chunk)
                usage = acc.result()
        else:
            payload = upstream.content or b""
            if status < 400 and resource_path(path, "/zen") == "/models":
                payload = _filter_free_models(payload)
            elif status < 400 and translate:
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    obj = {}
                if isinstance(obj, dict):
                    if use_responses:
                        # Responses 整包响应 → Anthropic Messages
                        payload = json.dumps(
                            responses_to_message(obj, model), ensure_ascii=False
                        ).encode("utf-8")
                    else:
                        payload = json.dumps(
                            chat_to_message(obj, model), ensure_ascii=False
                        ).encode("utf-8")
            elif status >= 400 and anthropic_client:
                # 透传上游真实错误（仅转 Anthropic 错误包络，不吞错误信息）
                payload = _wrap_upstream_error(payload, True, status)
            bytes_out = len(payload)
            ctype = "application/json; charset=utf-8"
            if not translate and not anthropic_client:
                ctype = upstream.headers.get("Content-Type") or ctype
            # 非流式：对原始上游响应解析 usage
            if status < 400:
                usage = extract_nonstream(upstream.content or b"")
            _send_bytes(handler, status, payload, ctype, extra)
    except (BrokenPipeError, ConnectionError, ConnectionResetError, ConnectionAbortedError):
        err = "client_disconnected"
        # 上游已成功(status<400)时属客户端提前断开（流式 CLI 读完即关），非故障
        if status < 400:
            logger.info(f"[网关] 客户端提前断开（上游已成功 HTTP {status}）{method} {path}")
        else:
            logger.warning(f"[网关] 客户端断开 {method} {path} HTTP {status}")
    except Exception as exc:
        code = curl_error_code(exc)
        proxy = str(config.PROXY or "").strip()
        proxy_ready = proxy_endpoint_ready(proxy, timeout=0.2) if proxy else True
        err = (
            f"{type(exc).__name__} curl={code or 'unknown'} "
            f"proxy={'ready' if proxy_ready else 'down'} bytes_out={bytes_out}: {exc}"
        )
        logger.error(f"[网关] 转发失败 {method} {path} {err}")
        if not response_started and not handler.wfile.closed:
            try:
                fail = _anthropic_error(err, 502) if anthropic_client else _openai_error(err, 502)
                _send_bytes(handler, 502, fail, "application/json; charset=utf-8")
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
            usage=usage,
            effort=effort_flag,
            ip=egress.current_ip(),
            client_ua=client_ua,
        )
        if err != "client_disconnected":
            logger.info(
                f"[网关] {method} {path} model={model or '-'} "
                f"status={status} {ms}ms in={len(body)} out={bytes_out} effort={effort_flag or '-'}"
            )
