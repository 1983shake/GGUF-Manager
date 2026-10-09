# app/routers/download.py
"""
下载路由。

改动：
- 之前用 FastAPI BackgroundTasks 启动 service.download_model，
  任务无状态、无进度、无法取消。现在交给 DownloadManager，
  POST /api/download 立即返回 task_id，前端据此跟踪进度。
- GET /api/download/files 供"查看"弹窗列出仓库里的 GGUF 文件。
"""

from fastapi import APIRouter, BackgroundTasks, Query

from app.config import settings
from app.logger import get_logger
from app.models import DownloadRequest, human_size
from app.services import huggingface, modelscope
from app.services.downloader import manager

logger = get_logger(__name__)
router = APIRouter(prefix="/api/download", tags=["download"])


@router.get("/files")
def list_gguf_files(
    model_id: str = Query(...),
    source: str = Query("huggingface"),
):
    logger.info(f"[/api/download/files] model_id={model_id!r}, source={source!r}")
    try:
        if source == "huggingface":
            files = huggingface.get_gguf_files(model_id)
        elif source == "modelscope":
            files = modelscope.get_gguf_files(model_id)
        else:
            return {"error": "Unsupported source", "files": []}

        for f in files:
            f["size_human"] = human_size(f.get("size"))

        total = sum(f["size"] for f in files if f.get("size"))
        logger.info(f"[/api/download/files] found {len(files)} files")
        return {
            "files": files,
            "total_size": total,
            "total_size_human": human_size(total),
        }
    except Exception as e:
        logger.exception(f"[/api/download/files] failed: {e}")
        # ⭐ 返回真实错误，而不是伪装成"没有文件"
        return {"error": str(e), "files": []}


@router.post("")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    logger.info(
        f"[/api/download] model_id={req.model_id!r}, filename={req.filename!r}, "
        f"source={req.source!r}, target_subdir={req.target_subdir!r}"
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
