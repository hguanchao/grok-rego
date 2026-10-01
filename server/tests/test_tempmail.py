"""TempMail.lol 邮箱服务商：创建收件箱、轮询验证码、启动校验。"""

from __future__ import annotations

import json

import pytest

from core import config, util
from workflow import jobs, mail

_CONFIG_KEYS = (
    "MAIL_PROVIDER",
    "TEMPMAIL_API_BASE",
    "TEMPMAIL_API_KEY",
    "TEMPMAIL_DOMAIN",
    "MAIL_DOMAIN_WHITELIST",
    "YYDS_API_BASE",
    "YYDS_API_KEY",
    "CF_API_BASE",
    "CF_DOMAINS",
    "CF_DOMAIN_MODE",
    "CF_API_KEY",
)


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def setup_function() -> None:
    util.clear_cancel()
    setup_function.saved = {key: getattr(config, key) for key in _CONFIG_KEYS}  # type: ignore[attr-defined]
    config.MAIL_DOMAIN_WHITELIST = []


@pytest.fixture(autouse=True)
def _skip_recreate_wait(monkeypatch):
    """重创间隔在生产是 2s；单测默认不真等，个别用例自行覆盖。"""
    monkeypatch.setattr(mail, "wait_or_cancel", lambda seconds: False)


def teardown_function() -> None:
    util.clear_cancel()
    saved = getattr(setup_function, "saved", None)
    if saved:
        for key, value in saved.items():
            setattr(config, key, value)
    mail._banned_cache = None
    mail._domain_index = 0


