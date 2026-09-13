"""号池任务：探活、认证池消化、巡检/重登。"""

from __future__ import annotations

"""
号池账号上游活体探测客户端。

- HTTP 直连 curl_cffi（Chrome TLS 指纹），代理走代理池
- 探活走 GET /billing 验证 token 有效性（不计费、不消耗生成额度）
- 降智判定不再存在：网关被动审计（gateway/quality.py）已于 2026-09-13 移除，
  accounts 的 quality_* 字段不再被写入（仅保留历史值与手动清理接口）
- 网络失败自动重试一次
"""

import json
import threading
import time
from datetime import datetime
from typing import Any

from curl_cffi import requests

from core.config import UPSTREAM_BASE
from core.logger import logger
from core.mutex import acquire as mutex_acquire, release as mutex_release
from core.util import (
    ACCOUNT_WORKER_GAP_SEC,
    ACCOUNT_WORKERS,
    decode_jwt_exp,
    elapsed_label,
    now_str,
    run_account_workers,
)
from db import (
    LIMITED_HOLD_SECONDS,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_LIMITED,
    STATUS_REAUTH,
    get_all_accounts,
    touch_inspected,
    update_account_status_by_ids,
    update_account_tokens,
)
from gateway.grok import _identity_headers
from workflow.oauth import auth_with_sso
from workflow.oauth import refresh_token as oauth_refresh

# 临期续期提前量：探活通过后 token 剩余寿命低于该值即续期
_RENEW_LEAD_SEC = 10 * 60

_PROBE_CONNECT_TIMEOUT = 20
_PROBE_READ_TIMEOUT = 60
_PROBE_RETRY_DELAY = 1.0

_thread_local = threading.local()


def _limited_hold_expired(acc: dict[str, Any]) -> bool:
    """限额账号是否已冻满 24h（以 updated_at 为冻结起点；空/非法视为已过期，放行探活）。"""
    raw = str(acc.get("updated_at") or "").strip()
    if not raw:
        return True
    try:
        marked = datetime.fromisoformat(raw)
    except ValueError:
        return True
    held = datetime.now(marked.tzinfo).timestamp() - marked.timestamp()
    return held >= LIMITED_HOLD_SECONDS


class ProbeClient:
    """上游探活客户端（线程级 Session 复用，代理变更时重建）。"""

    def __init__(self, *, base_url: str = "", model: str = "") -> None:
        self.base_url = str(base_url or UPSTREAM_BASE).rstrip("/")

    def _session(self, proxy: str) -> requests.Session:
        pair = getattr(_thread_local, "session_pair", None)
        if pair is None or pair[0] != proxy:
            session = requests.Session(impersonate="chrome")
            session.proxies = {"http": proxy, "https": proxy}
            _thread_local.session_pair = (proxy, session)
        return _thread_local.session_pair[1]

    def probe(self, account: dict[str, Any], proxy: str = "") -> dict[str, Any]:
        """单账号探活：GET /billing 验证 token 有效性（不计费、不做降智判定）。

        返回 dict：
          status_code     200=有效 / 401·403=失效 / 402=配额 / 429=限流 / 0=网络异常
          error           失败原因摘要
          elapsed_ms      请求耗时（毫秒）
        """
        result: dict[str, Any] = {"status_code": 0, "error": "", "elapsed_ms": 0}
        from core import proxypool

        use_proxy = str(proxy or proxypool.pick() or "").strip()
        email = str(account.get("email") or "").strip()
        if not use_proxy:
            result["error"] = "探活强制走代理，未配置 proxy"
            logger.error(f"[探活] {email} 未配置代理，跳过探活")
            return result
        session = self._session(use_proxy)
        headers = _identity_headers()
        headers["Authorization"] = f"Bearer {account.get('access_token') or ''!s}"
        headers["Accept"] = "application/json"
        t0 = time.monotonic()
        response = None
        for attempt in range(2):
            try:
                logger.debug(f"[探活] {email} 请求 GET /billing · 尝试 {attempt + 1}/2")
                response = session.get(
                    f"{self.base_url}/billing?format=credits",
                    headers=headers,
                    timeout=(_PROBE_CONNECT_TIMEOUT, _PROBE_READ_TIMEOUT),
                )
                break
            except requests.RequestsError as exc:
                if attempt == 0:
                    logger.warning(f"[探活] {email} 请求异常（重试中）: {type(exc).__name__}: {exc}")
                    time.sleep(_PROBE_RETRY_DELAY)
                    continue
                detail = str(exc)[:160]
                if "timed out" in detail or "Timeout" in detail or "Connection" in detail:
                    detail = "网络无响应"
                result["error"] = detail
                logger.error(f"[探活] {email} 请求失败: {type(exc).__name__}: {detail}")
                return result

        status = int(response.status_code)
        result["status_code"] = status
        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        if not (200 <= status < 300):
            err = _simplify_error(status, response.text or "")
            result["error"] = err
            logger.warning(f"[探活] {email} 探活失败 · HTTP {status} {err}")
            return result
        result["error"] = "探活通过"
        logger.debug(f"[探活] {email} 探活通过 · HTTP {status} · {result['elapsed_ms']}ms")
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
号池任务框架（进程内单例，镜像 push/manager.py 的任务惯例）。

