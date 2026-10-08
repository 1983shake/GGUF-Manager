# app/services/local_scanner.py
from pathlib import Path
from typing import List, Dict


def scan_models(model_dir: str) -> List[Dict]:
    """扫描指定路径下的所有 GGUF 模型文件"""
    models: List[Dict] = []
    root = Path(model_dir)
    if not root.exists():
        return models

    for f in root.rglob("*.gguf"):
        try:
            stat = f.stat()
        except OSError:
            continue

        models.append(
            {
                "name": f.stem,
                "filename": f.name,
                "path": str(f),
                "relative_path": str(f.relative_to(root)),
                "size": stat.st_size,
                "size_human": _human_size(stat.st_size),
                "modified": stat.st_mtime,
            }
        )

    models.sort(key=lambda x: x["modified"], reverse=True)
    return models


def delete_model(file_path: str, model_dir: str) -> bool:
    """删除指定模型文件（限制在模型目录内）"""
    target = Path(file_path).resolve()
    root = Path(model_dir).resolve()

    # 安全检查：防止越权删除
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("Path outside model directory")

    if target.exists() and target.suffix == ".gguf":
        target.unlink()
        # 尝试删除空的父目录（向上最多 1 层）
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return True
    return False


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"
