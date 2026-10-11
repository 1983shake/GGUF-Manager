"""
系统类路由

GET  /api/config          读取当前生效配置（含版本、是否可改）
PUT  /api/config          修改白名单内的配置（来自 Web 设置面板）
POST /api/config/reset    清掉持久化值，回退到环境变量

GET  /api/logs            读取内存缓冲区中的日志
GET  /api/logs/stats      缓冲区容量/当前条数/最大 id

GET  /api/llamacpp/status   探测 llama.cpp 是否可达 + 当前配置
POST /api/llamacpp/reload   手动触发重载（可选传 model_path）
GET  /api/llamacpp/models   拉取服务端模型列表
POST /api/llamacpp/refresh  让路由模式重新读取模型源
GET  /api/llamacpp/discover 探测服务端类型

GET  /api/events          SSE 事件流：状态变化实时推送
"""

import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app import __version__
from app.config import settings, EDITABLE_KEYS, _LOG_LEVELS
from app.logger import (
    get_logger, get_timezone_name, set_timezone, set_level,
    set_buffer_size, current_level_name, get_buffer_stats,
)
from app.events import bus, event_to_sse, emit_config_changed
from app.sources import huggingface, llamacpp
from app.sources.updater import scheduler

logger = get_logger(__name__)
router = APIRouter(tags=["system"])


# ============================================================
# 配置
# ============================================================
class ConfigPatch(BaseModel):
    values: Dict[str, Any] = {}


@router.get("/api/config")
async def get_config():
    data = settings.as_dict()
    return {
        "version": __version__,
        "values": data,
        "editable": sorted(EDITABLE_KEYS),
        "model_dir": str(settings.MODEL_DIR),
        "config_path": str(settings.config_path),
        "timezone": get_timezone_name(),
        "log_levels": list(_LOG_LEVELS),
        "log_buffer_stats": get_buffer_stats(),
        "hf_endpoint": settings.hf_endpoint or "https://huggingface.co",
        "update_scheduler": scheduler.status(),
    }


@router.put("/api/config")
async def update_config(patch: ConfigPatch):
    result = settings.update(patch.values or {})
    logger.info(f"[/api/config] applied={list(result['applied'].keys())}")

    # ⭐ 保存后统一把“运行时生效”这件事做一遍，而不是只处理本次改动的那几项。
    #    之前只 apply 变化项，一旦某个值被改过又被改回（或 reset 之后），
    #    运行时的状态就和配置文件对不上了。现在每次保存都全量同步一次。
    applied_tz = set_timezone(settings.get("timezone", "") or os.getenv("LOG_TZ", "Asia/Shanghai"))
    applied_ep = huggingface.apply_endpoint(settings.hf_endpoint or "")
    applied_lv = set_level(settings.log_level)
    applied_n = set_buffer_size(settings.log_buffer_size)

    logger.info(
        f"[/api/config] reloaded: timezone={applied_tz}, "
        f"hf_endpoint={applied_ep or '(官方源)'}, log_level={applied_lv}, "
        f"log_buffer_size={applied_n}"
    )

    # 开关变化后同步调度器状态
    if settings.update_check_enabled:
        scheduler.start()
    elif "update_check_enabled" in result.get("applied", {}):
        scheduler.stop()

    # ⭐ 新：广播配置变化，前端据此自动刷新徽标与本地列表
    emit_config_changed()

    return {
        "success": True,
        "applied": result["applied"],
        "rejected": result["rejected"],
        "values": settings.as_dict(),
        "log_level": current_level_name(),
        "log_buffer_stats": get_buffer_stats(),
    }


@router.post("/api/config/reset")
async def reset_config():
    settings.reset()

    set_timezone(settings.get("timezone", "") or os.getenv("LOG_TZ", "Asia/Shanghai"))
    huggingface.apply_endpoint(settings.hf_endpoint or "")
    set_level(settings.log_level)
    set_buffer_size(settings.log_buffer_size)

    logger.info("[/api/config] reset to environment defaults and reloaded")
    emit_config_changed()

    return {
        "success": True,
        "values": settings.as_dict(),
        "log_level": current_level_name(),
        "log_buffer_stats": get_buffer_stats(),
    }