- 内存任务快照 + daemon 线程；snapshot(after_log_id) 增量日志轮询
- 单任务互斥：巡检 / 重登共享同一任务槽（kind=inspect | reauth）
- 预筛跳过明细启动即可见（skipped_list）
- 探活 / 刷新均为真实上游请求，账号间随机间隔防风控
- 协作式取消：cancel 事件贯穿预筛 / 探活 / 刷新 / 重登降级各阶段

kind=inspect  GET /billing 探活验证 token：2xx 恢复 ACTIVE，临期（≤10min）自动续期 /
              401·403 恢复链（刷新后再探，仍失效标 REAUTH）/
              402·429 限流配额 / 网络与 5xx 不改状态只记原因
              续期失败不判死（探活已通过，旧 token 仍可用，下轮重试）；
              降智判定不在巡检（网关被动审计已移除，不再有任何写入方）
kind=reauth   重登闭环：有 refresh_token 先 OIDC 刷新；被拒或无刷新凭据
              降级为 device flow 重新授权（入认证池），远端交换 token
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
        """启动巡检 / 重登任务；已有任务进行中则抛 RuntimeError。"""
        if kind not in ("inspect", "reauth"):
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
            else:
                self._run_reauth(job)
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            title = "巡检" if job.kind == "inspect" else "重登"
            job.append_log("ERROR", f"[任务] {title}异常 {job.error}")
            logger.error(f"[号池任务] {job.kind} 异常: {job.error}")
        finally:
            job.status = "cancelled" if job.cancel_event.is_set() else "done"
            job.finished_at = now_str()
            title = "巡检" if job.kind == "inspect" else "重登"
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
        巡检 / 重登均不处理（满足“未认证账号只能执行认证”约束）。
        require_token=True（巡检）：状态为需重登(2) 亦跳过；
        require_token=False（重登）：指定 ids 时按 id 处理（需重登账号），
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
            # 未认证账号仅可走认证（/api/pool/auth），巡检/重登一律排除
            if not str(acc.get("access_token") or "").strip():
                skip_reason = "未认证，仅可认证"
            elif int(acc.get("status") or 1) == STATUS_DISABLED:
                # 禁用账号不参与巡检/重登（与自动续期 exclude DISABLED 口径一致），
                # 避免巡检探活通过后回写 ACTIVE 将禁用账号“复活”
                skip_reason = "账号已禁用"
            elif require_token and int(acc.get("status") or 1) == 2:
                # 巡检排除需重登状态（探活无意义，待重登闭环处理）
                skip_reason = "需重登"
            elif (
                require_token
                and int(acc.get("status") or 1) == STATUS_LIMITED
                and not _limited_hold_expired(acc)
            ):
                # 限额账号 24h 冻结期内不探活：/billing 对额度耗尽仍返回 2xx，
                # 提前捞回会让死号立刻回池再吃 429；冻满 24h 后正常探活恢复
                skip_reason = "限额冻结中（未到 24h）"
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
            """单账号探活：GET /billing 验证 token，按状态码分流并触发恢复链路。"""
            if job.cancel_event.is_set():
                return None
            t0 = time.monotonic()
            aid = int(acc.get("id") or 0)
            logger.info(f"[巡检] {_who(acc)} 开始探活")
            from core import proxypool

            result = self._probe_client.probe(acc, proxy=proxypool.pick())
            status = int(result.get("status_code") or 0)
            detail = str(result.get("error") or "").strip()

            # 通过：恢复 ACTIVE，刷新探活时间，并对临期 token 续期
            # （降智判定已移交网关被动审计，续期已从自动续期 daemon 并入巡检）
            if _HTTP_OK_MIN <= status <= _HTTP_OK_MAX:
                update_account_status_by_ids([aid], STATUS_ACTIVE, "")
                touch_inspected(aid)
                # 临期续期：剩余寿命 ≤ _RENEW_LEAD_SEC 且有刷新凭据时续期；
                # 续期失败不判死（探活已通过，旧 token 仍可用），下轮巡检重试
                renew_note = ""
                refresh_token = str(acc.get("refresh_token") or "").strip()
                exp = decode_jwt_exp(str(acc.get("access_token") or ""))
                if refresh_token and exp is not None and exp - time.time() <= _RENEW_LEAD_SEC:
                    data, http_status = oauth_refresh(refresh_token)
                    if data and data.get("access_token"):
                        update_account_tokens(
                            aid,
                            str(data["access_token"]),
                            str(data.get("refresh_token") or "") or None,
                            int(data.get("expires_in") or 0) or None,
                            reason="巡检临期续期",
                        )
                        renew_note = " · 已续期"
                        logger.info(f"[巡检] {_who(acc)} 临期续期成功")
                    else:
                        renew_note = f" · 续期失败({http_status or '网络'})"
                        logger.warning(
                            f"[巡检] {_who(acc)} 临期续期失败 http={http_status}"
                            f"（token 仍可用，下轮重试）"
                        )
                return {
                    "aid": aid,
                    "ok": True,
                    "message": f"探活通过{renew_note}",
                    "cost": elapsed_label(t0),
                }

            # 凭证失效（401/403）：刷新后再探
            if status in _HTTP_TOKEN_INVALID:
                refreshed = self._refresh_and_reprobe(job, acc)
                if refreshed:
                    update_account_status_by_ids([aid], STATUS_ACTIVE, "")
                    return {
                        "aid": aid,
                        "ok": True,
                        "message": f"{status} 刷新后探活通过",
                        "cost": elapsed_label(t0),
                    }
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

    def _refresh_and_reprobe(
        self, job: PoolJob, acc: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """刷新 token 成功后用新 token 再探一次；成功返回 (access_token, 探活结果)。"""
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
        from core import proxypool

        result = self._probe_client.probe(fresh, proxy=proxypool.pick())
        reprobe_status = int(result.get("status_code") or 0)
        if _HTTP_OK_MIN <= reprobe_status <= _HTTP_OK_MAX:
            touch_inspected(aid)
            logger.success(
                f"[巡检] {_who(acc)} 二次探活通过 · HTTP {reprobe_status} · "
                f"{int(result.get('elapsed_ms') or 0)}ms"
            )
            return new_token, result
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

    # ─── 公共：并发执行与结果汇总 ─────────────────────────

    def _run_concurrent(self, job: PoolJob, candidates, work) -> None:
        """20 个 worker 并发执行 work(acc)，每个 worker 做完一个号再隔 1 秒接下一个。"""
        label = "巡检" if job.kind == "inspect" else "重登"

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
