"""号池任务：探活、认证池消化、自动续期、巡检/重登/风控。"""

from __future__ import annotations

"""
号池账号上游活体探测客户端。

- HTTP 直连 curl_cffi（Chrome TLS 指纹），代理走 config.PROXY
- 探活用 GET /billing?format=credits：不消耗配额、不触发推理、秒级返回
- 网络失败自动重试一次
"""

import json
import threading
import time
from typing import Any

from curl_cffi import requests

from core import config
from core.config import UPSTREAM_BASE
from core.logger import logger
from core.mutex import acquire as mutex_acquire, release as mutex_release

_PROBE_CONNECT_TIMEOUT = 10
_PROBE_READ_TIMEOUT = 30
_PROBE_RETRY_DELAY = 1.0
_CLIENT_VERSION = "1.0.0"

_thread_local = threading.local()


class ProbeClient:
    """上游活体探测客户端（线程级 Session 复用，代理变更时重建）。"""

    def __init__(self, *, base_url: str = "", model: str = "") -> None:
        self.base_url = str(base_url or UPSTREAM_BASE).rstrip("/")

    def _session(self, proxy: str) -> requests.Session:
        pair = getattr(_thread_local, "session_pair", None)
        if pair is None or pair[0] != proxy:
            session = requests.Session(impersonate="chrome")
            session.proxies = {"http": proxy, "https": proxy}
            _thread_local.session_pair = (proxy, session)
        return _thread_local.session_pair[1]

    @staticmethod
    def _headers(account: dict[str, Any]) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account.get('access_token') or ''!s}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": _CLIENT_VERSION,
            "Accept": "application/json",
        }

    def probe(self, account: dict[str, Any], proxy: str = "") -> dict[str, Any]:
        """单账号活体探测：GET /billing?format=credits，返回探活结果。

        返回 dict：
          status_code  200=有效 / 401·403=失效 / 402=配额 / 429=限流 / 0=网络异常
          error        失败原因摘要
        """
        result: dict[str, Any] = {"status_code": 0, "error": ""}
        use_proxy = str(proxy or config.PROXY or "").strip()
        email = str(account.get("email") or "").strip()
        log_email = email
        if not use_proxy:
            result["error"] = "探活强制走代理，未配置 proxy"
            logger.error(f"[探活] {log_email} 未配置代理，跳过探活")
            return result
        session = self._session(use_proxy)

        for attempt in range(2):
            try:
                logger.debug(f"[探活] {log_email} 请求 GET /billing?format=credits · 尝试 {attempt + 1}/2")
                response = session.get(
                    f"{self.base_url}/billing?format=credits",
                    headers=self._headers(account),
                    timeout=(_PROBE_CONNECT_TIMEOUT, _PROBE_READ_TIMEOUT),
                )
                break
            except requests.RequestsError as exc:
                if attempt == 0:
                    logger.warning(f"[探活] {log_email} 请求异常（重试中）: {type(exc).__name__}: {exc}")
                    time.sleep(_PROBE_RETRY_DELAY)
                    continue
                detail = str(exc)[:160]
                if "timed out" in detail or "Timeout" in detail or "Connection" in detail:
                    detail = "网络无响应"
                result["error"] = detail
                logger.error(f"[探活] {log_email} 请求失败: {type(exc).__name__}: {detail}")
                return result

        result["status_code"] = int(response.status_code)
        if 200 <= response.status_code < 300:
            result["error"] = "探活通过"
            logger.debug(f"[探活] {log_email} 探活通过 · HTTP {response.status_code}")
        else:
            err = _simplify_error(response.status_code, response.text or "")
            result["error"] = err
            logger.warning(f"[探活] {log_email} 探活失败 · HTTP {response.status_code} {err}")
        return result


def _simplify_error(status: int, body: str) -> str:
    """从上游响应正文提取可读摘要（JSON error → message → 截断）。"""
    body = (body or "").strip()
    if not body:
        return ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            err = data.get("error") or data.get("message") or data.get("detail")
            if isinstance(err, dict):
                err = err.get("message") or err.get("error") or ""
            if isinstance(err, str) and err:
                return err[:120]
    except (json.JSONDecodeError, ValueError):
        pass
    first = body.splitlines()[0] if body else ""
    return first[:120].strip()


# 进程内单例（探活无状态，复用连接池）
probe_client = ProbeClient()


"""
认证池消化触发器（全局单飞）。

认证池消化：供管理 API 与重登任务共用触发。

- 全局锁 + 运行标志：同一时间只允许一个消化线程（xAI 对并发交换按 IP 限流）
- 消化本体在 workflow.register.run_auth_pool（保留 Camoufox 重依赖的局部导入）
"""

import threading

_auth_pool_lock = threading.Lock()
_auth_pool_running = False
_auth_pool_logs: list[dict[str, Any]] = []
_auth_pool_last_log_id = 0
_AUTH_LOG_LIMIT = 200


def _append_auth_pool_log(level: str, message: str) -> None:
    """追加认证池任务日志，供管理页按游标增量拉取。"""
    global _auth_pool_last_log_id
    with _auth_pool_lock:
        _auth_pool_last_log_id += 1
        _auth_pool_logs.append(
            {"id": _auth_pool_last_log_id, "level": level, "message": message}
        )
        if len(_auth_pool_logs) > _AUTH_LOG_LIMIT:
            del _auth_pool_logs[:-_AUTH_LOG_LIMIT]


