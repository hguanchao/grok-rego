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


def teardown_function() -> None:
    util.clear_cancel()
    saved = getattr(setup_function, "saved", None)
    if saved:
        for key, value in saved.items():
            setattr(config, key, value)
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


def test_load_config_drops_retired_mail_lists(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"mail_provider": "tempmail", "mail_domain_whitelist": "a.com", '
        '"mail_domain_blacklist": "b.com"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))
    config.load_config()
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["mail_provider"] == "tempmail"
    assert "mail_domain_whitelist" not in saved
    assert "mail_domain_blacklist" not in saved
    public = config.get_public_config()
    assert "mail_domain_whitelist" not in public
    assert "mail_domain_blacklist" not in public


def test_init_db_drops_mail_blacklist_table(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "mail.db"))
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE mail_domain_blacklist (domain TEXT PRIMARY KEY)"
        )
        conn.commit()
    db.init_db()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'mail_domain_blacklist'"
        ).fetchone()
    assert row is None


