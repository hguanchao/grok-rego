"""
Claude Code / Anthropic Messages 适配。

Claude Code 固定打 `/zen/v1/messages`。Zen 免费模型只认 `/v1/chat/completions`，
把 Messages 请求转成 Chat Completions，再把响应转回 Messages / SSE。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

_NATIVE_MESSAGES_PREFIX = ("claude-", "qwen")
_FREE_UPSTREAM = "big-pickle"
# Claude Code 内置档位 / 全名 → 免费模型（Zen public 密钥打不了付费 Claude）
_CLAUDE_CODE_ALIAS_PREFIX = (
    "claude-haiku",
    "claude-sonnet",
    "claude-opus",
    "claude-fable",
    "claude-3",
)
_CLAUDE_CODE_SHORT = {"haiku", "sonnet", "opus", "fable"}
_CLAUDE_CODE_MODELS = (
    ("claude-haiku-4-5", "Haiku"),
    ("claude-sonnet-4-5", "Sonnet"),
    ("claude-opus-4-5", "Opus"),
    ("claude-fable-5", "Fable"),
)

# 已知不支持推理的免费模型前缀（其余 -free 与 big-pickle 视为支持）
_REASONING_UNSUPPORTED_PREFIX = ("mimo", "nemotron", "laguna")
# 思考预算 → 推理档位映射阈值（budget_tokens ≥ 阈值取该档）
_EFFORT_BUDGETS = ((32768, "max"), (24576, "high"), (16384, "medium"), (8192, "low"))


def supports_reasoning(model: str | None) -> bool:
    """上游模型是否支持 reasoning_effort 参数。"""
    ident = (model or "").strip().lower()
    if not ident:
        return False
    if ident == "big-pickle":
        return True
    return ident.endswith("-free") and not ident.startswith(_REASONING_UNSUPPORTED_PREFIX)


_KNOWN_EFFORTS = ("max", "xhigh", "high", "medium", "low")


def effort_from_body(payload: dict[str, Any]) -> str | None:
    """从 Anthropic Messages 体提取推理档位。

    优先级：
    1. ``output_config.effort`` —— Claude Code ≥2.1 的权威来源
       （thinking 变为 adaptive，无 budget_tokens）；
    2. ``thinking.type == "adaptive"`` —— 无显式档位时按 high 处理；
    3. ``thinking.type == "enabled"`` —— 旧版协议，budget_tokens 分段映射。
    """
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = str(output_config.get("effort") or "").strip().lower()
        if effort in _KNOWN_EFFORTS:
            return effort

    thinking = payload.get("thinking")
    if not isinstance(thinking, dict):
        return None
    if thinking.get("type") == "adaptive":
        return "high"
    if thinking.get("type") != "enabled":
        return None
    try:
        budget = int(thinking.get("budget_tokens") or 0)
    except (TypeError, ValueError):
        budget = 0
    for threshold, effort in _EFFORT_BUDGETS:
        if budget >= threshold:
            return effort
    return "low" if budget > 0 else None


def rewrite_model_id(model: str | None) -> str | None:
    """Claude Code 档位名改写到免费上游模型；其它 id 原样。"""
    ident = (model or "").strip()
    if not ident:
        return model
    low = ident.lower()
    if low in _CLAUDE_CODE_SHORT or low.startswith(_CLAUDE_CODE_ALIAS_PREFIX):
        return _FREE_UPSTREAM
    return ident


def rewrite_body_model(body: bytes) -> bytes:
    """请求 JSON 的 model 字段按 Claude Code 别名改写。"""
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    mapped = rewrite_model_id(payload["model"])
    if mapped == payload["model"]:
        return body
    payload["model"] = mapped
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def claude_code_model_entries() -> list[dict[str, Any]]:
    """注入 Claude Code 能识别的 id，供 GET /v1/models 发现。"""
    now = int(time.time())
    return [
        {
            "id": model_id,
            "object": "model",
            "created": now,
            "owned_by": "opencode",
            "display_name": label,
        }
        for model_id, label in _CLAUDE_CODE_MODELS
    ]


def is_messages_path(path: str) -> bool:
    return path.rstrip("/") in (
        "/zen/v1/messages",
        "/zen/v1/messages/count_tokens",
    ) or path.startswith("/zen/v1/messages/")


def is_count_tokens_path(path: str) -> bool:
    return path.rstrip("/") == "/zen/v1/messages/count_tokens"


def uses_chat_completions(model: str | None) -> bool:
    """免费 / OpenAI-compat 模型走 chat/completions，不走原生 /messages。"""
    ident = (model or "").strip().lower()
    if not ident:
        return True
    return not ident.startswith(_NATIVE_MESSAGES_PREFIX)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text") or ""))
            elif btype == "tool_result":
                parts.append(_text_of(block.get("content")))
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def _estimate_tokens(text: str) -> int:
    raw = text or ""
    return max(1, (len(raw) + 3) // 4)


def count_tokens(body: bytes) -> bytes:
    """本地估算 input_tokens，避免上游 /count_tokens 404。"""
    text = ""
    model = ""
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        model = str(payload.get("model") or "")
        text += _text_of(payload.get("system"))
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                text += _text_of(msg.get("content"))
    out = {"input_tokens": _estimate_tokens(text), "model": model or "unknown"}
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


def _system_to_text(system: Any) -> str:
    return _text_of(system).strip()


def messages_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages 请求体 → OpenAI Chat Completions。"""
    messages: list[dict[str, Any]] = []
    system = _system_to_text(payload.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": _text_of(content)})
            continue

        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                texts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
            elif btype == "tool_result":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": _text_of(block.get("content")),
                    }
                )
        if role == "assistant" and (texts or tool_calls):
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        elif texts:
            messages.append({"role": role, "content": "".join(texts)})

    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": bool(payload.get("stream")),
    }
    # 思考预算 → 推理档位：仅在上游模型支持推理时携带 reasoning_effort
    effort = effort_from_body(payload)
    if effort and supports_reasoning(str(out.get("model") or "")):
        out["reasoning_effort"] = effort
    if payload.get("max_tokens") is not None:
        out["max_tokens"] = payload.get("max_tokens")
    if payload.get("temperature") is not None:
        out["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        out["top_p"] = payload.get("top_p")
    if payload.get("stop_sequences"):
        out["stop"] = payload.get("stop_sequences")
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description") or "",
                        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                    },
                }
            )
        if converted:
            out["tools"] = converted
    choice = payload.get("tool_choice")
    if isinstance(choice, str):
        if choice == "any":
            out["tool_choice"] = "required"
        elif choice in ("auto", "none"):
            out["tool_choice"] = choice
    elif isinstance(choice, dict):
        name = choice.get("name")
        if choice.get("type") == "tool" and name:
            out["tool_choice"] = {"type": "function", "function": {"name": name}}
        elif choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") in ("any", "required"):
            out["tool_choice"] = "required"
        elif choice.get("type") == "none":
            out["tool_choice"] = "none"
    return out


