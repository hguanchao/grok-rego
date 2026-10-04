"""限额巡检冻结与任务启动互斥。

用临时库和真实号池函数。探活客户端被换成内存替身，避免打上游。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import db
from api.pool_jobs import PoolJob, PoolJobManager, auth_pool_state, kick_auth_pool
from api.push import PushJob, PushManager
from core import mutex
from core.util import now_dt
from db import (
    STATUS_ACTIVE,
    STATUS_LIMITED,
    get_account_by_id,
    get_pool_stats,
    init_db,
    save_account,
    update_account_status,
)
from workflow.jobs import JobManager, RegisterJob


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr("core.config.DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr("core.config.DB_DIR", str(tmp_path))
    init_db()
    mutex._active.clear()
    yield
    mutex._active.clear()


def _account(email: str, *, status: int, token: str = "tok", refresh: str = "ref") -> int:
    account_id = save_account(
        email,
        "pw",
        "A",
        "B",
        "sso=1",
        access_token=token,
        refresh_token=refresh,
        status=status,
        reason="额度耗尽" if status == STATUS_LIMITED else "",
    )
    if status == STATUS_LIMITED:
        update_account_status(email, STATUS_LIMITED, "额度耗尽（冻结 24h）")
    return account_id


def _set_limited_at(account_id: int, when: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE accounts SET limited_at=? WHERE id=?",
            (when, account_id),
        )
        conn.commit()


class _Probe:
    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls = 0

    def probe(self, account, proxy=""):
        code = self.codes[min(self.calls, len(self.codes) - 1)]
        self.calls += 1
        return {
            "status_code": code,
            "error": "探活通过" if 200 <= code < 300 else f"HTTP {code}",
            "elapsed_ms": 1,
            "retry_after": None,
            "quota_reason": None,
        }


def _run_probe(monkeypatch, account_id: int, codes: list[int]) -> None:
    monkeypatch.setattr("api.pool_jobs.ACCOUNT_WORKER_GAP_SEC", 0)
    monkeypatch.setattr("core.proxypool.pick", lambda *args, **kwargs: "http://127.0.0.1:9")
    monkeypatch.setattr(
        "api.pool_jobs.oauth_refresh",
        lambda token: ({"access_token": "new-tok", "refresh_token": "new-ref", "expires_in": 3600}, 200),
    )
    manager = PoolJobManager()
    manager._probe = _Probe(codes)
    job = PoolJob("t-hold", "inspect", [account_id], 1)
    manager._run_inspect(job)


def test_limited_inside_24h_is_inspected_and_billing_2xx_keeps_hold(temp_db, monkeypatch):
    account_id = _account("held@example.com", status=STATUS_LIMITED)
    before = get_account_by_id(account_id)
    assert before is not None
    limited_at = before["limited_at"]
    assert limited_at

    manager = PoolJobManager()
    job = PoolJob("t-screen", "inspect", [account_id], 1)
    candidates = manager._screen(job, require_token=True)
    assert [row["id"] for row in candidates] == [account_id]
    assert get_pool_stats()["task_counts"]["inspect"] == 1

    _run_probe(monkeypatch, account_id, [200])
    after = get_account_by_id(account_id)
    assert after is not None
    assert after["status"] == STATUS_LIMITED
    assert after["limited_at"] == limited_at


def test_limited_refresh_2xx_inside_24h_does_not_reset_hold(temp_db, monkeypatch):
    account_id = _account("refresh@example.com", status=STATUS_LIMITED)
    before = get_account_by_id(account_id)
    assert before is not None
    limited_at = before["limited_at"]

    _run_probe(monkeypatch, account_id, [401, 200])
    after = get_account_by_id(account_id)
    assert after is not None
    assert after["status"] == STATUS_LIMITED
    assert after["limited_at"] == limited_at
    assert after["access_token"] == "new-tok"


def test_limited_after_24h_billing_2xx_restores_active(temp_db, monkeypatch):
    account_id = _account("due@example.com", status=STATUS_LIMITED)
    old = (now_dt() - timedelta(hours=25)).isoformat(timespec="seconds")
    _set_limited_at(account_id, old)

    _run_probe(monkeypatch, account_id, [200])
    after = get_account_by_id(account_id)
    assert after is not None
    assert after["status"] == STATUS_ACTIVE
    assert after["limited_at"] == ""


def test_pool_start_rejects_running_job_and_releases_mutex(temp_db):
    manager = PoolJobManager()
    manager._job = PoolJob("busy", "inspect", [1], 1)
    manager._job.status = "running"
    with pytest.raises(RuntimeError, match="已有号池任务"):
        manager.start("inspect", [9])
    assert mutex.active() == []
    mutex.acquire("推送")
    mutex.release("推送")
    assert mutex.active() == []


def test_pool_thread_start_failure_clears_job_and_mutex(temp_db, monkeypatch):
    import threading

    real_start = threading.Thread.start

    def boom(self):
        if getattr(self, "name", "").startswith("号池任务"):
            raise RuntimeError("线程没起来")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", boom)
    manager = PoolJobManager()
    with pytest.raises(RuntimeError, match="线程没起来"):
        manager.start("inspect", [9])
    assert manager.status()["status"] == "idle"
    assert mutex.active() == []
    monkeypatch.setattr(threading.Thread, "start", real_start)
    snap = manager.start("inspect", [])
    assert snap["id"]
    assert mutex.active() == ["号池"] or snap["status"] in ("done", "cancelled")
    worker = manager._worker
    if worker is not None:
        worker.join(timeout=3)
    assert mutex.active() == []


def test_push_start_rejects_running_job_and_releases_mutex(temp_db, monkeypatch):
    monkeypatch.setattr("core.config.G2A_BASE_URL", "http://g2a.example")
    monkeypatch.setattr("core.config.G2A_USERNAME", "u")
    monkeypatch.setattr("core.config.G2A_PASSWORD", "p")
    manager = PushManager()
    manager._job = PushJob("busy", ["g2a"], [1], 1)
    manager._job.status = "running"
    with pytest.raises(RuntimeError, match="已有推送任务"):
        manager.start(["g2a"], [9])
    assert mutex.active() == []
    mutex.acquire("号池")
    mutex.release("号池")


def test_push_thread_start_failure_clears_job_and_mutex(temp_db, monkeypatch):
    import threading

    monkeypatch.setattr("core.config.G2A_BASE_URL", "http://g2a.example")
    monkeypatch.setattr("core.config.G2A_USERNAME", "u")
    monkeypatch.setattr("core.config.G2A_PASSWORD", "p")
    real_start = threading.Thread.start

    def boom(self):
        if getattr(self, "name", "") == "推送任务":
            raise RuntimeError("线程没起来")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", boom)
    manager = PushManager()
    with pytest.raises(RuntimeError, match="线程没起来"):
        manager.start(["g2a"], [9])
    assert manager.status()["status"] == "idle"
    assert mutex.active() == []
    monkeypatch.setattr(threading.Thread, "start", real_start)
    snap = manager.start(["g2a"], [])
    assert snap["id"]
    worker = manager._worker
    if worker is not None:
        worker.join(timeout=3)
    assert mutex.active() == []
    import threading

    monkeypatch.setattr("core.config.G2A_BASE_URL", "http://g2a.example")
    monkeypatch.setattr("core.config.G2A_USERNAME", "u")
    monkeypatch.setattr("core.config.G2A_PASSWORD", "p")
    real_start = threading.Thread.start

    def boom(self):
        if getattr(self, "name", "") == "推送任务":
            raise RuntimeError("线程没起来")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", boom)
    manager = PushManager()
    with pytest.raises(RuntimeError, match="线程没起来"):
        manager.start(["g2a"], [9])
    assert manager.status()["status"] == "idle"
    assert mutex.active() == []
    monkeypatch.setattr(threading.Thread, "start", real_start)
    snap = manager.start(["g2a"], [])
    assert snap["id"]
    worker = manager._worker
    if worker is not None:
        worker.join(timeout=3)
    assert mutex.active() == []


def test_register_thread_start_failure_clears_job_and_mutex(temp_db, monkeypatch):
    import threading

    monkeypatch.setattr("core.config.MAIL_PROVIDER", "tempmail")
    monkeypatch.setattr("core.config.TEMPMAIL_API_BASE", "http://mail.example")
    monkeypatch.setattr("workflow.jobs.register_wf.run_signups", lambda **kwargs: None)
    monkeypatch.setattr("workflow.jobs.register_wf.run_auth_pool", lambda **kwargs: None)
    real_start = threading.Thread.start

    def boom(self):
        if getattr(self, "name", "") == "主控":
            raise RuntimeError("线程没起来")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", boom)
    manager = JobManager()
    with pytest.raises(RuntimeError, match="线程没起来"):
        manager.start(count=1, threads=1, headless=True)
    assert manager.get_status()["status"] == "idle"
    assert mutex.active() == []
    monkeypatch.setattr(threading.Thread, "start", real_start)
    snap = manager.start(count=1, threads=1, headless=True)
    assert snap["id"]
    worker = manager._worker
    if worker is not None:
        worker.join(timeout=3)
    assert mutex.active() == []


def test_auth_kick_when_already_running_releases_mutex_and_keeps_flag(temp_db):
    import api.pool_jobs as pool_jobs

    pool_jobs._auth_pool_running = True
    try:
        kick_auth_pool()
        assert mutex.active() == []
        assert auth_pool_state()["running"] is True
    finally:
        pool_jobs._auth_pool_running = False


def test_auth_kick_thread_start_failure_releases_mutex(temp_db, monkeypatch):
    def boom(self):
        raise RuntimeError("线程没起来")

    monkeypatch.setattr("api.pool_jobs.threading.Thread.start", boom)
    with pytest.raises(RuntimeError, match="线程没起来"):
        kick_auth_pool()
    assert mutex.active() == []
    assert auth_pool_state()["running"] is False
    mutex.acquire("号池")
    mutex.release("号池")


def test_register_start_rejects_running_job_and_releases_mutex(temp_db, monkeypatch):
    monkeypatch.setattr("core.config.MAIL_PROVIDER", "tempmail")
    monkeypatch.setattr("core.config.TEMPMAIL_API_BASE", "http://mail.example")
    manager = JobManager()
    manager._job = RegisterJob(
        id="busy",
        status="running",
        count=1,
        threads=1,
        headless=True,
        mail_provider="tempmail",
    )
    with pytest.raises(RuntimeError, match="已有注册任务"):
        manager.start(count=1, threads=1, headless=True)
    assert mutex.active() == []
    mutex.acquire("认证")
    mutex.release("认证")
