"""
账号注册一体化流程（浏览器注册 + SSO 协议级 Token 交换）。

浏览器阶段：注册页 → 邮箱（创建临时邮箱）→ 验证码 → 资料表单 → 等 sso cookie 落地
            → 入库（status=REAUTH）→ 入认证池 → grok.com 风控体检。
认证池阶段：run_auth_pool 串行用 sso cookie 走 device flow 协议级 approve 交换 Token。

重试策略：邮箱/验证码/资料阶段失败关闭浏览器重启重试（邮箱与资料复用，最多 MAX_ATTEMPTS 次）；
         仅 SSO 阶段失败在当前浏览器内刷新页面重试（POST_EMAIL_RETRIES 次，不重启浏览器）。
"""

import os
import random
import re
import string
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from camoufox.sync_api import Camoufox
from curl_cffi import requests

from core import config
from core.logger import logger
from core.util import decode_jwt_exp, elapsed_label, format_exp, now_str
from db import (
    STATUS_ACTIVE,
    STATUS_REAUTH,
    add_to_auth_pool,
    get_account_by_email,
    get_auth_pool,
    init_db,
    remove_from_auth_pool,
    save_account,
    update_account_status,
    update_risk,
)
from workflow.browser import (
    extract_sso_cookies,
    handle_turnstile,
    human_click_locator,
    human_mouse_move,
    human_type_locator,
    safe_goto,
    try_click_cookies,
)
from workflow.mail import create_temp_email, poll_for_code

# ─── 任务协作取消：API 停止时 set，run_signups 协作退出 ────────────────────
_cancel_event = threading.Event()

# 单次注册结果回调：(ok, email|None, risk_bfs|None)
ResultCallback = Callable[[bool, str | None, int | None], None]


def request_cancel() -> None:
    """请求取消进行中的批量注册（协作式，当前浏览器步骤结束后生效）。"""
    _cancel_event.set()


def clear_cancel() -> None:
    """清除取消标志，开始新任务前调用。"""
    _cancel_event.clear()


def is_cancelled() -> bool:
    """当前是否已请求取消。"""
    return _cancel_event.is_set()


def _proxies() -> dict[str, str] | None:
    """按当前配置返回代理。"""
    proxy = config.PROXY
    return {"http": proxy, "https": proxy} if proxy else None

# ─── 超时与重试参数 ─────────────────────────────────────────────────────
CF_WAIT_TIMEOUT = 90  # 等待 Cloudflare 挑战通过的最长时间（秒）
CF_POLL_INTERVAL = 2  # 风控扫描轮询间隔（秒）
MAX_ATTEMPTS = 3  # 邮箱/验证码/资料阶段失败允许重启浏览器的最大次数（邮箱与资料复用）
POST_EMAIL_RETRIES = 2  # 仅 SSO 阶段失败：刷新页面重试的次数（不重启浏览器）
EMAIL_PAGE_WAIT_SECS = 15  # 等待邮箱填写页出现的最长时间（秒）
OTP_PAGE_WAIT_SECS = 20  # 等待验证码页出现的最长时间（秒）
OTP_MAIL_TIMEOUT = 120  # 轮询邮件取验证码超时（秒）
OTP_MAIL_INTERVAL = 3  # 轮询邮件间隔（秒）
FORM_READY_TIMEOUT = 25  # 填码后等待资料表单就绪的最长时间（秒）
SSO_WAIT_TIMEOUT = 60  # 资料提交后等待 sso cookie 落地的最长时间（秒）
SSO_POLL_INTERVAL = 1.0  # SSO cookie 轮询间隔（秒）
SSO_CONTINUE_INTERVAL = 6.0  # 等待期间点「继续」推进的间隔（秒）
RELOAD_SETTLE_MS = 3000  # 刷新页面后的静置等待（毫秒）
DEBUG_DIR = os.path.join(config.LOG_DIR, "debug")
# 资料表单姓名框：只认明确字段，禁止兜底 input[type=text]（会误填邮箱/验证码）
FORM_FIRST_SELECTORS = [
    "input[name='givenName']",
    "input[name='firstName']",
    "input[autocomplete='given-name']",
]
FORM_LAST_SELECTORS = [
    "input[name='familyName']",
    "input[name='lastName']",
    "input[autocomplete='family-name']",
]

# 填写邮箱提交后出现的风控提示关键字（命中则视为邮箱阶段失败，允许重启浏览器复用邮箱）
RISK_PROMPT_KEYWORDS = (
    "too many",
    "please try again",
    "try again later",
    "security check",
    "complete the challenge",
    "verify you are human",
    "suspicious",
    "automated access",
    "rate limit",
    "access denied",
    "blocked",
)

FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian", "George", "Edward", "Ronald", "Timothy", "Jason", "Jeffrey", "Ryan", "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen", "Nancy", "Lisa", "Betty", "Margaret", "Sandra", "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda", "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia", "Nicholas", "Tyler", "Samuel", "Benjamin", "Nathan", "Alexander", "Peter", "Henry", "Douglas", "Zachary", "Brandon", "Patrick", "Jeremy", "Rachel", "Laura", "Amber", "Crystal", "Morgan", "Jasmine", "Nicole", "Brittany", "Danielle", "Samantha", "Alexis", "Victoria", "Grace", "Faith", "Autumn", "Sophia", "Natalia", "Marcus", "Dominic", "Vincent", "Adrian", "Elias", "Tristan", "Donovan", "Gabriel", "Camille", "Beatrice", "Daisy", "Evelyn", "Iris", "Naomi", "Quinn", "Wyatt", "Cole", "Easton", "Landon", "Jace", "Maxwell", "Orion", "Silas", "Asher", "Jonah", "Micah", "Ezra", "Ezra", "Simon", "Felix", "Hugo"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Phillips", "Evans", "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Watkins", "Fisher", "Bishop", "Wallace", "Simpson", "Daniels", "Gordon", "Austin", "Marshall", "Pierce", "Hawkins", "Jensen", "Crawford", "Bennett", "Robertson", "Boyd", "Mason", "Romero", "Robertson", "Fox", "Warren", "Burton", "Pierce", "Spencer", "Cole", "Holloway", "Brock", "Vasquez", "Montes", "Rhodes", "Cabrera", "Donovan", "Beck", "Sanford", "Kramer", "Whitfield", "Norris", "Townes", "Pemberton"]


