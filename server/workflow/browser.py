"""
浏览器自动化辅助函数。

模拟真人行为：逐字键入、鼠标移动、Cookie 处理、Turnstile 验证等。

拟人动作统一委托 workflow.human（贝塞尔轨迹 / 拟人点击 / 拟人键入 / 空闲微动），
本模块只负责元素定位与降级兜底。human_sim 关闭时行为与接入前一致。
"""

import random
import time
from typing import Any

from core.config import GOTO_TIMEOUT
from core.logger import logger
from core.util import elapsed_label, upstream_text
from workflow import human

# 全页拦截特征：禁止用 challenges.cloudflare.com（Turnstile widget 的 iframe/脚本也会命中）
_CF_INTERSTITIAL_URL = ("cf-chl-", "/cdn-cgi/challenge")
_CF_INTERSTITIAL_TITLE = ("just a moment",)
_CF_INTERSTITIAL_BODY = (
    "verify you are human",
    "enable javascript and cookies to continue",
)

_TURNSTILE_IFRAME_SEL = (
    "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], "
    "iframe[id*='cf-chl-widget'], iframe[title*='Cloudflare'], "
    "iframe[title*='Widget containing']"
)
_VERIFY_FAIL_MARKERS = (
    "verification failed",
    "refresh the page and try again",
)
# 自动通过宽限期：先等 token，不点勾选，避免打断 managed 模式
_TURNSTILE_AUTO_GRACE = 6.0
_COOKIE_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "button:text-is('Accept All Cookies')",
    "button:text-is('Accept All')",
    "button:text-is('Allow all')",
)


def human_mouse_move(page: Any) -> None:
    """页面就绪后的拟人热身：一次曲线轨迹 + 阅读停顿。

    轨迹由 workflow.human 自己发（带总时长预算与实测延迟自适应），
    不会像多段裸 mouse.move 那样在用户动鼠标 / 窗口最小化时卡住。
    """
    human.warmup(page)


def _human_click(page: Any, x: float, y: float) -> None:
    """坐标点击：优先拟人轨迹点击；引擎关闭或异常时退化为一次直达 click。"""
    if human.click_at(page, x, y):
        return
    page.mouse.click(x, y, delay=random.randint(30, 90))


def human_click_locator(page: Any, locator: Any) -> bool:
    """拟人点击元素；失败依次退回原生 locator.click 与坐标点击。"""
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    if human.click(page, locator, timeout=8000):
        return True
    try:
        locator.click(timeout=8000, delay=random.randint(30, 90))
        return True
    except Exception:
        pass
    try:
        box = locator.bounding_box()
        if box and box["width"] > 0 and box["height"] > 0:
            x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            y = box["y"] + box["height"] * random.uniform(0.40, 0.60)
            _human_click(page, x, y)
            return True
    except Exception:
        pass
    locator.click(timeout=5000, force=True)
    return True


def human_type_locator(
    page: Any, locator: Any, text: str, *, clear: bool = False
) -> bool:
    """拟人键入：曲线移动聚焦后逐字敲入（含错字回删、思考停顿）并回读校验。

    clear=True 时先全选删除（验证码复用）。引擎不可用时退化为原生逐字键入。
    """
    if human.type_text(page, locator, text, clear=clear):
        return True
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


def _visible_text(page: Any) -> str:
    """失败时摘录页面可见正文；取不到就记下异常原文。"""
    try:
        body = page.inner_text("body") if _has_body(page) else ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return upstream_text(body)


def safe_goto(page: Any, url: str) -> bool:
    """导航到指定 URL，超时不崩溃；出现 Cloudflare 全页挑战则模拟真人点击。"""
    t0 = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        logger.debug(f"[浏览器] 页面导航完成: {url}  · {elapsed_label(t0)}")
    except Exception as e:
        logger.warning(
            f"[浏览器] 页面导航失败: {type(e).__name__}: {e}  · {elapsed_label(t0)}"
        )
        if not _has_body(page):
            return False
        logger.debug("[浏览器] 页面已部分加载，继续尝试")
    handle_cf_challenge(page)
    # 落地后的阅读停顿：真人不会在页面刚出来就立刻动手
    human.reading_pause(page)
    return True