def kick_auth_pool() -> None:
    """后台线程消化认证池（20 worker，每 worker 间隔 1s）；已有消化线程则跳过本轮。

    全局互斥：其它重任务（推送/号池任务/注册）进行中直接拒绝（API 层转 409）。
    """
    global _auth_pool_running
    with _auth_pool_lock:
        if _auth_pool_running:
            logger.debug("[认证池] 已有消化线程运行，跳过本轮触发")
            return
    # 先占全局互斥再置位：互斥失败不产生“已标记运行却未启动”的脏状态
    mutex_acquire("认证")
    with _auth_pool_lock:
        if _auth_pool_running:  # 并发触发防御
            mutex_release("认证")
            return
        _auth_pool_running = True
        # 新一轮仅保留本轮日志，游标继续单调递增，避免前端 after 游标回退。
        _auth_pool_logs.clear()
    logger.info("[认证池] 消化线程启动")

    def worker() -> None:
        global _auth_pool_running
        try:
            # 局部导入：register 模块带 Camoufox 重依赖，避免启动时加载
            from workflow.register import run_auth_pool

            def on_result(email: str, ok: bool, reason: str) -> None:
                level = "SUCCESS" if ok else "ERROR"
                action = "认证成功，Token 已入库" if ok else f"认证失败：{reason}"
                _append_auth_pool_log(level, f"{email}·{action}")

            count = run_auth_pool(on_result=on_result)
            logger.info(f"[认证池] 消化完成: 成功 {count} 个")
        except Exception as exc:
            logger.error(f"[认证池] 后台消化异常: {type(exc).__name__}: {exc}")
        finally:
            with _auth_pool_lock:
                _auth_pool_running = False
            mutex_release("认证")

    threading.Thread(target=worker, daemon=True, name="认证池消化").start()


def auth_pool_state(after_log_id: int = 0) -> dict[str, Any]:
    """认证池当前状态（供 /api/pool/auth/status 展示，页面刷新后恢复认知）。"""
    from db import get_auth_pool

    with _auth_pool_lock:
        running = _auth_pool_running
        logs = [log for log in _auth_pool_logs if log["id"] > after_log_id]
        last_log_id = _auth_pool_last_log_id
    return {
        "running": running,
        "queue_size": len(get_auth_pool()),
        "logs": logs,
        "last_log_id": last_log_id,
    }

"""
临期账号自动续期 daemon。

借鉴 acorn 自动刷新思路（后台固定周期扫描临期账号并续期），按 grok-rego
栈重写并定制：
- 周期 30 分钟扫描，token 剩余 ≤10 分钟即续期（沿用参考参数）
- 临期账号最多 4 个 worker 并发刷新，每个 worker 做完一个号再隔 1 秒接下一个
- 失败分级：网络瞬时失败跳过本轮不判死；刷新凭据被拒标记 REAUTH
- 与手动号池任务 / 推送任务互斥：任一进行中则跳过本轮
- 状态只读暴露（GET /api/pool/auto-refresh），日志走统一 loguru
"""

import threading

from core.util import (
    ACCOUNT_WORKER_GAP_SEC,
    ACCOUNT_WORKERS,
    decode_jwt_exp,
    elapsed_label,
    format_exp,
    now_str,
    proxy_endpoint_ready,
    run_account_workers,
)
from db import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_LIMITED,
    STATUS_REAUTH,
    get_all_accounts,
    list_due_refresh_candidates,
    update_account_status_by_ids,
    update_account_tokens,
    update_risk,
)
from workflow.oauth import auth_with_sso
from workflow.oauth import refresh_token as oauth_refresh

# 固定参数：每 30 分钟检查一次，token 剩余 ≤10 分钟即续期
INTERVAL_MIN = 30
LEAD_MIN = 10
# 续期并发限制为 4，避免代理恢复/切换时形成连接风暴。
REFRESH_CONCURRENCY = min(4, ACCOUNT_WORKERS)
# 轮询唤醒间隔（秒）
_SCAN_WAKE_SEC = 30
_PROXY_CHECK_TIMEOUT_SEC = 1.0
# 任务日志内存环形保留条数
_LOG_LIMIT = 200


