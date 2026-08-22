"""
浏览器自动化辅助函数。

模拟真人行为：逐字键入、鼠标移动、Cookie 处理、Turnstile 验证等。
"""

import random
import time
from typing import Any

from core.config import ELEMENT_TIMEOUT, GOTO_TIMEOUT
from core.logger import logger
from core.util import elapsed_label

# Cloudflare 挑战页的识别特征
_CF_CHALLENGE_MARKERS = (
    "cf-chl-", "challenges.cloudflare.com",
    "Verify you are human", "Enable JavaScript and cookies to continue",
)

_TURNSTILE_IFRAME_SEL = (
    "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], "
    "iframe[title*='Cloudflare'], iframe[title*='Widget containing']"
)
_VERIFY_FAIL_MARKERS = (
    "verification failed",
    "refresh the page and try again",
)
# 自动通过宽限期：先等 token，不点勾选，避免打断 managed 模式
_TURNSTILE_AUTO_GRACE = 6.0


def human_mouse_move(page: Any) -> None:
    """模拟鼠标在页面上随机移动。"""
    for _ in range(random.randint(2, 4)):
        page.mouse.move(random.randint(100, 800), random.randint(100, 600))
        page.wait_for_timeout(random.randint(100, 300))


def _human_click(page: Any, x: float, y: float) -> None:
    """模拟真人鼠标轨迹移动到 (x, y) 并点击。"""
    page.mouse.move(x - 80, y - 40)
    page.wait_for_timeout(random.randint(300, 600))
    page.mouse.move(x - 30, y)
    page.wait_for_timeout(random.randint(200, 400))
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(200, 500))
    page.mouse.click(x, y)


def human_click_locator(page: Any, locator: Any) -> bool:
    """随机游走后沿轨迹点到元素中心附近并点击；拿不到盒模型时降级 locator.click。"""
    try:
        human_mouse_move(page)
        box = locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            locator.click()
            return True
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        _human_click(page, x, y)
        return True
    except Exception:
        locator.click()
        return True


def human_type_locator(
    page: Any, locator: Any, text: str, *, clear: bool = False
) -> bool:
    """点击聚焦后逐字键入；clear=True 时先全选删除（验证码复用）。"""
    human_click_locator(page, locator)
    page.wait_for_timeout(random.randint(200, 500))
    if clear:
        locator.press("Control+A")
        page.wait_for_timeout(random.randint(50, 120))
        locator.press("Backspace")
        page.wait_for_timeout(random.randint(80, 180))
    for char in text:
        locator.type(char, delay=random.randint(80, 250))
    page.wait_for_timeout(random.randint(300, 800))
    return True


def _has_body(page: Any) -> bool:
    return page.query_selector("body") is not None


