"""
源后端包。

对外只暴露两个后端实例（huggingface.backend / modelscope.backend），
调用方无需关心具体类，统一通过 SourceBackend 协议的方法访问。
"""

from app.sources.huggingface import backend as huggingface
from app.sources.modelscope import backend as modelscope

BACKENDS = {
    "huggingface": huggingface,
    "modelscope": modelscope,
}


def get_backend(source: str):
    """按名称取后端实例；找不到返回 None。"""
    return BACKENDS.get(source)


__all__ = ["huggingface", "modelscope", "BACKENDS", "get_backend"]
