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

from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings
from app.logger import get_logger
from app.sources import llamacpp
from app.sources.downloader import manager
from app.sources.local import (
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
# llama.cpp 联动
# ------------------------------------------------------------
def _notify_llamacpp(reason: str) -> Optional[Dict]:
    """
    通知 llama.cpp 模型集合有变化（新增/删除）。

    ⭐ 路由模式不监听磁盘，必须让它重新扫描；否则服务端列表里还留着已删模型。

    这里只做本地开关判断（未启用 / 未配地址直接跳过），有变化就让它重扫 ——
    不要为了“有没有候选模型名”先调一次 list_models()：那是一次实打实的
    HTTP 请求，代价高而信息量为零。
    """
    if not settings.llamacpp_enabled or not (settings.llamacpp_url or "").strip():
        return None
    try:
        return llamacpp.reload(reason=reason)
    except Exception as e:
        logger.warning(f"[/api/models] llamacpp notify failed: {e}")
        return None


# ------------------------------------------------------------
# 删除
# ------------------------------------------------------------
@router.delete("")
def remove_model(file_path: str = Query(...)):
    logger.info(f"[/api/models] delete file_path={file_path!r}")
    try:
        success = delete_model(file_path, str(settings.MODEL_DIR))
        logger.info(f"[/api/models] delete success={success}")
        if not success:
            return {"success": False}
        return {"success": True, "llamacpp": _notify_llamacpp("delete")}
    except ValueError as e:
        logger.warning(f"[/api/models] delete rejected: {e}")
        return {"error": str(e)}
    except Exception as e:
        logger.exception(f"[/api/models] delete failed: {e}")
        return {"error": str(e)}


@router.post("/batch-delete")
def batch_remove(req: PathsRequest):
    """批量删除。单个失败不影响其他，返回逐条结果。"""
    paths = req.file_paths or []
    logger.info(f"[/api/models/batch-delete] {len(paths)} files")

    deleted, failed = [], []
    for p in paths:
        try:
            if delete_model(p, str(settings.MODEL_DIR)):
                deleted.append(p)
            else:
                failed.append({"path": p, "error": "文件不存在或不是 .gguf"})
        except ValueError as e:
            failed.append({"path": p, "error": str(e)})
        except Exception as e:
            logger.exception(f"[/api/models/batch-delete] {p} failed: {e}")
            failed.append({"path": p, "error": str(e)})

    notify = _notify_llamacpp("delete") if deleted else None
    logger.info(f"[/api/models/batch-delete] deleted={len(deleted)} failed={len(failed)}")
    return {
        "success": len(failed) == 0,
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
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
