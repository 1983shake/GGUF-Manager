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
GET  /api/llamacpp/proxy    白名单端点的诊断代理

GET  /api/events          SSE 事件流：状态变化实时推送
"""

import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app import __version__
from app.config import EDITABLE_KEYS, _LOG_LEVELS, settings
from app.events import bus, emit_config_changed, event_to_sse
from app.logger import (
    current_level_name,
    get_buffer_stats,
    get_logger,
    get_logs,
    get_timezone_name,
    set_buffer_size,
    set_level,
    set_timezone,
)
from app.sources import huggingface, llamacpp
from app.sources.local import scheduler

logger = get_logger(__name__)
router = APIRouter(tags=["system"])


def _apply_runtime_settings() -> Dict[str, Any]:
    """
    把 settings 里的值全量同步到运行时对象。

    ⭐ 每次保存/重置都全量做一遍，而不是只处理“本次改动的那几项”：
       值被改过又改回、或 reset 之后，只 apply 变化项会让运行时与配置文件不一致。
    """
    applied_tz = set_timezone(settings.get("timezone", "") or os.getenv("LOG_TZ", "Asia/Shanghai"))
    applied_ep = huggingface.apply_endpoint(settings.hf_endpoint or "")
    applied_lv = set_level(settings.log_level)
    applied_n = set_buffer_size(settings.log_buffer_size)
    llamacpp.reset_flavor_cache()  # 地址/类型可能刚变，探测结果作废

    logger.info(
        f"[/api/config] reloaded: timezone={applied_tz}, "
        f"hf_endpoint={applied_ep or '(官方源)'}, log_level={applied_lv}, "
        f"log_buffer_size={applied_n}"
    )
    return {"timezone": applied_tz, "hf_endpoint": applied_ep}


# ============================================================
# 配置
# ============================================================
class ConfigPatch(BaseModel):
    values: Dict[str, Any] = {}


@router.get("/api/config")
async def get_config():
    return {
        "version": __version__,
        "values": settings.as_dict(),
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

    _apply_runtime_settings()

    # 开关变化后同步调度器状态
    if settings.update_check_enabled:
        scheduler.start()
    elif "update_check_enabled" in result.get("applied", {}):
        scheduler.stop()

    # 广播配置变化，前端据此自动刷新徽标与本地列表
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
    _apply_runtime_settings()
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
@router.get("/api/logs")
async def list_logs(
    since: int = Query(0, ge=0, description="只返回 id 大于该值的日志"),
    limit: int = Query(500, ge=1, le=2000),
):
    # 直接复用 logger 里的实现，避免两处过滤逻辑各写一份而逐渐漂移
    logs = get_logs(since_id=since, limit=limit)
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
        # 实际探测到的 flavor（与用户配置的分开显示）
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
            headers={"User-Agent": f"GGUF-Manager/{__version__}"},
        ) as c:
            resp = await c.get(base + clean)
            try:
                body = resp.json()
            except ValueError:
                # ⚠️ 这里不要写 `except (ValueError, json.JSONDecodeError)`：
                #    本模块并未 import json，一旦上游返回非 JSON，
                #    求值 except 元组本身就会 NameError，把 500 变成 500+堆栈。
                #    json.JSONDecodeError 本就是 ValueError 的子类，写 ValueError 足够。
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
async def events_stream(
    last_event_id: int = Query(0, description="断线重连时传上次收到的最后事件 id"),
    last_event_id_header: Optional[str] = Header(None, alias="Last-Event-ID"),
):
    """
    状态变化实时推送（Server-Sent Events）。

    前端用 EventSource 连接即可自动重连。断线重连时浏览器会自动带
    `Last-Event-ID` **请求头**（注意不是 query 参数），这里两个来源都读，
    取较大者作为回放起点，补上断线期间错过的事件，再持续推送新事件。
    这把原来“手动检测 + 定时轮询”的模式改成了“被动接收”，更新状态不再需要手动触发。

    ⭐ 首次连接（既没有 header 也没有 query 参数）**不回放历史 backlog**：
    历史事件对“刚打开页面”的客户端是过去时——比如几分钟前一次下载的
    几百条 `task_updated(downloading)` 进度事件，此时原样重放只会制造幽灵
    （前端会据此弹出任务面板、显示一个早已结束的下载中卡片）。页面的现状
    由前端启动时的全量拉取（/api/tasks、/api/config、/api/models）覆盖，
    不需要、也不应该由回放来重建。

    实现要点：EventBus 的 publish 可能来自任意工作线程，
    直接把事件投进 asyncio.Queue 不安全，因此注册一个回调，
    由回调统一走 loop.call_soon_threadsafe 投递；连接结束时摘掉回调。
    """
    import asyncio

    from starlette.responses import StreamingResponse

    resume_from = last_event_id
    if last_event_id_header:
        try:
            resume_from = max(resume_from, int(last_event_id_header))
        except (TypeError, ValueError):
            pass  # 头格式异常时忽略，按无回放处理

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # 仅断线重连（知道上次收到哪条）才补回放；首次连接从“现在”开始收
    if resume_from > 0:
        for ev in bus.history(since_id=resume_from):
            queue.put_nowait(ev)

    def _sink(event: Dict[str, Any]) -> None:
        """由发布线程调用；必须在事件循环线程里入队。"""

        def _put() -> None:
            # 客户端卡住时不让内存无限增长，超过上限就丢弃（前端还有轮询兜底）
            if queue.qsize() < 1000:
                queue.put_nowait(event)

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # 事件循环已关闭（服务正在退出）

    bus.add_listener(_sink)

    async def stream():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 定时 keep-alive，防止代理/浏览器因空闲断开
                    yield ": keep-alive\n\n"
                    continue
                yield event_to_sse(ev)
        finally:
            # 客户端断开 / 服务关闭都会走到这里，摘掉回调不留悬挂订阅
            bus.remove_listener(_sink)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 穿透 nginx 反代，避免缓冲
        },
    )
