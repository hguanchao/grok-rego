"""
注册任务状态机。

单例 JobManager：同时只允许一个批量注册任务；日志环形缓冲供前端轮询。
"""

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.logger import logger
from core.util import now_iso
from workflow import register as register_wf

# 单任务日志条数上限
_MAX_LOGS = 2000


def _now() -> str:
    """当前北京时间 ISO 字符串（YYYY-MM-DDTHH:MM:SS）。"""
    return now_iso()


def validate_mail_ready() -> None:
    """启动前校验当前邮箱配置；失败抛 ValueError（API 映射 400）。"""
    from core import config

    provider = (config.MAIL_PROVIDER or "cf").strip().lower()
    if provider == "yyds":
        if not (config.YYDS_API_BASE or "").strip():
            raise ValueError("请先配置 YYDS API 地址")
        if not (config.YYDS_API_KEY or "").strip():
            raise ValueError("请先配置 YYDS API 密钥")
        return
    if not (config.CF_API_BASE or "").strip():
        raise ValueError("请先配置 Cloudflare 邮箱 API 地址")
    if not config.CF_DOMAINS:
        raise ValueError("请先配置 Cloudflare 邮箱域名")


@dataclass
class RegisterJob:
    """一次批量注册任务快照。"""

    id: str
    status: str = "pending"  # pending|running|stopping|completed|cancelled|failed
    count: int = 1
    threads: int = 1
    headless: bool = True
    mail_provider: str = "cf"
    success: int = 0
    failed: int = 0
    denied: int = 0  # 风控 bfs in (1,2)
    done: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_MAX_LOGS))
    _log_seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, level: str, message: str, thread: str | None = None) -> None:
        """追加一条任务日志。"""
        with self._lock:
            self._log_seq += 1
            self.logs.append(
                {
                    "id": self._log_seq,
                    "ts": _now(),
                    "level": level,
                    "message": message,
                    "thread": thread,
                }
            )

    def snapshot(self, after_log_id: int = 0) -> dict[str, Any]:
        """序列化任务状态；可选只返回 after_log_id 之后的日志。"""
        with self._lock:
            logs = [item for item in self.logs if item["id"] > after_log_id]
            last_id = self._log_seq
            remaining = max(0, self.count - self.done)
            running = (
                min(self.threads, remaining)
                if self.status in ("running", "stopping") and remaining
                else 0
            )
            return {
                "id": self.id,
                "status": self.status,
                "count": self.count,
                "threads": self.threads,
                "headless": self.headless,
                "mail_provider": self.mail_provider,
                "success": self.success,
                "failed": self.failed,
                "denied": self.denied,
                "done": self.done,
                "running": running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "logs": logs,
                "last_log_id": last_id,
                "progress": round(self.done / self.count * 100, 2) if self.count else 0,
            }


