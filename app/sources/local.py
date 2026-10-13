"""
本地模型：扫描 / 校验 / 删除 / 缓存清理 + 来源注册表 + 更新检测。

三者是一条链上的上下游（扫盘 → 查来源记录 → 比对远端），因此放在同一个文件里，
让“相对路径口径”和“状态字段”只有一份定义：

  ① 扫描（scan_models）
       扫描 MODEL_DIR（或其他挂载目录）下的 .gguf 与 .gguf.part，给出有效性；
  ② 注册表（Registry）
       下载完成时记录“这个文件来自哪个仓库的哪个文件 + 远端大小/时间/指纹”，
       键为**相对 MODEL_DIR 的路径**；
  ③ 更新检测（check_one / check_all / UpdateScheduler）
       拿注册表里的远端元信息去比对当前远端，判断是否有更新。

⚠️ 路径口径（最容易踩的坑）：
   注册表的键相对 **MODEL_DIR**，而界面展示的 relative_path 相对**扫描根目录**。
   当「扫描路径」指向外部只读目录（如 /app/external-models）时，两者不同。
   查注册表必须统一用“相对 MODEL_DIR 的 POSIX 路径”（见 _rel_posix），
   否则外部目录里的同名文件会**错配**到 MODEL_DIR 中同名文件的来源与更新状态；
   不在 MODEL_DIR 内的文件一律视为未知来源。

有效性标签：
  valid       完整 .gguf，且文件头是 GGUF 魔数
  incomplete  .gguf.part —— 下载未完成或被取消的残留
  corrupt     .gguf 但文件头不是 GGUF（下载损坏 / 非 GGUF 文件）
  unreadable  读不了（权限或 IO 问题）
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from app.config import settings, resolve_config_dir, REGISTRY_FILENAME
from app.events import emit_check_completed, emit_model_updated
from app.logger import get_logger
from app.sources import get_backend
from app.utils import format_timestamp, human_size

logger = get_logger(__name__)

GGUF_MAGIC = b"GGUF"
PART_SUFFIXES = (".gguf.part", ".part")
CHECK_WORKERS = 4
MTIME_TOLERANCE = 300.0  # 5 分钟：CDN/镜像时间戳精度差异不误报


# ============================================================
# 路径工具
# ============================================================
def _model_root() -> Path:
    return Path(settings.MODEL_DIR).resolve()


def _rel_posix(path, root) -> Optional[str]:
    """
    求 path 相对 root 的路径，统一用正斜杠（POSIX 风格）。

    ⭐ 必须归一：Windows 下 Path.relative_to() 会给出 "a\\b.gguf"，
    而注册表键、前端 data-rel、SSE 事件里的 rel_path 若一半反斜杠一半正斜杠，
    就会出现“明明有来源记录却显示未知来源”这种极难查的问题。
    镜像跑在 Linux 上，这里钉死正斜杠，两个平台行为一致。
    """
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def rel_path_of(abs_path) -> Optional[str]:
    """把绝对路径转成相对 MODEL_DIR 的路径，不在目录内则返回 None。"""
    return _rel_posix(abs_path, _model_root())


# ============================================================
# 来源注册表
# ============================================================
def _store_path() -> Path:
    # 与 settings.json 同目录，保证两者一起被持久化
    d = resolve_config_dir(Path(settings.MODEL_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d / REGISTRY_FILENAME


class Registry:
    """线程安全的 JSON 文件注册表。"""

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
            except (OSError, ValueError) as e:
                logger.warning(f"[registry] load failed, start empty: {e}")
                self._data = {}
            self._loaded = True

    def _save(self) -> None:
        try:
            _store_path().write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
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

        just_downloaded=True（默认）表示刚下载/更新完成：
        内容此刻正是远端最新版本，直接标记为「已是最新」，
        而不是留空让界面显示“未检查”——用户还得手动点一次检测才变绿。
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
                    "up_to_date" if just_downloaded else old.get("last_check_status")
                ),
            }
            self._save()

        logger.info(
            f"[registry] put {rel_path} <- {source}:{model_id}/{filename}"
            + (" (marked up_to_date)" if just_downloaded else "")
        )
        emit_model_updated(
            rel_path,
            status="up_to_date" if just_downloaded else None,
            reason="download" if just_downloaded else "record",
        )

    def mark_checked(
        self,
        rel_path: str,
        status: str,
        remote_size: Optional[int] = None,
        remote_mtime: Optional[float] = None,
        remote_etag: str = "",
    ) -> None:
        """检测完成后更新状态并广播事件。"""
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

        emit_model_updated(rel_path, status=status, reason="check")

    def remove(self, rel_path: str) -> None:
        self._ensure_loaded()
        with self._lock:
            if self._data.pop(rel_path, None) is not None:
                self._save()
        emit_model_updated(rel_path, status=None, reason="remove")

    def prune_missing(self, existing_rel_paths) -> int:
        """删掉磁盘上已不存在的记录。"""
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


# ============================================================
# 扫描
# ============================================================
def _is_part(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(s) for s in PART_SUFFIXES)


def _strip_part(filename: str) -> str:
    """Qwen.gguf.part -> Qwen.gguf"""
    for s in PART_SUFFIXES:
        if filename.lower().endswith(s):
            return filename[: -len(s)] + ".gguf"
    return filename


def _check_magic(path: Path) -> Optional[bool]:
    """读文件头判断是不是 GGUF。None 表示读不了（unreadable）。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == GGUF_MAGIC
    except OSError:
        return None


