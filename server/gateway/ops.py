"""
网关运维聚合层（前端「网关运维」页唯一数据源，挂 /api/gateway/ops）。

设计要点（自有方案，不照搬参考项目）：
- 通道注册表 CHANNELS：Zen / Grok 各通道声明 id、标题、本机挂载点与快照来源，
  新增通道只需注册一行，聚合与前端渲染逻辑零改动。
- 单一聚合快照 ops_snapshot()：一次返回页面所需全部数据
  （运行时长 / 通道路由信息 / 近 24h 分通道请求与失败统计 / 号池账号运行态 /
  配置摘要），避免前端多次往返拼装。
- 24h 统计由 SQL 直接聚合 usages 表，
  不在内存驻留、不随页面刷新累积误差。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core import config
from core.util import mask_email, now_iso_tz
from db import get_all_accounts, query_account_usage_24h, query_channel_usage_24h
from gateway import grok as grok_gateway
from gateway import opencode as zen_gateway

# 进程启动时刻，用于 uptime 展示
_STARTED_AT = time.time()


def uptime_sec() -> int:
    """服务运行时长（秒）。"""
    return max(0, int(time.time() - _STARTED_AT))


def _channels() -> list[dict[str, Any]]:
    """通道注册表：路由信息 + 各自快照中的接入摘要。

    snapshot 返回的 stats / logs 体量较大且运维页不直接消费，
    这里只摘取路由相关字段。
    """
    registry: list[tuple[str, str, str, Callable[[], dict[str, Any]]]] = [
        ("zen", "OpenCode Zen", "/zen/v1", zen_gateway.snapshot),
        ("grok", "Grok 号池", "/grok/v1", grok_gateway.snapshot),
    ]
    out: list[dict[str, Any]] = []
    for channel_id, title, prefix, snapshot_fn in registry:
        snap = snapshot_fn()
        item: dict[str, Any] = {
            "id": channel_id,
            "title": title,
            "base_path": prefix,
            "client_base": snap.get("client_base") or "",
            "upstream": snap.get("upstream") or "",
        }
        # 通道特有提示字段（Zen 固定 key / Grok 选号方式）
        for extra in ("key", "select"):
            if snap.get(extra):
                item[extra] = snap[extra]
        out.append(item)
    return out


def _account_pool() -> dict[str, Any]:
    """号池账号运行态：计数汇总 + 逐账号脱敏行（含近 24h 请求数）。"""
    accounts = get_all_accounts()
    per_account = query_account_usage_24h()

    rows: list[dict[str, Any]] = []
    authed = 0
    disabled = 0
    for acc in accounts:
        has_token = bool(str(acc.get("access_token") or "").strip())
        is_disabled = int(acc.get("status") or 1) != 1
        if has_token:
            authed += 1
        if is_disabled:
            disabled += 1
        rows.append(
            {
                "id": acc["id"],
                # 安全约束：管理面展示一律脱敏邮箱
                "email": mask_email(acc.get("email")),
                "authed": has_token,
                "disabled": is_disabled,
                "requests_24h": per_account.get(int(acc["id"]), 0),
            }
        )

    return {
        "total": len(accounts),
        "authed": authed,
        "disabled": disabled,
        "requests_24h": sum(per_account.values()),
        "accounts": rows,
    }


def ops_snapshot() -> dict[str, Any]:
    """聚合运维快照：网关运维页单次拉取的全部数据。"""
    free_models = [entry["id"] for entry in zen_gateway.local_model_entries()]
    return {
        "uptime_sec": uptime_sec(),
        "generated_at": now_iso_tz(),
        "channels": _channels(),
        "stats_24h": query_channel_usage_24h(),
        "account_pool": _account_pool(),
        "config": {
            "proxy": str(config.PROXY or "").strip(),
            "free_models": free_models,
            # 鉴权密钥完整返回（仅本机管理面使用，与其它敏感配置同策）
            "api_key": config.GATEWAY_API_KEY,
            "auth_enabled": bool(config.GATEWAY_API_KEY.strip()),
        },
    }