def _is_signup_shell(page: Any) -> bool:
    """当前页是否已是注册落地/表单（有邮箱框或社交注册入口），用于排除 Turnstile 误判。"""
    try:
        if page.locator("input[type='email']").count() > 0:
            return True
    except Exception:
        pass
    try:
        body = (page.inner_text("body") if _has_body(page) else "").lower()
    except Exception:
        return False
    return "sign up with email" in body or "create your grok account" in body


def _is_cf_challenged(page: Any) -> bool:
    """仅识别 Cloudflare 全页拦截，不把资料页上的 Turnstile widget 当成拦截页。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if any(marker in url for marker in _CF_INTERSTITIAL_URL):
        return True
    try:
        title = (page.title() or "").lower()
        if any(marker in title for marker in _CF_INTERSTITIAL_TITLE):
            return True
    except Exception:
        pass
    if _is_signup_shell(page):
        return False
    try:
        body = (page.inner_text("body") if _has_body(page) else "").lower()
    except Exception:
        return False
    return any(marker in body for marker in _CF_INTERSTITIAL_BODY)


def handle_cf_challenge(page: Any, max_wait: int = 90) -> bool:
    """处理 Cloudflare 全页挑战（cf-chl 拦截页）。

    自动通过则跳过；否则点验证框左侧勾选，等待拦截页消失。
    """
    t0 = time.monotonic()
    if not _is_cf_challenged(page):
        logger.debug(f"[CF挑战] 未出现全页挑战  · {elapsed_label(t0)}")
        return True

    logger.debug("[CF挑战] 检测到 Cloudflare 全页拦截，模拟真人点击")
    elapsed = 0.0
    last_click = -10.0
    while elapsed < max_wait:
        if not _is_cf_challenged(page):
            logger.info(f"[CF挑战] 全页拦截已通过  · {elapsed_label(t0)}")
            return True
        if elapsed - last_click >= 4.0:
            widget = _turnstile_widget(page)
            if widget is not None and _click_turnstile_checkbox(page, widget):
                last_click = elapsed
                page.wait_for_timeout(3000)
                elapsed += 3.0
                continue
        time.sleep(1.0)
        elapsed += 1.0
    logger.warning(
        f"[CF挑战] 全页拦截等待超时（已等 {max_wait}s）  · {elapsed_label(t0)}\n"
        f"{_visible_text(page)}"
    )
    return False


def try_click_cookies(page: Any) -> bool:
    """点掉 OneTrust Cookie 横幅/偏好中心；资料页可能再次弹出，需可重复调用。"""
    # 优先 JS 点击：横幅常盖住其它控件；仅当横幅/偏好中心真正可见时点
    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    return true;
                };
                const banner = document.getElementById('onetrust-banner-sdk');
                const pc = document.getElementById('onetrust-pc-sdk');
                if (!visible(banner) && !visible(pc)) return '';
                const ids = [
                    'onetrust-accept-btn-handler',
                    'accept-recommended-btn-handler',
                ];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (!visible(el)) continue;
                    el.click();
                    return id;
                }
                return '';
            }"""
        )
        if clicked:
            logger.debug(f"[浏览器] 已点击 Cookie 同意（{clicked}）")
            page.wait_for_timeout(600)
            return True
    except Exception:
        pass

    deadline = time.time() + 2.0
    while time.time() < deadline:
        for selector in _COOKIE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if not locator.is_visible():
                    continue
                human_click_locator(page, locator)
                logger.debug("[浏览器] 已点击 Cookie 同意")
                page.wait_for_timeout(600)
                return True
            except Exception:
                continue
        page.wait_for_timeout(200)
    logger.debug("[浏览器] 未出现 Cookie 弹窗，跳过")
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


def _page_frames(page: Any) -> list[Any]:
    """主文档 + 子 frame，供 Turnstile iframe 跨 frame 查找。"""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    return frames or [page]


