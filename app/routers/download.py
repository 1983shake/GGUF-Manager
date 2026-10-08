# app/routers/download.py
from fastapi import APIRouter, BackgroundTasks, Query

from app.config import settings
from app.models import DownloadRequest
from app.services import huggingface, modelscope

router = APIRouter(prefix="/api/download", tags=["download"])


@router.get("/files")
async def list_gguf_files(
    model_id: str = Query(...),
    source: str = Query("huggingface"),
):
    """列出指定模型仓库中的 GGUF 文件"""
    if source == "huggingface":
        files = huggingface.get_gguf_files(model_id)
    elif source == "modelscope":
        files = modelscope.get_gguf_files(model_id)
    else:
        return {"error": "Unsupported source", "files": []}

    return {"files": files}


@router.post("")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    """启动下载任务"""
    if req.target_subdir:
        target_dir_path = settings.MODEL_DIR / req.target_subdir
        target_dir_path.mkdir(parents=True, exist_ok=True)
        target_dir = str(target_dir_path)
    else:
        target_dir = str(settings.MODEL_DIR)

    if req.source == "huggingface":
        if not req.filename:
            return {"error": "HuggingFace 下载需要指定文件名"}
        background_tasks.add_task(
            huggingface.download_model,
            req.model_id,
            req.filename,
            target_dir,
            settings.HF_TOKEN,
        )
    elif req.source == "modelscope":
        background_tasks.add_task(
            modelscope.download_model,
            req.model_id,
            target_dir,
        )
    else:
        return {"error": "Unsupported source"}

    return {"status": "started", "model_id": req.model_id}