def _stop_reason(finish: str | None) -> str:
    if finish == "length":
        return "max_tokens"
    if finish == "tool_calls":
        return "tool_use"
    return "end_turn"


def chat_to_message(payload: dict[str, Any], model: str | None) -> dict[str, Any]:
    """OpenAI Chat Completions 响应 → Anthropic Messages。"""
    choice = {}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content") if isinstance(message.get("content"), str) else ""
    thinking = message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else ""
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                parsed = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": parsed if isinstance(parsed, dict) else {},
                }
            )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "id": str(payload.get("id") or f"msg_{int(time.time())}"),
        "type": "message",
        "role": "assistant",
        "model": model or payload.get("model") or "",
        "content": content,
        "stop_reason": _stop_reason(str(choice.get("finish_reason") or "stop")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def iter_anthropic_sse(openai_chunks: Iterator[bytes], model: str | None) -> Iterator[bytes]:
    """把 OpenAI SSE 块转成 Anthropic Messages SSE。"""
    buf = b""
    started = False
    thinking_open = False
    text_open = False
    tool_open: int | None = None
    block_index = -1
    msg_id = f"msg_{int(time.time() * 1000)}"
    output_tokens = 0
    stop = "end_turn"
    tool_blocks: dict[int, int] = {}
    tool_args: dict[int, str] = {}

    def ensure_message() -> Iterator[bytes]:
        nonlocal started
        if started:
            return
        started = True
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model or "",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def close_text_thinking() -> Iterator[bytes]:
        nonlocal thinking_open, text_open
        if thinking_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
            thinking_open = False
        if text_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
            text_open = False

    def close_tool() -> Iterator[bytes]:
        nonlocal tool_open
        if tool_open is None:
            return
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": tool_open})
        tool_open = None

    def open_block(kind: str) -> Iterator[bytes]:
        nonlocal thinking_open, text_open, block_index
        yield from ensure_message()
        if kind == "thinking" and thinking_open:
            return
        if kind == "text" and text_open:
            return
        yield from close_tool()
        yield from close_text_thinking()
        block_index += 1
        if kind == "thinking":
            thinking_open = True
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
        else:
            text_open = True
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )

    def open_tool(call_index: int, call_id: str, name: str) -> Iterator[bytes]:
        nonlocal tool_open, block_index
        if call_index in tool_blocks:
            return
        yield from ensure_message()
        yield from close_text_thinking()
        yield from close_tool()
        block_index += 1
        tool_blocks[call_index] = block_index
        tool_args[call_index] = ""
        tool_open = block_index
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": call_id or f"toolu_{call_index}",
                    "name": name or "",
                    "input": {},
                },
            },
        )

    def close_all() -> Iterator[bytes]:
        if not started:
            yield from ensure_message()
            yield from open_block("text")
        yield from close_text_thinking()
        yield from close_tool()
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})

    for chunk in openai_chunks:
        if not chunk:
            continue
        buf += chunk
        while b"\n\n" in buf:
            part, buf = buf.split(b"\n\n", 1)
            data_lines = []
            for line in part.split(b"\n"):
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            data = b"\n".join(data_lines).decode("utf-8", "replace").strip()
            if data == "[DONE]":
                yield from close_all()
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("id"):
                msg_id = str(obj["id"])
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            if usage.get("completion_tokens"):
                output_tokens = int(usage["completion_tokens"])
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            finish = choice.get("finish_reason")
            if finish:
                stop = _stop_reason(str(finish))
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            think = delta.get("reasoning_content")
            if isinstance(think, str) and think:
                yield from open_block("thinking")
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "thinking_delta", "thinking": think},
                    },
                )
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                yield from open_block("text")
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    try:
                        call_index = int(call.get("index") or 0)
                    except (TypeError, ValueError):
                        call_index = 0
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(fn.get("name") or call.get("name") or "")
                    call_id = str(call.get("id") or "")
                    if call_index not in tool_blocks:
                        yield from open_tool(call_index, call_id, name)
                    frag = fn.get("arguments")
                    if isinstance(frag, str) and frag:
                        tool_args[call_index] = tool_args.get(call_index, "") + frag
                        yield _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": tool_blocks[call_index],
                                "delta": {"type": "input_json_delta", "partial_json": frag},
                            },
                        )
    yield from close_all()
