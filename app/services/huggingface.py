# app/services/huggingface.py
from typing import Optional, List, Dict

from huggingface_hub import HfApi, hf_hub_download

api = HfApi()


def search_models(query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
    """
    搜索 HuggingFace 模型
    - query: 模糊搜索关键词
    - gguf_only: 是否只返回包含 GGUF 文件的模型
    """
    filter_args = {}
    if gguf_only:
        filter_args["filter"] = "gguf"

    results: List[Dict] = []
    try:
        for model in api.list_models(
            search=query if query else None,
            sort="downloads",
            direction=-1,
            limit=limit,
            **filter_args,
        ):
            siblings = model.siblings or []

            # 二次确认：实际存在 .gguf 文件
            if gguf_only:
                has_gguf = any(s.rfilename.endswith(".gguf") for s in siblings)
                if not has_gguf:
                    continue
            else:
                has_gguf = any(s.rfilename.endswith(".gguf") for s in siblings)

            results.append(
                {
                    "id": model.id,
                    "author": model.author or "",
                    "downloads": model.downloads or 0,
                    "likes": model.likes or 0,
                    "last_modified": str(model.lastModified) if model.lastModified else "",
                    "tags": model.tags or [],
                    "has_gguf": has_gguf,
                    "source": "huggingface",
                }
            )
    except Exception as e:
        print(f"HuggingFace search error: {e}")

    return results


def get_gguf_files(model_id: str) -> List[Dict]:
    """获取指定模型仓库中所有 GGUF 文件"""
    try:
        info = api.model_info(model_id, files_metadata=True)
        gguf_files = [
            {
                "filename": s.rfilename,
                "size": s.size,
            }
            for s in (info.siblings or [])
            if s.rfilename.endswith(".gguf")
        ]
        # 按文件名排序，方便用户选择（一般 Q4_K_M 等量化版本名称有规律）
        gguf_files.sort(key=lambda x: x["filename"])
        return gguf_files
    except Exception as e:
        print(f"HuggingFace get_gguf_files error: {e}")
        return []


def download_model(
    model_id: str,
    filename: str,
    local_dir: str,
    token: Optional[str] = None,
):
    """下载 GGUF 文件到指定目录"""
    print(f"[HF] Downloading {model_id}/{filename} -> {local_dir}")
    try:
        path = hf_hub_download(
            repo_id=model_id,
            filename=filename,
            local_dir=local_dir,
            token=token or None,
            resume_download=True,
        )
        print(f"[HF] Download finished: {path}")
        return path
    except Exception as e:
        print(f"[HF] Download error: {e}")
        raise