def _generate_profile_name() -> tuple[str, str]:
    """随机资料姓名（进入邮箱填写页时生成）。"""
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def _email_local_part(first_name: str, last_name: str) -> str:
    """邮箱前缀：姓名 + 数字 + 可选分隔符，增加多样性降低批量特征。"""
    base = re.sub(r"[^a-z]", "", f"{first_name}{last_name}".lower()) or "user"
    digits = "".join(random.choices(string.digits, k=random.randint(2, 4)))
    sep = random.choice(["", ".", "_"])
    # 50% 概率加随机后缀字母，进一步降低重复
    suffix = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 2)))
    return f"{base}{sep}{digits}{suffix}"


def _generate_password() -> str:
    """生成 16 位强密码：大小写字母 + 数字 + 特殊字符。"""
    password_chars = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*"),
    ]
    password_chars += random.choices(string.ascii_letters + string.digits + "!@#$%^&*", k=12)
    random.shuffle(password_chars)
    return "".join(password_chars)


# ─────────────────────────────────────────────────────────────────────────
# 浏览器操作辅助：点击 / 填写 / 等待
# ─────────────────────────────────────────────────────────────────────────

def _frames(page: Any) -> list[Any]:
    """返回主 frame 与所有子 frame。注册表单可能在 iframe 内渲染，顶层定位会漏。"""
    try:
        frames = page.frames
    except Exception:
        frames = []
    if not frames:
        try:
            frames = [page.main_frame]
        except Exception:
            frames = [page]
    return frames


def _page_text(page: Any) -> str:
    """汇总所有 frame 的 URL 与正文文本（含 iframe 内表单内容）。"""
    parts = [page.url]
    for frame in _frames(page):
        try:
            parts.append(frame.inner_text("body"))
        except Exception:
            continue
    return " ".join(parts).lower()


def _has_input(page: Any, selector: str) -> bool:
    """任一 frame 中是否存在匹配的输入框。"""
    for frame in _frames(page):
        try:
            if frame.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_closed(page: Any) -> bool:
    """页面 / context / browser 是否已关闭。"""
    try:
        _ = page.url
        return False
    except Exception as exc:
        name = type(exc).__name__
        text = str(exc).lower()
        return "TargetClosed" in name or "closed" in text or "TargetClosed" in str(exc)


def _on_form_page(page: Any) -> bool:
    """是否已到姓名/密码资料页。"""
    if _is_closed(page):
        return False
    return any(_has_input(page, sel) for sel in FORM_FIRST_SELECTORS) or _has_input(
        page, "input[type='password']"
    )


def _camoufox_kwargs(headless: bool) -> dict[str, Any]:
    """Camoufox 启动参数：可视/无头 + 代理 + 反检测增强。

    - humanize：鼠标轨迹拟人化（每次移动 0.5-1.5s 随机）
    - geoip：按代理 IP 自动匹配时区/语言/地理位置
    - locale：英语系 locale，与代理出口 IP 地理一致
    """
    kwargs: dict[str, Any] = {
        "headless": headless,
        "humanize": True,
        "geoip": True,
        "locale": ["en-US", "en"],
    }
    if config.PROXY:
        kwargs["proxy"] = {"server": config.PROXY}
    return kwargs


def _open_page(browser: Any) -> Any:
    """复用启动时已有页面，避免 new_page 再开一扇窗。"""
    pages: list[Any] = []
    try:
        if getattr(browser, "pages", None):
            pages = list(browser.pages)
        elif getattr(browser, "contexts", None):
            for ctx in browser.contexts:
                pages.extend(list(ctx.pages))
    except Exception:
        pages = []
    if pages:
        page = pages[0]
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        return page
    return browser.new_page()


def _dump_page(page: Any, tag: str) -> None:
    """失败时记录 URL、可见文本、input 清单并截图，便于对照浏览器。"""
    if _is_closed(page):
        logger.warning(f"[诊断] 页面已关闭，无法 dump: {tag}")
        return
    try:
        url = page.url
    except Exception:
        url = "?"
    try:
        body = _page_text(page)[:800].replace("\n", " ")
    except Exception:
        body = ""
    inputs: list[Any] = []
    for frame in _frames(page):
        try:
            items = frame.evaluate(
                """() => Array.from(document.querySelectorAll('input,button')).slice(0, 40).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    inputmode: el.getAttribute('inputmode') || '',
                    maxlength: el.getAttribute('maxlength') || '',
                    placeholder: el.placeholder || '',
                    text: ((el.innerText || el.value || '') + '').slice(0, 48)
                }))"""
            )
            if items:
                inputs.extend(items)
        except Exception:
            continue
    logger.warning(f"[诊断] {tag} | URL: {url} | BODY: {body}")
    logger.warning(f"[诊断] {tag} | controls={inputs}")
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(DEBUG_DIR, f"{stamp}_{tag}.png")
        page.screenshot(path=path, full_page=True)
        logger.info(f"[诊断] 截图: {path}")
    except Exception as exc:
        logger.warning(f"[诊断] 截图失败: {type(exc).__name__}: {exc}")


def click(
    page: Any, text: str, timeout: int = 15000, retries: int = 2, quiet: bool = False
) -> bool:
    """点击文本匹配的元素（按钮/链接/提交按钮）；SPA 慢渲染重试，支持 iframe 内元素。

    覆盖 button/a/input[type=submit]/role=button 多种标签，多匹配时取第一个（避免 strict 模式报错）。
    """
    selectors = (
        f'text="{text}"',
        f"button:has-text('{text}')",
        f"a:has-text('{text}')",
        f"input[type='submit'][value='{text}']",
        f"[role='button']:has-text('{text}')",
    )
    for _ in range(retries + 1):
        if _is_closed(page):
            logger.debug(f"[注册] 页面已关闭，停止点击: {text}")
            return False
        for frame in _frames(page):
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.wait_for(state="visible", timeout=min(timeout, 3000))
                    human_click_locator(page, locator)
                    return True
                except Exception as e:
                    if "TargetClosed" in type(e).__name__:
                        logger.debug(f"[注册] 页面已关闭，停止点击: {text}")
                        return False
                    logger.debug(f"[注册] 点击失败 {selector}: {type(e).__name__} {str(e)[:120]}")
        try:
            page.wait_for_timeout(2000)
        except Exception:
            logger.debug(f"[注册] 页面已关闭，停止点击: {text}")
            return False
    if _is_closed(page) or quiet:
        return False
    url = page.url
    body = _page_text(page)[:300].replace("\n", " ")
    logger.debug(f"[注册] 未找到可点击元素: {text} | URL: {url} | BODY: {body}")
    return False


