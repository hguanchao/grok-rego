import json

from gateway.grok import (
    _ensure_prompt_cache_and_reasoning,
    _session_key,
    _stable_cache_key,
)
from gateway.usage import StreamUsageAccumulator, _openai_usage


class _FakeHandler:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_session_key_prefers_session_id_over_conv_id():
    handler = _FakeHandler(
        {
            "x-grok-session-id": "sess-root",
            "x-grok-conv-id": "recap-123",
        }
    )
    body = json.dumps({"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]}).encode()
    assert _session_key(handler, body) == "s:sess-root"


def test_side_call_shares_root_session_binding():
    root = _FakeHandler({"x-grok-session-id": "sess-root", "x-grok-conv-id": "sess-root"})
    recap = _FakeHandler({"x-grok-session-id": "sess-root", "x-grok-conv-id": "recap-9"})
    body = b'{"model":"grok-4.6"}'
    assert _session_key(root, body) == _session_key(recap, body)


def test_chat_completions_gets_prompt_cache_key():
    handler = _FakeHandler({"x-grok-session-id": "sess-root-abcdefghijklmnopqrstuvwxyz0123456789"})
    body = json.dumps({"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]}).encode()
    out = json.loads(_ensure_prompt_cache_and_reasoning(body, handler, "/grok/v1/chat/completions"))
    assert out["prompt_cache_key"] == "sess-root-abcdefghijklmnopqrstuvwxyz0123456789"[:64]
    assert "reasoning" not in out


def test_existing_prompt_cache_key_kept():
    handler = _FakeHandler({"x-grok-session-id": "sess-root"})
    payload = {"model": "grok-4.6", "prompt_cache_key": "already-set"}
    key = _stable_cache_key(handler, payload)
    assert key == "already-set"


def test_openai_usage_reads_prompt_cache_hit_tokens():
    got = _openai_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 80,
        }
    )
    assert got["cache_tokens"] == 80


def test_stream_accumulator_joins_split_usage_json():
    acc = StreamUsageAccumulator()
    acc.feed(
        b'data: {"usage":{"prompt_tokens":100,"prompt_tokens_details":{"cached_tokens":'
    )
    acc.feed(b"88}}}\n\n")
    got = acc.result()
    assert got["prompt_tokens"] == 100
    assert got["cache_tokens"] == 88
