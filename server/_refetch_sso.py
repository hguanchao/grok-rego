"""
对 status=1 且 sso_cookie 为空的账号，用邮箱+密码浏览器登录重取 SSO cookie。

用法（在 server 目录）:
  .venv\\Scripts\\python.exe _refetch_sso.py
  .venv\\Scripts\\python.exe _refetch_sso.py --headed
  .venv\\Scripts\\python.exe _refetch_sso.py --ids 31,32
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# 保证以 server 为 cwd / path
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from camoufox.sync_api import Camoufox  # noqa: E402

from core import config  # noqa: E402
from core.config import load_config  # noqa: E402
from core.logger import logger  # noqa: E402
from core.util import elapsed_label, mask_email  # noqa: E402
from db import update_account_sso_cookie  # noqa: E402
from workflow.browser import handle_turnstile, safe_goto, try_click_cookies  # noqa: E402
from workflow.register import (  # noqa: E402
    _camoufox_kwargs,
    _dump_page,
    _open_page,
    _take_sso_value,
    _wait_sso_ready,
    click,
    fill,
)

SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com"
EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[autocomplete='username']",
    "input[autocomplete='email']",
]
PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
]


def _list_targets(ids: list[int] | None) -> list[dict[str, Any]]:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    if ids:
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"""
            SELECT id, email, password FROM accounts
            WHERE COALESCE(is_deleted, 0) = 0
              AND id IN ({ph})
            ORDER BY id
            """,
            ids,
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT id, email, password FROM accounts
            WHERE COALESCE(is_deleted, 0) = 0
              AND status = 1
              AND (sso_cookie IS NULL OR TRIM(sso_cookie) = '')
              AND password IS NOT NULL AND TRIM(password) != ''
            ORDER BY id
            """
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _submit_login(page: Any) -> bool:
    """点登录/继续类按钮。"""
    for text in (
        "Sign in",
        "Log in",
        "Continue",
        "Next",
        "登录",
        "继续",
    ):
        if click(page, text, timeout=4000, retries=0, quiet=True):
            return True
    # 表单 submit
    try:
        for frame in page.frames:
            loc = frame.locator("button[type='submit'], input[type='submit']").first
            if loc.count() > 0:
                loc.click(timeout=3000)
                return True
    except Exception:
        pass
    return False


