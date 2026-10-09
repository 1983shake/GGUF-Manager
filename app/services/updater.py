# app/services/updater.py
"""
本地模型更新检测。

判定依据：远端文件大小（+ 是否存在）。
  远端大小 > 记录值且本地大小 == 记录值  -> update_available（远端出了新版本）
  本地大小 != 远端大小                    -> local_mismatch（下载不完整/被改动，需重新拉取）
  一致                                    -> up_to_date
  没有来源记录                            -> unknown（手动拷入的，无法判定）

支持两种触发：
  手动   check_one() / check_all()，由 Web 每个模型的"检测"按钮调用
  定期   UpdateScheduler 后台线程，间隔由 update_check_interval_minutes 配置

注意：查询是网络 IO，逐个串行会很慢，这里用线程池并发（默认 4）。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.config import settings
from app.logger import get_logger
from app.models import human_size, format_timestamp
from app.services import huggingface, modelscope
from app.services.registry import registry
from app.services.local_scanner import scan_models

logger = get_logger(__name__)

CHECK_WORKERS = 4


def _remote_meta(record: dict) -> Dict:
    """
    查远端该文件的当前元信息（大小 / 修改时间 / 内容标识）。

    只用大小判断是不够的：仓库重新量化或修了权重后，文件体积可能完全一样，
    只比 size 会漏报更新。这里三个维度一起取，任一维变化即判为"有更新"。
    """
    source = record.get("source", "")
    model_id = record.get("model_id", "")
    filename = record.get("filename", "")
    empty = {"size": None, "mtime": None, "etag": ""}

    if not model_id or not filename:
        return empty

    try:
        if source == "huggingface":
            return huggingface.get_file_meta(model_id, filename)
        if source == "modelscope":
            return modelscope.get_file_meta(model_id, filename)
    except Exception as e:
        logger.debug(f"[updater] remote lookup failed {source}:{model_id}/{filename}: {e}")
        raise
    return empty


def check_one(rel_path: str, local_size: Optional[int] = None) -> Dict:
    """
    检测单个模型。

    参数:
        rel_path:   相对 MODEL_DIR 的路径
        local_size: 本地实际字节数，不传则自行 stat
    """
    record = registry.get(rel_path)
    if not record:
        return {
            "rel_path": rel_path,
            "status": "unknown",
            "message": "无来源记录，无法检测更新",
        }

    if local_size is None:
        abs_path = os.path.join(str(settings.MODEL_DIR), rel_path)
        try:
            local_size = os.path.getsize(abs_path)
        except OSError:
            local_size = None

    base = {
        "rel_path": rel_path,
        "model_id": record.get("model_id", ""),
        "source": record.get("source", ""),
        "filename": record.get("filename", ""),
        "local_size": local_size,
        "local_size_human": human_size(local_size),
        "recorded_remote_size": record.get("remote_size"),
    }

    try:
        meta = _remote_meta(record)
    except Exception as e:
        registry.mark_checked(rel_path, "check_failed")
        return {
            **base,
            "status": "check_failed",
            "message": f"查询远端失败：{type(e).__name__}: {e}",
        }

    remote = meta.get("size")
    if remote is None:
        registry.mark_checked(rel_path, "check_failed")
        return {
            **base,
            "status": "check_failed",
            "message": "远端已无此文件",
        }

    remote_mtime = meta.get("mtime")
    remote_etag = meta.get("etag") or ""

    recorded_size = record.get("remote_size")
    recorded_mtime = record.get("remote_mtime")
    recorded_etag = record.get("remote_etag") or ""

    base["remote_size"] = remote
    base["remote_size_human"] = human_size(remote)
    base["remote_mtime"] = remote_mtime
    base["remote_mtime_human"] = format_timestamp(remote_mtime)

    # 三个维度任一变化 → 远端出了新版本
    reasons = []
    if recorded_etag and remote_etag and remote_etag != recorded_etag:
        reasons.append("文件内容已变更")
    if recorded_mtime and remote_mtime and remote_mtime > recorded_mtime:
        reasons.append(f"远端更新于 {format_timestamp(remote_mtime)}")
    if recorded_size is not None and remote != recorded_size:
        reasons.append(f"{human_size(recorded_size)} → {human_size(remote)}")

    # 判定顺序很关键：先看"远端自己变了没"，再看"本地对不对得上"。
    # 反过来的话，远端一旦更新，本地必然对不上，会被误判成"下载不完整"。
    if reasons:
        status = "update_available"
        message = "远端已更新：" + "，".join(reasons)
    elif local_size is not None and local_size != remote:
        status = "local_mismatch"
        message = (
            f"本地 {human_size(local_size)} 与远端 {human_size(remote)} 不一致，"
            "可能下载不完整，建议重新下载"
        )
    else:
        status = "up_to_date"
        message = (
            "已是最新（远端 " + human_size(remote)
            + (f"，更新于 {format_timestamp(remote_mtime)}" if remote_mtime else "")
            + "）"
        )

    registry.mark_checked(
        rel_path, status,
        remote_size=remote, remote_mtime=remote_mtime, remote_etag=remote_etag,
    )
    return {**base, "status": status, "message": message}


def check_all(rel_paths: Optional[List[str]] = None) -> Dict:
    """
    批量检测。rel_paths 为空时检测所有有记录的模型。

    返回每个模型的检测结果 + 汇总计数。
    """
    models = scan_models(str(settings.MODEL_DIR))
    size_by_rel = {m["relative_path"]: m["size"] for m in models}

    # 清掉磁盘上已不存在的记录
    registry.prune_missing(size_by_rel.keys())

    if rel_paths:
        targets = [rp for rp in rel_paths if rp in size_by_rel]
    else:
        targets = [rp for rp in registry.all().keys() if rp in size_by_rel]

    results: List[Dict] = []
    if targets:
        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            results = list(pool.map(lambda rp: check_one(rp, size_by_rel.get(rp)), targets))

    summary = {
        "total": len(results),
        "up_to_date": sum(1 for r in results if r["status"] == "up_to_date"),
        "update_available": sum(1 for r in results if r["status"] == "update_available"),
        "local_mismatch": sum(1 for r in results if r["status"] == "local_mismatch"),
        "check_failed": sum(1 for r in results if r["status"] == "check_failed"),
    }

    logger.info(f"[updater] check_all: {summary}")
    return {"results": results, "summary": summary}


class UpdateScheduler:
    """后台定期检测线程。间隔 <= 0 或开关关闭时不启动。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_run_at: Optional[float] = None
        self.last_summary: Optional[Dict] = None
        self.running = False

    def start(self) -> None:
        if not settings.update_check_enabled:
            logger.info("[updater] scheduler disabled")
            return
        interval = int(settings.update_check_interval_minutes or 0)
        if interval <= 0:
            logger.info("[updater] interval <= 0, scheduler not started")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="update-checker", daemon=True
        )
        self._thread.start()
        logger.info(f"[updater] scheduler started, every {interval} min")

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def _loop(self) -> None:
        # 启动后先等一个周期，避免和启动时的其他 IO 抢资源
        while not self._stop.wait(max(60, int(settings.update_check_interval_minutes) * 60)):
            if not settings.update_check_enabled:
                continue
            try:
                self.running = True
                logger.info("[updater] scheduled check started")
                res = check_all()
                self.last_summary = res.get("summary")
                self.last_run_at = time.time()
                logger.info(f"[updater] scheduled check done: {self.last_summary}")
            except Exception as e:
                logger.exception(f"[updater] scheduled check failed: {e}")
            finally:
                self.running = False

    def status(self) -> Dict:
        return {
            "enabled": bool(settings.update_check_enabled),
            "interval_minutes": int(settings.update_check_interval_minutes or 0),
            "running": self.running,
            "last_run_at": self.last_run_at,
            "last_summary": self.last_summary,
        }


scheduler = UpdateScheduler()
