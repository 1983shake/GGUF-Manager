"""
日志层：控制台 + 内存环形缓冲区。

要点：
  · 时区优先用 zoneinfo（Asia/Shanghai 等含夏令时的也能算对），
    拿不到 tzdata 时退回固定 UTC 偏移，绝不抛异常。
  · 所有 Formatter 集中注册到 _FORMATTERS，切时区时统一同步 converter。
  · 控制台时间戳也走本地时区（容器默认 UTC，否则排查时对不上）。
  · RingBufferHandler 用独立锁，父类自带的 RLock 保持不动，
    否则 emit() 内二次 acquire 会死锁。
  · 模块级 _log_buffer 只创建一次并在 setup_logging 里复用，
    容量可运行时调整；这样 get_logs / set_buffer_size 永远看到的是同一个对象。
"""

import logging
import os
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

# ============================================================
# 时区解析
# ============================================================
def _resolve_timezone(name: str = "") -> tuple:
    """
    解析日志时区。

    优先级：显式参数 > 环境变量 LOG_TZ > 默认值 Asia/Shanghai。
    刻意不读系统 TZ —— 容器里它通常是 UTC，而那正是我们要修正的对象。
    """
    name = (name or os.getenv("LOG_TZ", "")).strip() or "Asia/Shanghai"

    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        pass

    try:
        offset = float(os.getenv("LOG_TZ_OFFSET", "8"))
    except (TypeError, ValueError):
        offset = 8.0
    return timezone(timedelta(hours=offset)), f"UTC{offset:+g}"


LOG_TZ, LOG_TZ_NAME = _resolve_timezone()

# 所有已创建的 Formatter，切换时区时同步更新其 converter
_FORMATTERS: list = []


def set_timezone(name: str = "") -> str:
    """运行时切换日志时区（Web 设置面板改时区后调用），返回生效的时区名。"""
    global LOG_TZ, LOG_TZ_NAME
    LOG_TZ, LOG_TZ_NAME = _resolve_timezone(name)
    for fmt in _FORMATTERS:
        try:
            fmt.converter = _log_converter
        except Exception:
            pass
    return LOG_TZ_NAME


def get_timezone_name() -> str:
    return LOG_TZ_NAME