class AutoRefresher:
    """后台 daemon 线程：按固定周期扫描临期账号并并发续期。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._scanning = False
        self._last_run_at = ""
        # 用 monotonic 做间隔节流，避免北京时间串 + mktime 受系统时区影响
        self._last_run_mono = 0.0
        self._last_result = ""
        self._skip_reason = ""
        # 日志与进度（供前端号池页展示）
        self._logs: list[dict[str, Any]] = []
        self._last_log_id = 0
        self._done = 0
        self._total = 0

    def append_log(self, level: str, message: str) -> None:
        """追加扫描日志（环形保留 _LOG_LIMIT 条，id 单调递增供增量轮询）。"""
        with self._lock:
            self._last_log_id += 1
            self._logs.append({"id": self._last_log_id, "level": level, "message": message})
            if len(self._logs) > _LOG_LIMIT:
                self._logs = self._logs[-_LOG_LIMIT:]

    # ─── 状态 / 生命周期 ─────────────────────────────────────

    def state(self, after_log_id: int = 0) -> dict[str, Any]:
        """只读状态：供 GET /api/pool/auto-refresh 展示。"""
        with self._lock:
            logs = [log for log in self._logs if log["id"] > after_log_id]
            return {
                "running": self._scanning,
                "interval_min": INTERVAL_MIN,
                "lead_min": LEAD_MIN,
                "concurrency": REFRESH_CONCURRENCY,
                "last_run_at": self._last_run_at,
                "last_result": self._last_result,
                "skip_reason": self._skip_reason,
                "done": self._done,
                "total": self._total,
                "progress": round(self._done / self._total * 100, 2) if self._total else 0,
                "logs": logs,
                "last_log_id": self._last_log_id,
            }

    def start(self) -> None:
        """启动 daemon 线程（幂等）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="auto-refresh"
        )
        self._thread.start()
        logger.debug("自动续期后台线程已启动")

    def stop(self) -> None:
        """停止 daemon 线程（幂等）。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        logger.debug("自动续期后台线程已停止")

    # ─── 主循环 ─────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_scan()
            except Exception:
                logger.exception("[续期] 扫描异常")
            self._stop.wait(_SCAN_WAKE_SEC)

    def _maybe_scan(self) -> None:
        """间隔节流：距上次扫描不足 INTERVAL_MIN 则跳过。"""
        with self._lock:
            if self._scanning:
                return
            if (
                self._last_run_mono > 0
                and (time.monotonic() - self._last_run_mono) < INTERVAL_MIN * 60
            ):
                return
        self._scan()

    # ─── 扫描与续期 ─────────────────────────────────────────

    def _task_busy(self) -> bool:
        """任意手动重任务（推送 / 号池任务 / 认证 / 注册）进行中则本轮跳过。

        自动续期是低优先级后台任务：不占用全局互斥，主动避让，
        避免与手动任务并发刷同一批账号。
        """
        from core.mutex import active

        return bool(active())

    def _scan(self) -> None:
        with self._lock:
            self._scanning = True
            self._skip_reason = ""
        try:
            proxy = str(config.PROXY or "").strip()
            if proxy and not proxy_endpoint_ready(proxy, _PROXY_CHECK_TIMEOUT_SEC):
                reason = "代理未就绪，延迟本轮续期"
                self.append_log("WARNING", f"[续期] {reason}")
                logger.warning(f"[续期] {reason}")
                with self._lock:
                    self._skip_reason = reason
                return
            if self._task_busy():
                reason = "手动任务进行中，跳过本轮"
                self.append_log("WARNING", f"[续期] {reason}")
                with self._lock:
                    self._skip_reason = reason
                return
            due = self._collect_due()
            if not due:
                with self._lock:
                    self._last_run_at = now_str()
                    self._last_run_mono = time.monotonic()
                    self._last_result = "无临期账号"
                self.append_log("INFO", "[续期] 本轮无临期账号")
                return
            t0 = time.monotonic()
            with self._lock:
                self._done = 0
                self._total = len(due)
            self.append_log(
                "INFO",
                f"[续期] 扫描开始 临期 {len(due)} 个 / {REFRESH_CONCURRENCY} 线程 "
                f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s",
            )
            refreshed, rejected, transient = self._refresh_all(due)
            summary = (
                f"[续期] 扫描结束 续期 {refreshed} 刷新被拒 {rejected} "
                f"网络失败 {transient} · {elapsed_label(t0)}"
            )
            self.append_log(
                "SUCCESS" if refreshed > 0 else "WARNING", summary
            )
            with self._lock:
                self._last_run_at = now_str()
                self._last_run_mono = time.monotonic()
                self._last_result = (
                    f"续期 {refreshed} 个，刷新被拒 {rejected} 个，"
                    f"网络失败 {transient} 个"
                )
        finally:
            with self._lock:
                self._scanning = False

    def _collect_due(self) -> list[dict[str, Any]]:
        """收集临期账号：exp ≤ now + LEAD_MIN 且状态允许续期（查询收敛到 db 层）。"""
        return list_due_refresh_candidates(
            due_within_sec=int(LEAD_MIN * 60),
            exclude_statuses=(STATUS_REAUTH, STATUS_DISABLED),
        )

    def _refresh_one(self, row: dict[str, Any]) -> str:
        """单账号 OIDC 刷新。返回 refreshed / rejected / transient。"""
        account_id = int(row["id"])
        email = str(row["email"] or "")
        log_email = email
        data, http_status = oauth_refresh(str(row["refresh_token"] or ""))
        if data and data.get("access_token"):
            update_account_tokens(
                account_id,
                str(data["access_token"]),
                str(data.get("refresh_token") or "") or None,
                int(data.get("expires_in") or 0) or None,
                reason="自动续期成功",
            )
            update_account_status_by_ids([account_id], STATUS_ACTIVE, "自动续期成功")
            self.append_log("SUCCESS", f"{log_email}·续期成功")
            logger.info(f"[续期] 已续期 {log_email}")
            return "refreshed"
        if http_status == 0:
            self.append_log("WARNING", f"{log_email}·网络失败，下轮再试")
            logger.warning(f"[续期] 网络失败跳过 {log_email}（瞬时，下轮再试）")
            return "transient"
        update_account_status_by_ids(
            [account_id],
            STATUS_REAUTH,
            f"自动续期失败：刷新被拒（HTTP {http_status}）",
        )
        self.append_log("ERROR", f"{log_email}·刷新被拒（HTTP {http_status}），标记需重登")
        logger.warning(f"[续期] 刷新被拒 {log_email} http={http_status}，标记需重登")
        return "rejected"

    def _refresh_all(self, due: list[dict[str, Any]]) -> tuple[int, int, int]:
        """最多 4 个 worker 并发续期，每个 worker 做完一个号再隔 1 秒接下一个。

        返回 (refreshed, rejected, transient)：
        - 刷新成功 → 回写 token，状态恢复 ACTIVE
        - 刷新凭据被拒（4xx）→ 标记 REAUTH，需人工重登
        - 网络失败（http=0）→ 不动，下轮再试
        """
        refreshed = rejected = transient = 0
        def on_complete(_index: int, row: dict[str, Any], kind: Any) -> None:
            nonlocal refreshed, rejected, transient
            email = str(row.get("email") or "")
            log_email = email
            if isinstance(kind, Exception):
                self.append_log(
                    "WARNING",
                    f"{log_email}·续期异常 {type(kind).__name__}，下轮再试",
                )
                logger.warning(
                    f"[续期] 异常跳过 {log_email}：{type(kind).__name__}: {kind}"
                )
                result_kind = "transient"
            else:
                result_kind = str(kind or "transient")
            with self._lock:
                if result_kind == "refreshed":
                    refreshed += 1
                elif result_kind == "rejected":
                    rejected += 1
                else:
                    transient += 1
                self._done += 1

        run_account_workers(
            due,
            self._refresh_one,
            workers=REFRESH_CONCURRENCY,
            thread_name_prefix="续期",
            should_stop=self._stop.is_set,
            on_complete=on_complete,
        )
        return refreshed, rejected, transient


# 进程内单例
auto_refresher = AutoRefresher()

"""
号池任务框架（进程内单例，镜像 push/manager.py 的任务惯例）。

