"""
源后端包。

本包同时承载「源后端协议」与「模型卡片归一化」，对外只暴露两个后端实例
（huggingface.backend / modelscope.backend），调用方无需关心具体类，
统一通过 SourceBackend 的方法访问。

⚠️ 顺序要求：SourceBackend / normalize_model_card 必须先定义，
   再 import 两个具体后端 —— 它们会 `from app.sources import SourceBackend`，
   此刻本模块已在 sys.modules 中且协议已就绪，不会出现“半初始化”问题。
"""

from collections import OrderedDict
from copy import deepcopy
from threading import Lock
from time import monotonic
from typing import Any, Optional, Tuple


class SourceBackend:
    """源后端协议（鸭子类型，不要求显式继承）。"""

    name: str = ""

    def search_models(self, query: str, gguf_only: bool = True, limit: int = 50) -> list:
        """按关键词搜索模型，返回统一结构的列表。"""
        raise NotImplementedError

    def get_model_size(self, model_id: str) -> dict:
        """
        查询单模型 GGUF 总大小。

        返回 {"gguf_count": int, "total_size": int|None}
        """
        raise NotImplementedError

    def get_gguf_files(self, model_id: str) -> list:
        """
        列出仓库内 GGUF 文件。

        每项：{"filename", "size", "mtime", "etag"}
        """
        raise NotImplementedError

    def get_file_meta(self, model_id: str, filename: str) -> dict:
        """
        取远端单个文件的元信息，用于更新检测。

        返回 {"size": int|None, "mtime": float|None, "etag": str}
        三个维度任一变化即判为“远端出了新版本”：
          · size  大小（重新量化后可能刚好一致，单比它不够）
          · mtime 最后修改时间
          · etag  内容指纹（blob_id / sha256 等）
        """
        raise NotImplementedError

    # 可选：运行时切换端点（如 HF 镜像）
    def apply_endpoint(self, endpoint: str) -> str:
        return endpoint


class TTLCache:
    """
    极简 TTL + LRU 缓存（线程安全）—— 消除「同一个模型被反复查询」的重复上游请求。

    为什么需要：一次搜索要补全 50 个模型的体积，接着用户点开某个模型看文件列表，
    往往还是同一批 id。每次全量重拉上游文件列表（大仓库上百个文件）纯属浪费。

    TTL 取 60s：足够覆盖「搜索 → 浏览 → 点开下载」这段交互；
    又短到不会让「远端是否刚更新」的判断明显过期。

    ⚠️ 存取都走 deepcopy。调用方（如 routers/downloads.py 给每个文件补
       size_human）是**就地修改**列表元素的，直接共享对象会把脏数据写进缓存、
       并污染后续所有调用方。
    """

    def __init__(self, ttl: float = 60.0, maxsize: int = 512):
        self._ttl = ttl
        self._maxsize = maxsize
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> Tuple[Any, bool]:
        now = monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None, False
            ts, value = hit
            if now - ts >= self._ttl:
                del self._data[key]
                return None, False
            self._data.move_to_end(key)  # 命中即刷新 LRU 位置
            return deepcopy(value), True

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (monotonic(), deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def normalize_model_card(
    *, model_id: str, source: str, downloads: int = 0, likes: int = 0,
    last_modified: str = "", tags: Optional[list] = None,
    has_gguf: bool = False, gguf_count: int = 0,
    total_size: Optional[int] = None,
) -> dict:
    """
    把各源的原始模型卡片收敛成统一结构。
    total_size_human / size_known 由路由层统一填充。
    """
    author = model_id.split("/")[0] if model_id else ""
    return {
        "id": model_id,
        "author": author,
        "downloads": downloads or 0,
        "likes": likes or 0,
        "last_modified": last_modified or "",
        "tags": list(tags or []),
        "has_gguf": bool(has_gguf),
        "gguf_count": gguf_count,
        "total_size": total_size,
        "source": source,
    }


# ---- 具体后端实例（必须在协议定义之后导入）----
from app.sources.huggingface import backend as huggingface  # noqa: E402
from app.sources.modelscope import backend as modelscope  # noqa: E402

BACKENDS = {
    "huggingface": huggingface,
    "modelscope": modelscope,
}

SUPPORTED_SOURCES = tuple(BACKENDS)


def get_backend(source: str):
    """按名称取后端实例；找不到返回 None。"""
    return BACKENDS.get(source)


__all__ = [
    "SourceBackend",
    "TTLCache",
    "normalize_model_card",
    "huggingface",
    "modelscope",
    "BACKENDS",
    "SUPPORTED_SOURCES",
    "get_backend",
]
