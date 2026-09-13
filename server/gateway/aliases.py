"""Zen 网关单别名表（方案 A 第一步）。

合并原来散在三处的改名逻辑，一次查表同时确定「上游真实模型 + 路由」：

- ``opencode._map_body_model``：zen-models.json key→value 查表（CLI 传 key，透传 value）
- ``anthropic.rewrite_body_model / rewrite_model_id``：Claude Code 档位/别名 → big-pickle

旧调用方（``opencode.proxy``）逐个迁移到 :func:`resolve_model` 后，
上述三函数即删除，此模块是唯一的改名真相源。

路由与 ``anthropic.uses_responses / uses_chat_completions`` 同语义：
responses 优先判定（muse-spark- 前缀不在原生 Messages 前缀内，无冲突）。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

# 与 anthropic.py 同源的判定常量（迁移完成前两边并存，值保持一致；
# 第二步归一内部表示时收敛到此模块，anthropic.py 只留翻译逻辑）。
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
    """模型 id → 路由（与 anthropic.uses_responses/uses_chat_completions 同语义）。"""
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
    空值/空白原样返回 CHAT（旧 rewrite_model_id 同样原样透传）。
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


def resolve_body_model(body: bytes) -> bytes:
    """请求 JSON 的 model 字段按单别名表改写；不可解析/无 model 原样返回。"""
    if not body:
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return body
    mapped, _ = resolve_model(payload["model"])
    if mapped == payload["model"]:
        return body
    payload["model"] = mapped
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def discover_model_entries() -> list[dict[str, Any]]:
    """GET /models 用的发现列表：zen-models.json 清单 + Claude Code 别名（供合并）。

    纯数据函数：opencode.local_model_entries 的清单部分 + anthropic 别名部分
    在此组装，调用方只拼一次时间戳。此处不缓存，缓存仍由各调用方负责。
    """
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        data = {"big-pickle": "big-pickle"}
    if not isinstance(data, dict):
        data = {"big-pickle": "big-pickle"}
    return [str(mid).strip() for mid in data if str(mid).strip()]
