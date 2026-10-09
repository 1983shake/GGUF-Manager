# app/routers/logs.py
from fastapi import APIRouter, Query

from app.logger import get_logs, get_buffer_stats

router = APIRouter(prefix="/api/logs", tags=["logs"])


# 纯内存操作，async def 即可，不会阻塞
@router.get("")
async def list_logs(
    since: int = Query(0, ge=0, description="只返回 id 大于该值的日志"),
    limit: int = Query(500, ge=1, le=2000),
):
    logs = get_logs(since_id=since, limit=limit)
    return {"total": len(logs), "logs": logs}


@router.get("/stats")
async def log_stats():
    return get_buffer_stats()
