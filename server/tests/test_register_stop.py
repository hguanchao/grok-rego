import time

from core import util
from workflow import register as register_wf


def setup_function() -> None:
    util.clear_cancel()


def teardown_function() -> None:
    util.clear_cancel()


def test_wait_or_cancel_returns_immediately_when_flagged():
    util.request_cancel()
    t0 = time.monotonic()
    assert util.wait_or_cancel(2.0) is True
    assert time.monotonic() - t0 < 0.2


def test_request_cancel_closes_tracked_browsers():
    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    browser = FakeBrowser()
    register_wf._track_browser(browser)
    register_wf.request_cancel()
    assert browser.closed is True
    assert register_wf.is_cancelled() is True


def test_run_signup_bails_before_launching():
    util.request_cancel()
    ok, email = register_wf.run_signup(headless=True)
    assert ok is False
    assert email is None


class _FakePage:
    def __init__(self, browser):
        self.context = type("Ctx", (), {"browser": browser})()


def test_abort_before_submit_closes_browser():
    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    browser = FakeBrowser()
    register_wf._track_browser(browser)
    util.request_cancel()
    page = _FakePage(browser)
    assert register_wf.abort_if_cancelled(page) is True
    assert browser.closed is True


def test_abort_after_submit_keeps_browser():
    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    browser = FakeBrowser()
    register_wf._track_browser(browser)
    page = _FakePage(browser)
    register_wf._mark_browser_committed(page)
    util.request_cancel()
    assert register_wf.abort_if_cancelled(page) is False
    assert browser.closed is False
