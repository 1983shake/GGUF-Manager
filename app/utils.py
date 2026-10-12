"""
体积 / 时间工具。

本文件只放跨模块共用的纯工具函数（体积 / 时间格式化）。

这里不定义请求体模型：路由层实际收发的是普通 dict，真正需要校验的请求体
（如 DownloadRequest）放在使用它的路由模块里就近维护。

下面四个函数被 huggingface / modelscope / local 三处共用，
集中一处避免各写一份、结果不一致。
"""

import calendar
from datetime import datetime
from typing import Optional, Union

from app.logger import LOG_TZ

_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def human_size(size: Optional[Union[int, float]]) -> str:
    """
    把字节数转成人类可读字符串。

    None / 负数 / 非数字 → ""（表示“未知”，与“大小为 0”严格区分）
    """
    if size is None:
        return ""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""

    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            if unit in ("GB", "TB", "PB"):
                return f"{value:.2f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def coerce_timestamp(value) -> Optional[float]:
    """
    把各种形态的时间值统一成 POSIX 时间戳（秒，浮点）。

    上游返回的时间字段非常不统一：
      · ModelScope 旧版给毫秒整数 LastModified
      · 新版可能给 ISO 字符串 updated_at
      · 有的实现给秒级整数
    解析不了就返回 None —— 宁可少一个判据，也不能把脏数据当真。
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v <= 0:
            return None
        if v > 1e14:
            return v / 1e6
        if v > 1e11:
            return v / 1e3
        return v

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return coerce_timestamp(float(text))
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return calendar.timegm(dt.utctimetuple())
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        pass

    return None


def format_timestamp(ts) -> str:
    """
    时间戳 → 可读时间（本地时区），用于界面展示。

    ⚠️ `datetime` 必须是模块级 import。若只在 coerce_timestamp 内部局部 import，
    本函数直接引用会抛 NameError，被 except 吞掉后**永远返回空串**，
    “远端更新时间”在界面上就会一直是空的。
    LOG_TZ 在调用时才取，确保 set_timezone() 之后立刻生效。
    """
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), LOG_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def sum_sizes(sizes) -> Optional[int]:
    """
    累加一批可能为 None 的字节数。

    全为 None 时返回 None（表示“未知”），而不是 0 —— 0 会被误渲染成 "0 B"。
    """
    total = 0
    known = False
    for s in sizes:
        if s is None:
            continue
        try:
            total += int(s)
            known = True
        except (TypeError, ValueError):
            continue
    return total if known else None
