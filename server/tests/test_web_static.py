import io
import os
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

from api import server as api_server


class _FakeHandler(BaseHTTPRequestHandler):
    def __init__(self):
        self.command = "GET"
        self.wfile = io.BytesIO()
        self.headers_out: list[tuple[str, str]] = []
        self.status = 0

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.headers_out.append((keyword, value))

    def end_headers(self):
        return

    def log_message(self, fmt, *args):
        return


def test_safe_web_file_stays_inside_root(tmp_path):
    root = tmp_path / "web-dist"
    root.mkdir()
    (root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")

    inside = api_server._safe_web_file(str(root), "/assets/app.js")
    assert inside == os.path.realpath(assets / "app.js")

    escaped = api_server._safe_web_file(str(root), "/../pyproject.toml")
    assert escaped is None or not os.path.realpath(escaped).startswith(
        os.path.realpath(tmp_path / "secret") if False else os.path.realpath(str(root) + os.sep)
    )
    if escaped is not None:
        assert os.path.realpath(escaped).startswith(os.path.realpath(root) + os.sep)


def test_try_serve_web_falls_back_to_index(tmp_path, monkeypatch):
    root = tmp_path / "web-dist"
    root.mkdir()
    (root / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setattr(api_server, "WEB_DIST_DIR", str(root))

    handler = _FakeHandler()
    assert api_server.try_serve_web(handler, "GET", "/register") is True
    assert handler.status == 200
    assert handler.wfile.getvalue() == b"<html>spa</html>"


def test_try_serve_web_skips_api_paths(tmp_path, monkeypatch):
    root = tmp_path / "web-dist"
    root.mkdir()
    (root / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setattr(api_server, "WEB_DIST_DIR", str(root))
    handler = _FakeHandler()
    assert api_server.try_serve_web(handler, "GET", "/api/config") is False
    assert api_server.try_serve_web(handler, "GET", "/zen/v1/models") is False
    assert api_server.try_serve_web(handler, "POST", "/") is False


def test_try_serve_web_disabled_without_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(api_server, "WEB_DIST_DIR", str(tmp_path / "missing"))
    handler = _FakeHandler()
    assert api_server.try_serve_web(handler, "GET", "/") is False
