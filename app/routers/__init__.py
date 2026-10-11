"""路由包：按职责聚合，避免单文件过长。"""

from app.routers import models, downloads, sources, system

__all__ = ["models", "downloads", "sources", "system"]
