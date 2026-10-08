# app/routers/search.py
from fastapi import APIRouter, Query

from app.services import huggingface, modelscope

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query("", description="模糊搜索关键词"),
    source: str = Query("huggingface", description="来源: huggingface | modelscope"),
    gguf_only: bool = Query(True, description="是否过滤无 GGUF 的模型"),
    limit: int = Query(50, ge=1, le=200),
):
    if source == "huggingface":
        results = huggingface.search_models(q, gguf_only, limit)
    elif source == "modelscope":
        results = modelscope.search_models(q, gguf_only, limit)
    else:
        return {"error": "Unsupported source"}

    return {"total": len(results), "results": results}
