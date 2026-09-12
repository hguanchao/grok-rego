"""网关被动质量审计：转发时旁路扫描上游响应，判定降智（missing_thinking）。

信号信任层级对齐 grok2api v3.1.8 quality_retry 实现（被动无重试形态，阈值偏保守）：
- 明文思考增量（reasoning_text / reasoning_summary delta）是最强正常证据；
- 密文 blob 须过 256B 下限才算思考证据——一小截 cipher stub 不是思考；
- 账单为 0 的密文流（cipher drool，128k 状态循环口水签名）判降智；
- 假思考时序签名：思考证据齐全但可见文本在首字后 2s 内倾倒（真思考需要时间）。

与参考实现的差异（本上游实测校准）：
- 参考按 4B/reasoning_token 要求密文下限并不信任账单；实测本上游真实思考的
  密文/账单比为 3.4~5.3 B/token，照搬会误伤，故密文 blob ≥256B 即视为思考证据；
- 保留两个已验证的降智签名：cipher drool（密文过限但账单为 0）与
  假密文倾倒（证据齐全但可见文本秒级倾倒）。

扣流模式（live_verdict）：上游流先扣住不转发，边收边裁决——
- 明文思考增量 → 立即放行；密文证据 → 等可见文本流出 2s 后放行（防瞬间倾倒）；
- 首字后 2s 内倾倒 ≥128 token 且密文过限 → withhold（假密文 dump，换号重试）；
- 流终止仍全无思考证据且可见输出 ≥32 → withhold（missing_thinking，换号重试）；
- 输出过短 / 纯工具调用轮次 / 无终止事件的截断流 → 放行（无法判定，不惩罚）。
"""

from __future__ import annotations

import json
import time
from typing import Any

# 样本有效性：可见输出低于该 token 数不判定（短回答无法区分「没思考」与「不需要思考」）
MIN_OUTPUT_TOKENS = 32
# 密文思考证据下限：一小截 cipher stub 不是思考
MIN_ENCRYPTED_BYTES = 256
# 假思考时序签名：可见文本在首字后 2s 内倾倒（实测真实流式 ≈5-6 token/s，
# 128 token / 2s = 64 token/s，远超真实速率）
FAKE_DUMP_WINDOW_MS = 2000
FAKE_DUMP_MIN_VISIBLE = 128
FAKE_DUMP_MIN_REASONING = 80
# 扣流放行的最小可见输出（对齐参考实现 hold 路径 minOutput=8）；
# 终止判定用 max(可见 token, usage 输出 token)——工具调用轮次参数不走
# output_text 通道，可见为 0 但 usage 有输出，同样参与判定
HOLD_MIN_OUTPUT = 8

HEALTHY = "healthy"
DEGRADED = "degraded"
IGNORED = "ignored"

# 扣流实时裁决
HOLD_WAIT = "wait"
HOLD_DELIVER = "deliver"
HOLD_WITHHOLD = "withhold"


def _classify(
    *,
    plaintext_thinking: bool,
    encrypted_bytes: int,
    usage_reported: bool,
    usage_reasoning: int,
    visible_tokens: int,
    flush_ms: int,
) -> tuple[str, str]:
    """共享判定矩阵。flush_ms=-1 表示无时序信号（非流式）。"""
    if visible_tokens < MIN_OUTPUT_TOKENS:
        return IGNORED, "insufficient_output"
    if plaintext_thinking:
        return HEALTHY, "plaintext_thinking"
    has_bill = usage_reasoning >= FAKE_DUMP_MIN_REASONING or encrypted_bytes >= MIN_ENCRYPTED_BYTES
    if 0 <= flush_ms < FAKE_DUMP_WINDOW_MS and visible_tokens >= FAKE_DUMP_MIN_VISIBLE and has_bill:
        return DEGRADED, "fake_dump"
    if not usage_reported:
        return IGNORED, "usage_missing"
    if usage_reasoning > 0:
        return HEALTHY, "reasoning_billed"
    # 账单为 0 且无明文思考：密文过下限 = cipher 口水（假思考）；全无证据 = 纯降智
    if encrypted_bytes >= MIN_ENCRYPTED_BYTES:
        return DEGRADED, "cipher_drool"
    return DEGRADED, "missing_thinking"


