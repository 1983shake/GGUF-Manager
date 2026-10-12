"""
事件总线：让“状态变化”主动推送到前端，根治“更新状态不及时刷新”。

任何会改变界面状态的操作完成后发一个事件，
GET /api/events 用 SSE（Server-Sent Events）把事件推给浏览器，
前端据此精准刷新对应行/徽标，无需整表重扫、无需猜“该不该刷新”。

事件名约定：
  model_updated    单个模型记录（来源/更新状态）发生变化
  check_completed  一轮更新检测结束，附带 summary
  task_updated     某个下载任务状态变化（替代轮询）
  config_changed   运行时配置被修改，前端需重新拉取
  scheduler_tick   定期检测调度器状态变化（启停/间隔调整）

为什么用 SSE 而不是 WebSocket：
  单向推送就够用，SSE 基于 HTTP、自带断线重连、穿过反代不用额外配置，
  实现成本远低于 WebSocket，浏览器 API 也更简单（EventSource）。

订阅模型是「注册回调 add_listener()」：每个连接只登记一个回调，
  事件产生时用 loop.call_soon_threadsafe 投递到该连接的事件循环，
  断开时在 finally 里摘除。连接本身不带任何常驻线程，开销与连接数无关 ——
  不要退回“每连接一个队列 + 一个搬运线程”的写法：连接一多线程数就成倍增长。
"""

import json
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from app.logger import get_logger

logger = get_logger(__name__)

# 保留最近 N 条事件，用于新连接的客户端补回放（避免连接抖动期间丢事件）
_BACKLOG_SIZE = 200

Listener = Callable[[Dict[str, Any]], None]


class EventBus:
    """进程内的发布/订阅总线。线程安全。"""

    def __init__(self, backlog: int = _BACKLOG_SIZE):
        self._backlog: Deque[Dict[str, Any]] = deque(maxlen=backlog)
        self._listeners: list = []
        self._lock = threading.RLock()
        self._seq = 0

    def publish(self, name: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发布事件，返回完整事件对象（便于同步调用方直接拿到）。"""
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "event": name,
                "data": payload or {},
                "ts": time.time(),
            }
            self._backlog.append(event)
            listeners = list(self._listeners)

        # 回调在锁外执行：订阅者（SSE 连接）出问题不能拖垮发布方
        for fn in listeners:
            try:
                fn(event)
            except Exception as e:  # noqa: BLE001 - 单个订阅者异常必须隔离
                logger.debug(f"[events] listener failed: {e}")
        return event

    def history(self, since_id: int = 0) -> list:
        """返回 id 大于 since_id 的事件（供新连接做补回放）。"""
        with self._lock:
            return [e for e in self._backlog if e["id"] > since_id]

    def add_listener(self, fn: Listener) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass


bus = EventBus()


def emit_model_updated(rel_path: str, status: Optional[str] = None, reason: str = "change") -> None:
    """模型记录变化（来源写入/更新检测完成）。"""
    bus.publish("model_updated", {"rel_path": rel_path, "status": status, "reason": reason})


def emit_check_completed(summary: Optional[Dict[str, Any]] = None, results: Optional[list] = None) -> None:
    """一轮更新检测结束。results 只摘出 rel_path+status 精简下发。"""
    slim = None
    if results:
        slim = [{"rel_path": r.get("rel_path"), "status": r.get("status")} for r in results]
    bus.publish("check_completed", {"summary": summary or {}, "results": slim or []})


def emit_task_updated(task: Any) -> None:
    """下载任务状态变化。只下发摘要，避免把内部对象序列化。"""
    if hasattr(task, "to_dict"):
        bus.publish("task_updated", {"task": task.to_dict()})
    else:
        bus.publish("task_updated", {"task": task})


def emit_config_changed() -> None:
    bus.publish("config_changed", {})


def emit_scheduler_tick(state: Optional[Dict[str, Any]] = None) -> None:
    bus.publish("scheduler_tick", state or {})


def event_to_sse(event: Dict[str, Any]) -> str:
    """把事件对象序列化成 SSE 帧。data 行必须是 JSON 的单行载荷。"""
    payload = json.dumps(event.get("data", {}), ensure_ascii=False)
    return f"id: {event['id']}\n" f"event: {event['event']}\n" f"data: {payload}\n\n"