def test_create_temp_email_posts_prefix_and_bearer(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_API_KEY = "tm.testkey"
    config.TEMPMAIL_DOMAIN = "example.com"
    captured: dict = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp({"address": "joe@example.com", "token": "tok-1"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Joe")
    assert address == "joe@example.com"
    assert token == "tok-1"
    assert captured["url"] == "https://api.tempmail.lol/v2/inbox/create"
    assert captured["json"] == {"prefix": "joe", "domain": "example.com"}
    assert captured["headers"]["Authorization"] == "Bearer tm.testkey"


def test_create_temp_email_uses_community_when_no_custom_domain(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_API_KEY = ""
    config.TEMPMAIL_DOMAIN = ""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured["json"] = json
        return _FakeResp({"address": "a@b.com", "token": "tok-2"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    mail.create_temp_email("Ada")
    assert captured["json"] == {"prefix": "ada", "community": True}


def test_poll_for_code_extracts_xai_style_code(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    captured: dict = {}
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        calls["n"] += 1
        return _FakeResp(
            {
                "expired": False,
                "emails": [
                    {
                        "from": "noreply@x.ai",
                        "to": "a@b.com",
                        "subject": "Verify your email",
                        "body": "Your code is OE5-SDO. It expires soon.",
                        "html": None,
                        "date": 1_700_000_000_000,
                    }
                ],
            }
        )

    monkeypatch.setattr(mail.requests, "get", fake_get)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    code = mail.poll_for_code("inbox-token", timeout=9, interval=3)
    assert code == "OE5-SDO"
    assert captured["url"] == "https://api.tempmail.lol/v2/inbox"
    assert captured["params"] == {"token": "inbox-token"}
    assert calls["n"] == 1


def test_poll_for_code_stops_when_inbox_expired(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"

    monkeypatch.setattr(
        mail.requests,
        "get",
        lambda *args, **kwargs: _FakeResp({"expired": True, "emails": []}),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    assert mail.poll_for_code("dead-token", timeout=9, interval=3) is None


def test_validate_mail_ready_tempmail_only_needs_base():
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    jobs.validate_mail_ready()

    config.TEMPMAIL_API_BASE = ""
    with pytest.raises(ValueError, match="TempMail.lol API"):
        jobs.validate_mail_ready()


def test_apply_config_accepts_tempmail_provider():
    config._apply_config_data(
        {
            "mail_provider": "tempmail",
            "tempmail_api_base": "https://api.tempmail.lol/v2/",
            "tempmail_api_key": "tm.abc",
            "tempmail_domain": "https://Mail.Example.com/path",
        }
    )
    public = config.get_public_config()
    assert public["mail_provider"] == "tempmail"
    assert public["tempmail_api_base"] == "https://api.tempmail.lol/v2"
    assert public["tempmail_api_key"] == "tm.abc"
    assert public["tempmail_domain"] == "mail.example.com"


def test_mail_base_url_uses_tempmail(monkeypatch):
    from workflow import register as register_wf

    monkeypatch.setattr(
        config,
        "MAIL_PROVIDER",
        "tempmail",
        raising=False,
    )
    monkeypatch.setattr(
        config,
        "TEMPMAIL_API_BASE",
        "https://api.tempmail.lol/v2",
        raising=False,
    )
    assert register_wf._mail_base_url() == "https://api.tempmail.lol/v2"


def test_invalid_mail_provider_falls_back_to_cf():
    config._apply_config_data({"mail_provider": "unknown"})
    assert config.MAIL_PROVIDER == "cf"


def test_ban_rejected_address_keeps_existing_cache_and_skips_sld(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = None
    created: list[str] = []

    monkeypatch.setattr(
        "db.list_banned_mail_domains",
        lambda: ["auroracovia.com"],
    )
    monkeypatch.setattr("db.ban_mail_domain", lambda domain, reason="": True)

    def fake_post(url, json=None, headers=None, **kwargs):
        if not created:
            created.append("a@dl.auroracovia.com")
            return _FakeResp({"address": created[-1], "token": "tok-skip"})
        created.append("b@ok.example.com")
        return _FakeResp({"address": created[-1], "token": "tok-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    host = mail.ban_rejected_address("user@yqj.auroracovia.com")
    assert host == "auroracovia.com"
    assert "auroracovia.com" in mail._banned_cache

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@dl.auroracovia.com", "b@ok.example.com"]
    assert address == "b@ok.example.com"
    assert token == "tok-ok"


def test_ban_rejected_address_skips_blacklisted_host(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = set()
    banned: list[str] = []
    created: list[str] = []

    def fake_ban(domain: str, reason: str = "") -> bool:
        banned.append(domain)
        return True

    def fake_post(url, json=None, headers=None, **kwargs):
        if len(created) == 0:
            created.append("a@ss.imagesthere.com")
            return _FakeResp({"address": created[-1], "token": "tok-bad"})
        created.append("b@an.inovel26.com")
        return _FakeResp({"address": created[-1], "token": "tok-ok"})

    monkeypatch.setattr("db.ban_mail_domain", fake_ban)
    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    host = mail.ban_rejected_address("user@ss.imagesthere.com")
    assert host == "imagesthere.com"
    assert "imagesthere.com" in mail._banned_cache

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@ss.imagesthere.com", "b@an.inovel26.com"]
    assert address == "b@an.inovel26.com"
    assert token == "tok-ok"


def test_recreate_waits_two_seconds_between_requests(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = {"imagesthere.com"}
    waits: list[float] = []

    monkeypatch.setattr(mail, "wait_or_cancel", lambda seconds: waits.append(seconds) or False)
    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "x@e1g.imagesthere.com", "token": "tok"}
            if len(waits) == 0
            else {"address": "ok@example.com", "token": "tok-ok"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert waits == [mail._CREATE_RETRY_INTERVAL]
    assert address == "ok@example.com"
    assert token == "tok-ok"


def test_recreate_stops_when_cancelled_during_wait(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = {"imagesthere.com"}
    posts = {"n": 0}

    def fake_wait(seconds: float) -> bool:
        assert seconds == mail._CREATE_RETRY_INTERVAL
        return True

    def fake_post(*args, **kwargs):
        posts["n"] += 1
        return _FakeResp({"address": "x@e1g.imagesthere.com", "token": "tok"})

    monkeypatch.setattr(mail, "wait_or_cancel", fake_wait)
    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="已取消"):
        mail.create_temp_email("Ada")
    assert posts["n"] == 1


def test_create_tempmail_raises_when_all_hosts_banned(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = {"imagesthere.com"}

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "x@e1g.imagesthere.com", "token": "tok"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="已拉黑二级域名"):
        mail.create_temp_email("Ada")


def test_create_tempmail_skips_banned_sld_after_third_level_cache(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    mail._banned_cache = {"yqj.auroracovia.com"}
    created: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        if not created:
            created.append("a@dl.auroracovia.com")
            return _FakeResp({"address": created[-1], "token": "tok-skip"})
        created.append("b@ok.example.com")
        return _FakeResp({"address": created[-1], "token": "tok-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@dl.auroracovia.com", "b@ok.example.com"]
    assert address == "b@ok.example.com"
    assert token == "tok-ok"


def test_ban_rejected_address_promotes_cf_subdomain_to_root(monkeypatch):
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    mail._banned_cache = set()

    monkeypatch.setattr("db.ban_mail_domain", lambda domain, reason="": True)

    host = mail.ban_rejected_address("ada@ab.feqz.help")
    assert host == "feqz.help"
    assert "feqz.help" in mail._banned_cache


def test_ban_rejected_address_promotes_random_subdomain_to_second_level(monkeypatch):
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    mail._banned_cache = set()
    monkeypatch.setattr("db.ban_mail_domain", lambda domain, reason="": True)

    host = mail.ban_rejected_address("user@d2.inovel26.com")
    assert host == "inovel26.com"
    assert "inovel26.com" in mail._banned_cache

    host = mail.ban_rejected_address("user@yr.auroracovia.com")
    assert host == "auroracovia.com"


def test_canonical_ban_host_handles_multi_part_public_suffix():
    assert mail._canonical_ban_host("a.shop.co.uk") == "shop.co.uk"
    assert mail._canonical_ban_host("shop.co.uk") == "shop.co.uk"
    assert mail._canonical_ban_host("inovel26.com") == "inovel26.com"
    assert mail._canonical_ban_host("d2.inovel26.com") == "inovel26.com"


def test_whitelist_matches_same_second_level_domain():
    config.MAIL_DOMAIN_WHITELIST = ["ss.imagesthere.com"]
    mail._banned_cache = set()
    assert mail._host_allowed("ss.imagesthere.com") is True
    assert mail._host_allowed("xx.imagesthere.com") is True
    assert mail._host_allowed("imagesthere.com") is True
    assert mail._host_allowed("auroracovia.com") is False


def test_create_tempmail_accepts_sibling_subdomain_when_sld_whitelisted(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    config.MAIL_DOMAIN_WHITELIST = ["ss.imagesthere.com"]
    mail._banned_cache = set()

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "b@an.imagesthere.com", "token": "tok-ok"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert address == "b@an.imagesthere.com"
    assert token == "tok-ok"


def test_cf_create_skips_banned_root_domain(monkeypatch):
    config.MAIL_PROVIDER = "cf"
    config.CF_API_BASE = "https://tempmail.example"
    config.CF_API_KEY = ""
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    config.CF_DOMAIN_MODE = "poll"
    mail._banned_cache = {"feqz.help"}
    mail._domain_index = 0
    captured: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.append(json["domain"])
        return _FakeResp({"address": f"ada@{json['domain']}", "jwt": "jwt-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)
    monkeypatch.setattr(mail, "_random_subdomain", lambda: "ab")

    address, token = mail.create_temp_email("Ada")
    assert captured == ["ab.maep.cn"]
    assert address == "ada@ab.maep.cn"
    assert token == "jwt-ok"


def test_cf_create_raises_when_all_roots_banned(monkeypatch):
    config.MAIL_PROVIDER = "cf"
    config.CF_API_BASE = "https://tempmail.example"
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    mail._banned_cache = {"feqz.help", "maep.cn"}

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应再请求")),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="根域名均已拉黑"):
        mail.create_temp_email("Ada")


def test_yyds_create_skips_blacklisted_host(monkeypatch):
    config.MAIL_PROVIDER = "yyds"
    config.YYDS_API_BASE = "https://maliapi.215.im/v1"
    config.YYDS_API_KEY = "AC-test"
    mail._banned_cache = set()
    created: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        if not created:
            created.append("a@ss.imagesthere.com")
            return _FakeResp(
                {"data": {"address": created[-1], "token": "tok-bad"}}
            )
        created.append("b@an.inovel26.com")
        return _FakeResp({"data": {"address": created[-1], "token": "tok-ok"}})

    monkeypatch.setattr("db.ban_mail_domain", lambda domain, reason="": True)
    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    host = mail.ban_rejected_address("user@ss.imagesthere.com")
    assert host == "imagesthere.com"

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@ss.imagesthere.com", "b@an.inovel26.com"]
    assert address == "b@an.inovel26.com"
    assert token == "tok-ok"


def test_create_tempmail_skips_non_whitelisted_host(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    config.MAIL_DOMAIN_WHITELIST = ["inovel26.com"]
    mail._banned_cache = set()
    created: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        if not created:
            created.append("a@ss.auroracovia.com")
            return _FakeResp({"address": created[-1], "token": "tok-skip"})
        created.append("b@an.inovel26.com")
        return _FakeResp({"address": created[-1], "token": "tok-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@ss.auroracovia.com", "b@an.inovel26.com"]
    assert address == "b@an.inovel26.com"
    assert token == "tok-ok"


def test_create_tempmail_raises_when_host_not_in_whitelist(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    config.MAIL_DOMAIN_WHITELIST = ["good.com"]
    mail._banned_cache = set()

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "x@ss.auroracovia.com", "token": "tok"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="非白名单二级域名"):
        mail.create_temp_email("Ada")


def test_empty_whitelist_accepts_any_host(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    config.MAIL_DOMAIN_WHITELIST = []
    mail._banned_cache = set()

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "a@ss.auroracovia.com", "token": "tok-any"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert address == "a@ss.auroracovia.com"
    assert token == "tok-any"


def test_blacklist_still_applies_when_whitelisted(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = ""
    config.MAIL_DOMAIN_WHITELIST = ["imagesthere.com", "inovel26.com"]
    mail._banned_cache = {"imagesthere.com"}
    created: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        if not created:
            created.append("a@ss.imagesthere.com")
            return _FakeResp({"address": created[-1], "token": "tok-ban"})
        created.append("b@an.inovel26.com")
        return _FakeResp({"address": created[-1], "token": "tok-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Ada")
    assert created == ["a@ss.imagesthere.com", "b@an.inovel26.com"]
    assert address == "b@an.inovel26.com"
    assert token == "tok-ok"


def test_cf_create_skips_root_not_in_whitelist(monkeypatch):
    config.MAIL_PROVIDER = "cf"
    config.CF_API_BASE = "https://tempmail.example"
    config.CF_API_KEY = ""
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    config.CF_DOMAIN_MODE = "poll"
    config.MAIL_DOMAIN_WHITELIST = ["maep.cn"]
    mail._banned_cache = set()
    mail._domain_index = 0
    captured: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.append(json["domain"])
        return _FakeResp({"address": f"ada@{json['domain']}", "jwt": "jwt-ok"})

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)
    monkeypatch.setattr(mail, "_random_subdomain", lambda: "ab")

    address, token = mail.create_temp_email("Ada")
    assert captured == ["ab.maep.cn"]
    assert address == "ada@ab.maep.cn"
    assert token == "jwt-ok"


def test_apply_config_accepts_mail_whitelist():
    config._apply_config_data(
        {
            "mail_domain_whitelist": "Inovel26.com, ss.imagesthere.com, inovel26.com",
        }
    )
    public = config.get_public_config()
    assert public["mail_domain_whitelist"] == [
        "inovel26.com",
        "imagesthere.com",
    ]


def test_public_config_canonicalizes_runtime_whitelist():
    config.MAIL_DOMAIN_WHITELIST = ["ss.imagesthere.com", "inovel26.com"]
    public = config.get_public_config()
    assert public["mail_domain_whitelist"] == ["imagesthere.com", "inovel26.com"]


def test_tempmail_custom_domain_accepted_when_whitelisted(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = "example.com"
    config.MAIL_DOMAIN_WHITELIST = ["example.com"]
    mail._banned_cache = {"example.com"}

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "joe@example.com", "token": "tok-1"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Joe")
    assert address == "joe@example.com"
    assert token == "tok-1"


def test_tempmail_custom_domain_rejected_if_not_whitelisted(monkeypatch):
    config.MAIL_PROVIDER = "tempmail"
    config.TEMPMAIL_API_BASE = "https://api.tempmail.lol/v2"
    config.TEMPMAIL_DOMAIN = "example.com"
    config.MAIL_DOMAIN_WHITELIST = ["inovel26.com"]
    mail._banned_cache = set()

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"address": "joe@example.com", "token": "tok-1"}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="非白名单二级域名"):
        mail.create_temp_email("Joe")


def test_cf_create_raises_when_roots_not_whitelisted(monkeypatch):
    config.MAIL_PROVIDER = "cf"
    config.CF_API_BASE = "https://tempmail.example"
    config.CF_DOMAINS = ["feqz.help", "maep.cn"]
    config.MAIL_DOMAIN_WHITELIST = ["inovel26.com"]
    mail._banned_cache = set()

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应再请求")),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="均不在白名单"):
        mail.create_temp_email("Ada")


def test_ban_mail_domain_stores_second_level_only(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))
    assert db.ban_mail_domain("d2.inovel26.com", reason="xAI invalid") is True
    assert db.list_banned_mail_domains() == ["inovel26.com"]
    assert db.ban_mail_domain("ss.inovel26.com") is False


def test_load_config_rewrites_whitelist_to_second_level(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"mail_domain_whitelist": "ss.imagesthere.com, Inovel26.com"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))
    config.load_config()
    assert config.MAIL_DOMAIN_WHITELIST == ["imagesthere.com", "inovel26.com"]
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["mail_domain_whitelist"] == "imagesthere.com,inovel26.com"


def test_init_mail_blacklist_promotes_legacy_third_level_hosts(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))
    db.init_mail_blacklist_table()
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO mail_domain_blacklist (domain, reason, created_at) "
            "VALUES (?, ?, ?)",
            [
                ("d2.inovel26.com", "xAI unavailable", "2026-10-01T13:52:05+08:00"),
                ("yr.auroracovia.com", "xAI invalid", "2026-10-01T13:52:27+08:00"),
                ("inovel26.com", "xAI invalid", "2026-10-01T13:00:00+08:00"),
            ],
        )
        conn.commit()

    db.init_mail_blacklist_table()
    assert db.list_banned_mail_domains() == ["auroracovia.com", "inovel26.com"]


def test_replace_banned_mail_domains_roundtrip(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))
    mail._banned_cache = {"stale.com"}

    hosts = db.replace_banned_mail_domains(
        ["ImagesThere.com", " imagesthere.com ", "d2.inovel26.com", ""]
    )
    assert hosts == ["imagesthere.com", "inovel26.com"]
    assert db.list_banned_mail_domains() == ["imagesthere.com", "inovel26.com"]

    mail.invalidate_banned_cache()
    assert mail._host_banned("ss.imagesthere.com") is True
    assert mail._host_banned("inovel26.com") is True
    assert mail._host_banned("d2.inovel26.com") is True

    db.replace_banned_mail_domains([])
    mail.invalidate_banned_cache()
    assert db.list_banned_mail_domains() == []
    assert mail._host_banned("ss.imagesthere.com") is False


def test_update_public_config_writes_whitelist(tmp_path, monkeypatch):
    import db

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))

    public = config.update_public_config(
        {"mail_domain_whitelist": ["Inovel26.com", "ss.imagesthere.com"]}
    )
    assert public["mail_domain_whitelist"] == ["inovel26.com", "imagesthere.com"]
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["mail_domain_whitelist"] == "inovel26.com,imagesthere.com"


def test_update_public_config_replaces_blacklist(tmp_path, monkeypatch):
    import db

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"mail_provider": "tempmail"}', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))
    mail._banned_cache = {"stale.com"}

    public = config.update_public_config(
        {"mail_domain_blacklist": ["ImagesThere.com", "yr.auroracovia.com"]}
    )
    assert public["mail_domain_blacklist"] == [
        "auroracovia.com",
        "imagesthere.com",
    ]
    assert mail._banned_cache is None
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "mail_domain_blacklist" not in saved
    assert saved["mail_provider"] == "tempmail"


def test_yyds_create_raises_when_all_hosts_banned(monkeypatch):
    config.MAIL_PROVIDER = "yyds"
    config.YYDS_API_BASE = "https://maliapi.215.im/v1"
    config.YYDS_API_KEY = "AC-test"
    mail._banned_cache = {"imagesthere.com"}

    monkeypatch.setattr(
        mail.requests,
        "post",
        lambda *args, **kwargs: _FakeResp(
            {"data": {"address": "x@e1g.imagesthere.com", "token": "tok"}}
        ),
    )
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    with pytest.raises(RuntimeError, match="已拉黑二级域名"):
        mail.create_temp_email("Ada")
