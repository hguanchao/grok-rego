"""
全局仿真人引擎（防风控）。

注册链路里所有「机械式」鼠标/键盘动作改由本模块接管，覆盖：

- 鼠标轨迹：三次贝塞尔曲线 + 垂直方向抖动 + 落点过冲回拉，模拟真人挥动手腕的弧线
- 点击：落点带随机偏移（不总在正中心）、按下时长随机、点击前后有微动与停顿
- 键入：逐字高斯延迟、思考停顿、偶发错字回删重打、大写走 Shift
- 滚动：分段滚动 + 段间停顿
- 空闲：等待轮询期间随机微动 / 轻微滚动，避免光标长时间静止被判定为自动化

设计约束（重要）：

1. **绝不卡死主流程**。Camoufox 自带 humanize 会在鼠标移动上叠加最长 maxTime 的等待，
   用户动鼠标或窗口最小化时可能等不到轨迹结束。本模块因此：
   - 单次轨迹有总时长硬上限（move_budget）；
   - 逐步实测 `mouse.move` 真实耗时，动态下调步数（自适应）；
   - 每一步都吞异常，任何失败立刻退化为「一次直达移动 + 点击」。
2. **可全局关闭**。`config.json` 的 `human_sim=false` 时全部走原生直连路径，
   行为与接入前一致（Camoufox humanize 保持 0.8s）。
3. 多注册线程并发安全：状态按 page 隔离，随机源使用模块级 random。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Any

from core import config
from core.logger import logger

# ────────────────────────────────────────────────────────────────────────────
# 档位配置
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Profile:
    """一档拟人强度的全部可调参数。"""

    name: str
    move_budget: float  # 单次鼠标轨迹总时长硬上限（秒）
    step_ms: tuple[int, int]  # 轨迹每步之间的停顿（毫秒）
    steps_per_100px: float  # 每 100px 距离期望的轨迹步数
    max_steps: int  # 步数硬上限
    arc: float  # 贝塞尔弧线弯曲系数（相对距离）
    jitter: float  # 落点随机抖动（像素）
    overshoot_rate: float  # 过冲（先划过目标再回拉）概率
    pre_click_ms: tuple[int, int]  # 点击前驻留
    press_ms: tuple[int, int]  # 鼠标按下时长
    post_click_ms: tuple[int, int]  # 点击后停顿
    type_ms: tuple[int, int]  # 按键间隔（毫秒）
    type_sd: float  # 按键间隔标准差
    typo_rate: float  # 错字概率
    think_rate: float  # 思考停顿概率
    think_ms: tuple[int, int]  # 思考停顿时长（毫秒）
    read_ms: tuple[int, int]  # 导航/翻页后的阅读停顿（毫秒）
    fidget_rate: float  # 空闲轮询时触发微动的概率
    scroll_ms: tuple[int, int]  # 分段滚动段间停顿


_PROFILES: dict[str, _Profile] = {
    # 轻量：快，轨迹仍为曲线但不做过冲/错字，适合追求吞吐
    "light": _Profile(
        name="light",
        move_budget=0.35,
        step_ms=(4, 12),
        steps_per_100px=2.5,
        max_steps=10,
        arc=0.10,
        jitter=1.5,
        overshoot_rate=0.0,
        pre_click_ms=(40, 120),
        press_ms=(25, 70),
        post_click_ms=(60, 180),
        type_ms=(45, 115),
        type_sd=18.0,
        typo_rate=0.0,
        think_rate=0.0,
        think_ms=(0, 0),
        read_ms=(300, 900),
        fidget_rate=0.08,
        scroll_ms=(40, 110),
    ),
    # 标准：默认档，曲线 + 偶发错字 + 思考停顿，速度与拟真的平衡点
    "normal": _Profile(
        name="normal",
        move_budget=0.75,
        step_ms=(6, 22),
        steps_per_100px=4.5,
        max_steps=22,
        arc=0.16,
        jitter=2.5,
        overshoot_rate=0.18,
        pre_click_ms=(90, 260),
        press_ms=(40, 110),
        post_click_ms=(140, 420),
        type_ms=(70, 190),
        type_sd=42.0,
        typo_rate=0.015,
        think_rate=0.05,
        think_ms=(280, 900),
        read_ms=(700, 1900),
        fidget_rate=0.18,
        scroll_ms=(60, 180),
    ),
    # 重度：全量拟真，慢但最像人，适合被风控盯上时降速跑
    "heavy": _Profile(
        name="heavy",
        move_budget=1.25,
        step_ms=(8, 30),
        steps_per_100px=7.0,
        max_steps=34,
        arc=0.22,
        jitter=3.5,
        overshoot_rate=0.28,
        pre_click_ms=(150, 420),
        press_ms=(55, 150),
        post_click_ms=(240, 700),
        type_ms=(95, 265),
        type_sd=65.0,
        typo_rate=0.03,
        think_rate=0.10,
        think_ms=(400, 1400),
        read_ms=(1200, 3000),
        fidget_rate=0.28,
        scroll_ms=(90, 260),
    ),
}

LEVELS: tuple[str, ...] = ("light", "normal", "heavy")
DEFAULT_LEVEL = "normal"

# 键位相邻表：错字时敲成手滑键，再回删重打
_NEIGHBORS: dict[str, str] = {
    "a": "sqw", "b": "vgn", "c": "xdv", "d": "sfe", "e": "wrd", "f": "dgr",
    "g": "fht", "h": "gjy", "i": "uok", "j": "hkn", "k": "jlm", "l": "k",
    "m": "nk", "n": "bm", "o": "ipl", "p": "o", "q": "wa", "r": "etf",
    "s": "adw", "t": "ryg", "u": "yij", "v": "cb", "w": "qes", "x": "zc",
    "y": "tuh", "z": "x",
    "1": "2q", "2": "13w", "3": "24e", "4": "35r", "5": "46t", "6": "57y",
    "7": "68u", "8": "79i", "9": "80o", "0": "9p",
}

# 单步 mouse.move 实测耗时超过该值，说明底层（Camoufox humanize / 页面卡顿）
# 在叠加等待，立即收敛步数，避免整段轨迹拖垮流程
_SLOW_MOVE_SEC = 0.18
# 视口兜底尺寸（拿不到 viewport_size 时用）
_FALLBACK_VIEWPORT = (1280, 720)


# ────────────────────────────────────────────────────────────────────────────
# 档位与开关
# ────────────────────────────────────────────────────────────────────────────


def enabled() -> bool:
    """全局开关：config.json 的 human_sim。"""
    return bool(getattr(config, "HUMAN_SIM", True))


def level() -> str:
    """当前拟人档位，非法值回落 normal。"""
    raw = str(getattr(config, "HUMAN_LEVEL", DEFAULT_LEVEL) or "").strip().lower()
    return raw if raw in _PROFILES else DEFAULT_LEVEL


def profile() -> _Profile:
    """当前档位参数表。"""
    return _PROFILES[level()]


def describe() -> str:
    """日志用：开关 + 档位摘要。"""
    if not enabled():
        return "仿真人引擎=关闭（走 Camoufox humanize）"
    prof = profile()
    return (
        f"仿真人引擎=开启 档位={prof.name} "
        f"（轨迹≤{prof.move_budget:.2f}s · 键入{prof.type_ms[0]}~{prof.type_ms[1]}ms "
        f"· 错字率{prof.typo_rate:.0%}）"
    )


# ────────────────────────────────────────────────────────────────────────────
# 基础工具
# ────────────────────────────────────────────────────────────────────────────


def pause_ms(lo: int, hi: int) -> None:
    """按档位区间随机停顿（毫秒）。"""
    if hi <= lo:
        time.sleep(max(0, lo) / 1000)
        return
    time.sleep(random.randint(lo, hi) / 1000)


def pause_seconds(lo: float, hi: float) -> None:
    """秒级随机停顿。"""
    if hi <= lo:
        time.sleep(max(0.0, lo))
        return
    time.sleep(random.uniform(lo, hi))


class _PageState:
    """单个 page 的拟人状态：虚拟光标位置 + 实测 move 耗时。"""

    __slots__ = ("x", "y", "avg_move", "moves")

    def __init__(self) -> None:
        self.x: float | None = None
        self.y: float | None = None
        self.avg_move: float | None = None
        self.moves: int = 0

    def observe(self, cost: float) -> None:
        """记录一次 mouse.move 的真实耗时，做指数平滑。"""
        self.moves += 1
        if self.avg_move is None:
            self.avg_move = cost
        else:
            self.avg_move = self.avg_move * 0.7 + cost * 0.3


_STATES: dict[int, _PageState] = {}
_STATES_CAP = 128


def _state(page: Any) -> _PageState:
    """取 page 的拟人状态（按对象 id 隔离，并发线程各持有自己的 page）。"""
    key = id(page)
    state = _STATES.get(key)
    if state is None:
        if len(_STATES) >= _STATES_CAP:
            _STATES.clear()
        state = _PageState()
        _STATES[key] = state
    return state


def _viewport(page: Any) -> tuple[float, float]:
    """视口宽高，拿不到时用兜底值。"""
    try:
        size = page.viewport_size
        if size and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
    except Exception:
        pass
    return float(_FALLBACK_VIEWPORT[0]), float(_FALLBACK_VIEWPORT[1])


def _clamp_to_viewport(page: Any, x: float, y: float) -> tuple[float, float]:
    width, height = _viewport(page)
    return min(max(x, 1.0), width - 1.0), min(max(y, 1.0), height - 1.0)


# ────────────────────────────────────────────────────────────────────────────
# 鼠标轨迹
# ────────────────────────────────────────────────────────────────────────────


def _bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """三次贝塞尔在参数 t 上的单轴取值。"""
    mt = 1.0 - t
    return mt * mt * mt * p0 + 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t * t * t * p3


def _control_points(
    x0: float, y0: float, x1: float, y1: float, prof: _Profile
) -> tuple[float, float, float, float]:
    """按起点终点生成两个控制点，形成带自然弧度的轨迹（非直线）。"""
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    # 垂直单位向量：弧线向法线方向鼓出
    nx, ny = -dy / dist, dx / dist
    bend = dist * prof.arc * random.uniform(-1.0, 1.0)
    # 控制点沿路径方向分别落在 1/3、2/3 处，弧线更接近真人手腕摆动
    c1x = x0 + dx * 0.33 + nx * bend
    c1y = y0 + dy * 0.33 + ny * bend
    c2x = x0 + dx * 0.66 + nx * bend * 0.65
    c2y = y0 + dy * 0.66 + ny * bend * 0.65
    return c1x, c1y, c2x, c2y


def _planned_steps(dist: float, prof: _Profile, state: _PageState) -> int:
    """期望步数：距离越远步数越多，再按实测单步耗时收敛。"""
    by_dist = int(dist / 100.0 * prof.steps_per_100px) + 3
    steps = min(by_dist, prof.max_steps)
    if state.avg_move and state.avg_move > 0:
        affordable = int(prof.move_budget / state.avg_move)
        if affordable > 0:
            steps = min(steps, affordable)
    return max(1, steps)


def move_to(page: Any, x: float, y: float, *, budget: float | None = None) -> bool:
    """拟人移动光标到指定坐标，受总时长预算约束，失败不影响调用方。

    返回是否走完轨迹（False 表示已退化为直达移动，调用方无需处理）。
    """
    if not enabled():
        return False
    prof = profile()
    state = _state(page)
    tx, ty = _clamp_to_viewport(page, float(x), float(y))
    if state.x is None or state.y is None:
        # 无历史位置时先落一个随机起点，避免每次都从 (0,0) 划过来
        width, height = _viewport(page)
        state.x, state.y = random.uniform(width * 0.2, width * 0.8), random.uniform(
            height * 0.2, height * 0.8
        )
    x0, y0 = state.x, state.y
    dist = math.hypot(tx - x0, ty - y0)
    if dist < 2.0:
        state.x, state.y = tx, ty
        return True

    total_budget = prof.move_budget if budget is None else budget
    deadline = time.monotonic() + total_budget
    steps = _planned_steps(dist, prof, state)
    c1x, c1y, c2x, c2y = _control_points(x0, y0, tx, ty, prof)
    # 过冲：先多走一小段再回拉，模拟真人划过头
    overshoot = random.random() < prof.overshoot_rate and dist > 60
    if overshoot:
        c2x += (tx - x0) * 0.08
        c2y += (ty - y0) * 0.08

    last_x, last_y = x0, y0
    for index in range(1, steps + 1):
        if time.monotonic() >= deadline:
            break
        t = index / steps
        # 缓入缓出：起步慢、中段快、收尾慢
        eased = t * t * (3 - 2 * t)
        px = _bezier(eased, x0, c1x, c2x, tx)
        py = _bezier(eased, y0, c1y, c2y, ty)
        if index < steps:
            px += random.uniform(-prof.jitter, prof.jitter)
            py += random.uniform(-prof.jitter, prof.jitter)
        px, py = _clamp_to_viewport(page, px, py)
        started = time.monotonic()
        try:
            page.mouse.move(px, py)
        except Exception:
            break
        cost = time.monotonic() - started
        state.observe(cost)
        last_x, last_y = px, py
        if cost >= _SLOW_MOVE_SEC:
            # 底层在叠加等待（humanize 未关 / 页面卡顿）：立刻收敛，剩余距离一次直达
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pause_ms(*prof.step_ms)

    # 收尾：确保光标精确落在目标点上（过冲回拉也在此完成）
    try:
        page.mouse.move(tx, ty)
        last_x, last_y = tx, ty
    except Exception:
        pass
    state.x, state.y = last_x, last_y
    return True


def move_to_box(page: Any, box: dict[str, Any]) -> tuple[float, float]:
    """移动到元素盒模型内的随机落点，返回最终坐标。

    落点偏向中心但不固定在正中心（真人不会每次都点正中）。
    """
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    x = float(box.get("x") or 0)
    y = float(box.get("y") or 0)
    if width <= 0 or height <= 0:
        return x, y
    # 中心 ±22% 的随机偏移，再按元素尺寸留出安全边距
    fx = x + width * random.uniform(0.28, 0.72)
    fy = y + height * random.uniform(0.32, 0.68)
    return fx, fy


def click_at(page: Any, x: float, y: float) -> bool:
    """拟人点击指定坐标：移动 → 点击前驻留 → 按下/抬起 → 点击后停顿。"""
    if not enabled():
        return False
    try:
        move_to(page, x, y)
        prof = profile()
        pause_ms(*prof.pre_click_ms)
        page.mouse.click(x, y, delay=random.randint(*prof.press_ms))
        pause_ms(*prof.post_click_ms)
        return True
    except Exception:
        return False


def _hit_test(page: Any, locator: Any, x: float, y: float) -> bool:
    """落点是否真能命中目标元素，用于发现「被遮罩挡住」的情况。

    拟人点击走 page.mouse.click，绕过了 Playwright 的可点击性校验（actionability）。
    若元素被 Cookie 横幅等浮层盖住，鼠标会点在浮层上而目标毫发无损，
    因此点击前先用 elementFromPoint 做一次命中检测，不命中则交回原生 locator.click。

    iframe 内元素（如 Turnstile 勾选框）在主文档里无法命中检测，直接放行，
    否则会把最需要拟人的这一步退化掉。
    """
    try:
        handle = locator.element_handle()
        if handle is None:
            return True
        return bool(
            page.evaluate(
                """([el, x, y]) => {
                    if (!el) return true;
                    if (el.ownerDocument !== document) return true;
                    const top = document.elementFromPoint(x, y);
                    if (!top) return false;
                    return el === top || el.contains(top) || top.contains(el);
                }""",
                [handle, x, y],
            )
        )
    except Exception:
        # 拿不到 handle / 跨域限制等：不做判定，按原计划点
        return True


def click(page: Any, locator: Any, timeout: int = 8000) -> bool:
    """拟人点击元素：滚动入视 → 取盒模型 → 命中检测 → 曲线移动 → 点击。

    失败返回 False，由调用方走原生 locator.click 兜底（自带可点击性等待）。
    """
    if not enabled():
        return False
    try:
        try:
            locator.scroll_into_view_if_needed(timeout=min(timeout, 2000))
        except Exception:
            pass
        box = locator.bounding_box()
        if not box or float(box.get("width") or 0) <= 0 or float(box.get("height") or 0) <= 0:
            return False
        x, y = move_to_box(page, box)
        if not _hit_test(page, locator, x, y):
            return False
        return click_at(page, x, y)
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────────────
# 键入
# ────────────────────────────────────────────────────────────────────────────


def _next_delay(prof: _Profile) -> int:
    """按键间隔：高斯分布后夹到档位区间内，避免机械等距。"""
    mean = (prof.type_ms[0] + prof.type_ms[1]) / 2
    value = random.gauss(mean, prof.type_sd)
    return int(min(max(value, prof.type_ms[0]), prof.type_ms[1]))


def _press_char(page: Any, char: str) -> None:
    """按下一个字符；大写走 Shift+X，更接近真人按法。"""
    keyboard = page.keyboard
    if char.isupper():
        keyboard.press(f"Shift+{char}")
        return
    keyboard.press(char)


def _mistype(page: Any, char: str, prof: _Profile) -> None:
    """敲错一个邻键，停顿后回删，模拟真人手滑修正。"""
    wrong_list = _NEIGHBORS.get(char.lower())
    if not wrong_list:
        return
    wrong = random.choice(wrong_list)
    if wrong.isalpha() and char.isupper():
        wrong = wrong.upper()
    try:
        _press_char(page, wrong)
        pause_ms(120, 320)
        page.keyboard.press("Backspace")
        pause_ms(90, 240)
    except Exception:
        pass


def _select_all_delete(page: Any) -> None:
    """全选删除：清空输入框（验证码复用 / 键入重试前）。"""
    try:
        page.keyboard.press("Control+A")
        pause_ms(60, 160)
        page.keyboard.press("Backspace")
        pause_ms(90, 220)
    except Exception:
        pass


def _emit_keys(page: Any, prof: _Profile, text: str) -> None:
    """按真人节奏逐字敲入：思考停顿 + 偶发错字回删。"""
    since_think = 0
    for char in text:
        # 思考停顿：连续敲若干字符后停一下，节奏更像人
        since_think += 1
        if prof.think_rate > 0 and since_think >= random.randint(4, 11):
            if random.random() < prof.think_rate * 4:
                pause_ms(*prof.think_ms)
            since_think = 0
        if prof.typo_rate > 0 and random.random() < prof.typo_rate:
            _mistype(page, char, prof)
        _press_char(page, char)
        delay = _next_delay(prof)
        pause_ms(int(delay * 0.7), delay)


def type_text(
    page: Any, locator: Any, text: str, *, clear: bool = False, verify: bool = True
) -> bool:
    """拟人键入：点击聚焦 → 逐字（含错字回删/思考停顿）→ 回读校验，失败重打一次。

    clear=True 时先全选删除（验证码框复用场景）。
    verify=True 时回读输入框实际值；不一致则清掉重打一次，仍不一致仅告警不阻塞。

    返回 False 表示「一个字都没敲进去」，调用方应走原生兜底；
    已敲入但校验不符时仍返回 True，避免调用方重复键入造成内容翻倍。
    """
    if not enabled():
        return False
    if not text:
        return False
    prof = profile()
    for attempt in range(2):
        try:
            if not click(page, locator):
                return False
            pause_ms(120, 340)
            # 首次按 clear 清空；重试时一律先清空，避免内容翻倍
            if clear or attempt > 0:
                _select_all_delete(page)
            _emit_keys(page, prof, text)
            pause_ms(150, 420)
            if not verify or _verify_value(locator, text):
                return True
            logger.debug(
                f"[仿真人] 键入回读不符，重打一次（第 {attempt + 1} 次）"
            )
        except Exception:
            return False
    return True


def _verify_value(locator: Any, expected: str) -> bool:
    """回读输入框实际值，与预期不一致时告警（用于发现被前端格式化/吞字）。"""
    try:
        actual = locator.input_value()
    except Exception:
        return True  # 读不到（非 input / 已卸载）不视为失败
    if actual == expected:
        return True
    # OTP 分框、邮箱大小写归一化等场景会不一致，仅提示不阻塞
    if str(actual).strip().lower() != str(expected).strip().lower():
        logger.debug(
            f"[仿真人] 键入回读不一致：期望 {expected!r} 实际 {actual!r}"
        )
        return False
    return True


# ────────────────────────────────────────────────────────────────────────────
# 滚动 / 空闲微动 / 热身
# ────────────────────────────────────────────────────────────────────────────


def scroll(page: Any, total: int, segments: int | None = None) -> bool:
    """分段拟人滚动：一次滚到底太机械，拆成若干段并段间停顿。"""
    if not enabled() or not total:
        return False
    prof = profile()
    if segments is None:
        segments = max(2, min(6, abs(total) // 120 + 2))
    per = total / segments
    try:
        for _ in range(segments):
            page.mouse.wheel(0, per)
            pause_ms(*prof.scroll_ms)
        return True
    except Exception:
        return False


def fidget(page: Any, chance: float | None = None) -> bool:
    """空闲微动：在等待轮询里随机小幅晃一下光标或滚一滚，避免长时间静止。

    chance 默认取档位 fidget_rate；调用方在密集轮询里传入更低概率以控制开销。
    """
    if not enabled():
        return False
    prof = profile()
    rate = prof.fidget_rate if chance is None else chance
    if rate <= 0 or random.random() >= rate:
        return False
    state = _state(page)
    width, height = _viewport(page)
    if state.x is None or state.y is None:
        state.x, state.y = width * 0.5, height * 0.5
    # 小幅位移（≤12% 视口），预算压到 0.3s，避免在轮询里拖慢流程
    dx = random.uniform(-width * 0.12, width * 0.12)
    dy = random.uniform(-height * 0.12, height * 0.12)
    try:
        move_to(page, state.x + dx, state.y + dy, budget=0.3)
    except Exception:
        return False
    # 偶尔顺手滚一屏的一小截
    if random.random() < 0.25:
        try:
            page.mouse.wheel(0, random.choice((-90, -60, 60, 90)))
        except Exception:
            pass
    return True


def warmup(page: Any) -> None:
    """页面就绪后的热身：一次短轨迹 + 阅读停顿，替代原先的空操作。"""
    if not enabled():
        return
    prof = profile()
    width, height = _viewport(page)
    try:
        move_to(
            page,
            random.uniform(width * 0.25, width * 0.75),
            random.uniform(height * 0.25, height * 0.75),
        )
    except Exception:
        pass
    pause_ms(*prof.read_ms)


def reading_pause(page: Any, scale: float = 1.0) -> None:
    """翻页 / 提交后的阅读停顿（档位 read_ms 乘系数）。"""
    if not enabled():
        return
    prof = profile()
    lo = int(prof.read_ms[0] * scale)
    hi = int(prof.read_ms[1] * scale)
    pause_ms(lo, max(lo, hi))


def before_submit(page: Any) -> None:
    """提交前的短暂停顿：真人点提交前会扫一眼表单。"""
    if not enabled():
        return
    prof = profile()
    pause_ms(int(prof.pre_click_ms[0]), int(prof.pre_click_ms[1] * 2))
    fidget(page, chance=0.35)
