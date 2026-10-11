"""
本地模型管理路由。

GET    /api/models              扫描本地模型（附带来源与更新状态）
DELETE /api/models              删除单个文件
POST   /api/models/batch-delete 批量删除
POST   /api/models/check        检测更新（指定或全部）
GET|POST /api/models/check-one  检测单个模型（供每行“检测”按钮）
POST   /api/models/update       更新模型（重新下载，完成后原子替换）
POST   /api/models/clear-cache  清理 .gguf.part 缓存
GET    /api/models/schedule     定期检测任务状态

v1.3.0：扫描结果自带来源与更新状态（不再由前端二次拼接）；
        检测完成/下载完成/注册表变更均通过事件总线推送，界面自动刷新。
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings
from app.logger import get_logger
from app.sources import llamacpp
from app.sources.downloader import manager
from app.sources.scanner import clear_cache, delete_model, scan_models
from app.sources.registry import registry, rel_path_of
from app.sources.updater import check_all, check_one, scheduler

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

    # 外部挂载目录没有注册表归属，不附加来源信息
    if path:
        for m in models:
            if "source" not in m:
                m["has_update_info"] = False

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
# 删除
# ------------------------------------------------------------
@router.delete("")
def remove_model(file_path: str = Query(...)):
    logger.info(f"[/api/models] delete file_path={file_path!r}")
    try:
        success = delete_model(file_path, str(settings.MODEL_DIR))
        logger.info(f"[/api/models] delete success={success}")

        # ⭐ 删掉后通知 llama.cpp：路由模式不监听磁盘，
        #    不重新扫描的话服务端列表里还留着这个模型。
        if success:
            notify = llamacpp_notify(reason="delete", file_path=file_path)
            return {"success": True, "llamacpp": notify}
        return {"success": False}
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

    llamacpp_notify = None
    if deleted:
        llamacpp_notify = llamacpp_batch_notify(deleted, reason="delete")

    logger.info(f"[/api/models/batch-delete] deleted={len(deleted)} failed={len(failed)}")
    return {"success": len(failed) == 0, "deleted": deleted, "failed": failed,
            "deleted_count": len(deleted), "llamacpp": llamacpp_notify}


def _server_names_for(file_path: str) -> List[str]:
    """算出本地文件在 llama.cpp 那边可能的模型名（用于卸载）。"""
    try:
        matched = llamacpp.match_remote_name(file_path)
        if matched:
            return [matched]
        return llamacpp.local_name_candidates(file_path)[:2]
    except Exception:
        return []


def llamacpp_notify(reason: str, file_path: str = "", names: Optional[List[str]] = None) -> Optional[Dict]:
    """通知 llama.cpp 模型变化。"""
    try:
        if names is None:
            names = _server_names_for(file_path) if file_path else []
        if not names:
            return None
        res = llamacpp.reload(reason=reason)
        return res
    except Exception as e:
        logger.warning(f"[/api/models] llamacpp notify failed: {e}")
        return None


def llamacpp_batch_notify(deleted: List[str], reason: str) -> Optional[Dict]:
    try:
        names: List[str] = []
        for p in deleted:
            names.extend(_server_names_for(p))
        if not names:
            return None
        return llamacpp.reload(reason=reason)
    except Exception:
        return None


# ------------------------------------------------------------
# 缓存清理
# ------------------------------------------------------------
@router.post("/clear-cache")
def clear_part_cache():
    """
    清理所有下载中断留下的 .gguf.part 缓存文件。

    取消/失败的下载会在磁盘上留下大量残留，界面上看不到也删不掉，
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
            task = manager.start(
                model_id=rec.get("model_id", ""),
                filename=rec.get("filename", ""),
                source=rec.get("source", "huggingface"),
                target_dir=str(settings.MODEL_DIR),
                token=settings.hf_token if rec.get("source") == "huggingface" else "",
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
