"""
注册风控诊断：单账号 + 双账号并发。

- 控制台 DEBUG
- 风控体检命中时落 HTML 片段与 botFlag 原文
- 结束打印账号 bfs/risk 汇总
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 确保从 server/ 运行
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path.cwd()))

from core.config import LOG_DIR  # noqa: E402
from core.logger import logger  # noqa: E402
from db import connect, init_db  # noqa: E402
from workflow import register as reg  # noqa: E402

DIAG_DIR = Path(LOG_DIR) / "diag"
DIAG_DIR.mkdir(parents=True, exist_ok=True)


def _patch_risk_dump() -> None:
    """在风控解析处落盘 botFlag 上下文，便于对照 JS/RSC 字段。"""
    orig_parse = reg.parse_grok_risk
    orig_check = reg.check_account_risk

    def parse_grok_risk(html: str | None) -> dict:
        result = orig_parse(html)
        if result.get("found"):
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bfs = result.get("bot_flag_source")
            details = result.get("bot_flag_details") or ""
            # 截取 botFlag 附近上下文
            text = str(html or "")
            idx = text.find("botFlag")
            snippet = text[max(0, idx - 400) : idx + 800] if idx >= 0 else text[:1200]
            path = DIAG_DIR / f"risk_{stamp}_bfs{bfs}.json"
            path.write_text(
                json.dumps(
                    {
                        "bfs": bfs,
                        "details": details,
                        "snippet": snippet,
                        "html_len": len(text),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.warning(
                f"[诊断] 风控字段已落盘 {path.name} bfs={bfs} details={details!r}"
            )
            # 额外保存完整 HTML（可能较大）
            (DIAG_DIR / f"risk_{stamp}_bfs{bfs}.html").write_text(text, encoding="utf-8")
        return result

    def check_account_risk(page):
        try:
            url = page.url
        except Exception:
            url = "?"
        logger.info(f"[诊断] 开始风控体检 url={url}")
        # 记录当前 cookie 概况（脱敏）
        try:
            cookies = page.context.cookies()
            names = sorted({c.get("name") for c in cookies})
            domains = sorted({c.get("domain") for c in cookies})
            logger.info(f"[诊断] cookies names={names} domains={domains}")
        except Exception as exc:
            logger.warning(f"[诊断] 读取 cookie 失败: {type(exc).__name__}")
        bfs, details = orig_check(page)
        try:
            url2 = page.url
        except Exception:
            url2 = "?"
        logger.info(f"[诊断] 风控体检结束 bfs={bfs} details={details!r} url={url2}")
        # 页面可见文案摘要
        try:
            body = page.inner_text("body")[:500].replace("\n", " ")
            logger.info(f"[诊断] 页面文案: {body}")
        except Exception:
            pass
        return bfs, details

    reg.parse_grok_risk = parse_grok_risk
    reg.check_account_risk = check_account_risk


def _recent_accounts(since_iso: str) -> list[dict]:
    with connect() as conn:
        conn.row_factory = lambda c, r: {
            c.description[i][0]: r[i] for i in range(len(r))
        }
        rows = conn.execute(
            "SELECT id, email, status, bfs, risk, checked_at, created_at, "
            "sso_cookie IS NOT NULL AS has_sso, "
            "access_token IS NOT NULL AND access_token != '' AS has_token "
            "FROM accounts WHERE created_at >= ? ORDER BY id DESC LIMIT 20",
            (since_iso,),
        ).fetchall()
    return list(rows)


def run_case(label: str, count: int, threads: int, headless: bool) -> None:
    logger.info("=" * 60)
    logger.info(f"[诊断] === 用例 {label}: count={count} threads={threads} headless={headless} ===")
    logger.info("=" * 60)
    t0 = time.time()
    from core.util import now_iso_tz

    since = now_iso_tz()
    ok = reg.run_signups(count=count, threads=threads, headless=headless)
    # 认证池
    auth_ok = reg.run_auth_pool()
    elapsed = time.time() - t0
    rows = _recent_accounts(since[:19])  # 宽松匹配
    logger.info(
        f"[诊断] 用例结束 {label}: signup_ok={ok}/{count} auth_ok={auth_ok} "
        f"elapsed={elapsed:.1f}s"
    )
    for row in rows:
        logger.info(
            f"[诊断] 账号 #{row['id']} {row['email']} status={row['status']} "
            f"bfs={row['bfs']} risk={row['risk']!r} has_sso={row['has_sso']} "
            f"has_token={row['has_token']} checked={row['checked_at']}"
        )
    # 写汇总
    summary = {
        "label": label,
        "count": count,
        "threads": threads,
        "headless": headless,
        "signup_ok": ok,
        "auth_ok": auth_ok,
        "elapsed_sec": round(elapsed, 1),
        "accounts": rows,
    }
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = DIAG_DIR / f"summary_{stamp}_{label}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[诊断] 汇总已写 {path}")


def main() -> None:
    # 控制台提到 DEBUG，便于观察浏览器步骤
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        colorize=True,
        filter=lambda r: "诊断" in r["message"]
        or r["message"].startswith("[")
        or r["level"].no >= 20,
    )
    init_db()
    _patch_risk_dump()
    logger.info(f"[诊断] 诊断目录: {DIAG_DIR}")

    # 1) 单账号有头
    run_case("single_headed", count=1, threads=1, headless=False)

    # 2) 双账号并发有头（便于观察并发指纹差异）
    run_case("multi2_headed", count=2, threads=2, headless=False)

    logger.info("[诊断] 全部用例完成，请查看 logs/diag/ 与 logs/server.log")


if __name__ == "__main__":
    main()