def format_log_time(ts) -> str:
    """把时间戳格式化成本地时间字符串。失败返回空串而非抛异常。"""
    try:
        return datetime.fromtimestamp(float(ts), LOG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _log_converter(ts):
    """供 logging.Formatter 使用，让控制台输出也走本地时区。"""
    try:
        return datetime.fromtimestamp(ts, LOG_TZ).timetuple()
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(ts).timetuple()


_LEVEL_ORDER = {
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
}

# ============================================================
# 内存环形缓冲区 handler
# ============================================================
class RingBufferHandler(logging.Handler):
    """
    内存环形缓冲区，保留最近 N 条日志供 /api/logs 读取。

    注意：logging.Handler.handle() 会先 acquire 父类自带的 self.lock（RLock），
    然后再调用 emit()。如果我们在 __init__ 里把 self.lock 覆盖成不可重入的
    threading.Lock()，emit() 内再次 acquire 会死锁。因此这里使用独立的
    _buf_lock 保护 buffer，父类锁保持不动。
    """

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.capacity = capacity
        self.buffer: deque = deque(maxlen=capacity)
        self._buf_lock = threading.Lock()
        self._counter = 0

    def resize(self, capacity: int) -> int:
        """
        运行时调整容量（Web 设置面板改“日志条数”时调用）。

        deque 的 maxlen 不可变，只能重建；保留最近的 N 条，避免改设置
        就把历史日志清空。容量非法时保持原值并返回它（幂等）。
        """
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            return self.capacity
        if capacity < 1:
            return self.capacity

        with self._buf_lock:
            if capacity == self.capacity:
                return self.capacity
            kept = list(self.buffer)[-capacity:]
            self.buffer = deque(kept, maxlen=capacity)
            self.capacity = capacity
        return self.capacity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._buf_lock:
                self._counter += 1
                self.buffer.append({
                    "id": self._counter,
                    "time": format_log_time(record.created),
                    "level": record.levelname,
                    "name": record.name,
                    "message": record.getMessage(),
                })
        except Exception:
            self.handleError(record)


# 唯一的模块级缓冲区实例：setup_logging 只调整它的容量，绝不重建，
# 否则 get_logs / set_buffer_size 会拿到一个空的新对象（旧版曾踩过这个坑）。
_log_buffer = RingBufferHandler(capacity=1000)


def setup_logging(level: str = "INFO", buffer_size: int = 1000, tz_name: str = "") -> None:
    """初始化根日志配置。幂等：重复调用只会更新 handler 与级别。"""
    global LOG_TZ, LOG_TZ_NAME

    if tz_name:
        LOG_TZ, LOG_TZ_NAME = _resolve_timezone(tz_name)

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # ⭐ 让控制台时间戳也用本地时区（默认 time.localtime 在容器里通常是 UTC）
    formatter.converter = _log_converter
    _FORMATTERS.append(formatter)

    # 只调整容量，不新建对象 —— 保证 get_logs 始终读到同一份数据
    _log_buffer.resize(buffer_size)
    _log_buffer.setLevel(numeric_level)

    root = logging.getLogger()
    # 清掉已有 handler，避免重复 startup 时日志翻倍
    for h in list(root.handlers):
        root.removeHandler(h)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root.addHandler(stream_handler)
    root.addHandler(_log_buffer)
    root.setLevel(numeric_level)

    # 第三方库降噪：避免 httpx 重试刷屏掩盖真实错误
    for noisy in (
        "httpx", "httpcore", "urllib3", "huggingface_hub", "modelscope",
        "filelock", "asyncio", "multipart", "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# 需要在改级别/容量时同步调整的 logger 集合
_NOISY_LOGGERS = (
    "httpx", "httpcore", "urllib3", "huggingface_hub", "modelscope",
    "filelock", "asyncio", "multipart", "uvicorn.access",
)


def set_level(level: str) -> str:
    """
    运行时调整日志级别，返回生效的级别名。

    同时调整 root、控制台/缓冲区 handler，以及被降噪的第三方 logger。
    """
    name = str(level or "INFO").upper()
    numeric = getattr(logging, name, None)
    if numeric is None:
        return logging.getLevelName(logging.getLogger().level)

    root = logging.getLogger()
    root.setLevel(numeric)
    for h in root.handlers:
        if isinstance(h, (RingBufferHandler, logging.StreamHandler)):
            h.setLevel(numeric)

    # 降噪 logger 保持 WARNING，但用户想看 DEBUG 时放开它们
    if numeric <= logging.DEBUG:
        for n in _NOISY_LOGGERS:
            logging.getLogger(n).setLevel(logging.DEBUG)
    else:
        for n in _NOISY_LOGGERS:
            logging.getLogger(n).setLevel(
                logging.WARNING if numeric > logging.WARNING else numeric
            )
    return name


def set_buffer_size(size: int) -> int:
    """运行时调整日志缓冲条数，返回生效值。"""
    return _log_buffer.resize(size)


def current_level_name() -> str:
    return logging.getLevelName(logging.getLogger().level)


def get_logs(since_id: int = 0, min_level: str = "", limit: int = 500) -> list:
    """获取缓冲区中 id 大于 since_id 的日志（按时间顺序）。"""
    min_num = _LEVEL_ORDER.get(min_level.upper(), 0) if min_level else 0

    with _log_buffer._buf_lock:
        entries = list(_log_buffer.buffer)

    result = []
    for entry in entries:
        if entry["id"] <= since_id:
            continue
        if min_num and _LEVEL_ORDER.get(entry["level"], 0) < min_num:
            continue
        result.append(entry)

    if len(result) > limit:
        result = result[-limit:]
    return result


def get_buffer_stats() -> dict:
    with _log_buffer._buf_lock:
        return {
            "capacity": _log_buffer.capacity,
            "size": len(_log_buffer.buffer),
            "max_id": _log_buffer._counter,
        }