def _turnstile_widget(page: Any) -> Any | None:
    """取面积最大的可见 Turnstile iframe，避免点到隐藏的占位框。"""
    best = None
    best_area = 0.0
    for root in _page_frames(page):
        try:
            loc = root.locator(_TURNSTILE_IFRAME_SEL)
            count = loc.count()
        except Exception:
            continue
        for index in range(count):
            item = loc.nth(index)
            try:
                box = item.bounding_box()
            except Exception:
                continue
            if not box or box["width"] < 10 or box["height"] < 10:
                continue
            area = float(box["width"]) * float(box["height"])
            if area > best_area:
                best_area = area
                best = item
    if best is not None:
        return best
    # 兜底：按 widget id / data-sitekey 宿主找 iframe（SPA 里 src 可能短暂为空）
    for sel in (
        "iframe[id*='cf-chl-widget']",
        "[data-sitekey] iframe",
        "div:has(> input[name='cf-turnstile-response']) iframe",
    ):
        try:
            item = page.locator(sel).first
            if item.count() == 0:
                continue
            box = item.bounding_box()
            if box and box["width"] >= 10 and box["height"] >= 10:
                return item
        except Exception:
            continue
    try:
        found = page.evaluate("""() => {
            const host = document.querySelector('[data-sitekey]')
                || document.querySelector('input[name="cf-turnstile-response"]')?.parentElement;
            if (!host) return null;
            const root = host.shadowRoot || host;
            const iframe = root.querySelector(
                'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[id*="cf-chl-widget"]'
            ) || document.querySelector('iframe[id*="cf-chl-widget"]');
            if (!iframe) return null;
            const r = iframe.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return true;
        }""")
        if found:
            return page.locator("iframe[id*='cf-chl-widget'], [data-sitekey] iframe").first
    except Exception:
        pass
    return None


_TURNSTILE_CHECKBOX_SEL = (
    "input[type='checkbox']",
    "[role='checkbox']",
    ".ctp-checkbox-label",
    "label.ctp-checkbox",
    ".ctp-checkbox-start",
)


def _iframe_frame_locator(widget: Any) -> Any | None:
    """取 iframe 的 FrameLocator。Locator.content_frame 是属性，不是方法。"""
    frame_loc = getattr(widget, "content_frame", None)
    if frame_loc is None:
        return None
    if callable(frame_loc):
        try:
            return frame_loc()
        except TypeError:
            return None
    return frame_loc


def _click_turnstile_checkbox(page: Any, widget: Any) -> bool:
    """点 Turnstile 左侧勾选框。

    优先 FrameLocator 点真实勾选框（不 force，走拟人点击）；
    再退回 iframe 左侧坐标（勾选圆点约在左中，不是 widget 中心）。
    """
    try:
        widget.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    frame_loc = _iframe_frame_locator(widget)
    if frame_loc is not None:
        try:
            role_box = frame_loc.get_by_role("checkbox").first
            if role_box.count() > 0:
                human_click_locator(page, role_box)
                logger.debug("[Turnstile] 已点击勾选框（role=checkbox）")
                return True
        except Exception:
            pass
        for sel in _TURNSTILE_CHECKBOX_SEL:
            try:
                cand = frame_loc.locator(sel).first
                if cand.count() == 0:
                    continue
                human_click_locator(page, cand)
                logger.debug(f"[Turnstile] 已点击勾选框（iframe 内 {sel}）")
                return True
            except Exception:
                continue

    try:
        box0 = widget.bounding_box()
    except Exception:
        box0 = None
    if box0 and box0["width"] > 0 and box0["height"] > 0:
        # 勾选圆点在 widget 左侧，约 (26~30, height/2)，点中心会落在文案上导致验证失败
        x = box0["x"] + min(28.0, max(18.0, box0["width"] * 0.10))
        y = box0["y"] + box0["height"] * 0.50
        try:
            _human_click(page, x, y)
            logger.debug("[Turnstile] 已坐标点击勾选框（页面坐标左侧）")
            return True
        except Exception:
            pass
        try:
            widget.click(
                position={"x": 28, "y": min(32.0, box0["height"] * 0.5)},
                timeout=2500,
            )
            logger.debug("[Turnstile] 已坐标点击勾选框（iframe 内 (28, mid)）")
            return True
        except Exception:
            pass
    logger.warning("[Turnstile] 勾选框点击失败（无可见可点元素/盒模型）")
    return False


