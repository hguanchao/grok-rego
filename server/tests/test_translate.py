"""翻译层黄金测试（方案 A）：请求翻译 + SSE 翻译 + 整包翻译的行为冻结。

- 请求侧（messages_to_chat / messages_to_responses）：内联代表性 Messages，
  断言关键字段映射（system/tools/tool_choice/effort/孤儿过滤）。
- SSE 侧：tests/fixtures/*.sse 为输入，翻译输出必须含 message_start、
  文本/tool 增量、message_delta（含 usage）、message_stop。
- 整包侧：chat_to_message / responses_to_message 的 usage 与 tool 映射。

重构（归一内部表示/单状态机）前后跑同一套，绿=行为没变。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from gateway.anthropic import (
    _convert_tool_choice,
    _convert_tools,
    _iter_tool_blocks,
    chat_to_message,
    iter_anthropic_sse,
    iter_responses_sse,
    messages_to_chat,
    messages_to_responses,
    responses_to_message,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _events(raw: bytes) -> list[dict]:
    """Anthropic SSE 输出 → data JSON 列表（跳过 message_stop 空体）。"""
    out = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                out.append(json.loads(line[5:]))
            except ValueError:
                pass
    return out


# ─── 请求翻译 ────────────────────────────────────────────────


def test_messages_to_chat_basic() -> None:
    """system/tools/温度透传，tool_use→tool_calls，tool_result→tool 消息。"""
    payload = {
        "model": "big-pickle",
        "stream": True,
        "system": "you are helpful",
        "max_tokens": 64,
        "temperature": 0.5,
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"p": "a"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ],
            },
        ],
        "tools": [
            {"name": "Read", "description": "read", "input_schema": {"type": "object"}}
        ],
        "tool_choice": {"type": "auto"},
    }
    chat = messages_to_chat(payload)
    assert chat["model"] == "big-pickle"
    assert chat["messages"][0] == {"role": "system", "content": "you are helpful"}
    asst = next(m for m in chat["messages"] if m["role"] == "assistant")
    assert asst["tool_calls"][0]["id"] == "toolu_1"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"p": "a"}
    tool_msg = next(m for m in chat["messages"] if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "toolu_1"
    assert chat["tools"][0]["function"]["name"] == "Read"
    assert chat["tool_choice"] == "auto"
    assert chat["max_tokens"] == 64


def test_messages_to_responses_orphan_dropped() -> None:
    """/compact 残留的孤儿 tool_result 丢弃，配对的保留（400 防护冻结）。"""
    payload = {
        "model": "muse-spark-1.3-contributor-free",
        "stream": False,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_gone",
                        "content": "stale",
                    },
                ],
            },
        ],
    }
    resp = messages_to_responses(payload)
    outputs = [m for m in resp["input"] if m.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "toolu_1"
    calls = [m for m in resp["input"] if m.get("type") == "function_call"]
    assert len(calls) == 1


def test_messages_to_responses_effort_mapping() -> None:
    """output_config.effort=max → reasoning.effort=xhigh（上游不认 max）。"""
    payload = {
        "model": "muse-spark-1.3-contributor-free",
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"effort": "max"},
    }
    resp = messages_to_responses(payload)
    assert resp["reasoning"] == {"effort": "xhigh", "summary": "concise"}


def test_shared_converters_parity() -> None:
    """共享转换器与旧双份实现语义一致（chat/responses 旧分支逐条对照）。

    旧 chat 分支：str any→required/auto/none 直通/未知不设；
    dict tool+name→function，auto/any/required/none→required 归一；
    旧 responses 分支：dict tool+name→function，auto/required/none 直通。
    合并后的 _convert_tool_choice 必须同时满足两者（交集为超集时取并）。
    """
    assert _convert_tools(
        [{"name": "R", "description": "d", "input_schema": {"type": "object"}}]
    ) == [
        {
            "type": "function",
            "function": {
                "name": "R",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert _convert_tools("not-a-list") == []
    assert _convert_tool_choice("any") == "required"
    assert _convert_tool_choice("auto") == "auto"
    assert _convert_tool_choice("none") == "none"
    assert _convert_tool_choice("weird") is None
    assert _convert_tool_choice({"type": "tool", "name": "R"}) == {
        "type": "function",
        "function": {"name": "R"},
    }
    assert _convert_tool_choice({"type": "auto"}) == "auto"
    assert _convert_tool_choice({"type": "any"}) == "required"
    assert _convert_tool_choice({"type": "required"}) == "required"
    assert _convert_tool_choice({"type": "none"}) == "none"
    assert _convert_tool_choice({"type": "bogus"}) is None
    assert _convert_tool_choice(None) is None


def test_shared_tool_block_scan() -> None:
    """共享工具块扫描：声明集合去重 + 孤儿计数（缺 id 的 result 不算孤儿）。"""
    blocks, orphans = _iter_tool_blocks(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "a", "name": "R", "input": {}},
                        {"type": "tool_use", "id": "a", "name": "R", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "gone",
                            "content": "stale",
                        },
                        {"type": "tool_result", "content": "no-id"},
                    ],
                },
            ]
        }
    )
    assert [b["id"] for b in blocks] == ["a"]
    assert orphans == 1


# ─── SSE 翻译 ────────────────────────────────────────────────


def test_responses_text_fixture() -> None:
    """真实录制 responses_text.sse：10 文本增量 + usage 全量回传。"""
    raw = _read_fixture("responses_text.sse")
    out = b"".join(iter_responses_sse(iter([raw]), "muse-spark-1.3-contributor-free"))
    text = out.decode()
    assert len(re.findall(r"text_delta", text)) == 10
    evs = _events(out)
    assert evs[0]["type"] == "message_start"
    delta = next(e for e in evs if e["type"] == "message_delta")
    assert delta["usage"]["output_tokens"] == 7892
    assert delta["usage"]["input_tokens"] == 388
    assert delta["usage"]["cache_read_input_tokens"] == 384
    assert evs[-1]["type"] == "message_stop"


def test_responses_tool_fixture() -> None:
    """responses_tool.sse：空增量不落块，done 全量 arguments 兜底，stop 收敛 tool_use。"""
    raw = _read_fixture("responses_tool.sse")
    out = b"".join(iter_responses_sse(iter([raw]), "muse-spark-1.3-contributor-free"))
    text = out.decode()
    assert "tool_use" in text
    # partial_json 是 JSON 转义串（{\"cmd\":\"ls\"}），按 Anthropic input_json_delta 语义收敛后还原
    assert '"cmd' in text and "ls" in text
    evs = _events(out)
    delta = next(e for e in evs if e["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["input_tokens"] == 200


def test_chat_mixed_fixture() -> None:
    """chat_mixed.sse：thinking+文本+工具增量，usage 全量回传。"""
    raw = _read_fixture("chat_mixed.sse")
    out = b"".join(iter_anthropic_sse(iter([raw]), "big-pickle"))
    text = out.decode()
    assert "thinking_delta" in text
    assert "text_delta" in text
    assert "input_json_delta" in text
    evs = _events(out)
    delta = next(e for e in evs if e["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"] == {
        "output_tokens": 20,
        "input_tokens": 100,
        "cache_read_input_tokens": 60,
    }


# ─── 整包翻译 ────────────────────────────────────────────────


def test_chat_to_message_usage_and_tools() -> None:
    """chat 整包：usage 映射 + tool_calls→tool_use + 缓存命中。"""
    msg = chat_to_message(
        {
            "id": "chatcmpl-1",
            "model": "big-pickle",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "hi",
                        "reasoning_content": "thinking",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "Read", "arguments": '{"p":"a"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        },
        "big-pickle",
    )
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 60,
    }
    kinds = [c["type"] for c in msg["content"]]
    assert kinds == ["thinking", "text", "tool_use"]


def test_responses_to_message_usage_and_tools() -> None:
    """responses 整包：function_call→tool_use + usage 缓存。"""
    msg = responses_to_message(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "Bash",
                    "arguments": '{"cmd":"ls"}',
                },
            ],
            "usage": {
                "input_tokens": 200,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 150},
            },
        },
        "muse-spark-1.3-contributor-free",
    )
    assert msg["usage"] == {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_input_tokens": 150,
    }
    tool = next(c for c in msg["content"] if c["type"] == "tool_use")
    assert tool["input"] == {"cmd": "ls"}
