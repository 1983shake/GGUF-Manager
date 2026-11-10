"""
本地模型管理路由。

GET      /api/models              扫描本地模型（附带来源与更新状态）
DELETE   /api/models              删除单个文件
POST     /api/models/batch-delete 批量删除
POST     /api/models/check        检测更新（指定或全部）
GET|POST /api/models/check-one    检测单个模型（供每行“检测”按钮）
POST     /api/models/update       更新模型（重新下载，完成后原子替换）
POST     /api/models/clear-cache  清理 .gguf.part 缓存
GET      /api/models/schedule     定期检测任务状态

排序与过滤在浏览器端完成（见 static/app.js 的 applyLocalView）：
列表本来就要整表扫描一次，前端已有全量数据，再往返一次后端纯属浪费。
"""

from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings
from app.logger import get_logger
from app.sources import llamacpp
from app.sources.downloader import manager
from app.sources.local import (
    DELETE_WAIT_AFTER_UNLOAD,
    DeleteBlockedError,
    check_all,
    check_one,
    clear_cache,
    delete_model,
    registry,
    resolve_update_source,
    scan_models,
    scheduler,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])


class PathsRequest(BaseModel):
    file_paths: List[str] = []
    rel_paths: List[str] = []


# ------------------------------------------------------------
# 扫描
# ------------------------------------------------------------
@router.get("")
def list_local_models(path: str = Query("", description="自定义扫描路径")):
    scan_dir = path if path else str(settings.MODEL_DIR)
    logger.info(f"[/api/models] scan_dir={scan_dir!r}")
    models = scan_models(scan_dir)

    incomplete = [m for m in models if m.get("is_part")]
    invalid = [m for m in models if m.get("validity") in ("corrupt", "unreadable")]
    cache_bytes = sum(m.get("size") or 0 for m in incomplete)

    logger.info(
        f"[/api/models] found {len(models)} files "
        f"(incomplete={len(incomplete)}, invalid={len(invalid)})"
    )
    return {
        "scan_dir": scan_dir,
        "total": len(models),
        "models": models,
        "incomplete_count": len(incomplete),
        "invalid_count": len(invalid),
        "cache_bytes": cache_bytes,
    }


# ------------------------------------------------------------
# llama.cpp 联动：删前卸载 / 删后重扫
# ------------------------------------------------------------
def _try_unload_before_delete(paths: List[str]) -> Dict:
    """
    删除前先尝试解除加载，释放 llama.cpp 的 mmap 文件句柄。

    ⭐ best-effort：未启用、非路由形态、服务不可达、卸载报错都只记录不中断，
       删除照常进行（local.delete_model 自带退避重试，真删不掉会给中文提示）。
    ⭐ 批量删除一次传入全部路径，服务端模型列表只拉一次，请求数不随文件数放大。
    """
    if not settings.llamacpp_enabled or not (settings.llamacpp_url or "").strip():
        return {"skipped": "llama.cpp 未启用"}
    try:
        res = llamacpp.unload_for_delete(paths)
        logger.info(f"[/api/models] pre-delete unload: {res}")
        return res
    except Exception as e:
        logger.warning(f"[/api/models] pre-delete unload failed: {e}")
        return {"ok": False, "error": str(e)}


def _sync_llamacpp_after_delete() -> Optional[Dict]:
    """
    删除成功后只让路由模式重扫磁盘，不再加载模型。

    ⭐ 旧实现调 reload() → 内部 POST /models/load，会把刚卸载的模型或另一个模型
       重新拉起，既可能重新占用文件句柄，也不符合删除语义。
    ⭐ 路由模式不监听磁盘，不重扫的话服务端列表里会一直留着已删模型。
    """
    if not settings.llamacpp_enabled or not (settings.llamacpp_url or "").strip():
        return None
    try:
        return llamacpp.refresh_after_delete()
    except Exception as e:
        logger.warning(f"[/api/models] post-delete refresh failed: {e}")
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------
# 删除
# ------------------------------------------------------------
_GENERIC_HINT = "若仍失败，请检查目录权限、文件只读属性，或稍后重试。"


def _active_download_for(file_path: str):
    """找到正在写这个目标（.gguf 或同名 .part）的下载任务。"""
    try:
        target = Path(file_path).resolve()
    except OSError:
        return None
    for task in manager.list():
        if task.status not in ("pending", "downloading"):
            continue
        try:
            held = {
                Path(str(task.final_path)).resolve(),
                Path(str(task.part_path)).resolve(),
            }
        except OSError:
            continue
        if target in held:
            return task
    return None


def _downloading_failure(file_path: str) -> Optional[Dict]:
    """前置检查：下载线程持有文件句柄时，Windows 上删除必然失败，直接说清楚。"""
    task = _active_download_for(file_path)
    if not task:
        return None
    return {
        "code": "downloading",
        "error": "该文件正在下载或排队中，无法删除",
        "hint": f"请先在右下角任务面板取消任务（{task.filename}），等任务结束后重试。",
    }