def fill(page: Any, value: str, selectors: list[str], timeout: int = 20000) -> bool:
    """在任一 frame 中等待可见输入框，模拟鼠标轨迹后逐字键入；卡死时降级 JS 赋值。

    支持 iframe 内输入框与 React 延迟挂载（轮询等待元素出现）。timeout 单位毫秒。
    """
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if _is_closed(page):
            logger.debug("[注册] 页面已关闭，停止填写")
            return False
        for frame in _frames(page):
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.wait_for(state="visible", timeout=2000)
                    human_type_locator(page, locator, value)
                    return True
                except Exception as e:
                    if "TargetClosed" in type(e).__name__:
                        logger.debug("[注册] 页面已关闭，停止填写")
                        return False
                    logger.debug(f"[注册] 填入失败 {selector}: {type(e).__name__} {str(e)[:120]}")
        time.sleep(0.5)

    # 兜底：JS 直接赋值 + input 事件（输入框存在但点击/键入异常时使用）
    for frame in _frames(page):
        for selector in selectors:
            try:
                done = frame.evaluate(
                    """(sel, val) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        return true;
                    }""",
                    selector, value,
                )
                if done:
                    logger.debug(f"[注册] 输入框不可用，已降级 JS 赋值: {selector}")
                    return True
            except Exception:
                continue
    logger.debug(f"[注册] 未找到输入框: {selectors}")
    return False


def wait_until(
    page: Any, targets: list[str], timeout: int, check_for_errors: bool = False
) -> bool:
    """轮询等待 URL/页面文本（含 iframe）出现任一目标，返回是否命中。"""
    deadline = time.time() + timeout
    error_count = 0
    while time.time() < deadline:
        time.sleep(2)
        try:
            text = _page_text(page)
        except Exception:
            continue
        if any(target.lower() in text for target in targets):
            return True
        if check_for_errors and ("something went wrong" in text or "an error occurred" in text):
            error_count += 1
            if error_count >= 3:
                return False
    return False


# ---------------------------------------------------------------------------
# 风控检查
# ---------------------------------------------------------------------------

def parse_grok_risk(html: str | None) -> dict[str, Any]:
    """从 grok.com 主页 HTML（RSC 数据）解析风控字段。

    botFlagSource 取值：
      0 / null  → 无风控标记（正常）
      1         → 中等风控
      2         → 高风控
    """
    normalized = str(html or "").replace('\\"', '"')
    source_match = re.search(r'botFlagSource"\s*:\s*(null|-?\d+)', normalized)
    details_match = re.search(r'botFlagDetails"\s*:\s*(?:null|"([^"]*)")', normalized)

    source: int | None = None
    if source_match:
        raw = source_match.group(1)
        if raw != "null":
            try:
                source = int(raw)
            except (TypeError, ValueError):
                source = None
        else:
            # botFlagSource: null → 明确无风控标记，视为 0
            source = 0
    details = details_match.group(1) if details_match and details_match.group(1) else ""

    return {
        "found": bool(source_match or details_match),
        "bot_flag_source": source,
        "bot_flag_details": details,
    }


def _scan_grok_page(page: Any) -> tuple[int | None, str]:
    """轮询等待 grok.com 页面出现 botFlag 字段，返回 (bfs, details) 或 (None, "")。

    若连续 2 次检测到页面为未登录态（出现 Sign in / Sign up 入口且无 botFlag），
    说明 grok.com 未完成登录，体检无意义，提前结束。
    单次命中不退出：SPA 首屏渲染初期可能短暂包含 Sign up / Sign in 文案。
    """
    unauth_count = 0
    for _ in range(int(CF_WAIT_TIMEOUT / CF_POLL_INTERVAL)):
        page.wait_for_timeout(CF_POLL_INTERVAL * 1000)
        try:
            html = page.content()
        except Exception:
            continue
        if "botFlag" in html:
            result = parse_grok_risk(html)
            if result["found"]:
                return result["bot_flag_source"], result["bot_flag_details"]
        # 未登录态检测：连续 2 次才确认（避免 SPA 渲染初期误判）
        if "Sign up" in html and "Sign in" in html:
            unauth_count += 1
            if unauth_count >= 2:
                logger.debug("[风控] grok.com 连续 2 次显示未登录态，提前结束体检")
                return None, ""
        else:
            unauth_count = 0
    return None, ""


def _seed_sso_for_grok(page: Any) -> None:
    """把当前会话的 sso 值写入 grok.com，避免体检页落到未登录营销页。"""
    try:
        browser = page.context.browser if hasattr(page, "context") else page
        cookies = extract_sso_cookies(browser)
        from workflow.oauth import _extract_sso_value
        value = _extract_sso_value(cookies) if cookies else None
        if not value:
            return
        payload = []
        for name in ("sso", "sso-rw"):
            for domain in (".grok.com", "grok.com"):
                payload.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                })
        page.context.add_cookies(payload)
        logger.debug("[风控] 已将 SSO cookie 注入 grok.com")
    except Exception as exc:
        logger.debug(f"[风控] 注入 grok.com cookie 失败: {type(exc).__name__}: {exc}")


def check_account_risk(page: Any) -> tuple[int | None, str]:
    """检查账号风控状态，返回 (bfs, risk_details)。

    页面已位于 grok.com 则直接扫描，否则先跳转；解析失败自动重试一次，
    仍失败返回 (None, "")，由调用方标记 unknown，不阻塞主流程。
    """
    _seed_sso_for_grok(page)
    for attempt in (1, 2):
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if "grok.com" not in current_url or attempt == 2:
            logger.debug(f"[风控] 正在跳转 grok.com（第 {attempt}/2 次）")
            safe_goto(page, config.GROK_URL)
        else:
            logger.debug(f"[风控] 已在 grok.com，直接扫描（第 {attempt}/2 次）")

        bfs, details = _scan_grok_page(page)
        if bfs is not None:
            return bfs, details
        logger.debug(f"[风控] 第 {attempt} 次未解析到风控字段，重试")

    return None, ""


