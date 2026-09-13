"""
Grok 号池网关。

把本机 `/grok/v1/*` 转发到 `https://cli-chat-proxy.grok.com/v1`，
用号池账号的 access_token 鉴权。每次请求自动从号池取号
（ACTIVE + 已认证 + token 未过期），没降智与新注册账号优先、
层内轮询均匀分摊；
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
from core import proxypool
from core.config import UPSTREAM_BASE
from core.logger import logger
from core.util import (
    curl_error_code,
    decode_jwt_exp,
    iso_after_hours,
    now_iso_tz,
)
from db import (
    STATUS_LIMITED,
    STATUS_REAUTH,
    get_account_by_id,
    insert_usage,
    list_gateway_candidates,
    mark_quality_hit,
    update_account_status_by_ids,
)
from gateway import egress
from gateway.paths import ensure_local_v1, resource_path, upstream_url
from gateway.quality import (
    DEGRADED,
    HOLD_DELIVER,
    HOLD_WITHHOLD,
    QualityScanner,
    classify_nonstream,
)
from gateway.usage import StreamUsageAccumulator, extract_nonstream

GROK_BASE = UPSTREAM_BASE.rstrip("/")
_DEFAULT_CLIENT_VERSION = "1.0.16"

# 显式关闭推理的请求不参与质量审计（无思考是请求方的选择，不是账号降智）
_QUALITY_SKIP_EFFORTS = {"none", "disabled"}
# 扣流缓冲上限：超过即放弃重试直接放行（无法完整扣留的超大响应）
_HOLD_MAX_BUFFER = 8 * 1024 * 1024
# 会话级熔断：同一请求连续 withhold 达到该次数，说明降智与账号无关
# （多为会话上下文过大触发的上游保护，即「128k status-loop drool」），
# 继续换号只会把整个号池打进冷却——提前返回明确错误，让用户压缩会话
_HOLD_MAX_CONSECUTIVE_WITHHOLDS = 6


def _retry_quality_via_proxy(
    account_id: int, used_proxy: str, swapped: set[int], who: str
) -> bool:
    """降智时先给出口 IP 记升级（观察→冷却→排除），能换代理则同号重试。

    池里没有其它可用出口、或本号已经换过代理，返回 False，调用方再给账号记 strike。
    """
    action = proxypool.mark_quality_hit(used_proxy) if used_proxy else "unchanged"
    if (
        used_proxy
        and proxypool.has_other(used_proxy)
        and account_id not in swapped
    ):
        swapped.add(account_id)
        nxt = proxypool.pick(exclude=used_proxy)
        logger.warning(
            f"[Grok网关] 降智代理 {proxypool.redact(used_proxy)} 处置={action} "
            f"→ {proxypool.redact(nxt)} 同号重试 {who}"
        )
        return True
    if used_proxy:
        logger.warning(
            f"[Grok网关] 降智代理 {proxypool.redact(used_proxy)} 处置={action}，"
            f"无其它出口或已换过，改记账号 {who}"
        )
    return False


def _client_version() -> str:
    """运维页写入的 grok_version，空则回退 grok-build crate 版本。"""
    return (config.GROK_VERSION or _DEFAULT_CLIENT_VERSION).strip() or _DEFAULT_CLIENT_VERSION


def _identity_headers() -> dict[str, str]:
    """对齐 grok-build build_proxy_headers（Authorization 由号池 token 另填）。"""
    ver = _client_version()
    return {
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": ver,
        "x-grok-client-identifier": "grok-shell",
        "x-authenticateresponse": "authenticate-response",
        "User-Agent": f"xai-grok-workspace/{ver}",
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

# ── 选号优先分层（新注册 + 没降智）─────────────────────────
# 新注册优待窗口：created_at 在该窗口内的账号视为「新注册」。
# 新号日额度完整、尚未被质量审计命中过，注册补号后优先消化新号；
# 窗口与上游额度按日重置（24h）对齐，出窗后自然回归常规轮询。
_FRESH_ACCOUNT_SEC = 24 * 3600

# ── 账号临时冷却（对齐 CLIProxyAPI 凭据冷却链路）────────────────────
# 上游按状态码判定坏号后临时冻结，避免轮询反复命中坏号；到期自动解冻。
_AUTH_COOLDOWN_SECONDS = 600     # token 无效 / 权限被拒
_CHANNEL_COOLDOWN_SECONDS = 120  # 通道失效 / 风控拦截
_RATE_COOLDOWN_SECONDS = 120     # 限流（首次 429：瞬时突发解冻即恢复，避免粘性会话立刻再打同一号）
_QUOTA_RECHECK_COOLDOWN_SECONDS = 600  # 连续第 2 次 429：复测窗口
_QUOTA_RESET_SECONDS = 24 * 3600       # 额度耗尽：上游额度按日重置，冻满 24h 再回池
_QUOTA_STREAK_WINDOW_SECONDS = 1800    # 距上次失败超过该窗口则重新从第 1 次计起
_COOLDOWN_BY_STATUS = {
    401: _AUTH_COOLDOWN_SECONDS,
    403: _AUTH_COOLDOWN_SECONDS,
    402: _QUOTA_RESET_SECONDS,
    404: _CHANNEL_COOLDOWN_SECONDS,
    429: _RATE_COOLDOWN_SECONDS,
}
# 扣流路径的账号级失败（限流/额度/凭据/上游错误）会换号重试（有上限，
# 防止系统性 429 打光号池）；400/404 等请求级错误不重试
_RETRYABLE_UPSTREAM_STATUS = {401, 402, 403, 408, 429, 500, 502, 503, 504}
_MAX_UPSTREAM_FAIL_RETRIES = 6
_cooldowns: dict[int, float] = {}  # account_id → 冷却解冻的 monotonic 时间
_quota_streaks: dict[int, tuple[int, float]] = {}  # account_id → (连续 429 次数, 上次时刻)
_auth_streaks: dict[int, tuple[int, float]] = {}  # account_id → (连续 401/403 次数, 上次时刻)

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
        "select": "自动取号（新注册/没降智优先 + 粘性会话 + 轮询）",
        "proxy": ", ".join(proxypool.redact(u) for u in proxypool.urls())
        or str(config.PROXY or "").strip(),
        "stats": stats,
        "logs": logs,
        "cooled_accounts": cooled,
        "sticky_sessions": sticky,
    }


def _upstream_url(path: str, query: str) -> str:
    """拼 Grok 上游：base 已含 /v1，资源段再剥一层 /v1，避免 /v1/v1。"""
    return upstream_url(GROK_BASE, path, "/grok", query)


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


# 粘性选号：请求头 x-grok-session-id（会话稳定）。prompt_cache_key 只给上游缓存，不参与选号
# （main turn 的 cache_key 是 conv-id，side-call 是 session-id，用体会跳号）
_SESSION_BODY_KEYS = ("conversation_id", "session_id", "previous_response_id")
_SESSION_HEADERS = (
    "x-grok-session-id",
    "x-grok-conv-id",
)
_REASONING_SUMMARY = "concise"
_DEFAULT_REASONING_EFFORT = "high"


def _session_key(handler: BaseHTTPRequestHandler, body: bytes) -> str:
    """粘性选号键：优先请求头 x-grok-session-id（grok-build 会话稳定 id）。

    无 grok 头时才用体字段 / 首条用户消息哈希（非 CLI 客户端兜底）。
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


