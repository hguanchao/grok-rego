"""
任务全局互斥：推送 / 号池任务 / 认证 / 注册 同一时刻仅允许一个执行。

- 手动重任务（推送、巡检/重登、认证、注册）之间严格互斥：
  入口处 acquire（原子“检查全部活动任务 + 标记自身”），任务结束 release。
- active() 供只读探测：任意手动重任务进行中时，页面发起按钮整体禁用。

放在 core/ 下以避免业务包（api/workflow）互相导入造成的循环依赖。
"""

from __future__ import annotations

import threading

_lock = threading.RLock()
_active: dict[str, str] = {}


def acquire(name: str) -> None:
    """尝试全局占用；已有其它任务活动则抛 RuntimeError（API 层转为 409）。

    线程安全：检查与占用在同一把全局锁内完成，两个并发请求不会同时通过。
    """
    with _lock:
        for other in _active:
            raise RuntimeError(
                f"任务互斥：{other} 正在执行中，请先等待完成或取消"
            )
        _active[name] = "pending"


def release(name: str) -> None:
    """释放全局占用（幂等，重复调用无害）。"""
    with _lock:
        _active.pop(name, None)


def active() -> list[str]:
    """当前活动任务名列表（供自动续期避让判断，自动续期自身不占用）。"""
    with _lock:
        return list(_active)