# ---------------------------------------------------------------------------
# 注册流程
# ---------------------------------------------------------------------------

def _enter_signup_page(page: Any) -> bool:
    """打开注册页并进入邮箱填写入口（落地页需点 Sign up with email）。"""
    time.sleep(2)
    human_mouse_move(page)
    try_click_cookies(page)
    # 落地页为社交登录入口时点「Sign up with email」；已是表单页则跳过
    if _has_input(page, "input[type='email']"):
        return True
    if not click(page, "Sign up with email"):
        # 部分落地页先需 Sign up 入口
        if not click(page, "Sign up"):
            return False
        return click(page, "Sign up with email")
    return True


def _ensure_email(
    page: Any,
    email: str | None,
    jwt: str | None,
    first_name: str | None,
    last_name: str | None,
) -> tuple[str, str | None, str, str] | None:
    """等待邮箱填写页；首次进入时生成资料姓名并创建邮箱。返回 (email, jwt, first, last)。"""
    t0 = time.monotonic()
    deadline = time.time() + EMAIL_PAGE_WAIT_SECS
    while time.time() < deadline and not _has_input(page, "input[type='email']"):
        time.sleep(0.5)
    if not _has_input(page, "input[type='email']"):
        logger.warning(f"[邮箱] 填写页未就绪  · {elapsed_label(t0)}")
        return None
    if email is None:
        first_name, last_name = _generate_profile_name()
        create_t0 = time.monotonic()
        try:
            email, jwt = create_temp_email(_email_local_part(first_name, last_name))
        except Exception as exc:
            logger.error(
                f"[邮箱] 邮箱创建失败  {type(exc).__name__}  · {elapsed_label(create_t0)}"
            )
            return None
        if not email:
            logger.error(f"[邮箱] 邮箱创建失败  · {elapsed_label(create_t0)}")
            return None
        logger.debug(
            f"[邮箱] 已创建 {email} ({first_name} {last_name})  · {elapsed_label(create_t0)}"
        )
    if not first_name or not last_name:
        first_name, last_name = _generate_profile_name()
    return email, jwt, first_name, last_name


def _has_risk_prompt(page: Any) -> bool:
    """提交邮箱后页面是否出现风控提示（限流/人机校验/阻断等）。"""
    try:
        body = _page_text(page)
    except Exception:
        return False
    return any(keyword in body for keyword in RISK_PROMPT_KEYWORDS)


def _submit_email(page: Any, email: str) -> tuple[str, bool]:
    """填写邮箱并点击 Sign up。返回 (失败阶段, 是否成功)。

    失败阶段 'email'：邮箱填写页加载超时 / 未找到元素 / 填写后触发风控，
    允许重启浏览器复用邮箱重试；失败阶段 'post'：已越过邮箱填写阶段。
    """
    t0 = time.monotonic()
    if not fill(page, email, ["input[type='email']", "input"]):
        logger.warning(f"[邮箱] 未找到输入框  {email}  · {elapsed_label(t0)}")
        return "email", False
    if not click(page, "Sign up"):
        logger.warning(f"[邮箱] 未找到 Sign up 按钮  {email}  · {elapsed_label(t0)}")
        return "email", False
    page.wait_for_timeout(1500)
    if _has_risk_prompt(page):
        logger.warning(f"[邮箱] 提交后触发风控  {email}  · {elapsed_label(t0)}")
        return "email", False
    logger.success(f"[邮箱] 已填写并提交 {email}  · {elapsed_label(t0)}")
    return "post", True


def _type_otp(page: Any, selector: str, value: str) -> bool:
    """点击验证码框并逐字键入，触发 React OTP 受控组件 onChange。"""
    for frame in _frames(page):
        try:
            locator = frame.locator(selector).first
            if locator.count() == 0:
                continue
            locator.wait_for(state="visible", timeout=3000)
            human_type_locator(page, locator, value, clear=True)
            return True
        except Exception as exc:
            logger.debug(
                f"[验证码] 键入失败 {selector}: {type(exc).__name__} {str(exc)[:120]}"
            )
    return False


def _fill_otp(page: Any, code: str) -> bool:
    """填入验证码：去连字符后逐字键入（页面是 3-3 分框，连字符仅为装饰）。"""
    compact = re.sub(r"[^A-Za-z0-9]", "", code)
    if not compact:
        return False
    selectors = [
        "input[name='code']",
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[inputmode='text'][maxlength='6']",
        "input[name='otp']",
        "input[type='tel']",
        "input[maxlength='6']",
    ]
    for selector in selectors:
        if _type_otp(page, selector, compact):
            return True
    if len(compact) >= 6:
        for frame in _frames(page):
            try:
                boxes = frame.locator("input[maxlength='1']")
                count = boxes.count()
                if count < 6:
                    continue
                for index, char in enumerate(compact[:count]):
                    human_type_locator(page, boxes.nth(index), char)
                return True
            except Exception:
                continue
    return False


def _wait_form_ready(page: Any, timeout: int = FORM_READY_TIMEOUT) -> bool:
    """验证码提交后等待资料表单；必要时点 Confirm/Continue。"""
    deadline = time.time() + timeout
    last_click = 0.0
    while time.time() < deadline:
        if _is_closed(page):
            return False
        if _on_form_page(page):
            return True
        if time.time() - last_click >= 5:
            for text in ("Confirm email", "Confirm", "Continue", "Verify"):
                if click(page, text, timeout=2000, retries=0, quiet=True):
                    last_click = time.time()
                    break
            else:
                last_click = time.time()
        time.sleep(1)
    return _on_form_page(page)


def _form_ready_skip(page: Any, email: str, t0: float) -> bool:
    """页面已直接到达资料表单（验证码阶段可跳过）时返回 True。"""
    if _on_form_page(page):
        logger.success(f"[邮件] 已在资料表单，跳过验证码  {email}  · {elapsed_label(t0)}")
        return True
    return False


