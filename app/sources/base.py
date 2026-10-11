"""
源后端抽象。

HuggingFace 与 ModelScope 的搜索/文件列表接口差异极大（字段名、分页、鉴权都不同），
但对外暴露的能力完全一致：搜索模型、查单模型体积、列 GGUF 文件、取单文件元信息。
这一层把公共契约收拢成一个协议，避免上层 router 写一堆 if source == "huggingface"。

鸭子类型即可，不强制继承：两个实现各自独立 import 第三方 SDK，
缺哪个 SDK 都不会拖累另一个。
"""

from typing import Optional


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


def normalize_model_card(
    *, model_id: str, source: str, downloads: int = 0, likes: int = 0,
    last_modified: str = "", tags: Optional[list] = None,
    has_gguf: bool = False, gguf_count: int = 0,
    total_size: Optional[int] = None,
) -> dict:
    """
    把各源的原始模型卡片收敛成统一结构。
    total_size_human / size_known 由 router 层统一填充。
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
