from types import SimpleNamespace

from core.util import format_upstream_error, read_upstream_body


class _StreamResponse:
    def __init__(self, chunks: list[bytes], *, content: bytes = b""):
        self.content = content
        self.queue = object()
        self.curl = object()
        self._chunks = list(chunks)
        self._stream_closed = False
        self.iter_calls = 0

    def iter_content(self, chunk_size=4096):
        self.iter_calls += 1
        if self._stream_closed:
            raise AssertionError("stream already closed")
        yield from self._chunks
        self._stream_closed = True


def test_read_upstream_body_drains_stream_queue():
    raw = b'{"error":{"message":"Rate limit exceeded","code":"rate_limit"}}'
    response = _StreamResponse([raw[:20], raw[20:]])

    body = read_upstream_body(response)

    assert body == raw
    assert response.content == raw
    assert read_upstream_body(response) == raw
    assert response.iter_calls == 1


def test_read_upstream_body_empty_stream_does_not_reread():
    response = _StreamResponse([])

    assert read_upstream_body(response) == b""
    assert read_upstream_body(response) == b""
    assert response.iter_calls == 1


def test_read_upstream_body_uses_cached_content():
    response = SimpleNamespace(content=b'{"ok":true}')

    assert read_upstream_body(response) == b'{"ok":true}'


def test_format_upstream_error_prefers_json_body():
    body = b'{"error":{"message":"Too many requests","type":"rate_limit_error"}}'
    text = format_upstream_error(429, body, reason="Too Many Requests", retry_after="12")
    assert "Too many requests" in text
    assert "rate_limit_error" in text


def test_format_upstream_error_falls_back_to_status_and_retry_after():
    text = format_upstream_error(429, b"", reason="Too Many Requests", retry_after="8")
    assert text == "HTTP 429 Too Many Requests Retry-After=8"