def classify_nonstream(payload: bytes) -> tuple[str, str]:
    """非流式响应整包判定（无时序信号，仅基础规则）。"""
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return IGNORED, "unparseable"
    if not isinstance(obj, dict):
        return IGNORED, "unparseable"
    visible_chars = 0
    encrypted_bytes = 0
    usage_reasoning = 0
    usage_reported = False
    # Responses：output 数组（message 文本 + reasoning 密文）
    output = obj.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning":
                enc = item.get("encrypted_content")
                if isinstance(enc, str):
                    encrypted_bytes = max(encrypted_bytes, len(enc.strip()))
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        visible_chars += len(part["text"])
    # Chat：choices[].message.content
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            visible_chars += len(message["content"])
    usage = obj.get("usage")
    if isinstance(usage, dict):
        usage_reported = True
        details = usage.get("output_tokens_details") or usage.get("completion_tokens_details")
        if isinstance(details, dict):
            usage_reasoning = int(details.get("reasoning_tokens") or 0)
    visible_tokens = (visible_chars + 3) // 4
    return _classify(
        plaintext_thinking=False,
        encrypted_bytes=encrypted_bytes,
        usage_reported=usage_reported,
        usage_reasoning=usage_reasoning,
        visible_tokens=visible_tokens,
        flush_ms=-1,
    )