def _verify_email(page: Any, email: str, jwt: str) -> bool:
    """验证码阶段：等验证码页 → 取码 → 填码 → 等到资料表单。表单未出现视为失败。"""
    t0 = time.monotonic()
    if _form_ready_skip(page, email, t0):
        return True
    if not wait_until(page, ["verify your email", "one-time code"], OTP_PAGE_WAIT_SECS, check_for_errors=True):
        if _form_ready_skip(page, email, t0):
            return True
        logger.warning(f"[邮件] 验证码页未就绪  {email}  · {elapsed_label(t0)}")
        _dump_page(page, "otp-page-missing")
        return False
    code = poll_for_code(jwt, timeout=OTP_MAIL_TIMEOUT, interval=OTP_MAIL_INTERVAL)
    if not code:
        if _form_ready_skip(page, email, t0):
            return True
        logger.error(f"[邮件] 等待验证码超时  {email}  · {elapsed_label(t0)}")
        return False
    fill_t0 = time.monotonic()
    if not _fill_otp(page, code):
        if _form_ready_skip(page, email, t0):
            return True
        logger.error(f"[邮件] 未找到验证码框  {email}  · {elapsed_label(fill_t0)}")
        _dump_page(page, "otp-input-missing")
        return False
    logger.success(f"[邮件] 已填入验证码 {code}  · {elapsed_label(fill_t0)}")
    if not _wait_form_ready(page, timeout=FORM_READY_TIMEOUT):
        logger.warning(f"[资料] 填码后资料表单未就绪  {email}  · {elapsed_label(t0)}")
        _dump_page(page, "after-otp-no-form")
        return False
    return True


def _fill_signup_form(page: Any, first_name: str, last_name: str, password: str) -> bool:
    """填写注册表单（姓名 + 密码）并提交，处理 Turnstile 验证。"""
    t0 = time.monotonic()
    full_name = f"{first_name} {last_name}".strip()
    if not _on_form_page(page) and not _wait_form_ready(page, timeout=15):
        logger.warning(f"[资料] 资料表单未就绪  · {elapsed_label(t0)}")
        _dump_page(page, "form-not-ready")
        return False
    if not fill(page, first_name, FORM_FIRST_SELECTORS):
        logger.warning(f"[资料] 未找到姓名框  {full_name}  · {elapsed_label(t0)}")
        _dump_page(page, "givenName-missing")
        return False
    if not fill(page, last_name, FORM_LAST_SELECTORS):
        logger.warning(f"[资料] 未找到姓名框  {full_name}  · {elapsed_label(t0)}")
        _dump_page(page, "familyName-missing")
        return False
    if not fill(page, password, ["input[type='password']"]):
        logger.warning(f"[资料] 未找到密码框  {full_name}  · {elapsed_label(t0)}")
        _dump_page(page, "password-missing")
        return False

    cf_ok = False
    for cf_round in range(3):
        result = handle_turnstile(page)
        if result in ("passed", "skipped"):
            cf_ok = True
            break
        # 仅「可见 Verification failed」才刷新；超时失败不刷新
        if result != "refresh" or cf_round >= 2:
            break
        logger.debug(f"[CF挑战] 刷新资料页重试（第 {cf_round + 1}/2 次）")
        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception:
            break
        if not _on_form_page(page) and not _wait_form_ready(page, timeout=15):
            break
        if not (
            fill(page, first_name, FORM_FIRST_SELECTORS)
            and fill(page, last_name, FORM_LAST_SELECTORS)
            and fill(page, password, ["input[type='password']"])
        ):
            break
    if not cf_ok:
        logger.warning(f"[CF挑战] Turnstile 未通过  · {elapsed_label(t0)}")
        _dump_page(page, "turnstile-failed")
        return False

    if not click(page, "Complete sign up") and not click(page, "Create account") and not click(page, "Continue"):
        logger.warning(f"[资料] 未找到提交按钮  {full_name}  · {elapsed_label(t0)}")
        _dump_page(page, "complete-signup-missing")
        return False
    logger.success(f"[资料] 已提交 {full_name}  · {elapsed_label(t0)}")
    return True


def _has_sso_cookie(page: Any) -> bool:
    """检测浏览器上下文是否已落地 sso / sso-rw cookie。"""
    try:
        browser = page.context.browser if hasattr(page, "context") else page
        cookies = extract_sso_cookies(browser)
        return bool(cookies)
    except Exception as exc:
        logger.debug(f"[SSO] cookie 提取异常（继续轮询）: {type(exc).__name__}: {exc}")
        return False


def _take_sso_value(page: Any) -> str | None:
    """从浏览器提取 sso 会话凭证原值（字符串）。

    extract_sso_cookies 返回 cookie 列表 [{name,value,domain}]；
    sso 与 sso-rw value 相同，取任一即可，优先 sso-rw。
    """
    try:
        browser = page.context.browser if hasattr(page, "context") else page
        cookies = extract_sso_cookies(browser)
        if not cookies:
            return None
        from workflow.oauth import _extract_sso_value
        return _extract_sso_value(cookies) or None
    except Exception as exc:
        logger.debug(f"[SSO] value 提取异常: {type(exc).__name__}: {exc}")
        return None


def _wait_sso_ready(
    page: Any, email: str | None = None, clock: list[float] | None = None
) -> bool:
    """资料提交后等待 SSO cookie 落地：完成后点「继续」推进会话，最长 SSO_WAIT_TIMEOUT。

    注册表单提交成功后 xAI 会写入 sso cookie；落地页可能停在「继续」按钮处需点击推进。
    本函数轮询浏览器 context cookie，拿到 sso/sso-rw 即视为注册成功。
    成功日志由调用方在 save_account 之后打（需带账号 id）。
    """
    t0 = time.monotonic()
    if clock is not None:
        clock.clear()
        clock.append(t0)
    deadline = time.time() + SSO_WAIT_TIMEOUT
    last_continue = 0.0
    while time.time() < deadline:
        if _has_sso_cookie(page):
            return True
        if time.time() - last_continue >= SSO_CONTINUE_INTERVAL:
            for btn in ("Continue", "继续"):
                for frame in _frames(page):
                    try:
                        locator = frame.locator(f"button:has-text('{btn}')").first
                        if locator.count() > 0:
                            human_click_locator(page, locator)
                            last_continue = time.time()
                            break
                    except Exception:
                        continue
                else:
                    continue
                break
        time.sleep(SSO_POLL_INTERVAL)
    who = email or ""
    logger.error(
        f"[SSO] {who} 等待超时（已等 {SSO_WAIT_TIMEOUT}s）  · {elapsed_label(t0)}"
    )
    return False


