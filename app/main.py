"""
应用入口。

启动顺序：
  1. 加载配置 → 2. 初始化日志 → 3. 注册路由 → 4. 挂载静态文件 → 5. 启动更新调度器

使用 lifespan 而非 `@app.on_event("startup")`：on_event 在 FastAPI 0.109+ 已标记
deprecated，启动日志里那行 DeprecationWarning 会让“程序是否真的没问题”难以判断；
lifespan 同时提供关机清理点（停掉定期检测线程）。
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import REGISTRY_FILENAME, settings
from app.events import emit_scheduler_tick
from app.logger import get_logger, get_timezone_name, set_timezone, setup_logging
from app.routers import downloads, models, system
from app.sources.local import scheduler

logger = get_logger("app.main")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        scheduler.stop()
        logger.info(f"GGUF Model Manager v{__version__} stopped")


def _startup() -> None:
    """启动钩子：日志 → 配置概览 → 定期检测调度器。"""
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
    logger.info(
        f"  HF TIMEOUT       = {settings.hf_connect_timeout}s / "
        f"{settings.hf_read_timeout}s (retries={settings.hf_max_retries})"
    )
    logger.info(
        f"  LLAMACPP         = enabled={settings.llamacpp_enabled} "
        f"url={settings.llamacpp_url or '-'} mode={settings.llamacpp_reload_mode} "
        f"reload_on_download={settings.llamacpp_reload_on_download}"
    )
    logger.info(
        f"  UPDATE CHECK     = enabled={settings.update_check_enabled} "
        f"every {settings.update_check_interval_minutes} min"
    )
    logger.info(f"  TIMEZONE         = {get_timezone_name()}")
    logger.info("=" * 62)

    # ⭐ 配置目录独立于模型目录，可单独挂 volume 持久化
    cfg_dir = settings._store_dir
    logger.info(f"  CONFIG_DIR       = {cfg_dir}")
    if settings.created_config_file:
        logger.info(f"配置文件已自动生成: {settings._store_file}（可直接编辑）")
    else:
        logger.info(f"配置文件: {settings._store_file}")
    if cfg_dir != Path(settings.MODEL_DIR) / ".gguf-manager":
        logger.info("配置与模型目录分离，建议把 CONFIG_DIR 单独挂载以持久化")

    logger.info(f"  来源注册表        = {cfg_dir / REGISTRY_FILENAME}")

    # 启动定期更新检测（开关关闭时内部直接返回）
    scheduler.start()
    emit_scheduler_tick(scheduler.status())


app = FastAPI(title="GGUF Model Manager", version=__version__, lifespan=lifespan)

# 路由注册：按职责分组
app.include_router(models.router)     # /api/models
app.include_router(downloads.router)  # /api/search, /api/download, /api/tasks
app.include_router(system.router)     # /api/config, /api/logs, /api/llamacpp, /api/events

# 静态文件（前端）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/version")
async def version():
    """供页脚等处获取版本信息。"""
    return {
        "version": __version__,
        "name": "GGUF-Manager",
        "repo": "https://github.com/1983shake/GGUF-Manager",
    }
