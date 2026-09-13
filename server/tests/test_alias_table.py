"""单别名表（方案 A 第一步）的黄金行为：resolve_model 全量覆盖旧三层改名。

旧链路（opencode.proxy 内串行三层）：
  body 经 _map_body_model（zen-models.json key→value）
  → 经 rewrite_body_model（Claude Code 档位→big-pickle）
  → 取出 model 再过 rewrite_model_id（同上，防直读 model 字段的调用方）

新表 resolve_model 必须与旧链路逐条一致（含大小写/空白/未知模型原样）。
"""

from __future__ import annotations

import json

from gateway.aliases import RouteKind, resolve_model


def _legacy_resolve(raw_model: str | None) -> tuple[str | None, str]:
    """用旧三层函数复刻改名链路（_map_body_model 已删除，内联其逻辑），返回 (模型, 路由）。

    旧链路 = zen-models.json 查表（opencode._map_body_model，已删，此处按原文内联）
    → rewrite_body_model（档位改写） → rewrite_model_id（同上）。
    """
    from gateway.aliases import _zen_models_dict
    from gateway.anthropic import rewrite_body_model, rewrite_model_id

    ident = (raw_model or "").strip() if raw_model else raw_model
    mapping = _zen_models_dict()
    if isinstance(ident, str) and ident in mapping and str(mapping[ident]) != ident:
        model: str | None = str(mapping[ident])
    else:
        body = json.dumps({"model": raw_model}).encode()
        body = rewrite_body_model(body)
        try:
            model = json.loads(body.decode())["model"]
        except (ValueError, KeyError):
            model = raw_model
        model = rewrite_model_id(model)
    # 旧路由判定：uses_responses 优先（muse 在 NATIVE 前缀外，不冲突）
    from gateway.anthropic import uses_chat_completions, uses_responses

    if model is not None and uses_responses(model):
        return model, "responses"
    if model is not None and not uses_chat_completions(model):
        return model, "native"
    return model, "chat"


def _routes_equal(a: RouteKind, b: str) -> bool:
    return a.value == b


def test_claude_code_tiers_map_to_big_pickle() -> None:
    """Claude Code 档位/全名/短名 → big-pickle，走 chat 路由（旧行为冻结）。"""
    for raw in (
        "sonnet",
        "haiku",
        "opus",
        "fable",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-opus-4-5",
        "claude-fable-5",
        "claude-3-opus-20240229",
        "SONNET",
        "  sonnet  ",
    ):
        model, route = resolve_model(raw)
        legacy_model, legacy_route = _legacy_resolve(raw)
        assert model == "big-pickle", raw
        assert model == legacy_model, raw
        assert _routes_equal(route, legacy_route), raw
        assert route == RouteKind.CHAT, raw


def test_muse_models_go_responses() -> None:
    """muse-spark 系走 responses 路由，名字原样透传（旧行为冻结）。"""
    for raw in (
        "muse-spark-1.3-contributor-free",
        "muse-spark-1.2-contributor-free",
        "MUSE-SPARK-1.3-contributor-free",
    ):
        model, route = resolve_model(raw)
        legacy_model, legacy_route = _legacy_resolve(raw)
        assert model == legacy_model == raw.strip(), raw
        assert _routes_equal(route, legacy_route), raw
        assert route == RouteKind.RESPONSES, raw


def test_free_models_passthrough_chat() -> None:
    """其余 -free 与 big-pickle 原样走 chat（旧行为冻结）。"""
    for raw in (
        "big-pickle",
        "deepseek-v4-flash-free",
        "mimo-v2.5-free",
        "gpt-5.3-codex-spark",
    ):
        model, route = resolve_model(raw)
        legacy_model, legacy_route = _legacy_resolve(raw)
        assert model == legacy_model, raw
        assert _routes_equal(route, legacy_route), raw


def test_unknown_and_empty_passthrough() -> None:
    """未知模型/空值原样透传，不抛异常（旧 rewrite_* 的容错语义）。"""
    model, route = resolve_model("some-future-model-xyz")
    assert model == "some-future-model-xyz"
    assert route == RouteKind.CHAT
    model, _ = resolve_model(None)
    assert model is None
    model, _ = resolve_model("   ")
    assert model == "   "


def test_zen_models_json_keys_respected() -> None:
    """zen-models.json 的 key→value 映射优先于档位改写（旧 _map_body_model 先跑）。"""
    from gateway import aliases

    mapping = aliases._zen_models_dict()
    assert mapping, "zen-models.json 为空时本用例无意义"
    for key, value in mapping.items():
        model, _ = resolve_model(key)
        assert model == value, key
