# app/models.py
from typing import Optional
from pydantic import BaseModel


class SearchResult(BaseModel):
    id: str
    author: str = ""
    downloads: int = 0
    likes: int = 0
    last_modified: str = ""
    tags: list[str] = []
    has_gguf: bool = False
    source: str = "huggingface"


class DownloadRequest(BaseModel):
    model_id: str
    filename: str = ""  # HuggingFace 需要指定文件名
    source: str = "huggingface"
    target_subdir: str = ""  # 可选，下载到模型目录的子目录


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
