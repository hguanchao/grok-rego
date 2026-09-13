"""Zen 网关单别名表：模型改名 + 上游路由的唯一真相源。

一次查表同时确定「上游真实模型 + 路由」：

- zen-models.json key→value（CLI 传 key，透传 value）
- Claude Code 档位/别名 → big-pickle
- 路由：muse-spark- → responses，claude-/qwen → native，其余 → chat
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

_NATIVE_MESSAGES_PREFIX = ("claude-", "qwen")
_FREE_UPSTREAM = "big-pickle"
_CLAUDE_CODE_ALIAS_PREFIX = (
    "claude-haiku",
    "claude-sonnet",
    "claude-opus",
    "claude-fable",
    "claude-3",
)
_CLAUDE_CODE_SHORT = {"haiku", "sonnet", "opus", "fable"}
_RESPONSES_ONLY_PREFIX = ("muse-spark-",)

_MODELS_JSON = Path(__file__).resolve().parent / "zen-models.json"


class RouteKind(str, Enum):
    """上游路由：chat（/chat/completions）/ responses（/responses）/ native（原生透传）。"""

    CHAT = "chat"
    RESPONSES = "responses"
    NATIVE = "native"


def _zen_models_dict() -> dict[str, str]:
    """读 zen-models.json 扁平映射（key=cli 模型 id，value=实际上游模型 id）。"""
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _route_of(ident: str) -> RouteKind:
    """模型 id → 上游路由。responses 优先（muse 不在 native 前缀内）。"""
    low = ident.strip().lower()
    if low.startswith(_RESPONSES_ONLY_PREFIX):
        return RouteKind.RESPONSES
    if low.startswith(_NATIVE_MESSAGES_PREFIX):
        return RouteKind.NATIVE
    return RouteKind.CHAT


def _rewrite_claude_code_alias(ident: str) -> str:
    """Claude Code 档位/全名/短名 → big-pickle；其它原样。"""
    low = ident.strip().lower()
    if low in _CLAUDE_CODE_SHORT or low.startswith(_CLAUDE_CODE_ALIAS_PREFIX):
        return _FREE_UPSTREAM
    return ident


def resolve_model(raw: str | None) -> tuple[str | None, RouteKind]:
    """单别名表：原始模型名 → (上游真实模型, 路由)。

    顺序与旧链路一致：zen-models.json 查表优先，miss 才走档位改写；
    空值/空白原样返回 CHAT。
    """
    if raw is None:
        return None, RouteKind.CHAT
    if not raw.strip():
        return raw, RouteKind.CHAT
    ident = raw.strip()
    mapped = _zen_models_dict().get(ident)
    if mapped is not None and str(mapped) != ident:
        final = str(mapped)
    else:
        final = _rewrite_claude_code_alias(ident)
    return final, _route_of(final)
