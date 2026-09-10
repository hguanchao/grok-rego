"""
管理 API 服务（注册 + 号池 + OpenCode Zen 网关）。

用法:
  uv run python main.py --serve [port]     # 默认 8787
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from api.pool_jobs import (
    auth_pool_state,
    auto_refresher,
    kick_auth_pool,
    pool_job_manager,
)
from api.push import push_manager
from core import config
from core.config import API_HOST, API_PORT
from core.logger import logger
from db import (
    get_all_accounts,
    get_pool_stats,
    init_db,
    query_accounts,
    query_usage_grouped,
    query_usage_recent,
    query_usage_summary,
    soft_delete_accounts,
    update_account_status_by_ids,
)
from gateway.grok import proxy as grok_proxy
from gateway.opencode import probe as gateway_probe
from gateway.opencode import proxy as gateway_proxy
from gateway.ops import ops_snapshot
from workflow.jobs import manager

_CORS_ORIGIN = "*"
_MAX_BODY = 1_000_000


def _after_log_id(query: dict[str, list[str]]) -> int:
    try:
        return max(0, int(query.get("after", ["0"])[0]))
    except (TypeError, ValueError):
        return 0


def _body_int(body: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(body.get(key) or default)
    except (TypeError, ValueError):
        return default


def _is_digit_id_list(value: Any, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, (int, str)) and str(item).isdigit() for item in value)
    )


def _json_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > _MAX_BODY:
        raise ValueError("请求体过大")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", _CORS_ORIGIN)
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-Api-Key, X-Account-Id, HTTP-Referer, X-Title, OpenAI-Beta, anthropic-version, anthropic-beta, x-api-key",
    )
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _error_json(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    _send_json(handler, status, {"ok": False, "error": message})


def _handle_system_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "GET" and path in ("/api/health", "/health"):
        _send_json(handler, 200, {"ok": True, "service": "grok-rego"})
        return True
    if method == "GET" and path == "/api/config":
        _send_json(handler, 200, {"ok": True, "data": config.get_public_config()})
        return True
    if method == "PUT" and path == "/api/config":
        body = _json_body(handler)
        data = config.update_public_config(body if isinstance(body, dict) else {})
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    return False


def _handle_gateway_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """网关管理面：聚合运维快照（单次拉取）+ Zen 上游探测。"""
    if method == "GET" and path == "/api/gateway/ops":
        _send_json(handler, 200, {"ok": True, "data": ops_snapshot()})
        return True
    if method == "POST" and path == "/api/gateway/probe":
        _json_body(handler)  # 排空请求体，避免 keep-alive 把下一请求打成 501
        _send_json(handler, 200, {"ok": True, "data": gateway_probe()})
        return True
    return False


def _handle_register_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "GET" and path == "/api/register/status":
        after = _after_log_id(query)
        _send_json(handler, 200, {"ok": True, "data": manager.get_status(after_log_id=after)})
        return True
    if method == "POST" and path == "/api/register/start":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        count = int(body.get("count") or 1)
        threads = int(body.get("threads") or 1)
        headless = body.get("headless")
        if headless is None:
            headless = True
        cfg_patch = body.get("config")
        if isinstance(cfg_patch, dict) and cfg_patch:
            config.update_public_config(cfg_patch)
        data = manager.start(count=count, threads=threads, headless=bool(headless))
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "POST" and path == "/api/register/stop":
        data = manager.stop()
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "POST" and path == "/api/register/logs/clear":
        data = manager.clear_logs()
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    return False


def _handle_pool_accounts_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "GET" and path == "/api/pool/stats":
        _send_json(handler, 200, {"ok": True, "data": get_pool_stats()})
        return True
    if method == "GET" and path == "/api/pool/accounts":
        page = int(query.get("page", ["1"])[0] or "1")
        page_size = int(query.get("page_size", ["20"])[0] or "20")
        status = query.get("status", [None])[0]
        keyword = query.get("keyword", [None])[0]
        authed = query.get("authed", [None])[0]
        expiry = query.get("expiry", [None])[0]
        statuses = None
        if status and status != "all":
            parts = [int(s) for s in str(status).split(",") if s.strip().isdigit()]
            if parts:
                statuses = parts
        data = query_accounts(
            page=max(1, page),
            page_size=max(1, min(page_size, 100)),
            statuses=statuses,
            keyword=keyword or None,
            authed=authed if authed in ("authed", "unauthed") else None,
            expiry=expiry if expiry in ("soon", "expired", "valid") else None,
        )
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "POST" and path == "/api/pool/auth":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        # ids 缺省或空数组 = 全量模式：仅处理未认证账号
        ids = body.get("ids")
        if ids is not None and not _is_digit_id_list(ids, allow_empty=True):
            _error_json(handler, 400, "ids 必须是数组")
            return True
        from db import STATUS_DISABLED, add_to_auth_pool, get_auth_pool
        from workflow.oauth import _extract_sso_value

        id_set = {int(i) for i in ids} if ids else None
        accounts = get_all_accounts()
        # 按注册时间倒序执行（新注册的账号优先处理）
        accounts.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        pool_emails = {e["email"] for e in get_auth_pool()}
        results: list[dict[str, Any]] = []
        started = False
        for acc in accounts:
            if id_set is not None and acc["id"] not in id_set:
                continue
            if acc.get("access_token"):
                results.append({"id": acc["id"], "status": "skipped", "reason": "已认证，无需认证"})
                continue
            if int(acc.get("status") or 1) == STATUS_DISABLED:
                # 禁用账号不入认证池（认证成功会回写 ACTIVE），尊重禁用标记
                results.append({"id": acc["id"], "status": "skipped", "reason": "账号已禁用"})
                continue
            if acc["email"] in pool_emails:
                results.append(
                    {
                        "id": acc["id"],
                        "status": "pending",
                        "reason": "已在认证队列中，后台自动认证进行中",
                    }
                )
                continue
            if not _extract_sso_value(acc.get("sso_cookie")):
                results.append(
                    {
                        "id": acc["id"],
                        "status": "failed",
                        "reason": "缺少 SSO cookie，无法自动认证（需重新注册获取 SSO）",
                    }
                )
                continue
            add_to_auth_pool(acc["email"], "", 5, acc["id"])
            pool_emails.add(acc["email"])
            results.append(
                {
                    "id": acc["id"],
                    "email": acc["email"],
                    "status": "pending",
                    "reason": "已发起认证，后台 SSO 自动交换 Token 进行中",
                }
            )
            started = True
        if started:
            logger.info(
                f"[认证] {sum(1 for r in results if r['status'] == 'pending')} 个账号入认证池，触发 SSO 自动认证"
            )
            kick_auth_pool()
        # 日志聚合：全量模式可能数百条结果，逐个罗列会刷屏
        pending_total = sum(1 for r in results if r["status"] == "pending")
        skipped_total = sum(1 for r in results if r["status"] == "skipped")
        failed_total = sum(1 for r in results if r["status"] == "failed")
        detail = ", ".join(f"#{r['id']}={r['status']}" for r in results[:20])
        if len(results) > 20:
            detail += f" …（共 {len(results)} 个）"
        logger.info(
            f"[认证] 发起认证 {len(results)} 个："
            f"待认证 {pending_total} · 跳过 {skipped_total} · 失败 {failed_total}  {detail}"
        )
        _send_json(handler, 200, {"ok": True, "data": {"results": results}})
        return True
    if method == "DELETE" and path == "/api/pool/accounts":
        body = _json_body(handler)
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            _error_json(handler, 400, "ids 必须是非空数组")
            return True
        deleted = soft_delete_accounts([int(i) for i in ids])
        _send_json(handler, 200, {"ok": True, "data": {"deleted": deleted}})
        return True
    return False


def _handle_pool_operations_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "POST" and path == "/api/pool/inspect":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        ids = body.get("ids")
        # 空数组/缺省 = 全量模式：服务端筛选全部可巡检账号（已认证且非需重登）
        if ids is not None and not _is_digit_id_list(ids, allow_empty=True):
            _error_json(handler, 400, "ids 必须是数组")
            return True
        concurrency = _body_int(body, "concurrency", 20)
        try:
            data = pool_job_manager.start(
                kind="inspect",
                account_ids=[int(i) for i in ids] if ids else [],
                concurrency=concurrency,
            )
        except RuntimeError as exc:
            _error_json(handler, 400, str(exc))
            return True
        logger.info(
            f"[巡检] 任务启动 账号数={len(ids) if ids else '全部符合条件'} task={data.get('id')}"
        )
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "POST" and path == "/api/pool/status":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        ids = body.get("ids")
        status = body.get("status")
        if not isinstance(ids, list) or not ids:
            _error_json(handler, 400, "ids 必须是非空数组")
            return True
        if status not in (1, 2, 3, 4, 5, 6):
            _error_json(handler, 400, "status 必须是 1-6 的账号状态码")
            return True
        reason = body.get("reason") if isinstance(body.get("reason"), str) else None
        updated = update_account_status_by_ids([int(i) for i in ids], int(status), reason)
        _send_json(handler, 200, {"ok": True, "data": {"updated": updated}})
        return True
    return False


def _handle_pool_push_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "POST" and path == "/api/pool/push":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        targets = body.get("targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(t, str) and t in ("g2a", "cpa") for t in targets)
        ):
            _error_json(handler, 400, "targets 必须是非空的 ['g2a'|'cpa'] 数组")
            return True
        ids = body.get("ids")
        # 空数组/缺省 = 全量模式：服务端预筛仅保留已认证且状态正常账号
        if ids is not None and not _is_digit_id_list(ids, allow_empty=True):
            _error_json(handler, 400, "ids 必须是数组")
            return True
        concurrency = _body_int(body, "concurrency", 20)
        try:
            data = push_manager.start(
                targets=targets,
                account_ids=[int(i) for i in ids] if ids else [],
                concurrency=concurrency,
            )
        except RuntimeError as exc:
            _error_json(handler, 400, str(exc))
            return True
        labels = "、".join("G2A" if t == "g2a" else "CPA" for t in targets)
        logger.info(
            f"[推送] 任务启动 targets=[{labels}] 账号数={len(ids) if ids else '全部符合条件'} "
            f"task={data.get('id')}"
        )
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "GET" and path == "/api/pool/push/status":
        after = _after_log_id(query)
        _send_json(handler, 200, {"ok": True, "data": push_manager.status(after_log_id=after)})
        return True
    if method == "POST" and path == "/api/pool/push/cancel":
        try:
            data = push_manager.cancel()
        except RuntimeError as exc:
            _error_json(handler, 400, str(exc))
            return True
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    return False


def _handle_pool_maintenance_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    if method == "GET" and path == "/api/pool/inspect/status":
        after = _after_log_id(query)
        _send_json(
            handler, 200, {"ok": True, "data": pool_job_manager.status(after_log_id=after)}
        )
        return True
    if method == "POST" and path == "/api/pool/inspect/cancel":
        try:
            data = pool_job_manager.cancel()
        except RuntimeError as exc:
            _error_json(handler, 400, str(exc))
            return True
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "POST" and path == "/api/pool/reauth":
        body = _json_body(handler) or {}
        if not isinstance(body, dict):
            _error_json(handler, 400, "请求体必须是 JSON 对象")
            return True
        ids = body.get("ids")
        # 空数组/缺省 = 全量模式：服务端仅处理需重登(2)状态的账号
        if ids is not None and not _is_digit_id_list(ids, allow_empty=True):
            _error_json(handler, 400, "ids 必须是数组")
            return True
        concurrency = _body_int(body, "concurrency", 5)
        try:
            data = pool_job_manager.start(
                kind="reauth",
                account_ids=[int(i) for i in ids] if ids else [],
                concurrency=concurrency,
            )
        except RuntimeError as exc:
            _error_json(handler, 400, str(exc))
            return True
        logger.info(
            f"[重登] 任务启动 账号数={len(ids) if ids else '全部符合条件'} task={data.get('id')}"
        )
        _send_json(handler, 200, {"ok": True, "data": data})
        return True
    if method == "GET" and path == "/api/pool/auth/status":
        after = _after_log_id(query)
        _send_json(
            handler, 200, {"ok": True, "data": auth_pool_state(after_log_id=after)}
        )
        return True
    if method == "GET" and path == "/api/pool/auto-refresh":
        after = _after_log_id(query)
        _send_json(handler, 200, {"ok": True, "data": auto_refresher.state(after_log_id=after)})
        return True
    return False


def _handle_usage_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """用量统计：窗口聚合 + 分组维度（账号/模型）+ 最近明细分页。"""
    if method == "GET" and path == "/api/usage":
        try:
            days = int(query.get("days", ["1"])[0] or "1")
        except (TypeError, ValueError):
            days = 1
        _send_json(handler, 200, {"ok": True, "data": query_usage_summary(days)})
        return True
    if method == "GET" and path == "/api/usage/grouped":
        dimension = str(query.get("dim", [""])[0] or "").strip()
        if dimension not in ("account", "model"):
            _error_json(handler, 400, "dim 仅支持 account / model")
            return True
        try:
            _send_json(
                handler,
                200,
                {"ok": True, "data": query_usage_grouped(dimension=dimension)},
            )
        except ValueError as exc:
            _error_json(handler, 400, str(exc))
        return True
    if method == "GET" and path == "/api/usage/recent":
        try:
            offset = int(query.get("offset", ["0"])[0] or "0")
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(query.get("limit", ["20"])[0] or "20")
        except (TypeError, ValueError):
            limit = 20
        _send_json(
            handler,
            200,
            {"ok": True, "data": query_usage_recent(offset=offset, limit=limit)},
        )
        return True
    return False


def _handle_task_api(
    method: str, path: str, _query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    """全局任务互斥视图：供前端禁用其它任务按钮（防并发误触）。"""
    if method == "GET" and path == "/api/tasks/active":
        from core.mutex import active

        names = set(active())
        _send_json(
            handler,
            200,
            {
                "ok": True,
                "data": {
                    "register": "注册" in names,
                    "push": "推送" in names,
                    "pool": "号池" in names,
                    "auth": "认证" in names,
                },
            },
        )
        return True
    return False


def _handle_api(
    method: str, path: str, query: dict[str, list[str]], handler: BaseHTTPRequestHandler
) -> bool:
    for route_handler in (
        _handle_system_api,
        _handle_gateway_api,
        _handle_register_api,
        _handle_pool_accounts_api,
        _handle_pool_operations_api,
        _handle_pool_push_api,
        _handle_pool_maintenance_api,
        _handle_usage_api,
        _handle_task_api,
    ):
        if route_handler(method, path, query, handler):
            return True
    if path.startswith("/api/") or path == "/health":
        _error_json(handler, 404, f"not found: {path}")
        return True
    return False


class _ApiHandler(BaseHTTPRequestHandler):
    """管理 API 请求处理器。"""

    protocol_version = "HTTP/1.1"
    server_version = "grok-rego"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 管理 API 访问日志静默

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _CORS_ORIGIN)
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Api-Key, X-Account-Id, HTTP-Referer, X-Title, OpenAI-Beta, anthropic-version, anthropic-beta, x-api-key",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/zen/v1" or path.startswith("/zen/v1/"):
                gateway_proxy(self, method, path)
                return
            if path == "/grok/v1" or path.startswith("/grok/v1/"):
                grok_proxy(self, method, path)
                return
            if _handle_api(method, path, query, self):
                return
        except ValueError as e:
            _error_json(self, 400, str(e))
            return
        except RuntimeError as e:
            _error_json(self, 409, str(e))
            return
        except json.JSONDecodeError:
            _error_json(self, 400, "无效 JSON")
            return
        except Exception as e:
            logger.exception("[API] 未处理异常")
            _error_json(self, 500, f"{type(e).__name__}: {e}")
            return
        _error_json(self, 404, f"not found: {path}")


def serve(host: str | None = None, port: int | None = None) -> None:
    """启动管理 API 服务（阻塞）。"""
    init_db()
    bind_host = host or API_HOST
    bind_port = port or API_PORT
    server = ThreadingHTTPServer((bind_host, bind_port), _ApiHandler)
    server.daemon_threads = True
    auto_refresher.start()
    logger.success(f"[API] 服务启动: http://{bind_host}:{bind_port}")
    logger.info(
        "[API] 路由: /api/* （注册 / 号池 / 网关运维）  /zen/v1/* （Zen）  /grok/v1/* （号池 Grok）"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[API] 收到中断信号，停止服务")
    finally:
        server.server_close()