def _post_email_pipeline(
    page: Any,
    email: str,
    jwt: str,
    first_name: str,
    last_name: str,
    password: str,
    sso_clock: list[float] | None = None,
) -> str | None:
    """邮箱提交后的完整流程：验证码 → 表单 → 等待 SSO cookie。

    返回 None 表示全部成功；否则返回失败阶段：
    - "otp" ：验证码阶段（验证码页未就绪 / 取码超时 / 未找到验证码框 / 填码后表单未就绪），
      调用方应关闭当前浏览器重启重试（复用邮箱）。
    - "form" ：资料表单阶段（表单未就绪 / 缺姓名或密码框 / Turnstile 未过 / 缺提交按钮），
      调用方应关闭当前浏览器重启重试（复用邮箱与资料姓名）。
    - "sso" ：SSO 阶段，仅在当前浏览器内刷新页面重试。
    """
    if not _verify_email(page, email, jwt):
        return "otp"
    if not _fill_signup_form(page, first_name, last_name, password):
        return "form"
    if not _wait_sso_ready(page, email=email, clock=sso_clock):
        return "sso"
    return None


def _run_attempt(
    email: str | None,
    jwt: str | None,
    first_name: str | None,
    last_name: str | None,
    password: str,
    headless: bool = False,
) -> tuple[
    tuple[int | None, str],
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str,
]:
    """单次浏览器尝试：开页 → 邮箱 → 验证码 → 表单 → 等 SSO。

    返回 (risk, account_id, email, jwt, first_name, last_name, stage)。

    失败阶段 stage：
    - 'email'：邮箱填写页加载超时 / 未找到元素 / 填写后风控，调用方可重启浏览器复用邮箱重试。
    - 'otp' ：验证码阶段（验证码页 / 取码 / 填码 / 填码后表单未就绪）失败，
      调用方可重启浏览器复用邮箱重试。
    - 'form' ：资料表单阶段失败，调用方可重启浏览器复用邮箱与资料姓名重试。
    - 'post' ：仅 SSO 阶段失败，刷新页面重试仍失败则放弃，调用方不得重启浏览器。

    SSO cookie 拿到后：save_account 入库（status=REAUTH，无 token）→ 入认证池待统一交换。
    OAuth 交换不在本步进行，由 run_auth_pool 统一 LIFO 消化。
    """
    risk: tuple[int | None, str] = (None, "")
    account_id: int | None = None
    email_submitted = False
    sso_clock: list[float] = []

    def fail(stage: str) -> tuple[Any, ...]:
        """以指定失败阶段提前返回本次尝试结果（邮箱/资料在闭包中复用）。"""
        return risk, account_id, email, jwt, first_name, last_name, stage

    try:
        with Camoufox(**_camoufox_kwargs(headless)) as browser:
            page = _open_page(browser)
            nav_t0 = time.monotonic()
            if safe_goto(page, config.SIGNUP_URL):
                logger.success(
                    f"[注册] 注册页已打开  · {elapsed_label(nav_t0)}"
                )
            else:
                logger.error(
                    f"[注册] 注册页打开失败  · {elapsed_label(nav_t0)}"
                )

            for retry in range(POST_EMAIL_RETRIES + 1):
                if _is_closed(page):
                    logger.debug("[注册] 浏览器已关闭，中止本次尝试")
                    return fail("post" if email_submitted else "email")

                # 表单已提交后 SSO cookie 可能已落地（等待阶段超时误报 / SPA 刷新后种下）。
                # cookie 在即注册已实际完成，直接成功入库，无需任何重试。
                if email_submitted and _has_sso_cookie(page):
                    logger.success(
                        f"[SSO] {email} 重试路径检测到 sso cookie 已落地，注册完成"
                    )
                    break

                if email_submitted and _on_form_page(page):
                    # 已在资料表单页：跳过验证码，直接表单 + SSO
                    if not _fill_signup_form(page, first_name, last_name, password):
                        fail_stage = "form"
                    elif not _wait_sso_ready(page, email=email, clock=sso_clock):
                        fail_stage = "sso"
                    else:
                        fail_stage = None
                elif email_submitted and not _has_input(page, "input[type='email']"):
                    fail_stage = _post_email_pipeline(
                        page, email or "", jwt or "", first_name, last_name, password,
                        sso_clock=sso_clock,
                    )
                else:
                    if email_submitted:
                        # SPA 刷新后前端状态归零，页面回到注册入口落地页：
                        # 自动重新进入邮箱流程（复用已创建的邮箱）
                        logger.debug(
                            f"[注册] 页面已回到注册入口（刷新重置 SPA 状态），重新进入邮箱流程: {email}"
                        )
                    if not _enter_signup_page(page):
                        stage = "post" if email_submitted else "email"
                        _dump_page(page, "enter-signup-failed")
                        return fail(stage)
                    result = _ensure_email(page, email, jwt, first_name, last_name)
                    if result is None:
                        stage = "post" if email_submitted else "email"
                        return fail(stage)
                    email, jwt, first_name, last_name = result

                    email_stage, ok = _submit_email(page, email)
                    if not ok:
                        return fail(email_stage)
                    email_submitted = True
                    fail_stage = _post_email_pipeline(
                        page, email, jwt or "", first_name, last_name, password,
                        sso_clock=sso_clock,
                    )

                if fail_stage is None:
                    break
                if fail_stage in ("otp", "form"):
                    # 验证码 / 资料阶段失败：不刷新页面，关闭当前浏览器重启重试（邮箱、验证码与资料姓名复用）
                    label = "验证码" if fail_stage == "otp" else "资料"
                    logger.debug(
                        f"[注册] {label}阶段失败，关闭浏览器重启重试（复用邮箱 {email}）"
                    )
                    return fail(fail_stage)
                # 仅 SSO 阶段失败：在当前浏览器内刷新页面重试
                if retry < POST_EMAIL_RETRIES:
                    logger.debug(
                        f"[注册] SSO 阶段失败，刷新页面重试（第 {retry + 1}/{POST_EMAIL_RETRIES} 次）"
                    )
                    _dump_page(page, f"post-fail-retry-{retry + 1}")
                    try:
                        page.reload()
                        page.wait_for_timeout(RELOAD_SETTLE_MS)
                    except Exception as exc:
                        logger.debug(
                            f"[注册] 刷新失败（浏览器可能已关闭）: {type(exc).__name__}"
                        )
                        return fail("post")
            else:
                logger.warning("[SSO] SSO 阶段刷新重试均失败，放弃该邮箱，不重启浏览器")
                return fail("post")

            # SSO cookie 已拿到：立即入库（status=REAUTH，待认证）+ 入认证池
            sso_value = _take_sso_value(page)
            if not sso_value or not email:
                sso_t0 = sso_clock[0] if sso_clock else time.monotonic()
                logger.error(
                    f"[SSO] {email or ''} 会话凭证提取失败  · {elapsed_label(sso_t0)}"
                )
                _dump_page(page, "sso-missing")
                return fail("post")
            account_id = save_account(
                email, password, first_name, last_name, sso_value,
                status=STATUS_REAUTH, reason="待认证池交换 Token",
            )
            add_t0 = time.monotonic()
            add_to_auth_pool(email, "", 5, account_id)
            sso_t0 = sso_clock[0] if sso_clock else add_t0
            logger.success(
                f"[SSO] {email}  会话已落地，注册完成  · {elapsed_label(sso_t0)}"
            )
            logger.success(
                f"[入池] {email}  · {elapsed_label(add_t0)}"
            )

            if config.IS_AUTH:  # 浏览器仍开着，顺带 grok.com 风控体检（已登录）
                risk_t0 = time.monotonic()
                risk = check_account_risk(page)
                bfs, details = risk
                if bfs is None:
                    logger.warning(
                        f"[风控] {email}  未解析到风控字段，标记 unknown  · {elapsed_label(risk_t0)}"
                    )
                elif bfs in (1, 2):
                    extra = f"  details={details}" if details else ""
                    logger.warning(
                        f"[风控] {email}  被标记  bfs={bfs}{extra}  · {elapsed_label(risk_t0)}"
                    )
                else:
                    logger.success(
                        f"[风控] {email}  正常  bfs={bfs}  · {elapsed_label(risk_t0)}"
                    )
    except Exception as exc:
        if "TargetClosed" in type(exc).__name__ or "closed" in str(exc).lower():
            logger.debug(f"[注册] 浏览器已关闭: {type(exc).__name__}: {str(exc)[:160]}")
            return fail("post" if email_submitted else "email")
        raise

    return fail("post")


