# app/routers/tasks.py
"""
下载任务状态路由。

  GET    /api/tasks              列出所有任务 + 全局汇总（总速度、活跃数）
  GET    /api/tasks/{id}         单个任务详情
  POST   /api/tasks/{id}/cancel  取消任务（保留 .part 以支持断点续传）
  DELETE /api/tasks/{id}         从列表移除记录（不删文件）
  POST   /api/tasks/clear        清除所有已结束的任务
"""

from fastapi import APIRouter

from app.logger import get_logger
from app.models import human_size
from app.services.downloader import manager

logger = get_logger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks():
    tasks = [t.to_dict() for t in manager.list()]
    # 新任务排前面
    tasks.sort(key=lambda x: x["started_at"], reverse=True)

    total_speed = manager.total_speed_bps()
    return {
        "total": len(tasks),
        "tasks": tasks,
        "summary": {
            "active": manager.active_count(),
            "total_speed_bps": total_speed,
            "total_speed_human": (human_size(total_speed) + "/s") if total_speed > 0 else "",
        },
    }


@router.get("/{task_id}")
def get_task(task_id: str):
    task = manager.get(task_id)
    if not task:
        return {"error": "task not found"}
    return task.to_dict()


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = manager.get(task_id)
    if not task:
        return {"error": "task not found"}
    ok = manager.cancel(task_id)
    logger.info(f"[/api/tasks/{task_id}/cancel] ok={ok}")
    return {"success": ok, "task": task.to_dict()}


@router.delete("/{task_id}")
def remove_task(task_id: str):
    ok = manager.remove(task_id)
    logger.info(f"[/api/tasks/{task_id}] removed={ok}")
    return {"success": ok}


@router.post("/clear")
def clear_finished():
    n = manager.clear_finished()
    return {"success": True, "removed": n}
