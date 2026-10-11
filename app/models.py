"""
数据模型 + 体积工具。

体积格式统一放在这里，避免 huggingface / modelscope / scanner 三处各写一份不一致的实现。
所有 Pydantic 请求/响应模型也集中在此，前端看不到后端内部类型。
"""

from typing import Optional, Union

from pydantic import BaseModel, ConfigDict

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
        from datetime import datetime
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            import calendar
            return calendar.timegm(dt.utctimetuple())
        return dt.timestamp()
    except Exception:
        pass

    return None


def format_timestamp(ts) -> str:
    """时间戳 → 可读时间（本地时区），用于界面展示。"""
    if ts is None:
        return ""
    try:
        from app.logger import LOG_TZ
        return datetime.fromtimestamp(float(ts), LOG_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
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


# ============================================================
# Pydantic 请求 / 响应模型
# ============================================================
class SearchResult(BaseModel):
    """搜索结果（含 GGUF 文件体积信息）。"""

    id: str
    author: str = ""
    downloads: int = 0
    likes: int = 0
    last_modified: str = ""
    tags: list[str] = []
    has_gguf: bool = False
    source: str = "huggingface"

    gguf_count: int = 0
    total_size: Optional[int] = None
    total_size_human: str = ""
    size_known: bool = False


class DownloadRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    filename: str = ""
    source: str = "huggingface"
    target_subdir: str = ""


class LocalModel(BaseModel):
    name: str
    filename: str
    path: str
    relative_path: str
    size: int
    size_human: str
    modified: float


class GGUFFile(BaseModel):
    filename: str
    size: Optional[int] = None
    size_human: str = ""


class ModelSize(BaseModel):
    """单个模型的体积查询结果（供前端懒加载）。"""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    source: str = "huggingface"
    gguf_count: int = 0
    total_size: Optional[int] = None
    total_size_human: str = ""
    size_known: bool = False
    error: str = ""


class DownloadPatch(BaseModel):
    """更新检测时传入的模型清单（rel_paths 与 file_paths 二选一）。"""

    file_paths: list[str] = []
    rel_paths: list[str] = []
