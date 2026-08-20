"""
日志配置模块。

控制台彩色输出 + 当前文件 server.log。
每天午夜滚动为 yyyy-mm-dd.log（北京时间），保留 30 天。
日志时间统一为北京时间（UTC+8），与数据库/任务日志时间戳一致。
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

import loguru._logger as _loguru_logger
from loguru import logger
from loguru._datetime import datetime as _loguru_datetime

from core.config import LOG_DIR

_BEIJING_TZ = timezone(timedelta(hours=8))
_DATED_LOG = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
_LOGURU_STAMP = re.compile(
    r"^server\.(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}-\d{2}_\d+\.log$"
)


def _beijing_now():
    """返回带北京时区的 loguru datetime，供日志时间统一使用。"""
    now = datetime.now(_BEIJING_TZ)
    return _loguru_datetime.combine(
        now.date(), now.time().replace(tzinfo=_BEIJING_TZ)
    )


def _archive_rotated(src: str) -> None:
    """把 loguru 默认的 server.YYYY-MM-DD_HH-MM-SS_ffffff.log 改名为 YYYY-MM-DD.log。"""
    base = os.path.basename(src)
    match = _LOGURU_STAMP.match(base)
    day = match.group(1) if match else (
        datetime.now(_BEIJING_TZ) - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    dest = os.path.join(os.path.dirname(src), f"{day}.log")
    if os.path.abspath(src) == os.path.abspath(dest):
        return
    if os.path.exists(dest):
        with open(dest, "ab") as out, open(src, "rb") as inp:
            out.write(inp.read())
        os.remove(src)
        return
    os.rename(src, dest)


def _retain_dated(_files: list) -> None:
    """删除超过 30 天的 yyyy-mm-dd.log。"""
    cutoff = datetime.now(_BEIJING_TZ).date() - timedelta(days=30)
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return
    for name in names:
        match = _DATED_LOG.match(name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()  # noqa: DTZ007 日志文件名日期解析，无时区语义
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass


# loguru 记录时间取自 aware_now()（默认系统本地时区），此处替换为北京时间
_loguru_logger.aware_now = _beijing_now

os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()

logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
           "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    colorize=True,
)

logger.add(
    os.path.join(LOG_DIR, "server.log"),
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    rotation="00:00",
    compression=_archive_rotated,
    retention=_retain_dated,
    encoding="utf-8",
    enqueue=True,
    backtrace=True,
    diagnose=True,
)

__all__ = ["logger"]