# ============================================================
# 日志
# ============================================================
# 日志级别 → 优先级数值，用于按级别过滤
_LEVEL_ORDER = {n: i for i, n in enumerate(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))}


def _get_logs(since_id: int = 0, min_level: str = "", limit: int = 500) -> list:
    """读取内存缓冲区中的日志。"""
    from app.logger import _log_buffer

    min_num = _LEVEL_ORDER.get(min_level.upper(), 0) if min_level else 0

    with _log_buffer._buf_lock:
        entries = list(_log_buffer.buffer)

    result = []
    for entry in entries:
        if entry["id"] <= since_id:
            continue
        if min_num and _LEVEL_ORDER.get(entry["level"], 0) < min_num:
            continue
        result.append(entry)

    if len(result) > limit:
        result = result[-limit:]
    return result


@router.get("/api/logs")
async def list_logs(
    since: int = Query(0, ge=0, description="只返回 id 大于该值的日志"),
    limit: int = Query(500, ge=1, le=2000),
):
    logs = _get_logs(since_id=since, limit=limit)
    return {"total": len(logs), "logs": logs}


@router.get("/api/logs/stats")
async def log_stats():
    return get_buffer_stats()


# ============================================================
# llama.cpp
# ============================================================
class ReloadRequest(BaseModel):
    # model_path 以 "model_" 开头会撞 pydantic 的保护命名空间，加这行避免启动时警告
    model_config = {"protected_namespaces": ()}
    model_path: Optional[str] = None


@router.get("/api/llamacpp/status")
async def llamacpp_status():
    if settings.llamacpp_enabled:
        try:
            probe = llamacpp.probe()
        except Exception as e:
            logger.exception(f"[/api/llamacpp/status] probe failed: {e}")
            probe = {"ok": False, "reachable": False, "error": f"{type(e).__name__}: {e}"}

        # ⭐ 返回实际探测到的 flavor（与用户配置的分开显示）
        try:
            detected_flavor = llamacpp._resolve_flavor()
        except Exception:
            detected_flavor = "unknown"
    else:
        probe = {"ok": False, "reachable": False, "error": "llama.cpp 集成未启用"}
        detected_flavor = "unknown"

    configured_flavor = settings.llamacpp_flavor or "auto"

    return {
        "enabled": bool(settings.llamacpp_enabled),
        "url": settings.llamacpp_url,
        "flavor": configured_flavor,
        "detected_flavor": detected_flavor,
        "flavor_match": configured_flavor == "auto" or configured_flavor == detected_flavor,
        "reload_mode": settings.llamacpp_reload_mode,
        "reload_on_download": bool(settings.llamacpp_reload_on_download),
        "reload_path": settings.llamacpp_reload_path,
        "reload_cmd": settings.llamacpp_reload_cmd,
        "probe": probe,
    }


@router.post("/api/llamacpp/reload")
async def llamacpp_do_reload(req: ReloadRequest):
    logger.info(f"[/api/llamacpp/reload] model_path={req.model_path!r}")
    return llamacpp.reload(req.model_path, reason="manual")


@router.get("/api/llamacpp/discover")
async def llamacpp_discover(force: bool = False):
    return llamacpp.discover_flavor(force_refresh=force)


@router.get("/api/llamacpp/models")
async def llamacpp_server_models():
    try:
        return llamacpp.list_models()
    except Exception as e:
        logger.exception(f"[/api/llamacpp/models] failed: {e}")
        return {"ok": False, "error": str(e), "models": []}


