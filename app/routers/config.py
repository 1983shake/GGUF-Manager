# app/routers/config.py
"""
运行时配置接口。

GET /api/config          读取当前生效配置（含版本、是否可改）
PUT /api/config          修改白名单内的配置（来自 Web 设置面板）
POST /api/config/reset   清掉持久化值，回退到环境变量
"""

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.config import settings, EDITABLE_KEYS, _LOG_LEVELS
from app.logger import (
    get_logger,
    get_timezone_name,
    set_timezone,
    set_level,
    set_buffer_size,
    current_level_name,
    get_buffer_stats,
)
from app.services.huggingface import apply_endpoint
from app.services.updater import scheduler

logger = get_logger(__name__)
router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigPatch(BaseModel):
    values: Dict[str, Any]


@router.get("")
async def get_config():
    data = settings.as_dict()
    return {
        "version": __version__,
        "values": data,
        "editable": sorted(EDITABLE_KEYS),
        "model_dir": str(settings.MODEL_DIR),
        "config_path": str(settings.config_path),
        "timezone": get_timezone_name(),
        # 供设置面板渲染下拉/提示
        "log_levels": list(_LOG_LEVELS),
        "log_buffer_stats": get_buffer_stats(),
        "hf_endpoint": settings.hf_endpoint or "https://huggingface.co",
        "update_scheduler": scheduler.status(),
    }


@router.put("")
async def update_config(patch: ConfigPatch):
    result = settings.update(patch.values or {})
    logger.info(f"[/api/config] applied={list(result['applied'].keys())}")

    applied = result.get("applied") or {}

    # ⭐ 保存后统一把"运行时生效"这件事做一遍，而不是只处理本次改动的那几项。
    #    之前只 apply 变化项，一旦某个值被改过又被改回（或 reset 之后），
    #    运行时的状态就和配置文件对不上了。现在每次保存都全量同步一次。
    applied_tz = set_timezone(settings.timezone or "")
    applied_ep = apply_endpoint(settings.hf_endpoint or "")
    applied_lv = set_level(settings.log_level)
    applied_n = set_buffer_size(settings.log_buffer_size)

    logger.info(
        f"[/api/config] reloaded: timezone={applied_tz}, "
        f"hf_endpoint={applied_ep or '(官方源)'}, log_level={applied_lv}, "
        f"log_buffer_size={applied_n}"
    )

    # 扫描路径变化后清掉前端缓存视角（下次扫描会用新路径）
    if "scan_path" in applied:
        logger.info(f"[/api/config] scan_path -> {applied['scan_path'] or '(MODEL_DIR)'}")

    # 开关变化后同步调度器状态
    if settings.update_check_enabled:
        scheduler.start()
    elif "update_check_enabled" in applied:
        logger.info("[/api/config] update check disabled")

    return {
        "success": True,
        "applied": result["applied"],
        "rejected": result["rejected"],
        "values": settings.as_dict(),
        "log_level": current_level_name(),
        "log_buffer_stats": get_buffer_stats(),
    }


@router.post("/reset")
async def reset_config():
    settings.reset()

    # ⭐ reset 后同样要全量重新生效，否则回退的只是文件，运行时还是旧值
    set_timezone(settings.timezone or "")
    apply_endpoint(settings.hf_endpoint or "")
    set_level(settings.log_level)
    set_buffer_size(settings.log_buffer_size)

    logger.info("[/api/config] reset to environment defaults and reloaded")
    return {
        "success": True,
        "values": settings.as_dict(),
        "log_level": current_level_name(),
        "log_buffer_stats": get_buffer_stats(),
    }
