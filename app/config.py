# app/config.py
import os
from pathlib import Path


class Settings:
    # 模型存储根目录
    MODEL_DIR: Path = Path(os.getenv("MODEL_DIR", "/app/models"))

    # HuggingFace 镜像端点（国内加速，可留空）
    HF_ENDPOINT: str = os.getenv("HF_ENDPOINT", "")

    # HuggingFace Token（可选，用于下载 gated 模型）
    HF_TOKEN: str = os.getenv("HF_TOKEN", "")

    # 每个来源返回的最大结果数
    SEARCH_LIMIT: int = int(os.getenv("SEARCH_LIMIT", "50"))

    def __init__(self):
        self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        if self.HF_ENDPOINT:
            os.environ["HF_ENDPOINT"] = self.HF_ENDPOINT


settings = Settings()