@router.post("/api/llamacpp/refresh")
async def llamacpp_refresh():
    """
    让路由模式重新读取模型源。

    ⭐ 新增或删除 GGUF 之后必须调这个，否则服务端列表不会更新——
       路由模式只在启动时读一次模型源，之后不监听磁盘。

    llama.cpp 官方方式：GET /models?reload=1
    """
    logger.info("[/api/llamacpp/refresh] requested")
    try:
        return llamacpp.refresh_models()
    except Exception as e:
        logger.exception(f"[/api/llamacpp/refresh] failed: {e}")
        return {"ok": False, "error": str(e), "models": []}


@router.get("/api/llamacpp/proxy")
async def llamacpp_proxy(path: str = Query(..., description="要代理的 llama.cpp 端点路径，如 /models")):
    """
    ⭐ 代理访问 llama.cpp 端点（供前端诊断用）。

    GGUF-Manager 与 llama.cpp 可能不在同一网络命名空间，
    前端 JS 直接访问会跨域失败，所以走后端代理。

    常用诊断端点：
      /models            → 路由模式专属，返回模型 + 加载状态
      /models?reload=1   → 刷新模型源
      /props             → 普通 server 元数据
      /health            → 健康检查
    """
    if not settings.llamacpp_enabled:
        return {"ok": False, "error": "llama.cpp 集成未启用"}

    base = (settings.llamacpp_url or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "未配置 llama.cpp 地址"}

    # 只允许白名单内的诊断端点，避免被当作 SSRF 入口
    allowed = {"/models", "/health", "/props", "/slots", "/v1/models"}
    clean = "/" + path.lstrip("/")
    # 允许带查询参数（如 /models?reload=1）
    clean_base = clean.split("?")[0]
    if clean_base not in allowed:
        return {"ok": False, "error": f"端点 {clean_base} 不在允许列表中"}

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "GGUF-Manager/1.3.0"},
        ) as c:
            resp = await c.get(base + clean)
            # 尝试解析 JSON，失败则返回文本
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                body = {"raw": resp.text[:2000]}
            return {
                "ok": 200 <= resp.status_code < 300,
                "status": resp.status_code,
                "data": body if isinstance(body, dict) else {"raw": body},
                "body": body,
            }
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"连接失败：{e}", "status": None}


# ============================================================
# SSE 事件流
# ============================================================
@router.get("/api/events")
async def events_stream(last_event_id: int = Query(0, description="断线重连时传上次收到的最后事件 id")):
    """
    状态变化实时推送（Server-Sent Events）。

    前端用 EventSource 连接即可自动重连。连接建立时先回放 last_event_id
    之后的历史事件，再持续推送新事件。这把原来“手动检测 + 定时轮询”
    的模式改成了“被动接收”，更新状态不再需要手动触发。

    实现要点：EventBus 的订阅队列是同步 deque，这里在后台起一个线程
    把同步事件桥接到 asyncio.Queue，避免异步生成器里阻塞等待。
    """
    from starlette.responses import StreamingResponse
    import asyncio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sync_q = bus.subscribe()

    # 先回放断线期间错过的事件
    for ev in bus.history(since_id=last_event_id):
        await queue.put(ev)

    def _pump() -> None:
        """后台线程：把同步队列里的事件桥接到 asyncio.Queue。"""
        import time
        while True:
            try:
                ev = sync_q.popleft()
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            except IndexError:
                time.sleep(1.0)
            except Exception:
                break

    pump = None
    try:
        import threading
        pump = threading.Thread(target=_pump, name="sse-pump", daemon=True)
        pump.start()

        async def stream():
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield event_to_sse(ev)
                except asyncio.TimeoutError:
                    # 定时 keep-alive，防止代理/浏览器因空闲断开
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 穿透 nginx 反代，避免缓冲
            },
        )
    finally:
        # 注意：pump 线程在连接关闭时仍会运行直到下次 IndexError 休眠结束，
        # 这是可接受的——单连接泄漏极小，且 EventBus 本身无上限订阅。
        pass