def _existing_paths(paths: List[str]) -> List[str]:
    """只对确实存在的文件做卸载，避免误伤服务端同名但并未被删的模型。"""
    existing = []
    for p in paths:
        try:
            if Path(p).resolve().exists():
                existing.append(p)
        except OSError:
            continue
    return existing


def _blocked_payload(e: DeleteBlockedError, file_path: str, unload: Optional[Dict] = None) -> Dict:
    """把 WinError 5 翻译成用户看得懂、能动手的中文提示。"""
    payload = e.as_dict()
    task = _active_download_for(file_path)
    if task:
        payload.update(_downloading_failure(file_path) or {})
    elif settings.llamacpp_enabled:
        if unload and unload.get("unloaded"):
            prefix = (
                f"已自动卸载 llama.cpp 中加载的该模型，并轮询等待 "
                f"{DELETE_WAIT_AFTER_UNLOAD:.0f} 秒，文件仍被占用；"
            )
            suffix = (
                "常见原因：另有 llama-server / 其它程序占用该文件、卸载后模型被自动重新加载、"
                "或 Docker 文件共享层仍持有句柄。请停止对应进程（或重启其容器）后重试。"
            )
        elif unload and unload.get("skipped"):
            prefix = f"删除前自动卸载未完成（{unload['skipped']}）；"
            suffix = "请停止 llama-server（或重启其容器）后重试。"
        else:
            prefix = "已自动尝试解除 llama.cpp 加载，但未成功释放文件句柄；"
            suffix = "请停止 llama-server（或重启其容器）后重试。"
        payload["hint"] = prefix + suffix + payload["hint"]
    if unload:
        payload["unload"] = unload
    return payload


@router.delete("")
def remove_model(file_path: str = Query(...)):
    logger.info(f"[/api/models] delete file_path={file_path!r}")
    busy = _downloading_failure(file_path)
    if busy:
        return {"success": False, "path": file_path, **busy}

    # ⭐ 顺序：解除加载 → 删除 → 重扫。卸载失败不阻断删除，由重试兜底。
    unload = _try_unload_before_delete(_existing_paths([file_path]))
    # unload 返回 200 后 mmap 释放是异步的：给删除一个轮询窗口，
    # 一直重试到「文件真的可删」或窗口耗尽，而不是固定 1.8s 就放弃。
    wait = DELETE_WAIT_AFTER_UNLOAD if unload.get("unloaded") else 0.0
    try:
        success = delete_model(file_path, str(settings.MODEL_DIR), wait_seconds=wait)
    except ValueError as e:
        logger.warning(f"[/api/models] delete rejected: {e}")
        return {
            "success": False,
            "code": "invalid_path",
            "error": f"不允许删除该文件：{e}",
            "hint": "只允许删除模型目录内的 .gguf / .gguf.part 文件。",
        }
    except DeleteBlockedError as e:
        # 重试后仍被占用：不再抛原始 WinError 5，给出可操作提示
        logger.warning(f"[/api/models] delete blocked: {e.detail}")
        return {"success": False, "path": file_path, **_blocked_payload(e, file_path, unload)}
    except Exception as e:
        logger.exception(f"[/api/models] delete failed: {e}")
        return {
            "success": False,
            "code": "delete_failed",
            "error": f"删除失败：{e}",
            "detail": str(e),
            "hint": f"请刷新列表后重试；{_GENERIC_HINT}",
        }
    if not success:
        return {
            "success": False,
            "code": "not_found",
            "error": "文件不存在或不是 .gguf / .gguf.part",
            "hint": "列表可能已过期，请刷新后重试。",
            "unload": unload,
        }
    logger.info("[/api/models] delete success")
    return {
        "success": True,
        "unload": unload,
        "llamacpp": _sync_llamacpp_after_delete(),
    }


@router.post("/batch-delete")
def batch_remove(req: PathsRequest):
    """批量删除。单个失败不影响其他，逐条返回中文原因与处理建议。"""
    paths = req.file_paths or []
    logger.info(f"[/api/models/batch-delete] {len(paths)} files")

    # ⭐ 整批只解除加载一次：服务端列表只拉一次，命中多个模型时逐个 unload。
    unload = _try_unload_before_delete(_existing_paths(paths))
    wait = DELETE_WAIT_AFTER_UNLOAD if unload.get("unloaded") else 0.0
    deleted, failed = [], []
    for p in paths:
        busy = _downloading_failure(p)
        if busy:
            failed.append({"path": p, **busy})
            continue
        try:
            if delete_model(p, str(settings.MODEL_DIR), wait_seconds=wait):
                deleted.append(p)
            else:
                failed.append({
                    "path": p,
                    "code": "not_found",
                    "error": "文件不存在或不是 .gguf / .gguf.part",
                    "hint": "列表可能已过期，请刷新后重试。",
                })
        except ValueError as e:
            failed.append({"path": p, "code": "invalid_path", "error": str(e)})
        except DeleteBlockedError as e:
            logger.warning(f"[/api/models/batch-delete] {p} blocked: {e.detail}")
            failed.append({"path": p, **_blocked_payload(e, p, unload)})
        except Exception as e:
            logger.exception(f"[/api/models/batch-delete] {p} failed: {e}")
            failed.append({
                "path": p,
                "code": "delete_failed",
                "error": f"删除失败：{e}",
                "hint": _GENERIC_HINT,
            })

    notify = _sync_llamacpp_after_delete() if deleted else None
    logger.info(f"[/api/models/batch-delete] deleted={len(deleted)} failed={len(failed)}")
    return {
        "success": len(failed) == 0,
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "unload": unload,
        "llamacpp": notify,
    }