- 内存任务快照 + daemon 线程；snapshot(after_log_id) 增量日志轮询
- 单任务互斥：巡检 / 重登共享同一任务槽（kind=inspect | reauth）
- 预筛跳过明细启动即可见（skipped_list）
- 探活 / 刷新均为真实上游请求，账号间随机间隔防风控
- 协作式取消：cancel 事件贯穿预筛 / 探活 / 刷新 / 重登降级各阶段

kind=inspect  探活分流：2xx 通过 / 401·403 恢复链（刷新后再探，仍失效标
              REAUTH）/ 402·429 限流配额 / 网络与 5xx 不改状态只记原因
              风控（bfs/risk）不在巡检阶段更新：grok.com 全站 Cloudflare
              挑战拦截 curl_cffi，仅注册流程的浏览器路径能拿到 botFlag，
              巡检保留注册时写入的风控值即可
kind=reauth   重登闭环：有 refresh_token 先 OIDC 刷新；被拒或无刷新凭据
              降级为 device flow 重新授权（入认证池），远端交换 token
kind=risk     风控体检：单账号无头浏览器开 grok.com，自动过 CF 挑战后
              解析 botFlagSource / botFlagDetails 回写 risk / bfs
"""

import threading
import uuid

# 并发上限（与前端并发输入框 1-20 对齐；实际按 20 线程、每线程间隔 1s 跑号）
MAX_CONCURRENCY = ACCOUNT_WORKERS
# 任务日志内存环形保留条数
_LOG_LIMIT = 500

# 探活结果分流的 HTTP 状态
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 299
_HTTP_TOKEN_INVALID = (401, 403)
_HTTP_QUOTA = 402
_HTTP_RATE_LIMIT = 429


def _clamp_concurrency(value: Any) -> int:
    """并发钳制到 1-20。"""
    try:
        return max(1, min(int(value), MAX_CONCURRENCY))
    except (TypeError, ValueError):
        return MAX_CONCURRENCY


def _who(acc: dict[str, Any]) -> str:
    """账号日志标识：完整邮箱（排查用），缺邮箱则回退 #id。"""
    email = str(acc.get("email") or "").strip()
    aid = int(acc.get("id") or 0)
    return email if email else f"#{aid}"


def _end_log(job: PoolJob, title: str) -> None:
    """任务收尾：有成功即成功，全部失败才失败。"""
    skipped = len(job.skipped_list)
    cancelled = job.cancel_event.is_set()
    if cancelled:
        level, action = "WARNING", "已取消"
    elif job.pushed > 0:
        level, action = "SUCCESS", "结束"
    else:
        level, action = "ERROR", "结束"
    pending = f" 待认证 {job.pending}" if job.kind == "reauth" else ""
    job.append_log(
        level,
        f"[任务] {title}{action} 成功 {job.pushed} 失败 {job.failed}{pending} 跳过 {skipped}",
    )


class PoolJob:
    """单次号池任务（内存态；镜像 PushJob 的 snapshot 风格）。"""

    def __init__(
        self,
        task_id: str,
        kind: str,
        account_ids: list[int],
        concurrency: int,
    ) -> None:
        self.id = task_id
        self.kind = kind  # inspect / reauth
        self.account_ids = account_ids
        self.concurrency = concurrency
        self.status = "pending"  # pending / running / done / cancelled
        self.count = 0  # 通过预筛的候选数
        self.done = 0
        self.pushed = 0  # 成功（探活通过 / 刷新成功）
        self.failed = 0
        self.pending = 0  # 重登降级：等待用户授权的账号数
        self.skipped_list: list[dict[str, Any]] = []
        # 重登降级账号的授权信息（前端据此引导用户浏览器授权）
        self.pending_list: list[dict[str, Any]] = []
        self.started_at = now_str()
        self.finished_at = ""
        self.error = ""
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._logs: list[dict[str, Any]] = []
        self._last_log_id = 0

    def append_log(self, level: str, message: str) -> None:
        """追加任务日志（环形保留 _LOG_LIMIT 条，id 单调递增供增量轮询）。"""
        with self._lock:
            self._last_log_id += 1
            self._logs.append({"id": self._last_log_id, "level": level, "message": message})
            if len(self._logs) > _LOG_LIMIT:
                self._logs = self._logs[-_LOG_LIMIT:]

    def snapshot(self, after_log_id: int = 0) -> dict[str, Any]:
        """任务快照；增量日志按 after_log_id 截取。"""
        with self._lock:
            logs = [log for log in self._logs if log["id"] > after_log_id]
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "count": self.count,
                "concurrency": self.concurrency,
                "pushed": self.pushed,
                "failed": self.failed,
                "pending": self.pending,
                "done": self.done,
                "skipped": len(self.skipped_list),
                "skipped_list": list(self.skipped_list),
                "pending_list": list(self.pending_list),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "logs": logs,
                "last_log_id": self._last_log_id,
                "progress": round(self.done / self.count * 100, 2) if self.count else 0,
            }


