from types import SimpleNamespace

from core import util
from workflow import register as register_wf
from core import proxypool


def setup_function() -> None:
    util.clear_cancel()
    proxypool.reset()


def teardown_function() -> None:
    util.clear_cancel()
    proxypool.reset()


class _FakeCamoufox:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.pages = [_FakePage(self)]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class _FakePage:
    def __init__(self, browser):
        self.context = SimpleNamespace(browser=browser)
        self.url = "https://accounts.x.ai/sign-up"
        self._goto_calls = 0

    def goto(self, url, wait_until="domcontentloaded", timeout=60000):
        self._goto_calls += 1
        self.url = url

    def query_selector(self, selector):
        return object() if selector == "body" else None


def test_check_turnstile_passes_on_first_node(monkeypatch):
    monkeypatch.setattr(proxypool, "urls", lambda: ["http://user:pass@1.1.1.1:8080"])
    probes: list[bool] = []

    def probe(headless: bool) -> bool:
        probes.append(headless)
        assert proxypool.current() == "http://user:pass@1.1.1.1:8080"
        return True

    monkeypatch.setattr(register_wf, "_probe_signup_cf", probe)
    assert register_wf._check_turnstile(headless=True) is True
    assert probes == [True]
    assert proxypool.snapshot()["cooling"] == 0


def test_check_turnstile_cools_failing_node_and_tries_next(monkeypatch):
    pool = ["http://a.example:1", "http://b.example:2"]
    monkeypatch.setattr(proxypool, "urls", lambda: pool)
    seen: list[str] = []

    def probe(headless: bool) -> bool:
        seen.append(proxypool.current())
        return proxypool.current() == pool[1]

    monkeypatch.setattr(register_wf, "_probe_signup_cf", probe)
    assert register_wf._check_turnstile(headless=False) is True
    assert seen == pool
    snap = proxypool.snapshot()
    cooling = [item["display"] for item in snap["items"] if item["cooling"]]
    assert cooling == ["http://a.example:1"]


def test_check_turnstile_caps_probe_count(monkeypatch):
    pool = [f"http://n{i}.example:1" for i in range(5)]
    monkeypatch.setattr(proxypool, "urls", lambda: pool)
    seen: list[str] = []

    def probe(headless: bool) -> bool:
        seen.append(proxypool.current())
        return False

    monkeypatch.setattr(register_wf, "_probe_signup_cf", probe)
    monkeypatch.setattr(register_wf, "PREFLIGHT_TURNSTILE_MAX_NODES", 2)
    assert register_wf._check_turnstile() is False
    assert seen == pool[:2]


def test_check_turnstile_fails_when_all_nodes_fail(monkeypatch):
    pool = ["http://a.example:1", "http://b.example:2"]
    monkeypatch.setattr(proxypool, "urls", lambda: pool)
    monkeypatch.setattr(register_wf, "_probe_signup_cf", lambda headless: False)
    assert register_wf._check_turnstile() is False
    snap = proxypool.snapshot()
    assert snap["cooling"] == 2


def test_check_turnstile_stops_when_cancelled(monkeypatch):
    monkeypatch.setattr(proxypool, "urls", lambda: ["http://a.example:1"])
    called = {"n": 0}

    def probe(headless: bool) -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr(register_wf, "_probe_signup_cf", probe)
    util.request_cancel()
    assert register_wf._check_turnstile() is False
    assert called["n"] == 0


def test_probe_signup_cf_accepts_passed_turnstile(monkeypatch):
    monkeypatch.setattr(register_wf, "Camoufox", _FakeCamoufox)
    monkeypatch.setattr(register_wf, "_camoufox_kwargs", lambda headless: {"headless": headless})
    monkeypatch.setattr(register_wf, "_open_page", lambda browser: browser.pages[0])
    monkeypatch.setattr(register_wf, "try_click_cookies", lambda page: True)
    monkeypatch.setattr(register_wf, "handle_cf_challenge", lambda page, max_wait=45: True)
    monkeypatch.setattr(register_wf, "handle_turnstile", lambda page, max_wait=30: "passed")
    monkeypatch.setattr(register_wf, "_has_input", lambda page, selector: True)
    monkeypatch.setattr(register_wf, "_dump_page", lambda page, tag: None)
    monkeypatch.setattr(proxypool, "current", lambda: "http://node:1")
    monkeypatch.setattr(proxypool, "redact", lambda url: url)
    assert register_wf._probe_signup_cf(headless=True) is True


