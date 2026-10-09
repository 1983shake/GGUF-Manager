# app/main.py
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings, REGISTRY_FILENAME
from app.logger import setup_logging, get_logger, get_timezone_name
from app.routers import search, download, manage, logs, tasks, config, llamacpp
from app.services.updater import scheduler

# 初始化日志（含内存缓冲区）
setup_logging(settings.log_level, settings.log_buffer_size, settings.timezone)
logger = get_logger("app.main")

# ⭐ 版本号统一来自 app/__init__.py，Web 页脚也读这里
app = FastAPI(title="GGUF Model Manager", version=__version__)

app.include_router(search.router)
app.include_router(download.router)
app.include_router(manage.router)
app.include_router(logs.router)
app.include_router(tasks.router)      # 下载任务状态 / 取消
app.include_router(config.router)     # 运行时配置（弹窗时长、llama.cpp、更新检测）
app.include_router(llamacpp.router)   # llama.cpp 重载

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def on_startup():
    logger.info("=" * 62)
    logger.info(f"GGUF Model Manager v{__version__} is starting")
    logger.info(f"  MODEL_DIR        = {settings.MODEL_DIR}")
    logger.info(f"  HF_ENDPOINT      = {settings.hf_endpoint or '(default)'}")
    logger.info(f"  HF_TOKEN         = {'set' if settings.hf_token else '(not set)'}")
    logger.info(f"  SEARCH_LIMIT     = {settings.search_limit}")
    logger.info(f"  LOG_LEVEL        = {settings.log_level}")
    logger.info(f"  LOG_BUFFER_SIZE  = {settings.log_buffer_size}")
    logger.info(f"  TOAST_DURATION   = {settings.toast_duration}s")
    logger.info(f"  HF TIMEOUT       = {settings.hf_connect_timeout}s / {settings.hf_read_timeout}s "
                f"(retries={settings.hf_max_retries})")
    logger.info(f"  LLAMACPP         = enabled={settings.llamacpp_enabled} "
                f"url={settings.llamacpp_url or '-'} mode={settings.llamacpp_reload_mode} "
                f"reload_on_download={settings.llamacpp_reload_on_download}")
    logger.info(f"  UPDATE CHECK     = enabled={settings.update_check_enabled} "
                f"every {settings.update_check_interval_minutes} min")
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


@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api/version")
async def version():
    """供页脚等处获取版本信息。"""
    return {
        "version": __version__,
        "name": "GGUF-Manager",
        "repo": "https://github.com/1983shake/GGUF-Manager",
    }
