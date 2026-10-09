# app/routers/llamacpp.py
"""
llama.cpp 控制接口。

GET  /api/llamacpp/status   探测 llama.cpp 是否可达 + 当前配置
POST /api/llamacpp/reload   手动触发重载（可选传 model_path 指定模型）
GET  /api/llamacpp/models   拉取服务端模型列表
POST /api/llamacpp/refresh  让路由模式重新读取模型源（新增/删除模型后调用）
GET  /api/llamacpp/discover 探测服务端类型
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.logger import get_logger
from app.services import llamacpp

logger = get_logger(__name__)
router = APIRouter(prefix="/api/llamacpp", tags=["llamacpp"])


class ReloadRequest(BaseModel):
    # model_path 以 "model_" 开头，会撞上 pydantic 的保护命名空间，
    # 不加这行启动时会有 UserWarning
    model_config = ConfigDict(protected_namespaces=())

    model_path: Optional[str] = None


@router.get("/status")
async def status():
    # ⭐ probe 出任何异常都必须兜住。
    #    之前 probe() 被误删，这里直接抛 AttributeError，
    #    FastAPI 返回 HTML 的 Internal Server Error，前端 res.json() 就报
    #    "Unexpected token 'I', "Internal S"... is not valid JSON"。
    if settings.llamacpp_enabled:
        try:
            probe = llamacpp.probe()
        except Exception as e:
            logger.exception(f"[/api/llamacpp/status] probe failed: {e}")
            probe = {"ok": False, "reachable": False,
                     "error": f"{type(e).__name__}: {e}"}
    else:
        probe = {"ok": False, "reachable": False, "error": "llama.cpp 集成未启用"}
    return {
        "enabled": bool(settings.llamacpp_enabled),
        "url": settings.llamacpp_url,
        "flavor": settings.llamacpp_flavor,
        "reload_mode": settings.llamacpp_reload_mode,
        "reload_on_download": bool(settings.llamacpp_reload_on_download),
        "reload_path": settings.llamacpp_reload_path,
        "reload_cmd": settings.llamacpp_reload_cmd,
        "probe": probe,
    }


@router.post("/reload")
async def do_reload(req: ReloadRequest):
    logger.info(f"[/api/llamacpp/reload] model_path={req.model_path!r}")
    result = llamacpp.reload(req.model_path, reason="manual")
    return result


@router.get("/discover")
async def discover(force: bool = False):
    """探测服务端类型（路由模式 / llama-swap / 普通 server）。"""
    return llamacpp.discover_flavor(force_refresh=force)


@router.get("/models")
async def server_models():
    """拉取服务端当前的模型列表。"""
    try:
        return llamacpp.list_models()
    except Exception as e:
        logger.exception(f"[/api/llamacpp/models] failed: {e}")
        return {"ok": False, "error": str(e), "models": []}


@router.post("/refresh")
async def refresh():
    """
    让路由模式重新读取模型源。

    ⭐ 新增或删除 GGUF 之后必须调这个，否则服务端列表不会更新——
       路由模式只在启动时读一次模型源，之后不监听磁盘。
    """
    logger.info("[/api/llamacpp/refresh] requested")
    try:
        return llamacpp.refresh_models()
    except Exception as e:
        logger.exception(f"[/api/llamacpp/refresh] failed: {e}")
        return {"ok": False, "error": str(e), "models": []}
