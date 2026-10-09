# app/services/local_scanner.py
"""
本地模型扫描。

除了完整的 .gguf，这里也会扫描下载中断留下的 .gguf.part 缓存文件，
并给出"有效性"标签，方便在界面上直接清理：

  valid       完整 .gguf，且文件头是 GGUF 魔数
  incomplete  .gguf.part —— 下载未完成或被取消的残留
  corrupt     .gguf 但文件头不是 GGUF（下载损坏 / 非 GGUF 文件）
  unreadable  读不了（权限或 IO 问题）

不扫描 .part 的话，取消或失败的下载会在磁盘上留几十 GB 的垃圾，
而界面上完全看不到、也删不掉。
"""

from pathlib import Path
from typing import Dict, List, Optional

from app.logger import get_logger
from app.models import human_size

logger = get_logger(__name__)

GGUF_MAGIC = b"GGUF"
PART_SUFFIXES = (".gguf.part", ".part")


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
        "relative_path": str(f.relative_to(root)),
        "size": size,
        "size_human": human_size(size),
        "modified": stat.st_mtime,
        "is_part": part,
        "validity": validity,
    }


def scan_models(model_dir: str) -> List[Dict]:
    """扫描目录，返回完整模型 + 未完成的缓存文件。"""
    models: List[Dict] = []
    root = Path(model_dir)
    logger.debug(f"[local] scan_models: dir={model_dir!r}, exists={root.exists()}")

    if not root.exists():
        logger.warning(f"[local] scan dir does not exist: {model_dir}")
        return models

    seen = set()
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lower = f.name.lower()
        if not (lower.endswith(".gguf") or _is_part(lower)):
            continue
        if str(f) in seen:
            continue
        seen.add(str(f))

        entry = _entry(f, root)
        if entry:
            models.append(entry)

    models.sort(key=lambda x: (x["is_part"], -x["modified"]))
    logger.info(
        f"[local] scan_models: found {len(models)} files in {model_dir} "
        f"(incomplete={sum(1 for m in models if m['is_part'])})"
    )
    return models


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
    """删除单个文件（完整模型或 .part 缓存均可）。"""
    target = _safe_target(file_path, model_dir)

    if not target.exists():
        logger.warning(f"[local] delete skipped (not found): {target}")
        return False

    logger.info(f"[local] deleting {target}")
    target.unlink()
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return True


def delete_models(file_paths: List[str], model_dir: str) -> Dict:
    """批量删除，逐个独立处理，单个失败不影响其他。"""
    deleted, failed = [], []
    for p in file_paths or []:
        try:
            if delete_model(p, model_dir):
                deleted.append(p)
            else:
                failed.append({"path": p, "error": "文件不存在"})
        except ValueError as e:
            failed.append({"path": p, "error": str(e)})
        except OSError as e:
            failed.append({"path": p, "error": f"{type(e).__name__}: {e}"})

    logger.info(f"[local] batch delete: {len(deleted)} deleted, {len(failed)} failed")
    return {"deleted": deleted, "failed": failed}


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