def test_probe_signup_cf_rejects_failed_turnstile(monkeypatch):
    dumped: list[str] = []
    monkeypatch.setattr(register_wf, "Camoufox", _FakeCamoufox)
    monkeypatch.setattr(register_wf, "_camoufox_kwargs", lambda headless: {"headless": headless})
    monkeypatch.setattr(register_wf, "_open_page", lambda browser: browser.pages[0])
    monkeypatch.setattr(register_wf, "try_click_cookies", lambda page: True)
    monkeypatch.setattr(register_wf, "handle_cf_challenge", lambda page, max_wait=45: True)
    monkeypatch.setattr(register_wf, "handle_turnstile", lambda page, max_wait=30: "failed")
    monkeypatch.setattr(register_wf, "_dump_page", lambda page, tag: dumped.append(tag))
    monkeypatch.setattr(proxypool, "current", lambda: "http://node:1")
    monkeypatch.setattr(proxypool, "redact", lambda url: url)
    assert register_wf._probe_signup_cf(headless=True) is False
    assert dumped == ["preflight-turnstile-failed"]


def test_probe_signup_cf_rejects_when_signup_shell_missing(monkeypatch):
    dumped: list[str] = []
    monkeypatch.setattr(register_wf, "PREFLIGHT_SIGNUP_WAIT", 0)
    monkeypatch.setattr(register_wf, "Camoufox", _FakeCamoufox)
    monkeypatch.setattr(register_wf, "_camoufox_kwargs", lambda headless: {"headless": headless})
    monkeypatch.setattr(register_wf, "_open_page", lambda browser: browser.pages[0])
    monkeypatch.setattr(register_wf, "try_click_cookies", lambda page: True)
    monkeypatch.setattr(register_wf, "handle_cf_challenge", lambda page, max_wait=45: True)
    monkeypatch.setattr(register_wf, "handle_turnstile", lambda page, max_wait=30: "skipped")
    monkeypatch.setattr(register_wf, "_has_input", lambda page, selector: False)
    monkeypatch.setattr(register_wf, "_is_signup_landing", lambda page: False)
    monkeypatch.setattr(register_wf, "_dump_page", lambda page, tag: dumped.append(tag))
    monkeypatch.setattr(proxypool, "current", lambda: "http://node:1")
    monkeypatch.setattr(proxypool, "redact", lambda url: url)
    assert register_wf._probe_signup_cf(headless=True) is False
    assert dumped == ["preflight-signup-missing"]


def test_preflight_check_runs_turnstile_after_http_ok(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(register_wf, "_check_proxy", lambda: order.append("proxy") or True)
    monkeypatch.setattr(
        register_wf, "_check_reachable", lambda url, label: order.append(label) or True
    )
    monkeypatch.setattr(register_wf, "_mail_base_url", lambda: "https://mail.example")
    monkeypatch.setattr(
        register_wf, "_check_turnstile", lambda headless=True: order.append(f"cf:{headless}") or True
    )
    monkeypatch.setattr(register_wf, "human_describe", lambda: "sim=off")
    assert register_wf.preflight_check(headless=False) is True
    assert order == ["proxy", "注册入口", "邮箱服务", "cf:False"]


def test_preflight_check_fails_when_turnstile_fails(monkeypatch):
    monkeypatch.setattr(register_wf, "_check_proxy", lambda: True)
    monkeypatch.setattr(register_wf, "_check_reachable", lambda url, label: True)
    monkeypatch.setattr(register_wf, "_mail_base_url", lambda: "https://mail.example")
    monkeypatch.setattr(register_wf, "_check_turnstile", lambda headless=True: False)
    monkeypatch.setattr(register_wf, "human_describe", lambda: "sim=off")
    assert register_wf.preflight_check() is False


def test_preflight_check_skips_turnstile_when_http_fails(monkeypatch):
    called = {"cf": False}
    monkeypatch.setattr(register_wf, "_check_proxy", lambda: True)
    monkeypatch.setattr(register_wf, "_check_reachable", lambda url, label: False)
    monkeypatch.setattr(register_wf, "_mail_base_url", lambda: "https://mail.example")
    monkeypatch.setattr(
        register_wf, "_check_turnstile", lambda headless=True: called.__setitem__("cf", True) or True
    )
    monkeypatch.setattr(register_wf, "human_describe", lambda: "sim=off")
    assert register_wf.preflight_check() is False
    assert called["cf"] is False