def _dismiss_cookie_banner(page: Any) -> None:
    """尽量关掉 OneTrust 等 cookie 浮层，避免挡住 Login with email。"""
    try_click_cookies(page)
    for sel in (
        "#onetrust-accept-btn-handler",
        "#accept-recommended-btn-handler",
        "#onetrust-reject-all-handler",
        "#close-pc-btn-handler",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            continue
    page.wait_for_timeout(500)


def _enter_email_login(page: Any) -> bool:
    """落地页为社交登录入口时，先点 Login with email 进入邮箱表单。"""
    # 已有邮箱框则无需再点
    for sel in EMAIL_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            pass

    # 优先用宽松 has-text（大小写不敏感）
    for pattern in (
        "Login with email",
        "Log in with email",
        "Sign in with email",
        "Continue with email",
        "login with email",
    ):
        try:
            loc = page.locator(f"button:has-text('{pattern}')").first
            if loc.count() > 0:
                loc.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
        if click(page, pattern, timeout=4000, retries=0, quiet=True):
            page.wait_for_timeout(1200)
            return True
    return False


def _login_one(email: str, password: str, *, headless: bool) -> str | None:
    """浏览器登录，成功返回 sso 值，失败返回 None。"""
    t0 = time.monotonic()
    with Camoufox(**_camoufox_kwargs(headless)) as browser:
        page = _open_page(browser)
        if not safe_goto(page, SIGNIN_URL):
            logger.error(f"[补cookie] 打开登录页失败  {mask_email(email)}")
            return None
        time.sleep(1.5)
        _dismiss_cookie_banner(page)
        try:
            from workflow.browser import human_mouse_move

            human_mouse_move(page)
        except Exception:
            pass

        # 社交入口 → 邮箱入口
        if not _enter_email_login(page):
            _dismiss_cookie_banner(page)
            if not _enter_email_login(page):
                logger.debug(
                    f"[补cookie] 未点到 Login with email，直接找邮箱框  {mask_email(email)}"
                )
        page.wait_for_timeout(800)

        if not fill(page, email, EMAIL_SELECTORS, timeout=25000):
            # 再试一次点邮箱登录
            _enter_email_login(page)
            page.wait_for_timeout(1000)
            if not fill(page, email, EMAIL_SELECTORS, timeout=15000):
                logger.error(f"[补cookie] 未找到邮箱框  {mask_email(email)}")
                _dump_page(page, "refetch-email-missing")
                return None

        # 邮箱后通常点 Continue / Next 才出密码
        _submit_login(page)
        page.wait_for_timeout(1500)

        if not fill(page, password, PASSWORD_SELECTORS, timeout=25000):
            _submit_login(page)
            page.wait_for_timeout(1500)
            if not fill(page, password, PASSWORD_SELECTORS, timeout=20000):
                logger.error(f"[补cookie] 未找到密码框  {mask_email(email)}")
                _dump_page(page, "refetch-password-missing")
                return None

        # Turnstile（若有）
        cf_ok = False
        for cf_round in range(3):
            result = handle_turnstile(page)
            if result in ("passed", "skipped"):
                cf_ok = True
                break
            if result != "refresh" or cf_round >= 2:
                break
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                _enter_email_login(page)
                fill(page, email, EMAIL_SELECTORS, timeout=10000)
                _submit_login(page)
                page.wait_for_timeout(1000)
                fill(page, password, PASSWORD_SELECTORS, timeout=10000)
            except Exception:
                break
        if not cf_ok:
            logger.debug(f"[补cookie] Turnstile 未明确通过，继续提交  {mask_email(email)}")

        if not _submit_login(page):
            logger.warning(f"[补cookie] 未点到提交按钮，尝试回车  {mask_email(email)}")
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

        # 等待 SSO
        if not _wait_sso_ready(page, email=email):
            for _ in range(15):
                val = _take_sso_value(page)
                if val:
                    logger.success(
                        f"[补cookie] 登录成功  {mask_email(email)}  · {elapsed_label(t0)}"
                    )
                    return val
                page.wait_for_timeout(1000)
            logger.error(
                f"[补cookie] 登录后未拿到 SSO  {mask_email(email)}  · {elapsed_label(t0)}"
            )
            _dump_page(page, "refetch-sso-missing")
            return None

        val = _take_sso_value(page)
        if not val:
            logger.error(f"[补cookie] SSO 检测通过但取值失败  {mask_email(email)}")
            return None
        logger.success(
            f"[补cookie] 登录成功  {mask_email(email)}  · {elapsed_label(t0)}"
        )
        return val


def main() -> int:
    parser = argparse.ArgumentParser(description="重取空 SSO cookie")
    parser.add_argument(
        "--ids",
        type=str,
        default="",
        help="指定账号 id，逗号分隔；默认处理全部 status=1 且 cookie 空",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="可视浏览器（默认无头）",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="账号间隔秒数（默认 2）",
    )
    args = parser.parse_args()

    load_config()
    ids: list[int] | None = None
    if args.ids.strip():
        ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]

    targets = _list_targets(ids)
    if not targets:
        logger.warning("[补cookie] 没有需要处理的账号")
        return 0

    logger.info(f"[补cookie] 待处理 {len(targets)} 个账号  headless={not args.headed}")
    ok_n = 0
    fail_n = 0
    results: list[str] = []

    for i, acc in enumerate(targets, 1):
        aid = int(acc["id"])
        email = str(acc["email"] or "")
        password = str(acc["password"] or "")
        logger.info(f"[补cookie] ({i}/{len(targets)}) #{aid}  {mask_email(email)}")
        if not password:
            logger.error(f"[补cookie] #{aid} 无密码，跳过")
            fail_n += 1
            results.append(f"#{aid} FAIL no-password")
            continue
        try:
            sso = _login_one(email, password, headless=not args.headed)
        except Exception as exc:
            logger.error(
                f"[补cookie] #{aid} 异常: {type(exc).__name__}: {exc}"
            )
            sso = None
        if sso and update_account_sso_cookie(aid, sso, reason="补全 SSO cookie"):
            ok_n += 1
            results.append(f"#{aid} OK")
        else:
            fail_n += 1
            results.append(f"#{aid} FAIL")
        if i < len(targets) and args.sleep > 0:
            time.sleep(args.sleep)

    logger.info(f"[补cookie] 完成  成功 {ok_n}  失败 {fail_n}")
    for line in results:
        print(line)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
