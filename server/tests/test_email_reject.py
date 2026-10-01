"""邮箱拒绝 / 通道关闭：立即换号，禁止空等 OTP。"""

from types import SimpleNamespace

import pytest

from core import util
from workflow import register as register_wf


def setup_function() -> None:
    util.clear_cancel()


def teardown_function() -> None:
    util.clear_cancel()


class _BodyPage:
    def __init__(self, body: str):
        self.body = body
        self.frames = [SimpleNamespace(inner_text=lambda _sel: body)]
        self.url = "https://accounts.x.ai/sign-up"


def test_email_rejected_matches_invalid_address():
    page = _BodyPage(
        "Sign up with your email. Your email address is invalid. "
        "Please use a different email address."
    )
    assert register_wf._email_rejected(page) is True
    assert register_wf._email_unavailable(page) is False
    assert register_wf._email_blocked(page) is True


def test_email_unavailable_matches_curly_apostrophe():
    page = _BodyPage(
        "sign up with your email email email sign-up isn’t available "
        "right now. sign up another way. sign upgo back by continuing, "
        "you agree to xai's terms of service"
    )
    assert register_wf._email_rejected(page) is False
    assert register_wf._email_unavailable(page) is True
    assert register_wf._email_blocked(page) is True


def test_email_unavailable_matches_ascii_apostrophe():
    page = _BodyPage(
        "Email sign-up isn't available right now. Sign up another way."
    )
    assert register_wf._email_unavailable(page) is True
    assert register_wf._email_blocked(page) is True


def test_plain_signup_landing_is_not_blocked():
    page = _BodyPage(
        "create your account sign up with email sign up with google"
    )
    assert register_wf._email_blocked(page) is False


def test_verify_email_raises_on_unavailable(monkeypatch):
    page = _BodyPage(
        "email sign-up isn’t available right now. sign up another way."
    )
    dumps: list[str] = []
    monkeypatch.setattr(register_wf, "_dump_page", lambda _page, tag: dumps.append(tag))
    monkeypatch.setattr(register_wf, "_on_form_page", lambda _page: False)

    with pytest.raises(register_wf.EmailRejectedError):
        register_wf._verify_email(page, "user@84.inovel26.com", "jwt")

    assert dumps == ["email-rejected"]


def test_verify_email_raises_when_unavailable_appears_during_wait(monkeypatch):
    page = _BodyPage("sign up with your email")
    dumps: list[str] = []
    monkeypatch.setattr(register_wf, "_dump_page", lambda _page, tag: dumps.append(tag))
    monkeypatch.setattr(register_wf, "_on_form_page", lambda _page: False)

    def wait_until(page, targets, timeout, check_for_errors=False, done_when=None):
        page.body = (
            "email sign-up isn’t available right now. sign up another way."
        )
        page.frames = [SimpleNamespace(inner_text=lambda _sel: page.body)]
        if done_when is not None:
            return bool(done_when())
        return False

    monkeypatch.setattr(register_wf, "wait_until", wait_until)

    with pytest.raises(register_wf.EmailRejectedError):
        register_wf._verify_email(page, "user@84.inovel26.com", "jwt")

    assert dumps == ["email-rejected"]


def test_post_email_pipeline_maps_unavailable_to_reject(monkeypatch):
    page = _BodyPage("email sign-up isn’t available right now.")
    monkeypatch.setattr(register_wf, "_on_form_page", lambda _page: False)
    monkeypatch.setattr(register_wf, "_dump_page", lambda *a, **k: None)

    stage = register_wf._post_email_pipeline(
        page, "user@84.inovel26.com", "jwt", "Ada", "Lee", "Secret1!"
    )
    assert stage == "reject"


def test_run_signup_bound_drops_email_on_reject(monkeypatch):
    calls: list[tuple[str | None, str | None]] = []

    def fake_attempt(email, jwt, first_name, last_name, password, headless=False):
        calls.append((email, jwt))
        if len(calls) == 1:
            return None, "old@84.inovel26.com", "old-jwt", "Ada", "Lee", "reject"
        return 9, "new@ok.example", "new-jwt", "Ada", "Lee", "post"

    monkeypatch.setattr(register_wf, "init_db", lambda: None)
    monkeypatch.setattr(register_wf, "_generate_password", lambda: "Secret1!")
    monkeypatch.setattr(register_wf, "_run_attempt", fake_attempt)

    ok, email = register_wf._run_signup_bound(headless=True)
    assert ok is True
    assert email == "new@ok.example"
    assert calls[0] == (None, None)
    assert calls[1] == (None, None)