def _click_turnstile_by_response_host(page: Any) -> bool:
    """iframe 定位失败时：按 cf-turnstile-response / cf-chl-widget-* 宿主盒模型点左侧勾选。"""
    try:
        box = page.evaluate(
            """() => {
                const input = document.querySelector('input[name="cf-turnstile-response"]');
                if (!input) return null;
                const widgetId = (input.id || '').replace(/_response$/, '');
                const candidates = [];
                if (widgetId) {
                    const byId = document.getElementById(widgetId);
                    if (byId) candidates.push(byId);
                }
                let el = input.parentElement;
                for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                    candidates.push(el);
                }
                for (const node of candidates) {
                    const r = node.getBoundingClientRect();
                    if (r.width >= 40 && r.height >= 20) {
                        return { x: r.x, y: r.y, w: r.width, h: r.height };
                    }
                }
                return null;
            }"""
        )
    except Exception:
        box = None
    if not box:
        return False
    x = float(box["x"]) + min(28.0, max(18.0, float(box["w"]) * 0.10))
    y = float(box["y"]) + float(box["h"]) * 0.50
    try:
        _human_click(page, x, y)
        logger.debug("[Turnstile] 已坐标点击勾选框（response 宿主左侧）")
        return True
    except Exception:
        return False


def _turnstile_passed(page: Any) -> bool:
    """检查 Turnstile token 是否已生成（验证通过）；主文档优先，iframe 兑底。"""
    try:
        token = page.evaluate("""() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (!el) return '';
            return el.value || el.getAttribute('value') || '';
        }""")
        if token:
            return True
    except Exception:
        pass
    try:
        for frame in page.frames:
            try:
                t = frame.evaluate("""() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    if (!el) return '';
                    return el.value || el.getAttribute('value') || '';
                }""")
                if t:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _has_turnstile(page: Any) -> bool:
    """检测页面是否包含 Turnstile 验证组件（含 iframe 内）。"""
    for root in _page_frames(page):
        try:
            if (
                root.locator(_TURNSTILE_IFRAME_SEL).count() > 0
                or root.locator("input[name='cf-turnstile-response']").count() > 0
                or root.locator("[data-sitekey]").count() > 0
            ):
                return True
        except Exception:
            continue
    return False


def handle_turnstile(page: Any, max_wait: int = 60) -> str:
    """资料页 Turnstile，返回 passed / skipped / refresh / failed。

    三种形态：
    - 自动通过：等 token，不点勾选
    - 自动失败：可见「Verification failed」才 refresh
    - 勾选框：宽限期后仍无 token，再点左侧勾选
    """
    t0 = time.monotonic()
    logger.debug("[Turnstile] 检查 Cloudflare Turnstile 验证...")
    # 资料页 OneTrust 常再次弹出并盖住勾选框，先清遮罩再检测
    try_click_cookies(page)

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
    last_overlay = -10.0
    last_miss_log = -10.0
    while elapsed < max_wait:
        # token 优先：自动通过后脚本里仍可能残留失败文案
        if _turnstile_passed(page):
            logger.success(f"[CF挑战] Turnstile 已通过  · {elapsed_label(t0)}")
            return "passed"
        if _page_has_markers(page, _VERIFY_FAIL_MARKERS):
            logger.warning(f"[Turnstile] 可见校验失败\n{_visible_text(page)}")
            return "refresh"

        # 宽限期内只等自动通过，不点，避免打断 managed 模式
        if elapsed >= _TURNSTILE_AUTO_GRACE and elapsed - last_click >= 4.0:
            if elapsed - last_overlay >= 8.0:
                try_click_cookies(page)
                last_overlay = elapsed
            widget = _turnstile_widget(page)
            clicked = False
            if widget is not None:
                clicked = _click_turnstile_checkbox(page, widget)
            elif _click_turnstile_by_response_host(page):
                clicked = True
            if clicked or widget is not None:
                clicks += 1
                last_click = elapsed
                if clicked:
                    logger.debug(f"[Turnstile] 已点击勾选框（第 {clicks} 次）")
                page.wait_for_timeout(2500)
                elapsed += 2.5
                continue
            if clicks == 0 and elapsed - last_miss_log >= 5.0:
                logger.debug("[Turnstile] 验证框暂不可见，轮询等待...")
                last_miss_log = elapsed
        time.sleep(1.0)
        elapsed += 1.0

    if _turnstile_passed(page):
        logger.success(f"[CF挑战] Turnstile 已通过  · {elapsed_label(t0)}")
        return "passed"
    if _page_has_markers(page, _VERIFY_FAIL_MARKERS):
        return "refresh"
    logger.warning(
        f"[CF挑战] Turnstile 等待超时（已等 {max_wait}s）  · {elapsed_label(t0)}\n"
        f"{_visible_text(page)}"
    )
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