def _entry(f: Path, root: Path) -> Optional[Dict]:
    try:
        stat = f.stat()
    except OSError as e:
        logger.warning(f"[local] stat failed for {f}: {e}")
        return None

    size = stat.st_size
    part = _is_part(f.name)

    if part:
        validity = "incomplete"
        display_name = _strip_part(f.name)
    else:
        magic = _check_magic(f)
        if magic is None:
            validity = "unreadable"
        elif magic:
            validity = "valid"
        else:
            validity = "corrupt"
        display_name = f.name

    return {
        "name": display_name[:-5] if display_name.lower().endswith(".gguf") else display_name,
        "filename": f.name,
        "display_name": display_name,
        "path": str(f),
        "relative_path": f.relative_to(root).as_posix(),
        "size": size,
        "size_human": human_size(size),
        "modified": stat.st_mtime,
        "modified_human": format_timestamp(stat.st_mtime),
        "is_part": part,
        "validity": validity,
    }


def scan_models(model_dir: str) -> List[Dict]:
    """扫描目录，返回完整模型 + 未完成的缓存文件（含来源与更新状态）。"""
    models: List[Dict] = []
    root = Path(model_dir)
    logger.debug(f"[local] scan_models: dir={model_dir!r}, exists={root.exists()}")

    if not root.exists():
        logger.warning(f"[local] scan dir does not exist: {model_dir}")
        return models

    reg = registry.all()
    model_root = _model_root()

    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lower = f.name.lower()
        if not (lower.endswith(".gguf") or _is_part(lower)):
            continue

        entry = _entry(f, root)
        if not entry:
            continue

        # ⭐ 注册表键一律相对 MODEL_DIR（不是扫描根），否则扫外部目录会错配来源。
        #    前端做“检测/更新/删除”时也用这个键（字段名 rel_path），
        #    保证与服务端注册表、SSE 事件里的 rel_path 完全一致。
        entry["rel_path"] = _rel_posix(entry["path"], model_root) or ""

        if not entry["is_part"]:
            rel = entry["rel_path"] or None
            rec = reg.get(rel) if rel else None
            if rec:
                entry["source"] = rec.get("source", "")
                entry["model_id"] = rec.get("model_id", "")
                entry["remote_filename"] = rec.get("filename", "")
                entry["last_check_status"] = rec.get("last_check_status")
                entry["last_checked_at"] = rec.get("last_checked_at")
                entry["has_update_info"] = True
            else:
                entry["source"] = ""
                entry["model_id"] = ""
                entry["has_update_info"] = False

        models.append(entry)

    models.sort(key=lambda x: (x["is_part"], -x["modified"]))
    incomplete = sum(1 for m in models if m["is_part"])
    logger.info(
        f"[local] scan_models: found {len(models)} files in {model_dir} "
        f"(incomplete={incomplete})"
    )
    return models


