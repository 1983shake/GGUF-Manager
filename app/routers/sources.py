"""
搜索路由（v1.3.0）。

GET /api/search        搜索模型（体积能带就带，带不上由前端懒加载补齐）
GET /api/search/size   查询单个模型的 GGUF 总大小
GET /api/search/sizes  批量查询多个模型的体积（前端补齐整页时用）

改用 SourceBackend 协议调用各源，去除散落的 import 与重复逻辑。
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Query

from app.logger import get_logger
from app.models import human_size
from app.sources import get_backend

logger = get_logger(__name__)
router = APIRouter(prefix="/api/search", tags=["search"])

_SIZE_WORKERS = 6  # /size 并发上限（前端也有并发控制，这里兜底）


def _fill_size(item: dict) -> None:
    """统一格式化体积字段，缺失时留空（前端据此显示“未知”）。"""
    total = item.get("total_size")
    item["total_size_human"] = human_size(total)
    item["size_known"] = total is not None


@router.get("")
def search(
    q: str = Query("", description="模糊搜索关键词"),
    source: str = Query("huggingface", description="来源: huggingface | modelscope"),
    gguf_only: bool = Query(True, description="是否过滤无 GGUF 的模型"),
    limit: int = Query(50, ge=1, le=200),
):
    logger.info(
        f"[/api/search] q={q!r}, source={source!r}, "
        f"gguf_only={gguf_only}, limit={limit}"
    )

    backend = get_backend(source)
    if backend is None:
        return {"error": "Unsupported source", "total": 0, "results": []}

    try:
        results = backend.search_models(q, gguf_only, limit)
    except Exception as e:
        logger.exception(f"[/api/search] failed: {e}")
        return {"error": str(e), "total": 0, "results": []}

    for item in results:
        _fill_size(item)

    logger.info(f"[/api/search] returning {len(results)} results")
    return {"total": len(results), "results": results}


@router.get("/size")
def model_size(
    model_id: str = Query(..., description="模型 ID，如 Qwen/Qwen2.5-7B-Instruct-GGUF"),
    source: str = Query("huggingface", description="来源: huggingface | modelscope"),
):
    """
    查询单个模型的 GGUF 文件总大小。

    搜索接口拿不到体积时（ModelScope 不返回、HF 缺 gguf.total），
    前端渲染完列表后按需调用本接口补齐。
    """
    logger.info(f"[/api/search/size] model_id={model_id!r}, source={source!r}")

    backend = get_backend(source)
    if backend is None:
        return {"error": "Unsupported source", "model_id": model_id, "size_known": False}

    try:
        info = backend.get_model_size(model_id)
    except Exception as e:
        logger.warning(f"[/api/search/size] failed for {model_id}: {e}")
        return {
            "model_id": model_id,
            "source": source,
            "gguf_count": 0,
            "total_size": None,
            "total_size_human": "",
            "size_known": False,
            "error": str(e),
        }

    total = info.get("total_size")
    return {
        "model_id": model_id,
        "source": source,
        "gguf_count": info.get("gguf_count", 0),
        "total_size": total,
        "total_size_human": human_size(total),
        "size_known": total is not None,
        "error": "",
    }


@router.get("/sizes")
def batch_model_sizes(
    model_id: list[str] = Query(..., description="可重复传多个 model_id"),
    source: str = Query("huggingface"),
):
    """
    批量查询多个模型的体积（前端一次性补齐整页时用）。
    单个失败不影响其他条目。
    """
    logger.info(f"[/api/search/sizes] {len(model_id)} models, source={source!r}")

    backend = get_backend(source)
    if backend is None:
        return {"error": "Unsupported source", "sizes": []}

    def one(mid: str) -> dict:
        try:
            info = backend.get_model_size(mid)
            total = info.get("total_size")
            return {
                "model_id": mid,
                "gguf_count": info.get("gguf_count", 0),
                "total_size": total,
                "total_size_human": human_size(total),
                "size_known": total is not None,
                "error": "",
            }
        except Exception as e:
            logger.debug(f"[/api/search/sizes] {mid} failed: {e}")
            return {
                "model_id": mid,
                "gguf_count": 0,
                "total_size": None,
                "total_size_human": "",
                "size_known": False,
                "error": str(e),
            }

    try:
        with ThreadPoolExecutor(max_workers=_SIZE_WORKERS) as pool:
            sizes = list(pool.map(one, model_id))
    except Exception as e:
        logger.exception(f"[/api/search/sizes] failed: {e}")
        return {"error": str(e), "sizes": []}

    known = sum(1 for s in sizes if s["size_known"])
    logger.info(f"[/api/search/sizes] {known}/{len(sizes)} known")
    return {"total": len(sizes), "sizes": sizes}
