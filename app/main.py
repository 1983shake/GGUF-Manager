"""
应用入口。

启动顺序：
  1. 加载配置 → 2. 初始化日志 → 3. 注册路由 → 4. 挂载静态文件 → 5. 启动更新调度器

"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings, REGISTRY_FILENAME
from app.logger import (
    setup_logging,
    get_logger,
    get_timezone_name,
    set_timezone,
)
from app.routers import models, downloads, sources, system
from app.sources.updater import scheduler
from app.events import emit_scheduler_tick

logger = get_logger("app.main")

app = FastAPI(title="GGUF Model Manager", version=__version__)

# 路由注册：按职责分组
app.include_router(models.router)  # /api/models
app.include_router(downloads.router)  # /api/download, /api/tasks
app.include_router(sources.router)  # /api/search
app.include_router(system.router)  # /api/config, /api/logs, /api/llamacpp, /api/events

# 静态文件（前端）
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def on_startup() -> None:
    # 初始化日志（含内存缓冲区）
    # ⭐ 时区优先读环境变量 LOG_TZ（Dockerfile/compose 中设置），
    #    若 Web 面板已写过 timezone 字段到 settings.json，则用面板值。
    _tz = settings.get("timezone", "") or os.getenv("LOG_TZ", "")
    if _tz:
        set_timezone(_tz)
    setup_logging(settings.log_level, settings.log_buffer_size, _tz)
    logger.info("=" * 62)
    logger.info(f"GGUF Model Manager v{__version__} is starting")
    logger.info(f"  MODEL_DIR        = {settings.MODEL_DIR}")
    logger.info(f"  HF_ENDPOINT      = {settings.hf_endpoint or '(default)'}")
    logger.info(f"  HF_TOKEN         = {'set' if settings.hf_token else '(not set)'}")
    logger.info(f"  SEARCH_LIMIT     = {settings.search_limit}")
    logger.info(f"  LOG_LEVEL        = {settings.log_level}")
    logger.info(f"  LOG_BUFFER_SIZE  = {settings.log_buffer_size}")
    logger.info(f"  TOAST_DURATION   = {settings.task_panel_autohide}s")
    logger.info(f"  HF TIMEOUT       = {settings.hf_connect_timeout}s / {settings.hf_read_timeout}s " f"(retries={settings.hf_max_retries})")
    logger.info(
        f"  LLAMACPP         = enabled={settings.llamacpp_enabled} "
        f"url={settings.llamacpp_url or '-'} mode={settings.llamacpp_reload_mode} "
        f"reload_on_download={settings.llamacpp_reload_on_download}"
    )
    logger.info(f"  UPDATE CHECK     = enabled={settings.update_check_enabled} " f"every {settings.update_check_interval_minutes} min")
    logger.info(f"  TIMEZONE         = {get_timezone_name()}")
    logger.info("=" * 62)

    # ⭐ 配置目录独立于模型目录，可单独挂 volume 持久化
    cfg_dir = settings._store_dir
    logger.info(f"  CONFIG_DIR       = {cfg_dir}")
    if getattr(settings, "created_config_file", False):
        logger.info(f"配置文件已自动生成: {settings._store_file}（可直接编辑）")
    else:
        logger.info(f"配置文件: {settings._store_file}")
    if cfg_dir != Path(settings.MODEL_DIR) / ".gguf-manager":
        logger.info("配置与模型目录分离，建议把 CONFIG_DIR 单独挂载以持久化")

    registry_file = cfg_dir / REGISTRY_FILENAME
    logger.info(f"  来源注册表        = {registry_file}")

    # 启动定期更新检测（开关关闭时内部直接返回）
    scheduler.start()
    emit_scheduler_tick(scheduler.status())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api/version")
async def version():
    """供页脚等处获取版本信息。"""
    return {
        "version": __version__,
        "name": "GGUF-Manager",
        "repo": "https://github.com/1983shake/GGUF-Manager",
    }