# ============================================================
# 删除 / 缓存清理
# ============================================================
def _safe_target(file_path: str, model_dir: str) -> Path:
    """校验路径在模型目录内且是允许删除的类型，返回绝对路径。"""
    target = Path(file_path).resolve()
    root = Path(model_dir).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Path outside model directory")

    name = target.name.lower()
    if not (name.endswith(".gguf") or _is_part(name)):
        raise ValueError("只允许删除 .gguf / .gguf.part 文件")
    return target


def delete_model(file_path: str, model_dir: str) -> bool:
    """删除单个文件（完整模型或 .part 缓存均可），并清掉对应来源记录。"""
    target = _safe_target(file_path, model_dir)

    if not target.exists():
        logger.warning(f"[local] delete skipped (not found): {target}")
        return False

    logger.info(f"[local] deleting {target}")
    target.unlink()
    try:
        target.parent.rmdir()
    except OSError:
        pass  # 目录非空，正常

    rel = rel_path_of(str(target))
    if rel:
        registry.remove(rel)
    return True


def clear_cache(model_dir: str) -> Dict:
    """清理所有未完成下载的 .part 缓存文件。"""
    root = Path(model_dir)
    deleted, freed, failed = [], 0, []

    if not root.exists():
        return {"deleted": [], "freed_bytes": 0, "failed": []}

    for f in sorted(root.rglob("*")):
        if not f.is_file() or not _is_part(f.name):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        try:
            f.unlink()
            deleted.append(str(f.relative_to(root)))
            freed += size
        except OSError as e:
            failed.append({"path": str(f), "error": f"{type(e).__name__}: {e}"})

    logger.info(
        f"[local] clear_cache: removed {len(deleted)} part files, "
        f"freed {human_size(freed) or '0 B'}"
    )
    return {"deleted": deleted, "freed_bytes": freed, "failed": failed}


# ============================================================
# 更新检测
# ============================================================
def _check_source_candidates(record: dict) -> List[str]:
    """
    按设置里的「检测源」给出候选源顺序（去重、保序）。

    · record（默认）：只用模型记录里的下载来源；
    · huggingface / modelscope：强制源排最前，记录来源兜底——
      大量模型只在一个源上有，不回退会把“这个源没有”误判成
      “远端已无此文件”。
    """
    recorded = (record.get("source") or "").strip()
    preferred = str(settings.update_check_source or "record").strip().lower()

    candidates: List[str] = []
    if preferred in ("huggingface", "modelscope"):
        candidates.append(preferred)
    if recorded and recorded not in candidates:
        candidates.append(recorded)
    return candidates


def resolve_update_source(record: dict) -> str:
    """
    解析「更新下载」实际使用的源，与 _remote_meta 的口径保持一致：
    强制源上没有该文件时回退记录来源，避免重新下载打过去 404。

    检测刚跑过时上游文件列表还在 60s TTL 缓存里，这次确认基本不花钱。
    """
    candidates = _check_source_candidates(record)
    if not candidates:
        return "huggingface"
    if len(candidates) == 1:
        return candidates[0]

    model_id = record.get("model_id", "")
    filename = record.get("filename", "")
    if not model_id or not filename:
        return candidates[0]
    for src in candidates:
        backend = get_backend(src)
        if backend is None:
            continue
        try:
            meta = backend.get_file_meta(model_id, filename)
        except Exception:
            continue
        if meta.get("size") is not None:
            return src
    # 所有源都查不到该文件：按优先级用第一个，让下载方报明确的 404，
    # 好过静默换源下一个“同名但内容不同”的文件
    return candidates[0]