# ---------------------------------------------------------------------------
# 注册前预检：代理 / 关键站点连通性 / Cloudflare 人机验证
# ---------------------------------------------------------------------------

def _check_proxy() -> bool:
    """检测代理是否可用：通过代理访问中立快速端点（gstatic generate_204）。"""
    proxy = config.PROXY
    t0 = time.monotonic()
    if not proxy:
        logger.debug("[预检] 未配置代理（PROXY 为空），跳过代理检测")
        return True
    try:
        resp = requests.get(
            "https://www.gstatic.com/generate_204",
            proxies=_proxies(),
            timeout=15,
            impersonate="chrome",
        )
    except Exception as e:
        logger.error(
            f"[预检] 代理不可达  {proxy}  {type(e).__name__}  · {elapsed_label(t0)}"
        )
        return False
    if resp.status_code == 204:
        logger.debug(f"[预检] 代理可用: {proxy}  · {elapsed_label(t0)}")
        return True
    logger.error(
        f"[预检] 代理不可达  {proxy}  HTTP {resp.status_code}  · {elapsed_label(t0)}"
    )
    return False


def _check_reachable(url: str, label: str) -> bool:
    """HTTP 探测目标是否可达（走当前代理），5xx / 异常视为失败。"""
    t0 = time.monotonic()
    try:
        resp = requests.get(
            url,
            proxies=_proxies(),
            timeout=20,
            allow_redirects=True,
            impersonate="chrome",
        )
    except Exception as e:
        logger.error(
            f"[预检] {label}不可达  {url}  {type(e).__name__}  · {elapsed_label(t0)}"
        )
        return False
    if resp.status_code < 500:
        logger.success(
            f"[预检] {label}可达  {url}  HTTP {resp.status_code}  · {elapsed_label(t0)}"
        )
        return True
    logger.error(
        f"[预检] {label}不可达  {url}  HTTP {resp.status_code}  · {elapsed_label(t0)}"
    )
    return False


def _mail_base_url() -> str:
    """当前邮箱服务 API 根地址。"""
    if (config.MAIL_PROVIDER or "cf").strip().lower() == "yyds":
        return (config.YYDS_API_BASE or "").strip().rstrip("/")
    return (config.CF_API_BASE or "").strip().rstrip("/")


def preflight_check() -> bool:
    """注册前预检（主控线程调用）：注册入口、grok.com、邮箱 API。

    只做 HTTP 探测，不开浏览器。人机验证由正式注册窗口处理。
    任一项不通过返回 False，调用方应中止注册任务。
    """
    mail_base = _mail_base_url()
    results = {
        "proxy": _check_proxy(),
        "注册入口": _check_reachable(config.SIGNUP_URL, "注册入口"),
        "grok.com": _check_reachable("https://grok.com/", "grok.com "),
        "邮箱服务": _check_reachable(mail_base, "邮箱服务") if mail_base else False,
    }
    if not mail_base:
        logger.error("[预检] 邮箱服务不可达  （未配置邮箱 API 地址）  · 0.0s")
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        logger.error(f"[预检] 未通过：{', '.join(failed)}，任务中止")
        return False
    return True


