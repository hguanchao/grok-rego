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
from core import proxypool
from core.util import now_iso_tz
from db import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    get_all_accounts,
    query_account_usage_24h,
    query_channel_usage_24h,
)
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
    """号池账号运行态：四维 KPI（正常/在用/粘性/冷却）+ 逐账号行（含近 24h 请求数）。

    口径：正常=已认证且状态 ACTIVE(1) 且未降智；在用=近 24h 产生过请求的账号；
    粘性=网关粘性会话绑定数；冷却=当前短期冻结中账号数。邮箱完整展示供排查运维。
    """
    accounts = get_all_accounts()
    per_account = query_account_usage_24h()
    cooling_until = grok_gateway.cooldown_until_map()
    now_mono = time.monotonic()
    cooled_ids = {aid for aid, until in cooling_until.items() if until > now_mono}
    sticky = int(grok_gateway.snapshot().get("sticky_sessions") or 0)
    sticky_ids = grok_gateway.sticky_bound_account_ids()

    rows: list[dict[str, Any]] = []
    active = 0
    in_use = 0
    cooling = 0
    for acc in accounts:
        has_token = bool(str(acc.get("access_token") or "").strip())
        status = int(acc.get("status") or 1)
        is_cooling = int(acc["id"]) in cooled_ids
        is_disabled = status == STATUS_DISABLED
        is_sticky = int(acc["id"]) in sticky_ids
        is_dumbed = int(acc.get("dumbed") or 0) == 1
        requests = per_account.get(int(acc["id"]), 0)
        if has_token and status == STATUS_ACTIVE and not is_dumbed:
            active += 1
        if requests > 0:
            in_use += 1
        if is_cooling:
            cooling += 1
        rows.append(
            {
                "id": acc["id"],
                # 排查运维：管理面账号池展示完整邮箱（不做脱敏）
                "email": acc.get("email") or "",
                "authed": has_token,
                "status": status,
                "disabled": is_disabled,
                "cooling": is_cooling,
                "sticky": is_sticky,
                "dumbed": is_dumbed,
                "requests_24h": requests,
            }
        )

    return {
        "total": len(accounts),
        "active": active,
        "in_use": in_use,
        "sticky": sticky,
        "cooling": cooling,
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
            "proxy_pool": proxypool.snapshot(),
            "free_models": free_models,
            # 鉴权密钥完整返回（仅本机管理面使用，与其它敏感配置同策）
            "api_key": config.GATEWAY_API_KEY,
            "auth_enabled": bool(config.GATEWAY_API_KEY.strip()),
        },
    }