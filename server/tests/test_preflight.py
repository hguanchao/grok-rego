"""注册前预检只检查代理、注册入口和邮箱 API，不再开浏览器过人机。"""

from core import util
from workflow import register as register_wf


def setup_function() -> None:
    util.clear_cancel()


def teardown_function() -> None:
    util.clear_cancel()


def test_preflight_check_passes_on_http_only(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(register_wf, "_check_proxy", lambda: order.append("proxy") or True)
    monkeypatch.setattr(
        register_wf, "_check_reachable", lambda url, label: order.append(label) or True
    )
    monkeypatch.setattr(register_wf, "_mail_base_url", lambda: "https://mail.example")
    monkeypatch.setattr(register_wf, "human_describe", lambda: "sim=off")
    assert register_wf.preflight_check(headless=False) is True
    assert order == ["proxy", "注册入口", "邮箱服务"]


def test_preflight_check_fails_when_http_fails(monkeypatch):
    monkeypatch.setattr(register_wf, "_check_proxy", lambda: True)
    monkeypatch.setattr(register_wf, "_check_reachable", lambda url, label: False)
    monkeypatch.setattr(register_wf, "_mail_base_url", lambda: "https://mail.example")
    monkeypatch.setattr(register_wf, "human_describe", lambda: "sim=off")
    assert register_wf.preflight_check() is False


def test_preflight_has_no_turnstile_probe():
    assert not hasattr(register_wf, "_check_turnstile")
    assert not hasattr(register_wf, "_probe_signup_cf")
