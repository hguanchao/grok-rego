"""
Claude Code / Anthropic Messages 适配。

Claude Code 固定打 `/zen/v1/messages`。按模型路由译到上游：
CHAT → `/v1/chat/completions`，RESPONSES（muse）→ `/v1/responses`，
NATIVE（claude-/qwen）原样透传。响应再转回 Messages / SSE。

模型改名与路由判定在 ``gateway.aliases``，本模块只做协议翻译。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from core.logger import logger

# GET /models 注入的 Claude Code 可识别 id（Zen public 密钥打不了付费 Claude）
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
    p = path.rstrip("/")
    return (
        p in ("/zen/v1/messages", "/zen/v1/messages/count_tokens")
        or p.startswith("/zen/v1/messages/")
        or p.endswith("/v1/messages")
        or p.endswith("/v1/messages/count_tokens")
        or "/v1/messages/" in p
    )


def is_count_tokens_path(path: str) -> bool:
    p = path.rstrip("/")
    return p == "/zen/v1/messages/count_tokens" or p.endswith("/v1/messages/count_tokens")


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


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    """Anthropic tools → OpenAI function tools（chat / responses 共用，唯一真相源）。"""
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def _convert_tool_choice(choice: Any) -> Any:
    """Anthropic tool_choice → OpenAI tool_choice（chat / responses 共用）。

    返回 None 表示无选择（调用方不设字段）；dict 形态保留 function 名。
    """
    if isinstance(choice, str):
        if choice == "any":
            return "required"
        if choice in ("auto", "none"):
            return choice
        return None
    if not isinstance(choice, dict):
        return None
    name = choice.get("name")
    ctype = choice.get("type")
    if ctype == "tool" and name:
        return {"type": "function", "function": {"name": name}}
    if ctype in ("auto", "any", "required", "none"):
        return "required" if ctype in ("any", "required") else ctype
    return None


def flatten_tools_for_responses(tools: Any) -> list[dict[str, Any]]:
    """Chat 嵌套 ``function`` 或 Anthropic tools → Responses 扁平 tools。

    上游 /responses 要 ``tools[0].name``，不认 ``tools[0].function.name``，
    否则 400 missing required field name。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if fn is not None:
            name = fn.get("name")
            desc = fn.get("description") or ""
            params = fn.get("parameters") or {"type": "object", "properties": {}}
        else:
            name = tool.get("name")
            desc = tool.get("description") or ""
            params = tool.get("input_schema") or tool.get("parameters") or {
                "type": "object",
                "properties": {},
            }
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "name": name,
                "description": desc,
                "parameters": params,
            }
        )
    return out


def flatten_tool_choice_for_responses(choice: Any) -> Any:
    """Chat ``{type:function, function:{name}}`` → Responses ``{type:function, name}``。"""
    if isinstance(choice, dict):
        fn = choice.get("function") if isinstance(choice.get("function"), dict) else None
        if fn and fn.get("name"):
            return {"type": "function", "name": fn["name"]}
        if str(choice.get("type") or "") == "function" and choice.get("name"):
            return {"type": "function", "name": choice["name"]}
    converted = _convert_tool_choice(choice)
    if isinstance(converted, dict):
        fn = converted.get("function") if isinstance(converted.get("function"), dict) else None
        if fn and fn.get("name"):
            return {"type": "function", "name": fn["name"]}
    return converted


def _convert_sampling(payload: dict[str, Any], out: dict[str, Any]) -> None:
    """max_tokens / temperature / top_p / stop_sequences 透传（chat / responses 共用）。"""
    if payload.get("max_tokens") is not None:
        out["max_tokens"] = payload.get("max_tokens")
    if payload.get("temperature") is not None:
        out["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        out["top_p"] = payload.get("top_p")
    if payload.get("stop_sequences"):
        out["stop"] = payload.get("stop_sequences")


def _iter_tool_blocks(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """扫描 Messages 全文：返回 (tool_use 声明块列表, 孤儿 tool_result 数）。

    孤儿 = tool_use_id 配不上任何 tool_use.id（/compact 压缩残留）。
    chat 路由的上游是宽松匹配（tool_call_id 对不上只影响本轮），
    responses 路由是严格配对（直接 400），是否丢弃由调用方按路由决定；
    此处只统计、不丢弃，保证两路由看到同一份声明集合。
    """
    declared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") == "tool_use" and block.get("id"):
                bid = str(block["id"])
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    declared.append(block)
    orphans = 0
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            if call_id and call_id not in seen_ids:
                orphans += 1
    return declared, orphans


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
                # chat 上游宽松匹配：孤儿 result 仅影响本轮，不 400，原样透传
                # （responses 路由才需丢弃，见 messages_to_responses）。
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
    _convert_sampling(payload, out)
    converted = _convert_tools(payload.get("tools"))
    if converted:
        out["tools"] = converted
    tool_choice = _convert_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
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
    pdet = usage.get("prompt_tokens_details")
    cache_read = int(pdet.get("cached_tokens") or 0) if isinstance(pdet, dict) else 0
    nonstream_usage: dict[str, int] = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }
    if cache_read:
        nonstream_usage["cache_read_input_tokens"] = cache_read
    return {
        "id": str(payload.get("id") or f"msg_{int(time.time())}"),
        "type": "message",
        "role": "assistant",
        "model": model or payload.get("model") or "",
        "content": content,
        "stop_reason": _stop_reason(str(choice.get("finish_reason") or "stop")),
        "stop_sequence": None,
        "usage": nonstream_usage,
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
    input_tokens = 0
    cache_read_tokens = 0
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

    def _final_usage() -> dict[str, int]:
        # Anthropic 原生流：input_tokens 在 message_start，累计值在 message_delta。
        # 此前只回 output_tokens，客户端用量恒为 0；现把上游 usage 全量回传。
        out: dict[str, int] = {"output_tokens": output_tokens}
        if input_tokens:
            out["input_tokens"] = input_tokens
        if cache_read_tokens:
            out["cache_read_input_tokens"] = cache_read_tokens
        return out

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
                "usage": _final_usage(),
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
            if usage.get("prompt_tokens"):
                input_tokens = int(usage["prompt_tokens"])
                pdet = usage.get("prompt_tokens_details")
                if isinstance(pdet, dict) and pdet.get("cached_tokens"):
                    cache_read_tokens = int(pdet["cached_tokens"])
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



# OpenAI Responses API 适配（muse 等仅支持 /responses 的免费模型）
# Anthropic Messages <-> OpenAI Responses 双向翻译：请求走 /responses，响应转回
# Anthropic Messages（整包 + SSE）。reasoning 摘要为加密/空时不透出，message 文本
# 逐块翻译为 Anthropic text 块；usage 由 opencode.py 侧按原始响应采样，不在此。

def _parse_json_obj(raw: Any) -> dict[str, Any]:
    """安全地把 JSON 字符串/对象解析为 dict；非法输入返回空 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def messages_to_responses(payload: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages 请求体 → OpenAI Responses 请求体。


    上游 /responses 只认 model / input / instructions / max_output_tokens /
    reasoning.effort / tools / tool_choice。input 支持字符串或消息数组；
    system 映射为 instructions；max_tokens 映射为 max_output_tokens。

    """
    system = _system_to_text(payload.get("system"))
    messages: list[dict[str, Any]] = []
    # /compact 残留的孤儿 tool_result（无对应 tool_use）：responses 上游按 call_id
    # 严格配对会 400，此处预过滤；声明集合走共享扫描，与 chat 路由同源。
    _, orphans = _iter_tool_blocks(payload)
    declared_ids: set[str] = set()
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") == "tool_use" and block.get("id"):
                declared_ids.add(str(block["id"]))
    dropped_orphans = 0
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant"):
            role = role.replace("assistant", "user") if role == "assistant" else "user"
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": _text_of(content)})
            continue
        # 块式 content：text 合并为普通消息；工具调用/输出必须走顶层
        # function_call / function_call_output 项（上游按 call_id 校验配对，
        # 拼进普通文本会 400 No tool output found）
        text_parts: list[str] = []
        tool_items: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                output_text = _text_of(block.get("content"))
                if not call_id:
                    # 异常历史（缺 tool_use_id）：回退普通文本，保证请求可解析
                    text_parts.append(output_text)
                elif call_id not in declared_ids:
                    # 压缩后残留的孤儿 tool_result：上游配对校验必 400，直接丢弃
                    dropped_orphans += 1
                else:
                    tool_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output_text,
                        }
                    )
            elif btype == "tool_use":
                tool_items.append(
                    {
                        "type": "function_call",
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        "call_id": str(block.get("id") or ""),
                    }
                )
        if text_parts:
            messages.append({"role": role, "content": "".join(text_parts)})
        messages.extend(tool_items)
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "input": messages if messages else "",
        "stream": bool(payload.get("stream")),
    }
    if dropped_orphans:
        logger.warning(
            f"[网关] Responses 翻译丢弃 {dropped_orphans} 个孤儿 tool_result"
            "（疑 /compact 压缩残留，避免上游 400）"
        )
    if system:
        out["instructions"] = system
    # 思考预算 → 推理档位；Responses 用 reasoning.effort（上游不认 max，钳制到合法档）
    effort = effort_from_body(payload)
    if effort and supports_reasoning(str(out.get("model") or "")):
        # 上游 /responses 合法档：none/minimal/low/medium/high/xhigh；max 不认，映射 xhigh；未知档回退 high
        _resp_eff_alias = {"max": "xhigh"}
        _resp_eff = _resp_eff_alias.get(effort, effort)
        if _resp_eff not in ("none", "minimal", "low", "medium", "high", "xhigh"):
            _resp_eff = "high"
        out["reasoning"] = {"effort": _resp_eff, "summary": "concise"}
    # 不调用 _convert_sampling：chat 的 max_tokens / stop 对 /responses 是 unknown parameter，必 400
    if payload.get("temperature") is not None:
        out["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        out["top_p"] = payload.get("top_p")
    if payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload.get("max_tokens")
    converted = flatten_tools_for_responses(payload.get("tools"))
    if converted:
        out["tools"] = converted
    tool_choice = flatten_tool_choice_for_responses(payload.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    return out


def responses_to_message(payload: dict[str, Any], model: str | None) -> dict[str, Any]:
    """OpenAI Responses 整包响应 → Anthropic Messages。


    提取 message.output_text 为 text 块，function_call 为 tool_use；
    因上游该模型推理摘要加密/为空，不透出 thinking；usage 仅映射
    input/output_tokens（reasoning_tokens 由原响应 usage 单独落库）。"""
    content: list[dict[str, Any]] = []
    stop = "end_turn"
    incomplete = payload.get("incomplete_details")
    if isinstance(incomplete, dict) and incomplete.get("reason") in (
        "max_output_tokens",
        "max_output_tokens_stream_suggested_rec",
    ):
        stop = "max_tokens"
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("output_text", "text"):
                    txt = str(part.get("text") or "")
                    if txt:
                        content.append({"type": "text", "text": txt})
        elif item.get("type") == "function_call":
            content.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("call_id") or ""),
                    "name": str(item.get("name") or ""),
                    "input": _parse_json_obj(item.get("arguments")),
                }
            )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    idet = usage.get("input_tokens_details")
    resp_cache = int(idet.get("cached_tokens") or 0) if isinstance(idet, dict) else 0
    resp_usage: dict[str, int] = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    if resp_cache:
        resp_usage["cache_read_input_tokens"] = resp_cache
    return {
        "id": f"msg_{int(time.time())}",
        "type": "message",
        "role": "assistant",
        "model": model or payload.get("model") or "",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": resp_usage,
    }