def safe_goto(page: Any, url: str) -> bool:
    """导航到指定 URL，超时不崩溃；出现 Cloudflare 全页挑战则模拟真人点击。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        logger.debug(f"[浏览器] 页面导航完成: {url}")
    except Exception as e:
        logger.debug(f"[浏览器] 页面导航超时: {e}")
        if not _has_body(page):
            return False
        logger.debug("[浏览器] 页面已部分加载，继续尝试")
    handle_cf_challenge(page)
    return True


def _is_cf_challenged(page: Any) -> bool:
    """检测页面是否处于 Cloudflare 全页挑战状态。"""
    try:
        url = page.url
        body = page.inner_text("body") if _has_body(page) else ""
    except Exception:
        return False
    return any(marker in url or marker in body for marker in _CF_CHALLENGE_MARKERS)


def handle_cf_challenge(page: Any, max_wait: int = 90) -> bool:
    """处理 Cloudflare 全页挑战（cf-chl 拦截页）。

    自动通过则跳过；否则模拟真人鼠标轨迹点击验证框，等待挑战通过。
    """
    t0 = time.monotonic()
    if not _is_cf_challenged(page):
        logger.debug(f"[CF挑战] 未出现全页挑战  · {elapsed_label(t0)}")
        return True

    logger.debug("[CF挑战] 检测到 Cloudflare 全页挑战，模拟真人点击...")
    elapsed = 0
    while elapsed < max_wait:
        if not _is_cf_challenged(page):
            logger.debug(f"[CF挑战] 全页挑战已通过  · {elapsed_label(t0)}")
            return True
        frame = page.locator("iframe[src*='challenges.cloudflare.com']").first
        try:
            if frame.count() > 0:
                box = frame.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    _human_click(page, box["x"] + 30, box["y"] + box["height"] / 2)
                    logger.debug("[CF挑战] 已点击验证框")
                    page.wait_for_timeout(5000)
        except Exception as e:
            logger.debug(f"[CF挑战] 点击验证框异常: {e}")
        time.sleep(2)
        elapsed += 2
    logger.debug(f"[CF挑战] 全页挑战等待超时（已等 {max_wait}s）  · {elapsed_label(t0)}")
    return False


def try_click_cookies(page: Any) -> bool:
    """点击 Accept All Cookies，如果没出现则跳过。"""
    try:
        accept_button = page.locator("#onetrust-accept-btn-handler")
        accept_button.wait_for(state="attached", timeout=ELEMENT_TIMEOUT)
        page.evaluate("document.getElementById('onetrust-accept-btn-handler').click()")
        logger.info("[浏览器] 已点击 Accept All Cookies")
        page.wait_for_timeout(1000)
        return True
    except Exception:
        logger.info("[浏览器] 未出现 Cookie 弹窗，跳过")
        return False


def _page_has_markers(page: Any, markers: tuple[str, ...]) -> bool:
    """只扫可见正文（不含 HTML/脚本），避免 Turnstile JS 里的失败文案误判。"""
    lowered = tuple(m.lower() for m in markers)

    def _hit(text: str) -> bool:
        blob = (text or "").lower()
        return any(m in blob for m in lowered)

    try:
        if _has_body(page) and _hit(page.inner_text("body")):
            return True
    except Exception:
        pass
    try:
        for frame in page.frames:
            try:
                if _hit(frame.inner_text("body")):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _turnstile_widget(page: Any) -> Any | None:
    """取面积最大的可见 Turnstile iframe，避免点到隐藏的占位框。"""
    best = None
    best_area = 0.0
    loc = page.locator(_TURNSTILE_IFRAME_SEL)
    try:
        count = loc.count()
    except Exception:
        count = 0
    for index in range(count):
        item = loc.nth(index)
        try:
            box = item.bounding_box()
        except Exception:
            continue
        if not box or box["width"] < 20 or box["height"] < 20:
            continue
        area = float(box["width"]) * float(box["height"])
        if area > best_area:
            best_area = area
            best = item
    if best is not None:
        return best
    try:
        found = page.evaluate("""() => {
            const host = document.querySelector('[data-sitekey]');
            if (!host) return null;
            const root = host.shadowRoot || host;
            const iframe = root.querySelector(
                'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
            );
            if (!iframe) return null;
            const r = iframe.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return true;
        }""")
        if found:
            return page.locator("[data-sitekey] iframe").first
    except Exception:
        pass
    return None


def _click_turnstile_checkbox(page: Any, widget: Any) -> bool:
    """点 Turnstile 左侧勾选框（约 28×28，相对 iframe 左上）。"""
    try:
        widget.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        widget.click(position={"x": 28, "y": 30}, timeout=2500)
        return True
    except Exception:
        pass
    try:
        box = widget.bounding_box()
    except Exception:
        box = None
    if not box or box["width"] <= 0:
        return False
    x = box["x"] + 28
    y = box["y"] + min(box["height"] * 0.5, 32)
    try:
        _human_click(page, x, y)
        return True
    except Exception:
        return False


def _turnstile_passed(page: Any) -> bool:
    """检查 Turnstile token 是否已生成（验证通过）。"""
    try:
        token = page.evaluate("""() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? el.value : null;
        }""")
        return bool(token)
    except Exception:
        return False


def _has_turnstile(page: Any) -> bool:
    """检测页面是否包含 Turnstile 验证组件。"""
    return (
        page.locator(_TURNSTILE_IFRAME_SEL).count() > 0
        or page.locator("input[name='cf-turnstile-response']").count() > 0
        or page.locator("[data-sitekey]").count() > 0
    )


def handle_turnstile(page: Any, max_wait: int = 60) -> str:
    """资料页 Turnstile，返回 passed / skipped / refresh / failed。

    三种形态：
    - 自动通过：等 token，不点勾选
    - 自动失败：可见「Verification failed」才 refresh
    - 勾选框：宽限期后仍无 token，再点左侧勾选
    """
    t0 = time.monotonic()
    logger.debug("[Turnstile] 检查 Cloudflare Turnstile 验证...")

    if not _has_turnstile(page):
        for _ in range(8):
            page.wait_for_timeout(400)
            if _has_turnstile(page) or _turnstile_passed(page):
                break
        else:
            logger.success(f"[CF挑战] 未出现 Turnstile，跳过  · {elapsed_label(t0)}")
            return "skipped"

    logger.debug("[Turnstile] 检测到 Turnstile 验证组件")
    elapsed = 0.0
    clicks = 0
    last_click = -10.0
    while elapsed < max_wait:
        # token 优先：自动通过后脚本里仍可能残留失败文案
        if _turnstile_passed(page):
            logger.success(f"[CF挑战] Turnstile 已通过  · {elapsed_label(t0)}")
            return "passed"
        if _page_has_markers(page, _VERIFY_FAIL_MARKERS):
            logger.debug("[Turnstile] 可见校验失败，需要刷新页面")
            return "refresh"

        # 宽限期内只等自动通过，不点，避免打断 managed 模式
        if elapsed >= _TURNSTILE_AUTO_GRACE and elapsed - last_click >= 4.0:
            widget = _turnstile_widget(page)
            if widget is not None:
                clicks += 1
                last_click = elapsed
                if _click_turnstile_checkbox(page, widget):
                    logger.debug(f"[Turnstile] 已点击勾选框（第 {clicks} 次）")
                page.wait_for_timeout(2000)
                elapsed += 2.0
                continue
            if clicks == 0:
                logger.debug("[Turnstile] 验证框暂不可见，轮询等待...")
        time.sleep(1.0)
        elapsed += 1.0

    if _turnstile_passed(page):
        logger.success(f"[CF挑战] Turnstile 已通过  · {elapsed_label(t0)}")
        return "passed"
    if _page_has_markers(page, _VERIFY_FAIL_MARKERS):
        return "refresh"
    logger.warning(f"[CF挑战] Turnstile 等待超时（已等 {max_wait}s）  · {elapsed_label(t0)}")
    return "failed"


def extract_sso_cookies(browser: Any) -> list[dict[str, Any]] | None:
    """从浏览器中提取 sso / sso-rw cookie（按 name+domain+path 去重，保留各域的值）。"""
    try:
        context = browser.contexts[0] if browser.contexts else None
        if not context:
            logger.debug("[SSO] 未找到浏览器上下文，无法提取 cookie")
            return None

        cookies = context.cookies()
        seen: set[tuple[str, str, str]] = set()
        result: list[dict[str, Any]] = []
        for cookie in cookies:
            name = cookie.get("name", "")
            if name not in ("sso", "sso-rw"):
                continue
            key = (name, cookie.get("domain", ""), cookie.get("path", ""))
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "name": name,
                "value": cookie.get("value"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
            })

        if result:
            logger.debug(f"[SSO] 提取到 {len(result)} 个 SSO cookie（按 name+domain 去重）")
            return result

        logger.debug("[SSO] 未找到 sso / sso-rw cookie")
        return None
    except Exception as e:
        logger.debug(f"[SSO] 提取 cookie 时出错: {e}")
        return None