# ------------------------------------------------------------
# 缓存清理
# ------------------------------------------------------------
@router.post("/clear-cache")
def clear_part_cache():
    """
    清理所有下载中断留下的 .gguf.part 缓存文件。

    取消/失败的下载会在磁盘上留下大量残留，界面上也可以逐条删除，
    这个接口一次性清干净。
    """
    logger.info("[/api/models/clear-cache] requested")
    try:
        result = clear_cache(str(settings.MODEL_DIR))
        for item in result.get("failed", []):
            item.setdefault("hint", _GENERIC_HINT)
        result["success"] = not result.get("failed")
        logger.info(
            f"[/api/models/clear-cache] removed {len(result.get('deleted', []))} files"
        )
        return result
    except Exception as e:
        logger.exception(f"[/api/models/clear-cache] failed: {e}")
        return {"success": False, "error": str(e), "deleted": [], "freed_bytes": 0}


# ------------------------------------------------------------
# 更新检测
# ------------------------------------------------------------
@router.post("/check")
def check_updates(req: PathsRequest):
    """检测更新。rel_paths 为空则检测所有有来源记录的模型。"""
    targets = req.rel_paths or None
    logger.info(f"[/api/models/check] targets={targets}")
    try:
        result = check_all(targets)
        return {"success": True, **result}
    except Exception as e:
        logger.exception(f"[/api/models/check] failed: {e}")
        return {"success": False, "error": str(e), "results": [], "summary": {}}


def _check_one(rel_path: str) -> Dict:
    """检测单个模型。返回值一定带 status 与 message，前端不会拿到 undefined。"""
    logger.info(f"[/api/models/check-one] {rel_path!r}")
    try:
        result = check_one(rel_path)
        result.setdefault("status", "unknown")
        result.setdefault("message", "")
        return {"success": True, **result}
    except Exception as e:
        logger.exception(f"[/api/models/check-one] failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "rel_path": rel_path,
            "status": "check_failed",
            "message": f"检测失败：{type(e).__name__}: {e}",
        }


# ⭐ GET 与 POST 都注册。
#    修复前只有 POST，而前端用的是 GET，结果拿到 405 Method Not Allowed，
#    响应体里既没有 message 也没有 status，界面就显示成“xxx.gguf：undefined”。
@router.get("/check-one")
def check_one_get(rel_path: str = Query(...)):
    """检测单个模型（GET，供每行的“检测”按钮调用）。"""
    return _check_one(rel_path)


@router.post("/check-one")
def check_one_post(rel_path: str = Query(...)):
    """检测单个模型（POST，兼容旧调用方）。"""
    return _check_one(rel_path)


# ------------------------------------------------------------
# 更新（重新下载）
# ------------------------------------------------------------
@router.post("/update")
def update_models(req: PathsRequest):
    """
    重新下载指定模型。

    下载写 .part，完成后 os.replace 原子替换——旧文件在整个过程中保持可用，
    下载失败也不会破坏现有模型。
    """
    rel_paths = req.rel_paths or []
    logger.info(f"[/api/models/update] {len(rel_paths)} models")

    started, skipped = [], []
    for rel in rel_paths:
        rec = registry.get(rel)
        if not rec:
            skipped.append({"rel_path": rel, "reason": "无来源记录，无法更新"})
            continue

        try:
            # 与更新检测保持同一套源解析：设置里强制了检测源（且该源上
            # 确实有该文件）时，更新就从那个源重新下载，避免“检测说有
            # 更新、下载却打去了另一个源”
            source = resolve_update_source(rec)
            task = manager.start(
                model_id=rec.get("model_id", ""),
                filename=rec.get("filename", ""),
                source=source,
                target_dir=str(settings.MODEL_DIR),
                token=settings.hf_token if source == "huggingface" else "",
            )
            started.append({"rel_path": rel, "task_id": task.id, "status": task.status})
        except Exception as e:
            logger.exception(f"[/api/models/update] {rel} failed: {e}")
            skipped.append({"rel_path": rel, "reason": str(e)})

    return {
        "success": len(skipped) == 0,
        "started": started,
        "skipped": skipped,
        "started_count": len(started),
    }


# ------------------------------------------------------------
# 定期检测状态
# ------------------------------------------------------------
@router.get("/schedule")
def schedule_status():
    return scheduler.status()
