"""
来源检索 + 下载 + 任务状态路由。

搜索 / 文件列表 / 下载 / 任务属于同一条「找到模型 → 拉起下载 → 看进度」的
用户旅程，收拢在同一个文件里，体积填充逻辑（human_size / size_known）才有
唯一一份实现。

GET  /api/search                 搜索模型（体积能带就带，带不上由前端懒加载补齐）
GET  /api/search/size            查询单个模型的 GGUF 总大小
GET  /api/search/sizes           批量查询多个模型的体积（前端补齐整页时用）

GET  /api/download/files         列出仓库内 GGUF 文件（供“查看”弹窗）
POST /api/download               启动下载，立即返回 task_id

GET    /api/tasks                列出所有任务 + 全局汇总
GET    /api/tasks/{id}           单个任务详情
POST   /api/tasks/{id}/cancel    取消任务（保留 .part 以支持断点续传）
DELETE /api/tasks/{id}           从列表移除记录（不删文件）
POST   /api/tasks/clear          清除所有已结束的任务
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.logger import get_logger
from app.sources import SUPPORTED_SOURCES, get_backend
from app.sources.downloader import manager
from app.utils import human_size

logger = get_logger(__name__)

# 单 APIRouter 无法挂两个前缀，这里统一用完整路径（与 system.py 一致）
router = APIRouter(tags=["downloads"])

_SIZE_WORKERS = 6  # /sizes 并发上限（前端也有并发控制，这里兜底）


class DownloadRequest(BaseModel):
    # model_id 以 "model_" 开头会撞 pydantic 保护命名空间，加这行避免启动时警告
    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    filename: str = ""
    source: str = "huggingface"
    target_subdir: str = ""


# ============================================================
# 搜索（原 routers/sources.py）
# ============================================================
def _fill_size(item: dict) -> None:
    """统一格式化体积字段，缺失时留空（前端据此显示“未知”）。"""
    total = item.get("total_size")
    item["total_size_human"] = human_size(total)
    item["size_known"] = total is not None


@router.get("/api/search")
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
        # 超时/网络类错误把原始信息透出去，前端据此提示“调大读取超时”
        return {"error": str(e) or f"{type(e).__name__}", "total": 0, "results": []}

    for item in results:
        _fill_size(item)

    logger.info(f"[/api/search] returning {len(results)} results")
    return {"total": len(results), "results": results}


@router.get("/api/search/size")
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
            "error": str(e) or f"{type(e).__name__}",
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


@router.get("/api/search/sizes")
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


# ============================================================
# 下载
# ============================================================
@router.get("/api/download/files")
def list_gguf_files(
    model_id: str = Query(..., description="模型 ID"),
    source: str = Query("huggingface", description="来源: huggingface | modelscope"),
):
    logger.info(f"[/api/download/files] model_id={model_id!r}, source={source!r}")
    backend = get_backend(source)
    if backend is None:
        return {"error": "Unsupported source", "files": []}

    try:
        files = backend.get_gguf_files(model_id)
    except Exception as e:
        logger.exception(f"[/api/download/files] failed: {e}")
        # 返回真实错误，而不是伪装成“没有文件”
        return {"error": str(e) or f"{type(e).__name__}", "files": []}

    for f in files:
        f["size_human"] = human_size(f.get("size"))

    total = sum(f["size"] for f in files if f.get("size"))
    logger.info(f"[/api/download/files] found {len(files)} files")
    return {
        "files": files,
        "total_size": total,
        "total_size_human": human_size(total),
    }


def _resolve_target_dir(subdir: str) -> str:
    """
    解析下载目标目录。

    只允许落在 MODEL_DIR 内部的相对子目录 —— 否则 `target_subdir="../../etc"`
    就能把文件写到模型目录之外。不是安全边界（本服务本身就在内网跑），
    但没必要留这个坑。
    """
    if not subdir:
        return str(settings.MODEL_DIR)

    base = Path(settings.MODEL_DIR).resolve()
    target = (base / subdir).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("target_subdir 必须位于模型目录内")

    target.mkdir(parents=True, exist_ok=True)
    return str(target)


@router.post("/api/download")
def start_download(req: DownloadRequest):
    logger.info(
        f"[/api/download] model_id={req.model_id!r}, filename={req.filename!r}, "
        f"source={req.source!r}, target_subdir={req.target_subdir!r}"
    )

    if not req.filename:
        return {"error": "请先选择一个 GGUF 文件"}
    if req.source not in SUPPORTED_SOURCES:
        return {"error": "Unsupported source"}

    try:
        target_dir = _resolve_target_dir(req.target_subdir)
    except (ValueError, OSError) as e:
        return {"error": str(e)}

    try:
        task = manager.start(
            model_id=req.model_id,
            filename=req.filename,
            source=req.source,
            target_dir=target_dir,
            token=settings.hf_token if req.source == "huggingface" else "",
        )
    except Exception as e:
        logger.exception(f"[/api/download] start failed: {e}")
        return {"error": str(e)}

    logger.info(f"[/api/download] task {task.id} -> {target_dir}")
    return {
        "status": "started",
        "task_id": task.id,
        "model_id": req.model_id,
        "filename": req.filename,
        "target_dir": target_dir,
        # 直接附带首帧状态，前端不用再等下一次轮询
        "task": task.to_dict(),
    }


# ============================================================
# 任务状态
# ============================================================
@router.get("/api/tasks")
def list_tasks():
    tasks = [t.to_dict() for t in manager.list()]
    tasks.sort(key=lambda x: x["started_at"], reverse=True)

    total_speed = manager.total_speed_bps()
    return {
        "total": len(tasks),
        "tasks": tasks,
        "summary": {
            "active": manager.active_count(),
            "total_speed_bps": total_speed,
            "total_speed_human": (human_size(total_speed) + "/s") if total_speed > 0 else "",
        },
    }


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = manager.get(task_id)
    if not task:
        return {"error": "task not found"}
    return task.to_dict()


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    task = manager.get(task_id)
    if not task:
        return {"error": "task not found"}
    ok = manager.cancel(task_id)
    logger.info(f"[/api/tasks/{task_id}/cancel] ok={ok}")
    return {"success": ok, "task": task.to_dict()}


@router.delete("/api/tasks/{task_id}")
def remove_task(task_id: str):
    ok = manager.remove(task_id)
    logger.info(f"[/api/tasks/{task_id}] removed={ok}")
    return {"success": ok}


@router.post("/api/tasks/clear")
def clear_finished():
    n = manager.clear_finished()
    return {"success": True, "removed": n}
