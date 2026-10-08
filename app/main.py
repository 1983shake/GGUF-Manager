# app/main.py
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import search, download, manage

app = FastAPI(title="GGUF Model Manager", version="1.0.0")

# 注册 API 路由
app.include_router(search.router)
app.include_router(download.router)
app.include_router(manage.router)

# 静态文件目录
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api/config")
async def get_config():
    return {
        "model_dir": str(settings.MODEL_DIR),
        "hf_endpoint": settings.HF_ENDPOINT or "https://huggingface.co",
    }