def _header_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    value = (handler.headers.get(name) or "").strip()
    if value.lower() in {"", "null", "undefined", "none"}:
        return ""
    return value


def _stable_cache_key(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> str:
    """Responses 前缀缓存键：对齐 grok-build CreateResponse（已有 key，否则 conv-id）。"""
    existing = payload.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    for name in ("x-grok-conv-id", "x-grok-session-id", "x-conversation-id"):
        value = _header_value(handler, name)
        if value:
            return value
    for key in ("conversation_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_reasoning_model(model: Any) -> bool:
    ident = str(model or "").strip().lower()
    return ident.startswith("grok-3") or ident.startswith("grok-4")


def _ensure_prompt_cache_and_reasoning(
    body: bytes, handler: BaseHTTPRequestHandler, path: str
) -> bytes:
    """Responses 缺字段时按 grok-build 补缓存键、reasoning、store、include。"""
    if not body or "/responses" not in path:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body

    changed = False
    cache_key = _stable_cache_key(handler, payload)
    existing = payload.get("prompt_cache_key")
    if cache_key and (not isinstance(existing, str) or not existing.strip()):
        payload["prompt_cache_key"] = cache_key
        changed = True

    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
        top = payload.get("reasoning_effort")
        if isinstance(top, str) and top.strip():
            reasoning["effort"] = top.strip()
        payload["reasoning"] = reasoning
        changed = True
    if not str(reasoning.get("summary") or "").strip():
        reasoning["summary"] = _REASONING_SUMMARY
        changed = True
    effort = reasoning.get("effort")
    if not (isinstance(effort, str) and effort.strip()) and _looks_reasoning_model(
        payload.get("model")
    ):
        # grok-build default_reasoning_effort=high；不填则上游可能不推理
        reasoning["effort"] = _DEFAULT_REASONING_EFFORT
        changed = True

    # grok-build apply_response_defaults：store 默认 false（ZDR）；include 加密思考链
    if payload.get("store") is None:
        payload["store"] = False
        changed = True
    include = payload.get("include")
    if not isinstance(include, list):
        include = []
        payload["include"] = include
        changed = True
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
        changed = True
    if _patch_reasoning_text_types(payload):
        changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _patch_reasoning_text_types(payload: dict[str, Any]) -> bool:
    """对齐 grok-build：reasoning input 的 content 补 type=reasoning_text，否则上游 400。"""
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    changed = False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and "type" not in part:
                part["type"] = "reasoning_text"
                changed = True
    return changed


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


def _preferred_pool(
    rows: list[dict[str, Any]], fresh_cutoff: str
) -> list[dict[str, Any]]:
    """按「没降智 + 新注册」取最优一层候选，层内保持原有顺序交给轮询分摊。

    分层键 (clean, fresh)，值越小越优先：clean = 无降智 strike 记录
    （quality_strikes=0，出现过降智的账号即使新也不如久经审计的干净老号）；
    fresh = created_at 在优待窗口内（created_at 与 fresh_cutoff 同为
    北京 ISO 带时区格式，字符串比较即时序比较，缺失/空串视为非新号）。
    返回最优一层候选；全员同层时即原候选集，退化为纯轮询。
    """
    def tier(row: dict[str, Any]) -> tuple[int, int]:
        clean = int(row.get("quality_strikes") or 0) == 0
        fresh = str(row.get("created_at") or "") >= fresh_cutoff
        return (0 if clean else 1, 0 if fresh else 1)

    tiered = [(tier(row), row) for row in rows]
    best = min(t for t, _ in tiered)
    return [row for t, row in tiered if t == best]


def _pick_account(
    session_key: str = "", exclude_ids: set[int] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """从号池候选（ACTIVE + 已认证 + 未降智）挑选账号：「新注册/没降智优先 + 粘性会话 + 轮询」。

    - 过滤 JWT 已过期的账号（exp 解析失败的视为有效，交由上游判定）
    - 过滤冷却中的账号（上游判定坏号后临时冻结，到期自动解冻）
    - 过滤质量冷却 / 长期排除的账号（候选查询已排除）
    - exclude_ids：扣流重试时本请求已试过的账号，过滤后为空则放开限制
      （无限重试语义：全部试过时允许复选，由 strike 升级制自然收敛）
    - 优先分层：没降智（无 strike 记录）且新注册（优待窗口内）的账号最先，
      逐层退化到干净老号、有 strike 记录的账号；每层内仍按轮询均匀分摊
    - 粘性会话（对齐 CLIProxyAPI SessionAffinity）：会话标识非空时，
      绑定账号在 TTL 内复用（粘性优先于分层，绑定账号不因出优待层被换掉）；
      绑定账号冷却 / 禁用 / 删除 / token 过期则自动解绑并故障切换到新号；
      无会话标识时退化为纯轮询
    - 轮询游标全局递增，同层多账号时均匀分摊请求
    """
    global _rr_index
    now = int(time.time())
    now_mono = time.monotonic()
    candidates = [
        row
        for row in list_gateway_candidates()
        if (exp := decode_jwt_exp(row.get("access_token"))) is None or exp > now
    ]
    if exclude_ids:
        fresh = [row for row in candidates if int(row["id"]) not in exclude_ids]
        candidates = fresh or candidates
    if not candidates:
        return None, "号池无可用账号（需 ACTIVE、已认证且未降智）"
    fresh_cutoff = iso_after_hours(-_FRESH_ACCOUNT_SEC / 3600)
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
        pool = _preferred_pool(ready, fresh_cutoff)
        idx = _rr_index % len(pool)
        _rr_index = _rr_index + 1
        picked = pool[idx]
        if session_key:
            _session_bindings[session_key] = (int(picked["id"]), now_mono)
        return picked, ""


def _cooldown_account(account_id: int, seconds: float) -> None:
    """把账号临时冷却 seconds 秒；并发安全，重复写入覆盖为最新。"""
    with _lock:
        _cooldowns[int(account_id)] = time.monotonic() + seconds


def _mark_account_ok(account_id: int) -> None:
    """上游 2xx 说明该账号凭据与额度均可用，清零失败计数，避免半可用账号被落库处置。"""
    with _lock:
        _quota_streaks.pop(int(account_id), None)
        _auth_streaks.pop(int(account_id), None)


def _bump_streak(streaks: dict[int, tuple[int, float]], account_id: int) -> int:
    """连续失败计数 +1；距上次失败超过窗口视为新一轮从 1 计起。"""
    now = time.monotonic()
    with _lock:
        count, last_ts = streaks.get(int(account_id), (0, 0.0))
        count = count + 1 if now - last_ts <= _QUOTA_STREAK_WINDOW_SECONDS else 1
        streaks[int(account_id)] = (count, now)
    return count


def _apply_upstream_cooldown(account_id: int, who: str, status: int) -> None:
    """按上游状态码处置账号：内存冻结，达到阈值时落库改状态并移出选号池。

    - 429：连续命中逐级升级（120s → 10min 复测 → 24h）。额度按日重置，
      连续 429 说明额度耗尽（实测同一账号 5 小时内连续 429 十余次零成功），
      第 3 次起落库 STATUS_LIMITED，选号候选只取 ACTIVE，随即自动移出；
    - 402：上游明确的配额不足信号，直接 24h + 落库 STATUS_LIMITED；
    - 401/403：token 失效，首次仅 600s 冷却防瞬时抖动，窗口内再犯落库
      STATUS_REAUTH，由重登任务刷新/重登恢复；
    - 任一次 2xx 成功即清零计数（见 _mark_account_ok），半可用账号不受影响。
    """
    cool_secs = _COOLDOWN_BY_STATUS.get(status)
    if cool_secs is None:
        return
    note = ""
    mark_status: int | None = None
    mark_reason = ""
    if status == 429:
        count = _bump_streak(_quota_streaks, account_id)
        if count == 2:
            cool_secs = _QUOTA_RECHECK_COOLDOWN_SECONDS
            note = f"（连续第 {count} 次，复测）"
        elif count >= 3:
            cool_secs = _QUOTA_RESET_SECONDS
            mark_status = STATUS_LIMITED
            mark_reason = f"额度耗尽（连续 {count} 次 429，冻结 24h 待日额度重置）"
            note = f"（连续第 {count} 次，疑似额度耗尽，冻结至日额度重置）"
    elif status == 402:
        mark_status = STATUS_LIMITED
        mark_reason = "额度耗尽（上游 402 配额不足，冻结 24h 待日额度重置）"
        note = "（配额不足，冻结至日额度重置）"
    elif status in (401, 403):
        count = _bump_streak(_auth_streaks, account_id)
        if count >= 2:
            mark_status = STATUS_REAUTH
            mark_reason = f"token 失效（上游 {status}，待刷新或重登）"
            note = f"（连续第 {count} 次，token 失效）"
    _cooldown_account(account_id, cool_secs)
    if mark_status is not None:
        update_account_status_by_ids([int(account_id)], mark_status, mark_reason)
        logger.warning(
            f"[Grok网关] 账号冷却 {who} status={status} 冻结 {cool_secs}s{note}"
            f" → 已落库状态 {mark_status}，移出选号池"
        )
    else:
        logger.warning(f"[Grok网关] 账号冷却 {who} status={status} 冻结 {cool_secs}s{note}")


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
    out.update(_identity_headers())
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


def _proxy_kwargs(proxy: str | None = None) -> dict[str, Any]:
    url = proxypool.current() if proxy is None else proxy
    kwargs: dict[str, Any] = {
        "timeout": (_CONNECT_TIMEOUT, _READ_TIMEOUT),
        "allow_redirects": False,
        "verify": True,
        "impersonate": "chrome",
        # 强制 HTTP/1.1：规避 HTTP/2 帧层兼容问题（curl 92 PROTOCOL_ERROR reset）
        "http_version": "v1",
    }
    if url:
        kwargs["proxy"] = url
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
    headers = _identity_headers()
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
    path = ensure_local_v1(path, "/grok")
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
    body = _ensure_prompt_cache_and_reasoning(body, handler, path)

    # ── 模型列表：读本地 grok-models.json（扁平 dict）转 OpenAI 列表，不请求上游 ──
    if method in ("GET", "HEAD") and resource_path(path, "/grok") == "/models":
        payload = json.dumps(
            {"object": "list", "data": _local_model_entries()},
            ensure_ascii=False,
        ).encode("utf-8")
        t0 = time.monotonic()
        _send_bytes(handler, 200, payload, "application/json; charset=utf-8")
        logger.info(f"[Grok网关] GET {path} model=- status=200 {int((time.monotonic() - t0) * 1000)}ms in=0 out={len(payload)}")
        return

    session_key = _session_key(handler, body)
    model, stream_flag, effort_flag = _extract_meta(body)
    # 质量门控：仅推理模型、未显式关闭推理的生成请求参与扣流降智判定与换号重试
    quality_scan = (
        method == "POST"
        and _looks_reasoning_model(model)
        and str(effort_flag or "").strip().lower() not in _QUALITY_SKIP_EFFORTS
    )
    url = _upstream_url(path, parsed.query)
    client_ip = egress.current_ip()  # 出口 IP（经代理探测，带缓存）
    client_ua = str(handler.headers.get("User-Agent") or "")[:255]

    if quality_scan:
        return _proxy_hold(
            handler, method, path, body, model, effort_flag, session_key,
            url, client_ip, client_ua,
        )
    return _proxy_direct(
        handler, method, path, body, model, stream_flag, effort_flag, session_key,
        url, client_ip, client_ua,
    )


def _proxy_direct(
    handler: BaseHTTPRequestHandler,
    method: str,
    path: str,
    body: bytes,
    model: str | None,
    stream_flag: bool,
    effort_flag: str | None,
    session_key: str,
    url: str,
    client_ip: str,
    client_ua: str,
) -> None:
    """直通转发（不参与扣流判定）：单次选号 → 转发 → 记账。"""
    acc, acc_err = _pick_account(session_key)
    if acc is None:
        _send_bytes(
            handler,
            400,
            _openai_error(acc_err, 400),
            "application/json; charset=utf-8",
        )
        return

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
    who = email
    # 用量采集：非流式整包解析 / 流式旁路累积
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    acc_us: StreamUsageAccumulator | None = None

    used_proxy = proxypool.pick()
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
                    **_proxy_kwargs(used_proxy),
                )
                status = int(upstream.status_code)
                if status < 400:
                    _mark_account_ok(account_id)
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
                can_retry = (
                    request_attempt == 0
                    and bytes_out == 0
                    and code in _UPSTREAM_RETRY_CODES
                    and (stream_flag or method in {"GET", "HEAD"})
                )
                logger.warning(
                    f"[Grok网关] 上游请求异常 curl={code or 'unknown'} "
                    f"proxy={proxypool.status_label(used_proxy)} "
                    f"attempt={request_attempt + 1}/2 retry={can_retry}"
                )
                if used_proxy:
                    proxypool.mark_fail(used_proxy)
                if not can_retry:
                    raise
                nxt = proxypool.pick(exclude=used_proxy)
                if nxt:
                    used_proxy = nxt
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
        err = (
            f"{type(exc).__name__} curl={code or 'unknown'} "
            f"proxy={proxypool.status_label(used_proxy)} bytes_out={bytes_out}: {exc}"
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
            effort=effort_flag,
            account_id=account_id,
            account=who,
            account_email=email or None,
            usage=usage,
            ip=client_ip,
            client_ua=client_ua,
        )
        # 上游明确判定的坏号进入临时冷却（网络异常 / 客户端断开不归咎账号）
        if err is None and status >= 400:
            _apply_upstream_cooldown(account_id, who, status)
        if err != "client_disconnected":
            logger.info(
                f"[Grok网关] {method} {path} {who} model={model or '-'} "
                f"status={status} {ms}ms in={len(body)} out={bytes_out}"
            )


def _proxy_hold(
    handler: BaseHTTPRequestHandler,
    method: str,
    path: str,
    body: bytes,
    model: str | None,
    effort_flag: str | None,
    session_key: str,
    url: str,
    client_ip: str,
    client_ua: str,
) -> None:
    """扣流重试（对齐 grok2api quality retry）：扣住上游流实时判定降智，命中即换号重发。

    - 重试无上限：每次 withhold 都对当次账号记一次 strike（1 观察 / 2 冷却 12h /
      3 长期排除），冷却中的账号自然退出候选，同一账号不会被本请求连续重试；
    - 号池耗尽（全部冷却 / 无可用）→ 502（fail_closed）；
    - 判定为正常 / 无法判定（输出过短、纯工具调用轮次、截断流）→ 回放已扣住的
      内容并继续实时转发剩余部分，客户端拿到完整响应；
    - 客户端断开 → 中止重试。
    """
    tried: set[int] = set()
    proxy_swapped: set[int] = set()
    attempt = 0
    net_failures = 0
    consecutive_withholds = 0
    fail_retries = 0
    while True:
        attempt += 1
        acc, acc_err = _pick_account(session_key, exclude_ids=tried)
        if acc is None:
            logger.warning(
                f"[Grok网关] 扣流重试 {attempt - 1} 次后无可用账号（{acc_err}）"
            )
            _send_bytes(
                handler,
                502,
                _openai_error(
                    f"号池账号全部降智冷却中（已重试 {max(0, attempt - 1)} 次），请稍后重试",
                    502,
                ),
                "application/json; charset=utf-8",
            )
            return

        account_id = int(acc["id"])
        email = str(acc.get("email") or "")
        who = email
        token = str(acc.get("access_token") or "")
        usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_tokens": 0,
            "reasoning_tokens": 0,
        }
        acc_us = StreamUsageAccumulator()
        scanner = QualityScanner()
        status = 502
        err: str | None = None
        upstream: requests.Response | None = None
        response_started = False
        ta = time.monotonic()
        bytes_out = 0
        used_proxy = proxypool.pick()
        try:
            upstream = requests.request(
                method,
                url,
                headers=_forward_headers(handler.headers, token),
                data=body if body else None,
                stream=True,
                **_proxy_kwargs(used_proxy),
            )
            status = int(upstream.status_code)
            if status < 400:
                _mark_account_ok(account_id)
            content_type = (upstream.headers.get("Content-Type") or "").lower()
            is_stream = status < 400 and (
                "text/event-stream" in content_type or not content_type
            )
            extra = _response_headers(upstream)
            net_failures = 0

            if status >= 400 or not is_stream:
                # 上游错误 / 非流式：整包读取（非流式降智同样换号重试）
                payload = upstream.content or b""
                bytes_out = len(payload)
                ctype = upstream.headers.get("Content-Type") or "application/json; charset=utf-8"
                if 200 <= status < 400:
                    usage = extract_nonstream(payload)
                    verdict, reason = classify_nonstream(payload)
                    if verdict == DEGRADED:
                        err = f"quality_degraded:{reason}"
                        _record(
                            method=method, path=path, model=model, stream=False,
                            status=status, ms=int((time.monotonic() - ta) * 1000),
                            bytes_in=len(body), bytes_out=bytes_out, error=err,
                            effort=effort_flag, account_id=account_id, account=who,
                            account_email=email or None, usage=usage,
                            ip=client_ip, client_ua=client_ua,
                        )
                        if _retry_quality_via_proxy(
                            account_id, used_proxy, proxy_swapped, who
                        ):
                            continue
                        tried.add(account_id)
                        action = mark_quality_hit(account_id)
                        logger.warning(
                            f"[Grok网关] 扣流命中降智 {who} reason={reason} "
                            f"attempt={attempt} 处置={action} → 换号重试"
                        )
                        continue
                    _send_bytes(handler, status, payload, ctype, extra)
                    _record(
                        method=method, path=path, model=model, stream=False,
                        status=status, ms=int((time.monotonic() - ta) * 1000),
                        bytes_in=len(body), bytes_out=bytes_out, error=None,
                        effort=effort_flag, account_id=account_id, account=who,
                        account_email=email or None, usage=usage,
                        ip=client_ip, client_ua=client_ua,
                    )
                    return
                # 错误响应：按状态码冷却该账号；账号级失败（限流/额度/凭据/5xx）
                # 换号重试（有上限），重试耗尽或请求级错误（400/404 等）透传给客户端
                _apply_upstream_cooldown(account_id, who, status)
                if (
                    status in _RETRYABLE_UPSTREAM_STATUS
                    and fail_retries < _MAX_UPSTREAM_FAIL_RETRIES
                ):
                    fail_retries += 1
                    err = f"upstream_{status}"
                    _record(
                        method=method, path=path, model=model, stream=False,
                        status=status, ms=int((time.monotonic() - ta) * 1000),
                        bytes_in=len(body), bytes_out=bytes_out, error=err,
                        effort=effort_flag, account_id=account_id, account=who,
                        account_email=email or None, usage=usage,
                        ip=client_ip, client_ua=client_ua,
                    )
                    logger.warning(
                        f"[Grok网关] 扣流上游失败 {who} status={status} "
                        f"attempt={attempt} fail_retry={fail_retries} → 换号重试"
                    )
                    continue
                _send_bytes(handler, status, payload, ctype, extra)
                _record(
                    method=method, path=path, model=model, stream=False,
                    status=status, ms=int((time.monotonic() - ta) * 1000),
                    bytes_in=len(body), bytes_out=bytes_out, error=None,
                    effort=effort_flag, account_id=account_id, account=who,
                    account_email=email or None, usage=usage,
                    ip=client_ip, client_ua=client_ua,
                )
                return

            # ── 流式扣流：缓冲 + 实时裁决 ──
            held = bytearray()
            deliver = False
            withhold_reason = ""
            raw_iter = upstream.iter_content(chunk_size=4096)
            for chunk in raw_iter:
                if not chunk:
                    continue
                scanner.feed(chunk)
                acc_us.feed(chunk)
                held.extend(chunk)
                verdict, reason = scanner.live_verdict()
                if verdict == HOLD_WITHHOLD:
                    deliver = False
                    withhold_reason = reason
                    break
                if verdict == HOLD_DELIVER:
                    deliver = True
                    break
                if len(held) > _HOLD_MAX_BUFFER:
                    # 超大响应放弃重试，直接放行（无法完整扣留）
                    deliver = True
                    break
            else:
                # 流自然结束仍未裁决：按终止态裁决（截断流放行，不重试）
                verdict, withhold_reason = scanner.live_verdict()
                deliver = verdict != HOLD_WITHHOLD

            if not deliver:
                # 降智：丢弃已扣内容。先换出口 IP 同号重试；确认不是 IP 再给账号记 strike。
                usage = acc_us.result()
                err = f"quality_degraded:{withhold_reason}"
                _record(
                    method=method, path=path, model=model, stream=True,
                    status=status, ms=int((time.monotonic() - ta) * 1000),
                    bytes_in=len(body), bytes_out=bytes_out, error=err,
                    effort=effort_flag, account_id=account_id, account=who,
                    account_email=email or None, usage=usage,
                    ip=client_ip, client_ua=client_ua,
                )
                if _retry_quality_via_proxy(
                    account_id, used_proxy, proxy_swapped, who
                ):
                    continue
                tried.add(account_id)
                action = mark_quality_hit(account_id)
                consecutive_withholds += 1
                if consecutive_withholds >= _HOLD_MAX_CONSECUTIVE_WITHHOLDS:
                    # 会话级熔断：连续多个账号都返回无思考响应，说明降智与账号无关
                    # （典型为会话上下文过大触发的上游保护），换号已无意义——
                    # 提前返回明确错误，避免把整个号池打进冷却
                    logger.error(
                        f"[Grok网关] 连续 {consecutive_withholds} 个账号返回无思考响应，"
                        f"判定为会话级降智（疑上下文过大），熔断重试 {who}"
                    )
                    _send_bytes(
                        handler,
                        502,
                        _openai_error(
                            f"连续 {consecutive_withholds} 个账号返回无思考响应——"
                            "多为会话上下文过大触发的上游降智保护，"
                            "请压缩或重开会话后重试",
                            502,
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                logger.warning(
                    f"[Grok网关] 扣流命中降智 {who} reason={withhold_reason} "
                    f"attempt={attempt} 处置={action} → 换号重试"
                )
                continue

            # ── 放行：回放已扣住的内容，再继续实时转发剩余部分 ──
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
            handler.wfile.write(bytes(held))
            handler.wfile.flush()
            bytes_out += len(held)
            for chunk in raw_iter:
                if not chunk:
                    continue
                scanner.feed(chunk)
                acc_us.feed(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
                bytes_out += len(chunk)
            usage = acc_us.result()
            ms = int((time.monotonic() - ta) * 1000)
            _record(
                method=method, path=path, model=model, stream=True,
                status=status, ms=ms, bytes_in=len(body), bytes_out=bytes_out,
                error=None, effort=effort_flag, account_id=account_id, account=who,
                account_email=email or None, usage=usage,
                ip=client_ip, client_ua=client_ua,
            )
            if attempt > 1:
                logger.info(f"[Grok网关] 扣流重试成功 attempt={attempt} {who}")
            return
        except (BrokenPipeError, ConnectionError, ConnectionResetError, ConnectionAbortedError):
            err = "client_disconnected"
            if status < 400:
                logger.info(
                    f"[Grok网关] 客户端提前断开（上游已成功 HTTP {status}）{method} {path} {who}"
                )
            else:
                logger.warning(f"[Grok网关] 客户端断开 {method} {path} {who} HTTP {status}")
            _record(
                method=method, path=path, model=model, stream=True,
                status=status, ms=int((time.monotonic() - ta) * 1000),
                bytes_in=len(body), bytes_out=bytes_out, error=err,
                effort=effort_flag, account_id=account_id, account=who,
                account_email=email or None, usage=usage,
                ip=client_ip, client_ua=client_ua,
            )
            return
        except requests.RequestsError as exc:
            code = curl_error_code(exc)
            net_failures += 1
            logger.warning(
                f"[Grok网关] 扣流上游异常 curl={code or 'unknown'} "
                f"attempt={attempt} 连续网络失败={net_failures}"
            )
            if net_failures >= 5:
                err = f"upstream_error:curl={code or 'unknown'}"
                _send_bytes(
                    handler,
                    502,
                    _openai_error(f"上游连续网络失败（已尝试 {attempt} 次），请稍后重试", 502),
                    "application/json; charset=utf-8",
                )
                _record(
                    method=method, path=path, model=model, stream=True,
                    status=502, ms=int((time.monotonic() - ta) * 1000),
                    bytes_in=len(body), bytes_out=bytes_out, error=err,
                    effort=effort_flag, account_id=account_id, account=who,
                    account_email=email or None, usage=usage,
                    ip=client_ip, client_ua=client_ua,
                )
                return
            time.sleep(_UPSTREAM_RETRY_DELAY)
            continue
        except Exception as exc:
            code = curl_error_code(exc)
            if used_proxy:
                proxypool.mark_fail(used_proxy)
            err = (
                f"{type(exc).__name__} curl={code or 'unknown'} "
                f"proxy={proxypool.status_label(used_proxy)} bytes_out={bytes_out}: {exc}"
            )
            logger.error(f"[Grok网关] 扣流转发失败 {method} {path} {who} {err}")
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
            _record(
                method=method, path=path, model=model, stream=True,
                status=502, ms=int((time.monotonic() - ta) * 1000),
                bytes_in=len(body), bytes_out=bytes_out, error=err,
                effort=effort_flag, account_id=account_id, account=who,
                account_email=email or None, usage=usage,
                ip=client_ip, client_ua=client_ua,
            )
            return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass
