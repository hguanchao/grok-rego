"""网关用量 Token 提取工具。

网关转发上游响应时，逐块/整包采样并解析其中的 usage token 计量，
供 `insert_usage` 落库统计。所有解析均「尽力而为」：解析失败返回全 0，
绝不因解析异常影响转发主流程。

支持两种协议形态：
- OpenAI Chat Completions / Responses：`usage` 字段含
  prompt_tokens / completion_tokens，可选的 prompt_tokens_details.cached_tokens
  与 completion_tokens_details.reasoning_tokens。
- Anthropic Messages：usage 含 input_tokens / output_tokens，
  cache_creation_input_tokens / cache_read_input_tokens 与 thinking（reasoning）。

对外两个入口：
- `extract_nonstream(body)`：整包 JSON 字节 → 用量字典。
- `StreamUsageAccumulator`：流式逐 chunk 喂入，结束后 `.result()` 取用量。
"""

from __future__ import annotations

import json
from typing import Any

# 采样容量上限：超过即停止累积，避免流式响应无限增长内存
_MAX_BUFFER = 8 * 1024 * 1024


class StreamOutcome:
    """观察 SSE 错误与正常终止事件，供透传、协议转换和用量记录共用。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frame = bytearray()
        self.failed = False
        self.error = ""
        self.warning = ""
        self.terminal = False

    def fail(self, message: str) -> None:
        self.failed = True
        if message and not self.error:
            self.error = " ".join(message.split())[:255]

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            line = line.removesuffix(b"\r")
            if not line:
                self._consume(bytes(self._frame))
                self._frame.clear()
            else:
                self._frame.extend(line + b"\n")
        if len(self._buf) + len(self._frame) > 64 * 1024:
            self._buf.clear()
            self._frame.clear()

    def _consume(self, event: bytes) -> None:
        event_name = ""
        data_lines: list[bytes] = []
        for line in event.splitlines():
            if line.startswith(b"event:"):
                event_name = line[6:].decode("utf-8", "replace").strip()
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return
        raw = b"\n".join(data_lines).decode("utf-8", "replace").strip()
        if raw == "[DONE]":
            self.terminal = True
            return
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            if event_name.lower() == "error":
                self.fail(raw)
            return
        if not isinstance(obj, dict):
            return
        kind = str(obj.get("type") or event_name).lower()
        if kind in ("response.completed", "response.incomplete", "message_stop"):
            self.terminal = True
        if kind == "response.incomplete" and not self.warning:
            response = obj.get("response") if isinstance(obj.get("response"), dict) else {}
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            self.warning = f"响应不完整：{reason or '上游标记为 incomplete'}"[:255]
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                finish = choice.get("finish_reason") if isinstance(choice, dict) else None
                if finish:
                    self.terminal = True
                    if finish == "content_filter" and not self.warning:
                        self.warning = "上游内容过滤导致响应提前结束"
        if kind in ("error", "response.failed") or obj.get("error"):
            error = obj.get("error")
            if not error and isinstance(obj.get("response"), dict):
                error = obj["response"].get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or error.get("type") or "").strip()
                message = str(error.get("message") or error.get("detail") or "").strip()
                detail = ": ".join(part for part in (code, message) if part)
            elif isinstance(error, str):
                detail = error.strip()
            else:
                detail = raw
            self.fail(detail or f"上游 SSE 事件 {kind}")

    def finish(self) -> None:
        if self._buf:
            self._frame.extend(self._buf)
            self._buf.clear()
        if self._frame:
            self._consume(bytes(self._frame))
            self._frame.clear()
        if not self.failed and not self.terminal:
            self.fail("上游 SSE 流提前结束（未收到正常结束事件）")


def _to_int(value: Any) -> int:
    """安全转 int，非数字返回 0。"""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _openai_usage(usage: Any) -> dict[str, int]:
    """OpenAI 风格 usage 字典 → (prompt, completion, cache, reasoning)。"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    prompt = _to_int(usage.get("prompt_tokens"))
    completion = _to_int(usage.get("completion_tokens"))
    # Responses API 兼容：input_tokens / output_tokens（OpenAI Chat 用 prompt/completion）
    if not prompt:
        prompt = _to_int(usage.get("input_tokens"))
    if not completion:
        completion = _to_int(usage.get("output_tokens"))
    cache = 0
    reason = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cache += _to_int(details.get("cached_tokens"))
    # Responses API：缓存命中字段是 input_tokens_details.cached_tokens（OpenAI Chat 才是 prompt_tokens_details）
    # 两者互斥出现，未从 prompt 侧取到缓存时再查 input 侧，避免漏计缓存命中
    if not cache:
        i_details = usage.get("input_tokens_details")
        if isinstance(i_details, dict):
            cache += _to_int(i_details.get("cached_tokens"))
    c_details = usage.get("completion_tokens_details")
    if isinstance(c_details, dict):
        reason += _to_int(c_details.get("reasoning_tokens"))
    # Responses API：推理 token 也可能在 output_tokens_details.reasoning_tokens（与 completion 侧同语义兜底）
    if not reason:
        o_details = usage.get("output_tokens_details")
        if isinstance(o_details, dict):
            reason += _to_int(o_details.get("reasoning_tokens"))
    # Anthropic 兼容字段（上游偶发混用）
    if not cache:
        cache = _to_int(usage.get("cache_read_input_tokens")) + _to_int(
            usage.get("cache_creation_input_tokens")
        )
    if not reason:
        reason = _to_int(usage.get("thinking_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_tokens": cache,
        "reasoning_tokens": reason,
    }


def _anthropic_usage(usage: Any) -> dict[str, int]:
    """Anthropic 风格 usage 字典 → (input, output, cache, thinking)。"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    cache = _to_int(usage.get("cache_read_input_tokens")) + _to_int(
        usage.get("cache_creation_input_tokens")
    )
    return {
        "prompt_tokens": _to_int(usage.get("input_tokens")),
        "completion_tokens": _to_int(usage.get("output_tokens")),
        "cache_tokens": cache,
        "reasoning_tokens": _to_int(usage.get("thinking_tokens")),
    }


def _dispatch_usage(usage: Any) -> dict[str, int] | None:
    """按 usage 键形态自动归类 OpenAI / Anthropic。

    Responses API 的 input_tokens/output_tokens 与 Anthropic 同名，但带
    input_tokens_details / output_tokens_details 特征键可区分；带 details 键一律走
    OpenAI 风格，否则 input/output 键走 Anthropic 风格。
    """
    if not isinstance(usage, dict):
        return None
    has_details = any(
        k in usage
        for k in ("input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details")
    )
    if has_details:
        return _openai_usage(usage)
    if ("input_tokens" in usage or "output_tokens" in usage) and not (
        "prompt_tokens" in usage or "completion_tokens" in usage
    ):
        return _anthropic_usage(usage)
    return _openai_usage(usage)


def _parse_object(payload: Any) -> dict[str, int] | None:
    """从已解析 JSON 顶层取 usage；Anthropic message 的 usage 在 message 内。"""
    if not isinstance(payload, dict):
        return None
    if "usage" in payload:
        return _dispatch_usage(payload.get("usage"))
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return _anthropic_usage(message.get("usage"))
    return None


def extract_nonstream(body: bytes) -> dict[str, int]:
    """整包（非流式）响应字节 → 用量字典；解析失败返回全 0。"""
    if not body:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # SSE 误入非流式（理论不应发生），尽力累积一次
        acc = StreamUsageAccumulator()
        acc.feed(body)
        return acc.result()
    got = _parse_object(obj)
    if got is not None:
        return got
    # Responses 风格：usage 嵌套在顶层，但与 Anthropic message 结构冲突时走兜底
    return {"prompt_tokens": 0, "completion_tokens": 0, "cache_tokens": 0, "reasoning_tokens": 0}


class StreamUsageAccumulator:
    """流式响应逐 chunk 累积器。

    用法：
        acc = StreamUsageAccumulator()
        for chunk in upstream.iter_content(chunk_size=4096):
            acc.feed(chunk)     # 纯解析不改写，可与时块转发并行
            handler.wfile.write(chunk)
        usage = acc.result()    # 流结束后取用量
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._prompt = 0
        self._completion = 0
        self._cache = 0
        self._reasoning = 0

    def _apply(self, usage: dict[str, int]) -> None:
        p = usage.get("prompt_tokens", 0)
        c = usage.get("completion_tokens", 0)
        if p or c:
            # 取各字段最大非零值：SSE 首尾块可能只带一侧字段
            self._prompt = max(self._prompt, p)
            self._completion = max(self._completion, c)
        self._cache = max(self._cache, usage.get("cache_tokens", 0))
        self._reasoning = max(self._reasoning, usage.get("reasoning_tokens", 0))

    def feed(self, chunk: bytes) -> None:
        """喂入一个响应块并解析其中的 usage 片段。"""
        if not chunk:
            return
        # 上一块末尾可能残留不完整行，拼接到当前第一行（同一 data 行被拆包）
        lines = chunk.split(b"\n")
        if self._buf:
            lines[0] = bytes(self._buf) + lines[0]
        self._buf = bytearray()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            # 最后一行且不以 } 结尾 → 可能不完整，暂存等下一块拼接；否则立即解析
            if i == len(lines) - 1 and not line.rstrip().endswith(b"}"):
                self._buf.extend(line)
                break
            self._consume_line(line)
        if len(self._buf) > _MAX_BUFFER:
            self._buf = bytearray()

    def _consume_line(self, raw: bytes) -> None:
        """解析单条 SSE data 行。"""
        text = raw.decode("utf-8", "replace")
        marker = text.find("data:")
        if marker < 0:
            return
        data = text[marker + 5 :].strip()
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        got = self._parse_payload(obj)
        if got is not None:
            self._apply(got)
            return
        # Anthropic SSE：message_start 带 message.usage；message_delta 顶层 usage
        m = obj.get("message")
        if isinstance(m, dict) and isinstance(m.get("usage"), dict):
            self._apply(_anthropic_usage(m.get("usage")))
        elif isinstance(obj.get("usage"), dict):
            self._apply(_anthropic_usage(obj.get("usage")))

    @staticmethod
    def _parse_payload(payload: Any) -> dict[str, int] | None:
        """从已解析 JSON 取 usage；按键风格自动区分 OpenAI / Anthropic。"""
        if not isinstance(payload, dict):
            return None
        if "usage" in payload:
            return _dispatch_usage(payload.get("usage"))
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return _anthropic_usage(message.get("usage"))
        # Responses 流式事件（response.completed）：usage 深层嵌套在 response.usage
        inner = payload.get("response")
        if isinstance(inner, dict):
            return _dispatch_usage(inner.get("usage"))
        return None

    def result(self) -> dict[str, int]:
        """流结束后的最终用量。"""
        return {
            "prompt_tokens": self._prompt,
            "completion_tokens": self._completion,
            "cache_tokens": self._cache,
            "reasoning_tokens": self._reasoning,
        }