class JobManager:
    """全局注册任务管理器（进程内单例）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: RegisterJob | None = None
        self._worker: threading.Thread | None = None
        self._log_sink_id: int | None = None

    def current(self) -> RegisterJob | None:
        return self._job

    def get_status(self, after_log_id: int = 0) -> dict[str, Any]:
        """当前任务状态；无任务时返回 idle 占位。"""
        job = self._job
        if job is None:
            return {
                "id": None,
                "status": "idle",
                "count": 0,
                "threads": 0,
                "headless": True,
                "mail_provider": "",
                "success": 0,
                "failed": 0,
                "denied": 0,
                "done": 0,
                "running": 0,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "logs": [],
                "last_log_id": 0,
                "progress": 0,
            }
        return job.snapshot(after_log_id=after_log_id)

    def start(
        self,
        count: int = 1,
        threads: int = 1,
        headless: bool = True,
    ) -> dict[str, Any]:
        """启动批量注册；已有运行中任务则抛错。"""
        count = max(1, min(int(count), 100))
        threads = max(1, min(int(threads), 20))
        validate_mail_ready()

        with self._lock:
            if self._job is not None and self._job.status in ("pending", "running", "stopping"):
                raise RuntimeError("已有注册任务进行中，请先停止或等待完成")
            from core import config

            job = RegisterJob(
                id=uuid.uuid4().hex[:12],
                status="pending",
                count=count,
                threads=threads,
                headless=bool(headless),
                mail_provider=config.MAIL_PROVIDER,
            )
            self._job = job
            register_wf.clear_cancel()
            self._attach_log_sink(job)
            worker = threading.Thread(
                target=self._run_job,
                args=(job,),
                name="主控",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        return self.get_status()

    def stop(self) -> dict[str, Any]:
        """请求停止当前任务。"""
        with self._lock:
            job = self._job
            if job is None or job.status not in ("pending", "running"):
                raise RuntimeError("当前没有可停止的注册任务")
            job.status = "stopping"
            job.append_log("WARNING", "[任务] 收到停止请求，等待当前步骤结束后退出")
            register_wf.request_cancel()
        return self.get_status()

    def clear_logs(self) -> dict[str, Any]:
        """清空当前任务日志缓冲（不中断任务）。"""
        job = self._job
        if job is None:
            return self.get_status()
        with job._lock:
            job.logs.clear()
            # 保留序号单调递增，避免前端 after 游标回退
        job.append_log("INFO", "[任务] 日志已清空")
        return self.get_status()

    def _is_relevant_log(self, thread_name: str | None, text: str) -> bool:
        """只保留主控 / 注册线程 / 认证池相关日志，其余线程日志一律过滤。"""
        t = (thread_name or "").strip()
        if not t or t == "主控":
            return True
        if "注册线程" in t or "ThreadPoolExecutor" in t:
            return True
        # 认证池消化线程或日志内容含认证池/认证阶段标记
        return "认证池" in t or "认证池" in text or text.startswith("[认证]")

    def _attach_log_sink(self, job: RegisterJob) -> None:
        """把 loguru 输出镜像到任务日志缓冲。"""
        self._detach_log_sink()

        def _sink(message: Any) -> None:
            record = message.record
            level = record["level"].name
            text = record["message"]
            thread_name = record["thread"].name if record.get("thread") else None
            # 过滤过吵的 http 访问日志
            if text.startswith("[API] ") and "GET /api/register" in text:
                return
            # 只保留主控 / 注册线程 / 认证池日志，其余一律不显示
            if not self._is_relevant_log(thread_name, text):
                return
            job.append_log(level, text, thread=thread_name)

        self._log_sink_id = logger.add(
            _sink,
            level="INFO",
            format="{message}",
            enqueue=False,
        )

    def _detach_log_sink(self) -> None:
        if self._log_sink_id is not None:
            try:
                logger.remove(self._log_sink_id)
            except ValueError:
                pass
            self._log_sink_id = None

    def _run_job(self, job: RegisterJob) -> None:
        """后台线程：批量注册 + 串行认证池。"""
        job.status = "running"
        job.started_at = _now()
        job.append_log(
            "INFO",
            f"[任务] 启动 {job.count} 账号 / {job.threads} 线程",
        )

        def on_result(ok: bool, email: str | None, risk_bfs: int | None) -> None:
            with job._lock:
                job.done += 1
                if ok:
                    job.success += 1
                    if risk_bfs in (1, 2):
                        job.denied += 1
                else:
                    job.failed += 1

        try:
            register_wf.run_signups(
                count=job.count,
                threads=job.threads,
                headless=job.headless,
                on_result=on_result,
            )
            if not register_wf.is_cancelled():
                register_wf.run_auth_pool(stop_when=register_wf.is_cancelled)
            cancelled = register_wf.is_cancelled()
            with job._lock:
                if cancelled:
                    job.status = "cancelled"
                elif job.success > 0:
                    job.status = "completed"
                else:
                    job.status = "failed"
                job.finished_at = _now()
            # 有一个账号成功即成功；全部失败才打失败
            if cancelled:
                end_level = "WARNING"
            elif job.success > 0:
                end_level = "SUCCESS"
            else:
                end_level = "ERROR"
            job.append_log(
                end_level,
                f"[任务] 结束 成功 {job.success} 失败 {job.failed} 拒绝 {job.denied}",
            )
        except Exception as e:
            with job._lock:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.finished_at = _now()
            job.append_log("ERROR", f"[任务] 异常 {job.error}")
            logger.exception("[任务] 注册任务失败")
        finally:
            self._detach_log_sink()
            register_wf.clear_cancel()


# 进程级单例
manager = JobManager()