def iter_responses_sse(gen: Iterator[bytes], model: str | None) -> Iterator[bytes]:
    """把 OpenAI Responses SSE 块转成 Anthropic Messages SSE。


    本实现把 message 的 output_text 增量逐块转成 Anthropic text 块，
    function_call 增量转成 tool_use 块（stop_reason 相应收敛为 tool_use，
    否则 CLI 拿不到工具调用而表现为空回复）；reasoning 摘要（上游为加密/
    空时不透出）一律不产生可见块，避免 Claude Code 拿到大片空白 thinking。
    usage 由调用侧对原始响应采样，不在此处理。"""
    buf = b""
    started = False
    text_open = False
    block_index = -1
    msg_id = f"msg_{int(time.time() * 1000)}"
    output_tokens = 0
    input_tokens = 0
    cache_read_tokens = 0
    stop = "end_turn"
    # 流终 stop_reason 收敛为 tool_use，CLI 才会执行工具并回传 tool_result
    tool_seen = False
    tool_open: int | None = None
    tool_args = ""

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

    def open_text() -> Iterator[bytes]:
        nonlocal text_open, block_index
        if text_open:
            return
        yield from ensure_message()
        block_index += 1
        text_open = True
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def close_text() -> Iterator[bytes]:
        nonlocal text_open
        if not text_open:
            return
        text_open = False
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": block_index},
        )

    def close_tool() -> Iterator[bytes]:
        nonlocal tool_open, tool_args
        if tool_open is None:
            return
        yield _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": tool_open},
        )
        tool_open = None
        tool_args = ""

    def open_tool(call_id: str, name: str) -> Iterator[bytes]:
        nonlocal block_index, tool_open, tool_seen, tool_args
        yield from ensure_message()
        yield from close_text()
        yield from close_tool()
        block_index += 1
        tool_open = block_index
        tool_seen = True
        tool_args = ""
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {
                    "type": "tool_use",
                    # Anthropic 侧 id 用 call_id，tool_result 回传时按 id 对上 tool_use_id
                    "id": call_id or f"toolu_{block_index}",
                    "name": name or "",
                    "input": {},
                },
            },
        )

    def _final_usage() -> dict[str, int]:
        # Responses 路由此前只回 output_tokens，客户端用量恒为 0；
        # input_tokens / cache_read_input_tokens 从 response.usage 透传。
        out: dict[str, int] = {"output_tokens": output_tokens}
        if input_tokens:
            out["input_tokens"] = input_tokens
        if cache_read_tokens:
            out["cache_read_input_tokens"] = cache_read_tokens
        return out

    def finish() -> Iterator[bytes]:
        # 先发 message_start：reasoning 耗尽预算等场景上游无任何文本增量，
        # 不补 start 会让 Claude Code 收到无头 SSE 而判定流式不完整
        yield from ensure_message()
        yield from close_text()
        yield from close_tool()
        final_stop = stop
        if final_stop == "end_turn" and tool_seen:
            final_stop = "tool_use"
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": final_stop, "stop_sequence": None},
                "usage": _final_usage(),
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})

    for chunk in gen:
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
                yield from finish()
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            t = obj.get("type")
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            # 结束/进度事件携带 response 全量（含 usage / id / incomplete）
            resp = obj.get("response")
            if isinstance(resp, dict):
                if resp.get("id"):
                    msg_id = str(resp["id"])
                u = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
                if u.get("input_tokens"):
                    input_tokens = int(u["input_tokens"])
                    idet = u.get("input_tokens_details")
                    if isinstance(idet, dict) and idet.get("cached_tokens"):
                        cache_read_tokens = int(idet["cached_tokens"])
                if u.get("output_tokens"):
                    output_tokens = int(u["output_tokens"])
                inc = resp.get("incomplete_details")
                if isinstance(inc, dict) and inc.get("reason") in (
                    "max_output_tokens",
                    "max_output_tokens_stream_suggested_rec",
                ):
                    stop = "max_tokens"
            if t == "response.output_item.added":
                # function_call item 在 added 已带 call_id/name，先开块才能接住后续参数增量
                if item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    yield from open_tool(call_id, str(item.get("name") or ""))
            elif t == "response.output_text.delta":
                piece = obj.get("delta")
                if isinstance(piece, str) and piece:
                    yield from open_text()
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {"type": "text_delta", "text": piece},
                        },
                    )
            elif t == "response.function_call_arguments.delta":
                # 参数增量逐段转发 partial_json；上游该事件 delta 可能为空串，空串不落块
                piece = obj.get("delta")
                if isinstance(piece, str) and piece and tool_open is not None:
                    tool_args += piece
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_open,
                            "delta": {"type": "input_json_delta", "partial_json": piece},
                        },
                    )
            elif t == "response.reasoning_summary_text.delta":
                pass  # 上游该模型不产出可见推理，忽略
            elif t == "response.output_item.done":
                if item.get("type") == "message":
                    yield from close_text()
                elif item.get("type") == "function_call":
                    # 增量流为空时用 done 携带的全量 arguments 兜底（上游两种形态都出现过）
                    raw = item.get("arguments")
                    if (
                        tool_open is not None
                        and not tool_args
                        and isinstance(raw, str)
                        and raw
                    ):
                        yield _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": tool_open,
                                "delta": {"type": "input_json_delta", "partial_json": raw},
                            },
                        )
                    yield from close_tool()
            elif t in ("response.completed", "response.incomplete"):
                yield from finish()
                return
    yield from finish()  # 循环自然结束未收尾，补收尾