def run_signup(headless: bool = False) -> tuple[bool, str | None, int | None]:
    """注册新账号：开页 → 邮箱 → 验证码 → 表单 → 等 SSO cookie → 入库（待认证）。

    返回 (success, email, risk_bfs)。
    - 邮箱 / 验证码 / 资料表单阶段失败（加载超时/未找到元素/填写后风控/验证码与表单异常）：
      关闭当前浏览器重启重试，邮箱与资料姓名复用，最多 MAX_ATTEMPTS 次尝试。
    - 仅 SSO 阶段失败：刷新页面重试 POST_EMAIL_RETRIES 次，不重启浏览器。
    SSO cookie 拿到即入库（status=REAUTH，无 token），OAuth 交换由认证池统一 LIFO 消化。
    """
    if is_cancelled():
        return False, None, None

    init_db()
    first_name, last_name = None, None
    password = _generate_password()
    email, jwt = None, None

    risk: tuple[int | None, str] = (None, "")
    account_id: int | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if is_cancelled():
            logger.warning("[注册] 已取消，中止当前账号")
            break
        if attempt > 1:
            logger.debug(
                f"[注册] 第 {attempt}/{MAX_ATTEMPTS} 次尝试"
                f"{f'  {email}' if email else ''}"
            )
        risk, account_id, email, jwt, first_name, last_name, stage = _run_attempt(
            email, jwt, first_name, last_name, password, headless=headless
        )
        if account_id is not None:
            break
        if is_cancelled():
            break
        if stage == "post":
            logger.debug(
                f"[注册] 第 {attempt} 次尝试在 SSO 阶段失败，不再重启浏览器，放弃: {email}"
            )
            break
        label = {"email": "邮箱", "otp": "验证码", "form": "资料"}.get(stage, stage)
        logger.debug(
            f"[注册] {label}阶段失败，重启浏览器"
            f"{f'  {email}' if email else ''}"
        )

    if account_id and email:
        logger.debug(f"[SSO] 账号已入库，待认证池交换 Token: {email}")
        if config.IS_AUTH and risk[0] is not None:
            update_risk(email, risk[0], risk[1] or None, now_str())
        return True, email, risk[0]

    logger.error(f"[注册] 全部尝试失败，放弃: {email}")
    return False, email, risk[0]


def run_signups(
    count: int = 1,
    threads: int = 1,
    headless: bool = False,
    on_result: ResultCallback | None = None,
) -> int:
    """批量多线程注册，返回成功数量。

    on_result: 每个账号结束时回调 (ok, email, risk_bfs)。
    协作取消：request_cancel() 后不再提交新结果统计，已运行 worker 尽快退出。
    """
    init_db()
    worker_count = max(1, min(int(threads), int(count), 20))
    total = max(1, min(int(count), 100))
    if not preflight_check():
        return 0
    logger.info(f"[任务] 注册阶段开始: {total} 账号 / {worker_count} 线程")
    success_count = 0

    def _delayed_signup(idx: int, hless: bool) -> tuple[bool, str | None, int | None]:
        """带随机启动延迟的注册 worker，错开多浏览器并发窗口降低风控概率。"""
        if worker_count > 1:
            delay = random.uniform(3.0, 8.0) * idx
            logger.debug(f"[注册] 线程 {idx + 1} 延迟 {delay:.1f}s 启动")
            time.sleep(delay)
        return run_signup(headless=hless)

    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="注册线程"
    ) as executor:
        futures = [
            executor.submit(_delayed_signup, i, headless) for i in range(total)
        ]
        for future in as_completed(futures):
            try:
                ok, email, risk_bfs = future.result()
            except Exception as e:
                ok, email, risk_bfs = False, None, None
                logger.error(f"[注册] 线程任务异常: {type(e).__name__}: {e}")
            if ok:
                success_count += 1
            if on_result is not None:
                try:
                    on_result(ok, email, risk_bfs)
                except Exception as cb_err:
                    logger.warning(f"[批量注册] on_result 回调异常: {cb_err}")
    logger.info(f"[任务] 注册阶段结束: 成功 {success_count}/{total}")
    return success_count


def run_auth_pool(stop_when: Callable[[], bool] | None = None) -> int:
    """串行消化认证池：用 SSO cookie 协议级自动交换 Token，每账号固定 1 秒间隔。

    全自动认证：从账号记录取 sso_cookie，走 device flow 协议级 approve
    （verify + approve 模拟用户授权），再轮询 token 端点。
    成功补写 accounts 并出池；失败出池并标记需重登（保留 sso 可重试）。
    stop_when: 可选，返回 True 时提前结束（用于任务取消）。
    """
    # 局部导入：oauth 仅在认证池消化时需要
    from workflow.oauth import auth_with_sso

    auth_entries = get_auth_pool()
    if not auth_entries:
        logger.info("[出池] 队列为空，无需消化")
        return 0
    logger.info(f"[出池] 开始消化 {len(auth_entries)} 个")
    success_count = 0
    for entry in auth_entries:
        if stop_when is not None and stop_when():
            logger.warning("[出池] 收到停止信号，中止后续认证")
            break
        email = entry["email"]
        account = get_account_by_email(email)
        aid = int(entry.get("account_id") or (account or {}).get("id") or 0)
        if account is None:
            logger.warning(f"[出池] {email}  账号不存在，已移除")
            remove_from_auth_pool(email)
            continue
        if not aid:
            aid = int(account.get("id") or 0)
        t0 = time.monotonic()
        token, reason = auth_with_sso(account.get("sso_cookie"))
        if token:
            save_account(
                account["email"], account["password"], account["first_name"], account["last_name"],
                account.get("sso_cookie"),
                access_token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                expires_in=token.get("expires_in"),
                status=STATUS_ACTIVE,
                reason=reason,
            )
            remove_from_auth_pool(email)
            success_count += 1
            exp_txt = format_exp(decode_jwt_exp(token.get("access_token")))
            logger.success(
                f"[出池] {email}  认证成功  Token 到期 {exp_txt}  · {elapsed_label(t0)}"
            )
        else:
            update_account_status(email, STATUS_REAUTH, f"0 {reason}")
            remove_from_auth_pool(email)
            logger.warning(
                f"[出池] {email}  认证失败：{reason}  · {elapsed_label(t0)}"
            )
        time.sleep(2.0)  # 账号间固定 2 秒间隔
    logger.info(f"[出池] 消化完成: 成功 {success_count}/{len(auth_entries)}")
    return success_count
