"""HTTP body reading with a bounded client wait time."""

from http.server import BaseHTTPRequestHandler

CLIENT_BODY_READ_TIMEOUT = 15.0


class IncompleteRequestBodyError(ConnectionError):
    """The client disconnected before sending Content-Length bytes."""


class RequestBodyTooLarge(ValueError):
    """The declared request body exceeds this endpoint's configured limit."""


def read_request_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int,
) -> bytes:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > max_bytes:
        handler.close_connection = True
        raise RequestBodyTooLarge(f"请求体过大（>{max_bytes} bytes）")

    connection = handler.connection
    previous_timeout = connection.gettimeout()
    connection.settimeout(CLIENT_BODY_READ_TIMEOUT)
    try:
        body = handler.rfile.read(length)
    finally:
        connection.settimeout(previous_timeout)
    if len(body) != length:
        raise IncompleteRequestBodyError("请求体未完整接收")
    return body