class PoolJobManager:
    """号池任务管理器（进程内单例）：巡检 / 重登共享任务槽。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: PoolJob | None = None
        self._worker: threading.Thread | None = None
        # 延迟导入：probe / register 自带重依赖（Camoufox 重）
        self._probe = None

    # ─── 对外接口 ─────────────────────────────────────────

    def status(self, after_log_id: int = 0) -> dict[str, Any]:
        """当前任务状态；无任务时返回 idle 占位。"""
        job = self._job
        if job is None:
            return {
                "id": None,
                "kind": "",
                "status": "idle",
                "count": 0,
                "concurrency": 0,
                "pushed": 0,
                "failed": 0,
                "pending": 0,
                "done": 0,
                "skipped": 0,
                "skipped_list": [],
                "pending_list": [],
                "started_at": None,
                "finished_at": None,
                "error": None,
                "logs": [],
                "last_log_id": 0,
                "progress": 0,
            }
        return job.snapshot(after_log_id)

    def start(
        self,
        kind: str,
        account_ids: list[int] | None,
        concurrency: int = MAX_CONCURRENCY,
    ) -> dict[str, Any]:
        """启动巡检 / 重登 / 风控任务；已有任务进行中则抛 RuntimeError。"""
        if kind not in ("inspect", "reauth", "risk"):
            raise RuntimeError(f"未知任务类型: {kind}")
        concurrency = _clamp_concurrency(concurrency)
        ids = [int(i) for i in account_ids] if account_ids else []
        # 全局互斥：其它重任务（推送/认证/注册）进行中则拒绝
        mutex_acquire("号池")

        with self._lock:
            job = self._job
            if job is not None and job.status in ("pending", "running"):
                raise RuntimeError("已有号池任务进行中，请等待完成或取消")
            job = PoolJob(
                task_id=uuid.uuid4().hex[:12],
                kind=kind,
                account_ids=ids,
                concurrency=concurrency,
            )
            self._job = job
            self._worker = threading.Thread(
                target=self._run_job, args=(job,), name=f"号池任务-{kind}", daemon=True
            )
            self._worker.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        """请求取消当前任务（协作式，由 worker 在检查点退出）。"""
        with self._lock:
            job = self._job
            if job is None or job.status not in ("pending", "running"):
                raise RuntimeError("当前没有可取消的号池任务")
            job.cancel_event.set()
        return self.status()

    # ─── 任务执行 ─────────────────────────────────────────

    @property
    def _probe_client(self):
        """探活客户端懒加载。"""
        if self._probe is None:
            self._probe = probe_client
        return self._probe

    def _run_job(self, job: PoolJob) -> None:
        try:
            if job.kind == "inspect":
                self._run_inspect(job)
            elif job.kind == "reauth":
                self._run_reauth(job)
            else:
                self._run_risk(job)
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            title = "巡检" if job.kind == "inspect" else "重登" if job.kind == "reauth" else "风控"
            job.append_log("ERROR", f"[任务] {title}异常 {job.error}")
            logger.error(f"[号池任务] {job.kind} 异常: {job.error}")
        finally:
            job.status = "cancelled" if job.cancel_event.is_set() else "done"
            job.finished_at = now_str()
            title = "巡检" if job.kind == "inspect" else "重登" if job.kind == "reauth" else "风控"
            _end_log(job, title)
            mutex_release("号池")
            logger.info(
                f"[号池任务] {job.kind} {job.id} 结束: status={job.status} "
                f"成功={job.pushed} 失败={job.failed} 待授权={job.pending} "
                f"跳过={len(job.skipped_list)}"
            )

    # ─── 公共：预筛 ─────────────────────────────────────────

    def _screen(
        self, job: PoolJob, *, require_token: bool
    ) -> list[dict[str, Any]]:
        """资格预筛：返回候选账号行；跳过账号写入 skipped_list。

        未认证账号（无 access_token）一律排除：仅可走认证（/api/pool/auth），
        巡检 / 重登 / 风控均不处理（满足“未认证账号只能执行认证”约束）。
        require_token=True（巡检）：状态为需重登(2) 亦跳过；
        require_token=False（重登 / 风控）：指定 ids 时按 id 处理（需重登账号），
        重登全量模式（未传 ids）仅处理需重登(2)状态的账号。
        跳过明细只记入 skipped_list 与文件日志，任务日志聚合为一条摘要，
        避免全量模式下逐账号刷屏、日志一次性倾泻。
        """
        by_id = {a["id"]: a for a in get_all_accounts()}
        accounts = (
            [by_id[i] for i in job.account_ids if i in by_id]
            if job.account_ids
            else list(by_id.values())
        )
        # 重登全量模式：仅需重登(2)状态的账号纳入候选
        reauth_all_only_pending = job.kind == "reauth" and not job.account_ids
        tag = "巡检" if job.kind == "inspect" else "重登"
        candidates: list[dict[str, Any]] = []
        skip_summary: dict[str, int] = {}
        for acc in accounts:
            if job.cancel_event.is_set():
                break
            aid = int(acc.get("id") or 0)
            skip_reason = ""
            # 未认证账号仅可走认证（/api/pool/auth），巡检/重登/风控一律排除
            if not str(acc.get("access_token") or "").strip():
                skip_reason = "未认证，仅可认证"
            elif int(acc.get("status") or 1) == STATUS_DISABLED:
                # 禁用账号不参与巡检/重登/风控（与自动续期 exclude DISABLED 口径一致），
                # 避免巡检探活通过后回写 ACTIVE 将禁用账号“复活”
                skip_reason = "账号已禁用"
            elif require_token and int(acc.get("status") or 1) == 2:
                # 巡检排除需重登状态（探活无意义，待重登闭环处理）
                skip_reason = "需重登"
            elif (
                reauth_all_only_pending
                and int(acc.get("status") or 1) != 2
            ):
                skip_reason = "状态无需重登"
            if skip_reason:
                job.skipped_list.append({"id": aid, "reason": skip_reason})
                skip_summary[skip_reason] = skip_summary.get(skip_reason, 0) + 1
                logger.debug(f"[{tag}] {_who(acc)} 已跳过：{skip_reason}")
                continue
            candidates.append(acc)
        if skip_summary:
            detail = "、".join(
                f"{reason} {n} 个"
                for reason, n in sorted(skip_summary.items(), key=lambda kv: -kv[1])
            )
            job.append_log(
                "INFO",
                f"[{tag}] 预筛完成：候选 {len(candidates)} 个，跳过 {detail}",
            )
        # 按注册时间倒序执行（新注册的账号优先处理）
        candidates.sort(
            key=lambda a: str(a.get("created_at") or ""), reverse=True
        )
        job.count = len(candidates)
        return candidates

    # ─── kind=inspect：巡检探活 ─────────────────────────────

    def _run_inspect(self, job: PoolJob) -> None:
        job.status = "running"
        candidates = self._screen(job, require_token=True)
        job.append_log(
            "INFO",
            f"[任务] 巡检开始 {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s",
        )
        logger.info(
            f"[号池任务] 巡检启动 候选 {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s / 任务 {job.id}"
        )
        if not candidates:
            return

        def work(acc: dict[str, Any]) -> dict[str, Any] | None:
            """单账号探活：GET /billing 探活，按状态码分流。"""
            if job.cancel_event.is_set():
                return None
            t0 = time.monotonic()
            aid = int(acc.get("id") or 0)
            logger.info(f"[巡检] {_who(acc)} 开始探活")
            result = self._probe_client.probe(acc, proxy=str(config.PROXY or "").strip())
            status = int(result.get("status_code") or 0)
            detail = str(result.get("error") or "").strip()

            # 通过：恢复 ACTIVE（风控字段保留注册时的值，巡检不更新）
            if _HTTP_OK_MIN <= status <= _HTTP_OK_MAX:
                update_account_status_by_ids([aid], STATUS_ACTIVE, "")
                exp_str = format_exp(decode_jwt_exp(str(acc.get("access_token") or "")))
                msg = f"{status} 探活通过 · 到期时间 {exp_str}"
                return {"aid": aid, "ok": True, "message": msg, "cost": elapsed_label(t0)}

            # 凭证失效（401/403）：刷新后再探
            if status in _HTTP_TOKEN_INVALID:
                new_token = self._refresh_and_reprobe(job, acc)
                if new_token:
                    update_account_status_by_ids([aid], STATUS_ACTIVE, "")
                    exp_str = format_exp(decode_jwt_exp(new_token))
                    msg = f"{status} 刷新后探活通过 · 到期时间 {exp_str}"
                    return {"aid": aid, "ok": True, "message": msg, "cost": elapsed_label(t0)}
                update_account_status_by_ids(
                    [aid], STATUS_REAUTH, "探活失败，token 失效，需重新登录"
                )
                return {"aid": aid, "ok": False, "message": f"{status} 凭证失效{'：' + detail if detail else ''}", "cost": elapsed_label(t0)}

            # 限流 / 配额
            if status == _HTTP_RATE_LIMIT:
                update_account_status_by_ids([aid], STATUS_LIMITED, f"限流（429）：{detail}")
                return {"aid": aid, "ok": False, "message": f"{status} 被限流{'：' + detail if detail else ''}", "cost": elapsed_label(t0)}
            if status == _HTTP_QUOTA:
                update_account_status_by_ids([aid], STATUS_LIMITED, f"配额不足（402）：{detail}")
                return {"aid": aid, "ok": False, "message": f"{status} 配额不足{'：' + detail if detail else ''}", "cost": elapsed_label(t0)}

            # 网络 / 5xx / 超时 / status=0：不改状态只记原因
            update_account_status_by_ids([aid], None, f"探活失败：{detail}")
            return {"aid": aid, "ok": False, "message": f"{status or 'N/A'} {detail or '网络异常'}", "cost": elapsed_label(t0)}

        self._run_concurrent(job, candidates, work)

    def _refresh_and_reprobe(self, job: PoolJob, acc: dict[str, Any]) -> str | None:
        """刷新 token 成功后用新 token 再探一次；成功返回新 access_token，失败返回 None。"""
        refresh_token = str(acc.get("refresh_token") or "").strip()
        if not refresh_token:
            logger.warning(f"[巡检] {_who(acc)} 无 refresh_token，无法刷新")
            return None
        logger.info(f"[巡检] {_who(acc)} token 失效，开始刷新")
        data, http_status = oauth_refresh(refresh_token)
        if not data:
            if http_status != 0:
                logger.warning(f"[巡检] {_who(acc)} 刷新被拒 http={http_status}")
            else:
                logger.warning(f"[巡检] {_who(acc)} 刷新网络失败")
            return None
        new_token = str(data.get("access_token") or "")
        if not new_token:
            logger.warning(f"[巡检] {_who(acc)} 刷新响应无 access_token")
            return None
        aid = int(acc.get("id") or 0)
        new_refresh = str(data.get("refresh_token") or "") or None
        update_account_tokens(
            aid,
            new_token,
            new_refresh,
            int(data.get("expires_in") or 0) or None,
            reason="探活触发续期",
        )
        logger.info(f"[巡检] {_who(acc)} token 刷新成功，开始二次探活")
        fresh = dict(acc)
        fresh["access_token"] = new_token
        if new_refresh:
            fresh["refresh_token"] = new_refresh
        result = self._probe_client.probe(fresh, proxy=str(config.PROXY or "").strip())
        reprobe_status = int(result.get("status_code") or 0)
        if _HTTP_OK_MIN <= reprobe_status <= _HTTP_OK_MAX:
            logger.success(f"[巡检] {_who(acc)} 二次探活通过 · HTTP {reprobe_status}")
            return new_token
        logger.warning(f"[巡检] {_who(acc)} 二次探活失败 · HTTP {reprobe_status or 'N/A'}")
        return None

    # ─── kind=reauth：重登闭环 ─────────────────────────────

    def _run_reauth(self, job: PoolJob) -> None:
        job.status = "running"
        candidates = self._screen(job, require_token=False)
        job.append_log(
            "INFO",
            f"[任务] 重登开始 {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s",
        )
        logger.info(
            f"[号池任务] 重登启动 候选 {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s / 任务 {job.id}"
        )
        if not candidates:
            return

        def work(acc: dict[str, Any]) -> dict[str, Any] | None:
            """单账号重登：刷新优先，失败降级为 device flow 重新授权。"""
            if job.cancel_event.is_set():
                return None
            t0 = time.monotonic()
            aid = int(acc.get("id") or 0)
            refresh_token = str(acc.get("refresh_token") or "").strip()
            logger.info(f"[重登] {_who(acc)} 开始重登")

            # 1. 有刷新凭据：OIDC 刷新（被拒才降级，网络错误不判死）
            if refresh_token:
                logger.info(f"[重登] {_who(acc)} 尝试 token 刷新")
                data, http_status = oauth_refresh(refresh_token)
                if data and data.get("access_token"):
                    update_account_tokens(
                        aid,
                        str(data["access_token"]),
                        str(data.get("refresh_token") or "") or None,
                        int(data.get("expires_in") or 0) or None,
                        reason="重登刷新成功",
                    )
                    # 只恢复状态，保留 reason 提示（不覆盖）
                    update_account_status_by_ids([aid], STATUS_ACTIVE, None)
                    logger.success(f"[重登] {_who(acc)} token 刷新成功 · HTTP {http_status} · {elapsed_label(t0)}")
                    return {"aid": aid, "ok": True, "message": "Token 已刷新", "cost": elapsed_label(t0)}
                if http_status == 0:
                    logger.warning(f"[重登] {_who(acc)} 刷新网络失败，保留原状态")
                    return {
                        "aid": aid,
                        "ok": False,
                        "message": "刷新网络失败，保留原状态",
                        "cost": elapsed_label(t0),
                    }
                logger.warning(f"[重登] {_who(acc)} 刷新被拒 http={http_status}，降级 SSO 重新认证")

            # 2. 降级：SSO 协议级重新认证（无刷新凭据 / 刷新被拒）
            #    任务内直接执行，不入认证池（与巡检同款闭环，进度由本任务日志呈现）
            sso_cookie = str(acc.get("sso_cookie") or "").strip()
            if not sso_cookie:
                logger.warning(f"[重登] {_who(acc)} 无刷新凭据且缺少 SSO cookie，无法重登")
                return {
                    "aid": aid,
                    "ok": False,
                    "message": "无刷新凭据且缺少 SSO cookie",
                    "cost": elapsed_label(t0),
                }
            token, reason = auth_with_sso(sso_cookie)
            if token and token.get("access_token"):
                update_account_tokens(
                    aid,
                    str(token["access_token"]),
                    str(token.get("refresh_token") or "") or None,
                    int(token.get("expires_in") or 0) or None,
                    reason="重登 SSO 认证成功",
                )
                update_account_status_by_ids([aid], STATUS_ACTIVE, None)
                logger.success(
                    f"[重登] {_who(acc)} SSO 重新认证成功 · {elapsed_label(t0)}"
                )
                return {
                    "aid": aid,
                    "ok": True,
                    "message": "SSO 重新认证成功",
                    "cost": elapsed_label(t0),
                }
            # 失败保留需重登状态，reason 写入便于排查
            update_account_status_by_ids([aid], STATUS_REAUTH, reason)
            logger.warning(
                f"[重登] {_who(acc)} SSO 重新认证失败：{reason} · {elapsed_label(t0)}"
            )
            return {
                "aid": aid,
                "ok": False,
                "message": f"SSO 重新认证失败：{reason}",
                "cost": elapsed_label(t0),
            }

        self._run_concurrent(job, candidates, work)

    # ─── kind=risk：风控体检 ─────────────────────────────

    def _run_risk(self, job: PoolJob) -> None:
        """单账号风控体检：无头 Camoufox 开 grok.com，自动过 CF 挑战后解析 botFlag。

        仅支持单账号（前端按行触发，不做批量）；无 SSO cookie 跳过。
        """
        job.status = "running"
        candidates = self._screen(job, require_token=False)
        job.append_log("INFO", f"[任务] 风控体检开始 {job.count} 个")
        logger.info(f"[号池任务] 风控启动 候选 {job.count} 个 / 任务 {job.id}")
        if not candidates:
            return

        # 风控体检必须串行（浏览器重资源 + CF 挑战易触发风控），单账号场景无需并发
        for acc in candidates:
            if job.cancel_event.is_set():
                break
            result = self._risk_one(acc)
            if result is None:
                continue
            job.done += 1
            body = str(result.get("message") or "")
            if result.get("ok"):
                job.pushed += 1
                job.append_log("SUCCESS", f"[风控] {_who(acc)} {body}")
            else:
                job.failed += 1
                job.append_log("ERROR", f"[风控] {_who(acc)} {body}")

    def _risk_one(self, acc: dict[str, Any]) -> dict[str, Any] | None:
        """单账号浏览器风控体检：注入 SSO → grok.com → 过 CF → 解析 botFlag 回写。

        返回结果 dict（含 aid / ok / message）；账号无 SSO 或浏览器异常返回 None（跳过计数）。
        """
        # 局部导入：Camoufox 重依赖，避免模块加载时拉起
        from camoufox.sync_api import Camoufox

        from core import config
        from workflow.register import check_account_risk

        aid = int(acc.get("id") or 0)
        email = str(acc.get("email") or "")
        sso = str(acc.get("sso_cookie") or "").strip()
        if not sso:
            logger.warning(f"[风控] {_who(acc)} 无 SSO cookie，跳过")
            return None

        t0 = time.monotonic()
        logger.info(f"[风控] {_who(acc)} 开始风控体检（无头浏览器）")
        kwargs: dict[str, Any] = {
            "headless": True,
            "humanize": True,
            "geoip": True,
            "locale": ["en-US", "en"],
        }
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}

        try:
            with Camoufox(**kwargs) as browser:
                context = browser.new_context()
                # 注入 SSO cookie 到 grok.com 域
                payload = []
                for name in ("sso", "sso-rw"):
                    for domain in (".grok.com", "grok.com"):
                        payload.append({
                            "name": name, "value": sso, "domain": domain,
                            "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax",
                        })
                context.add_cookies(payload)
                page = context.new_page()
                # check_account_risk 内部完成 SSO 注入 + 导航 + CF 挑战 + 轮询 botFlag
                bfs, details = check_account_risk(page)
                if bfs is None:
                    logger.warning(f"[风控] {_who(acc)} 未解析到风控字段 · {elapsed_label(t0)}")
                    return {"aid": aid, "ok": False, "message": f"未解析到风控字段 · {elapsed_label(t0)}"}

                update_risk(email, bfs, details or None, now_str())
                tag = "风控正常" if bfs not in (1, 2) else "风控被标记"
                extra = f" details={details}" if details else ""
                logger.success(f"[风控] {_who(acc)} {tag} bfs={bfs}{extra} · {elapsed_label(t0)}")
                return {"aid": aid, "ok": True, "message": f"bfs={bfs} · {elapsed_label(t0)}"}
        except Exception as exc:
            logger.error(f"[风控] {_who(acc)} 浏览器异常: {type(exc).__name__}: {exc} · {elapsed_label(t0)}")
            return {"aid": aid, "ok": False, "message": f"浏览器异常: {type(exc).__name__} · {elapsed_label(t0)}"}

    # ─── 公共：并发执行与结果汇总 ─────────────────────────

    def _run_concurrent(self, job: PoolJob, candidates, work) -> None:
        """20 个 worker 并发执行 work(acc)，每个 worker 做完一个号再隔 1 秒接下一个。"""
        label = "巡检" if job.kind == "inspect" else "重登" if job.kind == "reauth" else "风控"

        def on_complete(_index: int, acc: dict[str, Any], result: Any) -> None:
            level = "ERROR"
            message = ""
            with job._lock:
                if isinstance(result, Exception):
                    job.failed += 1
                    job.done += 1
                    message = f"[{label}] {_who(acc)} {label}失败：{type(result).__name__}: {result}"
                elif result is None:
                    return
                else:
                    job.done += 1
                    cost = result.get("cost")
                    suffix = f" · {cost}" if cost else ""
                    body = str(result.get("message") or "")
                    if result.get("ok"):
                        job.pushed += 1
                        level = "SUCCESS"
                    elif result.get("pending"):
                        job.pending += 1
                        level = "INFO"
                    else:
                        job.failed += 1
                    message = f"[{label}] {_who(acc)} {body}{suffix}"
            job.append_log(level, message)
            if isinstance(result, Exception):
                logger.error(message)

        run_account_workers(
            candidates,
            work,
            workers=ACCOUNT_WORKERS,
            thread_name_prefix="号池",
            should_stop=job.cancel_event.is_set,
            on_complete=on_complete,
        )


# 进程内单例（模块级，对齐 push.manager.push_manager 的用法）
pool_job_manager = PoolJobManager()