class QualityScanner:
    """上游流式响应的旁路质量扫描器（与转发循环同块消费，零额外 IO）。

    逐行解析 SSE data 事件，累积思考/可见输出/账单信号；finish() 在流结束后
    给出判定。协议按事件形态自动识别（Responses / Chat Completions）。
    """

    def __init__(self) -> None:
        self._pending = b""
        self._plaintext_thinking = False
        self._encrypted_bytes = 0
        self._visible_chars = 0
        self._first_visible_mono = 0.0
        self._usage_reasoning = 0
        self._usage_output = 0
        self._usage_reported = False
        self._terminal = False

    @property
    def visible_tokens(self) -> int:
        """流式可见输出 token 估算（字符数 / 4，对齐参考实现的粗估口径）。"""
        return (self._visible_chars + 3) // 4

    @property
    def encrypted_bytes(self) -> int:
        return self._encrypted_bytes

    @property
    def usage_reasoning(self) -> int:
        return self._usage_reasoning

    def feed(self, chunk: bytes) -> None:
        """喂入一个转发块（纯解析，不改写转发内容）。"""
        if not chunk:
            return
        self._pending += chunk
        # 上一块末尾可能残留不完整行，逐行消费直至无完整行
        while True:
            index = self._pending.find(b"\n")
            if index < 0:
                if len(self._pending) > (1 << 20):
                    self._pending = b""
                return
            line = self._pending[:index].strip()
            self._pending = self._pending[index + 1 :]
            if not line:
                continue
            payload = line[5:].strip() if line.startswith(b"data:") else b""
            if not payload:
                continue
            if payload == b"[DONE]":
                self._terminal = True
                continue
            self._observe(payload)

    def live_verdict(self) -> tuple[str, str]:
        """扣流模式实时裁决。返回 (verdict, reason)。

        verdict: HOLD_WAIT=继续扣留观察 / HOLD_DELIVER=放行（回放已扣内容后续传）/
                 HOLD_WITHHOLD=降智（丢弃本次响应，换号重试）。

        规则对齐 grok2api ClassifyQualityHold + cipherDrool：
        - 明文思考增量 = 直接放行（最强证据）；
        - 密文证据 ≥256B：等可见文本流出 2s 后放行（防假密文瞬间倾倒）或流终止；
          但终止时账单为 0 且有输出 → cipher drool（假思考循环签名）→ withhold；
        - 全无思考证据：终止时 max(可见, usage 输出) ≥8 → withhold（missing_thinking，
          账单不作证据）；输出过短（纯工具调用轮次）→ 放行。
        """
        now = time.monotonic()
        flush_ms = (
            int((now - self._first_visible_mono) * 1000) if self._first_visible_mono else -1
        )
        visible = self.visible_tokens
        output = max(visible, self._usage_output)
        if self._plaintext_thinking:
            return HOLD_DELIVER, "plaintext_thinking"
        if (
            0 <= flush_ms < FAKE_DUMP_WINDOW_MS
            and visible >= FAKE_DUMP_MIN_VISIBLE
            and self._encrypted_bytes >= MIN_ENCRYPTED_BYTES
        ):
            return HOLD_WITHHOLD, "fake_dump"
        if self._encrypted_bytes >= MIN_ENCRYPTED_BYTES:
            # 密文思考证据：等可见文本流出 2s 再放行（防假密文瞬间倾倒），或流终止。
            # 终止时账单为 0 的密文流是 cipher drool（无明文思考的假思考循环）→ withhold
            if self._terminal:
                if self._usage_reported and self._usage_reasoning <= 0 and visible >= HOLD_MIN_OUTPUT:
                    return HOLD_WITHHOLD, "cipher_drool"
                return HOLD_DELIVER, "encrypted_thinking"
            if visible >= HOLD_MIN_OUTPUT and flush_ms >= FAKE_DUMP_WINDOW_MS:
                return HOLD_DELIVER, "encrypted_thinking"
            return HOLD_WAIT, ""
        if self._terminal:
            # 全无思考证据：有实际输出 → 降智（missing_thinking，账单不作证据）；
            # 输出过短（含纯工具调用轮次）→ 无法判定，放行
            if output >= HOLD_MIN_OUTPUT:
                return HOLD_WITHHOLD, "missing_thinking"
            return HOLD_DELIVER, "insufficient_output"
        return HOLD_WAIT, ""

    # ─── 事件解析 ─────────────────────────────────────────

    def _note_visible(self, text: str) -> None:
        if not text:
            return
        if not self._first_visible_mono:
            self._first_visible_mono = time.monotonic()
        self._visible_chars += len(text)

    def _observe(self, payload: bytes) -> None:
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        if "choices" in obj:
            self._observe_chat(obj)
        else:
            self._observe_responses(obj)

    def _observe_chat(self, obj: dict[str, Any]) -> None:
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    for key in ("reasoning", "reasoning_content", "thinking_content"):
                        value = delta.get(key)
                        if isinstance(value, str) and value.strip():
                            self._plaintext_thinking = True
                    content = delta.get("content")
                    if isinstance(content, str):
                        self._note_visible(content)
                if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
                    self._terminal = True
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._usage_reported = True
            self._usage_output = max(
                self._usage_output, int(usage.get("completion_tokens") or 0)
            )
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict):
                self._usage_reasoning = max(
                    self._usage_reasoning, int(details.get("reasoning_tokens") or 0)
                )

    def _observe_responses(self, obj: dict[str, Any]) -> None:
        etype = str(obj.get("type") or "")
        if etype in ("response.completed", "response.incomplete", "response.failed"):
            self._terminal = True
        if etype in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            delta = obj.get("delta")
            if isinstance(delta, str) and delta.strip():
                self._plaintext_thinking = True
        if etype in ("response.output_item.added", "response.output_item.done"):
            self._note_reasoning_item(obj.get("item"))
        if etype == "response.output_text.delta":
            delta = obj.get("delta")
            if isinstance(delta, str):
                self._note_visible(delta)
        payload = obj.get("response") if etype == "response.completed" else None
        if isinstance(payload, dict):
            for item in payload.get("output") or []:
                self._note_reasoning_item(item)
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._usage_reported = True
                self._usage_output = max(
                    self._usage_output, int(usage.get("output_tokens") or 0)
                )
                details = usage.get("output_tokens_details")
                if isinstance(details, dict):
                    self._usage_reasoning = max(
                        self._usage_reasoning, int(details.get("reasoning_tokens") or 0)
                    )

    def _note_reasoning_item(self, item: Any) -> None:
        """reasoning item：只累计密文字节数，item 本身不作为思考证据。"""
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            return
        enc = item.get("encrypted_content")
        if isinstance(enc, str):
            self._encrypted_bytes = max(self._encrypted_bytes, len(enc.strip()))
