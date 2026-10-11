"""
下载路由 + 任务状态路由（v1.3.0 合并自 download.py 与 tasks.py）。

POST /api/download              启动下载，立即返回 task_id
GET  /api/download/files        列出仓库内 GGUF 文件（供“查看”弹窗）

GET    /api/tasks               列出所有任务 + 全局汇总
GET    /api/tasks/{id}          单个任务详情
POST   /api/tasks/{id}/cancel   取消任务（保留 .part 以支持断点续传）
DELETE /api/tasks/{id}          从列表移除记录（不删文件）
POST   /api/tasks/clear         清除所有已结束的任务

"""

from fastapi import APIRouter, Query

from app.config import settings
from app.logger import get_logger
from app.models import DownloadRequest, human_size
from app.sources import get_backend
from app.sources.downloader import manager

logger = get_logger(__name__)

router = APIRouter(tags=["downloads"])


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
        # ⭐ 返回真实错误，而不是伪装成“没有文件”
        return {"error": str(e), "files": []}

    for f in files:
        f["size_human"] = human_size(f.get("size"))

    total = sum(f["size"] for f in files if f.get("size"))
    logger.info(f"[/api/download/files] found {len(files)} files")
    return {
        "files": files,
        "total_size": total,
        "total_size_human": human_size(total),
    }


@router.post("/api/download")
def start_download(req: DownloadRequest):
    logger.info(
        f"[/api/download] model_id={req.model_id!r}, filename={req.filename!r}, " f"source={req.source!r}, target_subdir={req.target_subdir!r}"
    )

    if req.target_subdir:
        target_dir_path = settings.MODEL_DIR / req.target_subdir
        target_dir_path.mkdir(parents=True, exist_ok=True)
        target_dir = str(target_dir_path)
    else:
        target_dir = str(settings.MODEL_DIR)

    if not req.filename:
        return {"error": "请先选择一个 GGUF 文件"}
    if req.source not in ("huggingface", "modelscope"):
        return {"error": "Unsupported source"}

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