def _remote_meta(record: dict) -> Dict:
    """
    查远端该文件的当前元信息（大小 / 修改时间 / 内容标识）。

    只用大小判断不够：仓库重新量化或修了权重后体积可能完全一致，
    只比 size 会漏报更新。这里三个维度一起取，任一维变化即判为“有更新”。

    查询源由 _check_source_candidates 决定：默认按记录来源；设置里强制
    某个源时优先查它，查不到再回退记录来源。
    """
    empty = {"size": None, "mtime": None, "etag": ""}
    model_id = record.get("model_id", "")
    filename = record.get("filename", "")
    if not model_id or not filename:
        return empty

    candidates = _check_source_candidates(record)
    if not candidates:
        return empty

    last_error: Optional[Exception] = None
    answered = False  # 是否有源明确返回“没有这个文件”（区别于网络异常）
    for src in candidates:
        backend = get_backend(src)
        if backend is None:
            continue
        try:
            meta = backend.get_file_meta(model_id, filename)
        except Exception as e:
            # 网络/超时类错误：先记下，尝试回退到下一个源
            last_error = e
            logger.debug(f"[updater] {src} lookup failed for {model_id}/{filename}: {e}")
            continue
        if meta.get("size") is not None:
            return meta
        answered = True

    if last_error is not None and not answered:
        # 全是异常（网络不通/超时），不是“文件不存在”：
        # 抛出去让 check_one 报“查询远端失败”，避免误报“远端已无此文件”
        raise last_error
    return empty


def _failed(rel_path: str, base: Dict, message: str) -> Dict:
    """统一处理检测失败：落库 + 广播 + 返回体。"""
    registry.mark_checked(rel_path, "check_failed")
    emit_check_completed({"up_to_date": 0, "update_available": 0,
                          "local_mismatch": 0, "check_failed": 1})
    return {**base, "status": "check_failed", "message": message}


def check_one(rel_path: str, local_size: Optional[int] = None) -> Dict:
    """检测单个模型。"""
    record = registry.get(rel_path)
    if not record:
        return {
            "rel_path": rel_path,
            "status": "unknown",
            "message": "无来源记录，无法检测更新",
        }

    if local_size is None:
        try:
            local_size = (Path(settings.MODEL_DIR) / rel_path).stat().st_size
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
        logger.debug(f"[updater] remote lookup failed for {rel_path}: {e}")
        return _failed(rel_path, base, f"查询远端失败：{type(e).__name__}: {e}")

    remote = meta.get("size")
    if remote is None:
        return _failed(rel_path, base, "远端已无此文件")

    remote_mtime = meta.get("mtime")
    remote_etag = meta.get("etag") or ""
    recorded_size = record.get("remote_size")
    recorded_mtime = record.get("remote_mtime")
    recorded_etag = record.get("remote_etag") or ""

    base["remote_size"] = remote
    base["remote_size_human"] = human_size(remote)
    base["remote_mtime"] = remote_mtime
    base["remote_mtime_human"] = format_timestamp(remote_mtime)

    # 多重维度判断“远端是否出了新版本”。
    # mtime 加 5 分钟容差，避免 CDN/镜像时间戳精度不一致导致误报；size 与 etag 是硬指标。
    size_changed = recorded_size is not None and remote != recorded_size
    etag_changed = bool(recorded_etag and remote_etag and remote_etag != recorded_etag)
    mtime_changed = bool(
        recorded_mtime and remote_mtime
        and abs(float(remote_mtime) - float(recorded_mtime)) > MTIME_TOLERANCE
    )

    if size_changed or etag_changed:
        reasons = []
        if etag_changed:
            reasons.append("文件内容已变更")
        if size_changed:
            reasons.append(f"{human_size(recorded_size)} → {human_size(remote)}")
        status = "update_available"
        message = "远端已更新：" + "，".join(reasons)
    elif mtime_changed:
        status = "up_to_date"
        message = (
            f"已是最新（远端 {human_size(remote)}，"
            f"时间已同步至 {format_timestamp(remote_mtime)}）"
        )
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
    """批量检测。rel_paths 为空时检测所有有记录的模型。"""
    models = scan_models(str(settings.MODEL_DIR))
    size_by_rel = {m["relative_path"]: m["size"] for m in models}

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

    emit_check_completed(summary)
    logger.info(f"[updater] check_all: {summary}")
    return {"results": results, "summary": summary}


class UpdateScheduler:
    """后台定期检测线程。开关关闭或间隔 <= 0 时不启动。"""

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
        if self._thread and self._thread.is_alive():
            return  # 已在运行，不重复起线程

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
            "source": str(settings.update_check_source or "record"),
            "running": self.running,
            "last_run_at": self.last_run_at,
            "last_summary": self.last_summary,
        }


scheduler = UpdateScheduler()
