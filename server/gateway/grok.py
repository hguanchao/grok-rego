"""
Grok 号池网关。

把本机 `/grok/v1/*` 转发到 `https://cli-chat-proxy.grok.com/v1`，
用号池账号的 access_token 鉴权。每次请求自动从号池取号
（ACTIVE + 已认证 + token 未过期），轮询均匀分摊；
上游判定为坏号的账号（401/403/404/429 等）进入临时冷却，避免反复命中。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests

from core import config
from core.config import UPSTREAM_BASE
from core.logger import logger
from core.util import (
    curl_error_code,
    decode_jwt_exp,
    now_iso_tz,
    proxy_endpoint_ready,
)
from db import get_account_by_id, insert_usage, list_gateway_candidates
from gateway import egress
from gateway.usage import StreamUsageAccumulator, extract_nonstream, output_tps

GROK_BASE = UPSTREAM_BASE.rstrip("/")
_CLIENT_VERSION = "0.2.120"  # 与 Grok CLI chat-proxy 期望的客户端版本保持同步（对齐 CLIProxyAPI 维护值）
_GROK_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": _CLIENT_VERSION,
    "x-grok-client-identifier": "grok-shell",
    "x-authenticateresponse": "authenticate-response",
    "User-Agent": f"xai-grok-workspace/{_CLIENT_VERSION}",
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
_rr_index = 0  # 自动选号轮询游标（跨请求共享，均匀分摊流量）

# ── 账号临时冷却（对齐 CLIProxyAPI 凭据冷却链路）────────────────────
# 上游按状态码判定坏号后临时冻结，避免轮询反复命中坏号；到期自动解冻。
_AUTH_COOLDOWN_SECONDS = 600     # token 无效 / 权限被拒
_CHANNEL_COOLDOWN_SECONDS = 120  # 通道失效 / 风控拦截
_RATE_COOLDOWN_SECONDS = 60      # 限流 / 配额
_COOLDOWN_BY_STATUS = {
    401: _AUTH_COOLDOWN_SECONDS,
    403: _AUTH_COOLDOWN_SECONDS,
    402: _RATE_COOLDOWN_SECONDS,
    404: _CHANNEL_COOLDOWN_SECONDS,
    429: _RATE_COOLDOWN_SECONDS,
}
_cooldowns: dict[int, float] = {}  # account_id → 冷却解冻的 monotonic 时间

# ── 粘性会话（对齐 CLIProxyAPI SessionAffinity）─────────────────
# 会话标识 → (账号 id, 绑定时刻)。TTL 内同一会话固定同一账号，绑定失效自动故障切换。
_SESSION_TTL_SEC = 3600  # 会话绑定保留时长（对齐 CLIProxyAPI SessionAffinityTTL 默认 1h）
_session_bindings: dict[str, tuple[int, float]] = {}
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
    effort: str | None = None,
    account_id: int | None = None,
    account: str = "",
    account_email: str | None = None,
    usage: dict[str, int] | None = None,
    ip: str | None = None,
    client_ua: str | None = None,
    first_ms: int = 0,
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
            effort=effort,
            stream=stream,
            account_id=account_id,
            account_email=account_email or (account if account and "@" in account else None),
            status=1 if 200 <= status < 400 else 0,
            reason=error or None,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cache_tokens=usage.get("cache_tokens", 0),
            reasoning_tokens=usage.get("reasoning_tokens", 0),
            output_tps=output_tps(
                usage.get("completion_tokens", 0),
                ms,
                first_ms if stream else 0,
            ),
        )
    except Exception:
        # 落库失败绝不阻塞网关转发
        logger.exception("[Grok网关] 用量落库失败")


def snapshot() -> dict[str, Any]:
    """管理面快照：接入信息 + 计数 + 最近请求（含冷却/粘性会话数）。"""
    now_mono = time.monotonic()
    with _lock:
        stats = dict(_stats)
        logs = list(_logs)
        cooled = sum(1 for until in _cooldowns.values() if until > now_mono)
        # 清理过期会话绑定，避免内存无界增长
        sticky_expired = [
            k for k, (_, at) in _session_bindings.items()
            if at + _SESSION_TTL_SEC < now_mono
        ]
        for k in sticky_expired:
            del _session_bindings[k]
        sticky = len(_session_bindings)
    return {
        "upstream": GROK_BASE,
        "client_base": f"http://{config.API_HOST}:{config.API_PORT}/grok/v1",
        "base_path": "/grok/v1",
        "select": "自动轮询取号（ACTIVE + 已认证）",
        "proxy": str(config.PROXY or "").strip(),
        "stats": stats,
        "logs": logs,
        "cooled_accounts": cooled,
        "sticky_sessions": sticky,
    }


def _upstream_url(path: str, query: str) -> str:
    prefix = "/grok/v1"
    suffix = path[len(prefix):] if path.startswith(prefix) else path
    url = GROK_BASE + (suffix or "")
    if query:
        url = f"{url}?{query}"
    return url


# ── 模型列表：本地清单文件（与 zen-models.json 同构的扁平 dict），由上游 /models 快照固化 ──
_MODELS_JSON = Path(__file__).resolve().parent / "grok-models.json"

_MODELS_ENTRY_CACHE: tuple[float, int, list[dict[str, Any]]] | None = None


def _model_entry(model_id: str, created: int) -> dict[str, Any]:
    """构造单个 OpenAI models 列表项（与 zen 网关同构）。"""
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": "grok",
        "display_name": model_id,
    }


def _local_model_entries() -> list[dict[str, Any]]:
    """从 grok-models.json（扁平 dict）读取模型清单，转成 OpenAI models 列表项。

    按 (mtime, size) 缓存：文件热更新后自动失效，无需重启服务；
    文件缺失或损坏时回退到内置 grok-4.6，保证端点可用。
    """
    global _MODELS_ENTRY_CACHE
    try:
        stat = _MODELS_JSON.stat()
        if _MODELS_ENTRY_CACHE is not None and _MODELS_ENTRY_CACHE[:2] == (stat.st_mtime, stat.st_size):
            return _MODELS_ENTRY_CACHE[2]
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"grok-4.6": "grok-4.6"}
    except OSError:
        return [_model_entry("grok-4.6", int(time.time()))]
    if not isinstance(data, dict):
        data = {"grok-4.6": "grok-4.6"}
    created = int(stat.st_mtime)
    items = [_model_entry(str(mid).strip(), created) for mid in data if str(mid).strip()]
    _MODELS_ENTRY_CACHE = (stat.st_mtime, stat.st_size, items)
    return items


def _models_dict() -> dict[str, str]:
    """读 grok-models.json 扁平映射表（key=cli 模型 id，value=实际上游模型 id）。"""
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _map_body_model(body: bytes) -> bytes:
    """请求体 model 查表透传：cli 传 key → grok-models.json 命中则替换为 value，未命中原样。"""
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    ident = payload["model"].strip()
    mapping = _models_dict()
    mapped = mapping.get(ident)
    if mapped is None or str(mapped) == ident:
        return body
    payload["model"] = str(mapped)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


_SESSION_BODY_KEYS = ("conversation_id", "session_id", "previous_response_id", "prompt_cache_key")
_SESSION_HEADERS = (
    "x-grok-conv-id",
    "x-session-id",
    "session-id",
    "session_id",
    "x-client-request-id",
    "x-conversation-id",
)


def _session_key(handler: BaseHTTPRequestHandler, body: bytes) -> str:
    """解析粘性会话标识（空串=无会话，选号退化为纯轮询）。

    对齐 CLIProxyAPI SessionAffinity 的优先级：
    显式会话头 → 请求体会话 ID → 首条用户消息内容哈希兜底。
    """
    for name in _SESSION_HEADERS:
        value = (handler.headers.get(name) or "").strip()
        if value and value.lower() not in {"null", "undefined", "none"}:
            return f"h:{name}:{value[:128]}"
    if not body:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in _SESSION_BODY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"b:{key}:{value.strip()[:128]}"
    conversation = payload.get("conversation")
    if isinstance(conversation, dict):
        value = conversation.get("id")
        if isinstance(value, str) and value.strip():
            return f"b:conversation.id:{value.strip()[:128]}"
    if isinstance(conversation, str) and conversation.strip():
        return f"b:conversation:{conversation.strip()[:128]}"
    digest = _first_user_hash(payload)
    if digest:
        return f"c:{digest}"
    return ""


def _first_user_hash(payload: dict[str, Any]) -> str:
    """兜底：首条用户消息内容前 512 字符的稳定哈希（新对话内容不同 → 新会话，不串号）。"""
    for key in ("messages", "input"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in (None, "user"):
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                text = content.strip()
                return hashlib.sha1(text[:512].encode("utf-8", "replace")).hexdigest()[:16]
            if isinstance(content, list):
                parts = [
                    p.get("text")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                ]
                if parts:
                    text = "".join(parts)
                    return hashlib.sha1(text[:512].encode("utf-8", "replace")).hexdigest()[:16]
    return ""


def _extract_meta(body: bytes) -> tuple[str | None, bool, str | None]:
    """解析请求体元信息：(model, stream, reasoning_effort)。"""
    if not body:
        return None, False, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, False, None
    if not isinstance(payload, dict):
        return None, False, None
    model = payload.get("model")
    model_s = str(model).strip() if isinstance(model, str) else None
    # 推理档位：OpenAI 风格 reasoning_effort 字符串，或 Responses 风格 reasoning.effort
    effort: str | None = None
    raw_effort = payload.get("reasoning_effort")
    if isinstance(raw_effort, str) and raw_effort.strip():
        effort = raw_effort.strip()
    else:
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            nested = reasoning.get("effort")
            if isinstance(nested, str) and nested.strip():
                effort = nested.strip()
    return model_s or None, bool(payload.get("stream")), effort


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > _MAX_BODY:
        raise ValueError(f"请求体过大（>{_MAX_BODY} bytes）")
    return handler.rfile.read(length)


def _pick_account(session_key: str = "") -> tuple[dict[str, Any] | None, str]:
    """从号池候选（ACTIVE + 已认证 + 未降智）按「粘性会话 + 轮询」挑选 token 未过期、未冷却的账号。

    - 过滤 JWT 已过期的账号（exp 解析失败的视为有效，交由上游判定）
    - 过滤冷却中的账号（上游判定坏号后临时冻结，到期自动解冻）
    - 过滤 dumbed=1（推理巡检判定降智，候选查询已排除）
    - 粘性会话（对齐 CLIProxyAPI SessionAffinity）：会话标识非空时，
      绑定账号在 TTL 内复用；绑定账号冷却 / 禁用 / 删除 / token 过期
      则自动解绑并故障切换到新号后重建绑定；无会话标识时退化为纯轮询
    - 轮询游标全局递增，多账号时均匀分摊请求
    """
    global _rr_index
    now = int(time.time())
    now_mono = time.monotonic()
    candidates = [
        row
        for row in list_gateway_candidates()
        if (exp := decode_jwt_exp(row.get("access_token"))) is None or exp > now
    ]
    if not candidates:
        return None, "号池无可用账号（需 ACTIVE、已认证且未降智）"
    with _lock:
        # 清理已到期的冷却记录，避免内存无界增长
        expired_ids = [acc_id for acc_id, until in _cooldowns.items() if until <= now_mono]
        for acc_id in expired_ids:
            del _cooldowns[acc_id]
        ready = [row for row in candidates if _cooldowns.get(int(row["id"])) is None]
        if not ready:
            return None, "号池账号全部冷却中（请稍后重试）"
        if session_key:
            binding = _session_bindings.get(session_key)
            if binding is not None:
                bound_id, bound_at = binding
                if bound_at + _SESSION_TTL_SEC < now_mono:
                    # 绑定过期：解绑后按轮询挑新号重建
                    del _session_bindings[session_key]
                else:
                    matched = next((r for r in ready if int(r["id"]) == bound_id), None)
                    if matched is not None:
                        return matched, ""  # 命中绑定：同一会话复用同一账号
                    # 绑定失效（账号冷却 / 禁用 / 删除 / token 过期）→ 解绑并故障切换
                    del _session_bindings[session_key]
            idx = _rr_index % len(ready)
            _rr_index = _rr_index + 1
            picked = ready[idx]
            _session_bindings[session_key] = (int(picked["id"]), now_mono)
            return picked, ""
        idx = _rr_index % len(ready)
        _rr_index = _rr_index + 1
    return ready[idx], ""


def _cooldown_account(account_id: int, seconds: float) -> None:
    """把账号临时冷却 seconds 秒；并发安全，重复写入覆盖为最新。"""
    with _lock:
        _cooldowns[int(account_id)] = time.monotonic() + seconds


def cooldown_until_map() -> dict[int, float]:
    """当前冷却中账号 id → 解冻的 monotonic 时刻（供运维页逐账号标记冷却）。"""
    with _lock:
        return dict(_cooldowns)


def sticky_bound_account_ids() -> set[int]:
    """当前活跃粘性会话绑定的账号 id 集合（顺带清理过期绑定，供运维页逐账号标记粘性）。"""
    now_mono = time.monotonic()
    with _lock:
        expired = [
            k for k, (_, at) in _session_bindings.items()
            if at + _SESSION_TTL_SEC < now_mono
        ]
        for k in expired:
            del _session_bindings[k]
        return {acc_id for acc_id, _ in _session_bindings.values()}


def _forward_headers(incoming: Any, token: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in incoming.items():
        low = key.lower()
        if low in _HOP_BY_HOP or low in {
            "authorization",
            "x-account-id",
            "x-xai-token-auth",
            "x-grok-client-version",
            "x-grok-client-identifier",
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
        # 强制 HTTP/1.1：规避 HTTP/2 帧层兼容问题（curl 92 PROTOCOL_ERROR reset）
        "http_version": "v1",
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
    """把当前请求用号池账号 token 转发到 Grok 上游（自动取号）。"""
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

    # 模型映射：cli 传 model → grok-models.json 查 value 透传上游
    body = _map_body_model(body)

    # ── 模型列表：读本地 grok-models.json（扁平 dict）转 OpenAI 列表，不请求上游 ──
    if method in ("GET", "HEAD") and path.endswith("/models"):
        payload = json.dumps(
            {"object": "list", "data": _local_model_entries()},
            ensure_ascii=False,
        ).encode("utf-8")
        t0 = time.monotonic()
        _send_bytes(handler, 200, payload, "application/json; charset=utf-8")
        logger.info(f"[Grok网关] GET {path} model=- status=200 {int((time.monotonic() - t0) * 1000)}ms in=0 out={len(payload)}")
        return

    acc, acc_err = _pick_account(_session_key(handler, body))
    if acc is None:
        _send_bytes(
            handler,
            400,
            _openai_error(acc_err, 400),
            "application/json; charset=utf-8",
        )
        return

    model, stream_flag, effort_flag = _extract_meta(body)
    url = _upstream_url(path, parsed.query)
    token = str(acc.get("access_token") or "")
    headers = _forward_headers(handler.headers, token)
    t0 = time.monotonic()
    t_first: float | None = None
    bytes_out = 0
    status = 502
    err: str | None = None
    upstream: requests.Response | None = None
    response_started = False
    email = str(acc.get("email") or "")
    account_id = int(acc["id"])
    who = email
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
                            t_first = time.monotonic()
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
        # 上游已成功(status<400)时属客户端提前断开（流式 CLI 读完即关），非故障；仅上游失败才报 WARNING
        if status < 400:
            logger.info(
                f"[Grok网关] 客户端提前断开（上游已成功 HTTP {status}）{method} {path} {who}"
            )
        else:
            logger.warning(f"[Grok网关] 客户端断开 {method} {path} {who} HTTP {status}")
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
        first_ms = int((t_first - t0) * 1000) if t_first is not None else 0
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
            effort=effort_flag,
            account_id=account_id,
            account=who,
            account_email=email or None,
            usage=usage,
            ip=client_ip,
            client_ua=client_ua,
            first_ms=first_ms,
        )
        # 上游明确判定的坏号进入临时冷却（网络异常 / 客户端断开不归咎账号）
        if (
            err is None
            and status >= 400
            and (cool_secs := _COOLDOWN_BY_STATUS.get(status)) is not None
        ):
            _cooldown_account(account_id, cool_secs)
            logger.warning(
                f"[Grok网关] 账号冷却 {who} status={status} 冻结 {cool_secs}s"
            )
        if err != "client_disconnected":
            logger.info(
                f"[Grok网关] {method} {path} {who} model={model or '-'} "
                f"status={status} {ms}ms in={len(body)} out={bytes_out}"
            )
