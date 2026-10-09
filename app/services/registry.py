# app/services/registry.py
"""
本地模型来源注册表。

问题：本地只有一堆 .gguf 文件，没有任何"这个模型是从哪个仓库下的"的信息，
      因此无法判断远端是否有更新。

方案：下载完成时记录元数据到配置目录下的 registry.json（默认 /app/config，
      与模型目录分离，可单独挂载持久化），键为相对路径（相对于 MODEL_DIR），
      内容包含来源仓库、远端文件、下载时的远端大小。检测更新时重新查远端
      大小做对比。

没有记录的模型（用户手动拷进去的）标记为"未知来源"，不支持更新检测，
界面上明确区分，而不是假装"已是最新"。
"""

import json

import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from app.config import settings, resolve_config_dir, REGISTRY_FILENAME
from app.logger import get_logger

logger = get_logger(__name__)

_FILENAME = REGISTRY_FILENAME


def _store_path() -> Path:
    # 与 settings.json 同目录，保证两者一起被持久化
    d = resolve_config_dir(Path(settings.MODEL_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d / _FILENAME


class Registry:
    """线程安全的 JSON 文件注册表，写操作带节流合并的简化实现（直接落盘）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._data: Dict[str, dict] = {}
        self._loaded = False

    # ---------- 载入 / 落盘 ----------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                p = _store_path()
                if p.exists():
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._data = raw
            except Exception as e:
                logger.warning(f"[registry] load failed, start empty: {e}")
                self._data = {}
            self._loaded = True

    def _save(self) -> None:
        try:
            _store_path().write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[registry] save failed: {e}")

    # ---------- 读写 ----------
    def get(self, rel_path: str) -> Optional[dict]:
        self._ensure_loaded()
        with self._lock:
            return self._data.get(rel_path)

    def all(self) -> Dict[str, dict]:
        self._ensure_loaded()
        with self._lock:
            return dict(self._data)

    def put(
        self,
        rel_path: str,
        model_id: str,
        source: str,
        filename: str,
        remote_size: Optional[int] = None,
        local_size: Optional[int] = None,
        remote_mtime: Optional[float] = None,
        remote_etag: str = "",
        just_downloaded: bool = True,
    ) -> None:
        """
        记录来源。

        just_downloaded=True（默认）表示这次是刚下载/更新完成的：
        ⭐ 内容此刻正是远端的最新版本，所以直接标记为「已是最新」，
           而不是留空让界面显示"未检查"——用户还得手动点一次检测才变绿，
           这个体验很别扭。远端是否真有更新交给后续检测去判断。
        """
        self._ensure_loaded()
        now = time.time()
        with self._lock:
            old = self._data.get(rel_path) or {}
            self._data[rel_path] = {
                "model_id": model_id,
                "source": source,
                "filename": filename,
                "remote_size": remote_size,
                "remote_mtime": remote_mtime,
                "remote_etag": remote_etag or "",
                "local_size": local_size,
                "downloaded_at": old.get("downloaded_at", now),
                "updated_at": now,
                "last_checked_at": now if just_downloaded else old.get("last_checked_at"),
                "last_check_status": (
                    "up_to_date" if just_downloaded
                    else old.get("last_check_status")
                ),
            }
            self._save()
        logger.info(
            f"[registry] put {rel_path} <- {source}:{model_id}/{filename}"
            + (" (marked up_to_date)" if just_downloaded else "")
        )

    def mark_checked(
        self,
        rel_path: str,
        status: str,
        remote_size: Optional[int] = None,
        remote_mtime: Optional[float] = None,
        remote_etag: str = "",
    ) -> None:
        self._ensure_loaded()
        with self._lock:
            rec = self._data.get(rel_path)
            if not rec:
                return
            rec["last_checked_at"] = time.time()
            rec["last_check_status"] = status
            if remote_size is not None:
                rec["remote_size"] = remote_size
            if remote_mtime is not None:
                rec["remote_mtime"] = remote_mtime
            if remote_etag:
                rec["remote_etag"] = remote_etag
            self._save()

    def remove(self, rel_path: str) -> None:
        self._ensure_loaded()
        with self._lock:
            if self._data.pop(rel_path, None) is not None:
                self._save()

    def remove_many(self, rel_paths: List[str]) -> int:
        self._ensure_loaded()
        n = 0
        with self._lock:
            for rp in rel_paths:
                if self._data.pop(rp, None) is not None:
                    n += 1
            if n:
                self._save()
        return n

    # ---------- 清理 ----------
    def prune_missing(self, existing_rel_paths) -> int:
        """删掉磁盘上已经不存在的记录。"""
        self._ensure_loaded()
        existing = set(existing_rel_paths)
        with self._lock:
            stale = [k for k in self._data if k not in existing]
            for k in stale:
                del self._data[k]
            if stale:
                self._save()
        if stale:
            logger.info(f"[registry] pruned {len(stale)} stale records")
        return len(stale)


registry = Registry()


def rel_path_of(abs_path: str) -> Optional[str]:
    """把绝对路径转成相对 MODEL_DIR 的路径，不在目录内则返回 None。"""
    try:
        return str(Path(abs_path).resolve().relative_to(Path(settings.MODEL_DIR).resolve()))
    except (ValueError, OSError):
        return None
