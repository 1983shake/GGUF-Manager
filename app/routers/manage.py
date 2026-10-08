# app/routers/manage.py
from fastapi import APIRouter, Query

from app.config import settings
from app.services.local_scanner import scan_models, delete_model

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def list_local_models(path: str = Query("", description="自定义扫描路径")):
    """列出本地已下载的 GGUF 模型"""
    scan_dir = path if path else str(settings.MODEL_DIR)
    models = scan_models(scan_dir)
    return {"scan_dir": scan_dir, "total": len(models), "models": models}


@router.delete("")
async def remove_model(file_path: str = Query(...)):
    """删除指定模型"""
    try:
        success = delete_model(file_path, str(settings.MODEL_DIR))
        return {"success": success}
    except ValueError as e:
        return {"error": str(e)}
