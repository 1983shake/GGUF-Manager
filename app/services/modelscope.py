# app/services/modelscope.py
from typing import List, Dict

from modelscope.hub.api import HubApi
from modelscope.hub.snapshot_download import snapshot_download


def search_models(query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
    """搜索 ModelScope 模型"""
    results: List[Dict] = []
    try:
        api = HubApi()
        response = api.list_models(
            model_name=query if query else None,
            page_number=1,
            page_size=limit,
        )
        models = response.get("Models", []) if isinstance(response, dict) else []

        for m in models:
            tags = m.get("Tags", []) or []
            tag_lower = [str(t).lower() for t in tags]

            has_gguf = "gguf" in tag_lower or m.get("ModelType", "").lower() == "gguf"

            if gguf_only and not has_gguf:
                continue

            results.append(
                {
                    "id": m.get("Path", ""),
                    "author": m.get("Path", "").split("/")[0] if m.get("Path") else "",
                    "downloads": m.get("Downloads", 0) or 0,
                    "likes": m.get("Stars", 0) or 0,
                    "last_modified": m.get("LastUpdatedTime", "") or "",
                    "tags": tags,
                    "has_gguf": has_gguf,
                    "source": "modelscope",
                }
            )
    except Exception as e:
        print(f"ModelScope search error: {e}")

    return results


def get_gguf_files(model_id: str) -> List[Dict]:
    """获取 ModelScope 模型仓库中的 GGUF 文件列表"""
    try:
        api = HubApi()
        files = api.get_model_files(model_id, recursive=True)
        gguf_files = []
        for f in files:
            name = f.get("Path") or f.get("Name") or ""
            if name.endswith(".gguf"):
                gguf_files.append(
                    {
                        "filename": name,
                        "size": f.get("Size"),
                    }
                )
        gguf_files.sort(key=lambda x: x["filename"])
        return gguf_files
    except Exception as e:
        print(f"ModelScope get_gguf_files error: {e}")
        return []


def download_model(model_id: str, local_dir: str):
    """下载 ModelScope 模型到指定目录"""
    print(f"[MS] Downloading {model_id} -> {local_dir}")
    try:
        path = snapshot_download(
            model_id=model_id,
            cache_dir=local_dir,
        )
        print(f"[MS] Download finished: {path}")
        return path
    except Exception as e:
        print(f"[MS] Download error: {e}")
        raise
