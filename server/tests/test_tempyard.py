"""Tempyard 邮箱：创建地址、轮询验证码、启动校验。"""

from __future__ import annotations

import pytest

from core import config, util
from workflow import jobs, mail

_CONFIG_KEYS = (
    "MAIL_PROVIDER",
    "TEMPYARD_API_BASE",
    "TEMPYARD_DOMAINS",
    "TEMPYARD_DOMAIN_MODE",
    "TEMPMAIL_API_BASE",
    "CF_API_BASE",
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
    mail._tempyard_settings = None
    mail._tempyard_settings_at = 0.0
    mail._tempyard_domain_index = 0


def teardown_function() -> None:
    util.clear_cancel()
    saved = getattr(setup_function, "saved", None)
    if saved:
        for key, value in saved.items():
            setattr(config, key, value)
    mail._tempyard_settings = None
    mail._tempyard_settings_at = 0.0
    mail._tempyard_domain_index = 0


def test_create_uses_configured_domain_without_subdomain(monkeypatch):
    config.MAIL_PROVIDER = "tempyard"
    config.TEMPYARD_API_BASE = "https://mail.aibyte.de5.net"
    config.TEMPYARD_DOMAINS = ["aibyte.kdns.fr"]
    config.TEMPYARD_DOMAIN_MODE = "random"
    captured: dict = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(
            {
                "address": "yxjoe@aibyte.kdns.fr",
                "jwt": "jwt-1",
                "address_id": 1,
            }
        )

    def fake_get(*args, **kwargs):
        raise AssertionError("配置了域名时不应再请求 settings")

    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail.requests, "get", fake_get)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    address, token = mail.create_temp_email("Joe")
    assert address == "yxjoe@aibyte.kdns.fr"
    assert token == "jwt-1"
    assert captured["url"] == "https://mail.aibyte.de5.net/api/new_address"
    assert captured["json"] == {
        "name": "joe",
        "domain": "aibyte.kdns.fr",
        "enableRandomSubdomain": False,
    }


def test_create_fetches_domains_when_unset(monkeypatch):
    config.MAIL_PROVIDER = "tempyard"
    config.TEMPYARD_API_BASE = "https://mail.example.test"
    config.TEMPYARD_DOMAINS = []
    calls = {"settings": 0}

    def fake_get(url, **kwargs):
        calls["settings"] += 1
        assert url == "https://mail.example.test/open_api/settings"
        return _FakeResp({"domains": ["one.example"], "defaultDomains": []})

    def fake_post(url, json=None, **kwargs):
        assert json["domain"] == "one.example"
        return _FakeResp({"address": "a@one.example", "jwt": "jwt-2"})

    monkeypatch.setattr(mail.requests, "get", fake_get)
    monkeypatch.setattr(mail.requests, "post", fake_post)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    mail.create_temp_email("ada")
    mail.create_temp_email("ada")
    assert calls["settings"] == 1


def test_poll_uses_tempyard_base_and_bearer(monkeypatch):
    config.MAIL_PROVIDER = "tempyard"
    config.TEMPYARD_API_BASE = "https://mail.aibyte.de5.net"
    captured: dict = {}

    def fake_get(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp(
            {
                "results": [
                    {
                        "id": 7,
                        "source": "noreply@x.ai",
                        "subject": "Verify your email",
                        "text": "Your code is OE5-SDO.",
                    }
                ]
            }
        )

    monkeypatch.setattr(mail.requests, "get", fake_get)
    monkeypatch.setattr(mail, "_proxies", lambda: None)

    code = mail.poll_for_code("user@one.example", "jwt-9", timeout=9, interval=3)
    assert code == "OE5-SDO"
    assert captured["url"] == "https://mail.aibyte.de5.net/api/parsed_mails?limit=20&offset=0"
    assert captured["headers"]["Authorization"] == "Bearer jwt-9"


def test_validate_mail_ready_tempyard_only_needs_base():
    config.MAIL_PROVIDER = "tempyard"
    config.TEMPYARD_API_BASE = "https://mail.aibyte.de5.net"
    config.TEMPYARD_DOMAINS = []
    jobs.validate_mail_ready()

    config.TEMPYARD_API_BASE = ""
    with pytest.raises(ValueError, match="Tempyard"):
        jobs.validate_mail_ready()


def test_apply_config_accepts_tempyard_provider():
    config._apply_config_data(
        {
            "mail_provider": "tempyard",
            "tempyard_api_base": "https://mail.aibyte.de5.net/",
            "tempyard_domains": "https://Aibyte.kdns.fr/path, one.example",
            "tempyard_domain_mode": "poll",
        }
    )
    public = config.get_public_config()
    assert public["mail_provider"] == "tempyard"
    assert public["tempyard_api_base"] == "https://mail.aibyte.de5.net"
    assert public["tempyard_domains"] == ["aibyte.kdns.fr", "one.example"]
    assert public["tempyard_domain_mode"] == "poll"


def test_invalid_tempyard_domain_mode_falls_back():
    config._apply_config_data({"tempyard_domain_mode": "round"})
    assert config.TEMPYARD_DOMAIN_MODE == "random"


def test_mail_base_url_uses_tempyard(monkeypatch):
    from workflow import register as register_wf

    monkeypatch.setattr(config, "MAIL_PROVIDER", "tempyard", raising=False)
    monkeypatch.setattr(
        config,
        "TEMPYARD_API_BASE",
        "https://mail.aibyte.de5.net",
        raising=False,
    )
    assert register_wf._mail_base_url() == "https://mail.aibyte.de5.net"
