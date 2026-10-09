# app/services/downloader.py
"""
下载任务管理器。

设计要点：
1) 之前用 FastAPI BackgroundTasks 启动下载，任务无状态、无进度、无法取消。
   这里改为自管理的线程池 + 内存任务表，每个任务有独立状态机。
2) 自己走 httpx 流式下载（而不是 hf_hub_download / snapshot_download），
   因为 SDK 那两个函数不暴露进度回调，拿不到已下载字节数，
   自然也算不出速度和 ETA，更没法中途取消。
3) 断点续传：先写 <name>.gguf.part，完成后 rename。
   取消时保留 .part，下次下载同一文件自动从断点继续。
4) 速度用滑动窗口（默认 3 秒）计算，避免进度条抖动。

支持的两个源：
  HuggingFace   {endpoint}/{repo_id}/resolve/main/{filename}
  ModelScope    {base}/api/v1/models/{model_id}/repo?Revision=master&FilePath={path}
"""

import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

from app.config import settings
from app.logger import get_logger
from app.models import human_size

logger = get_logger(__name__)

CHUNK_SIZE = 256 * 1024          # 每块 256KB
SPEED_WINDOW = 3.0               # 速度滑动窗口（秒）
MAX_SAMPLES = 120                # 采样点上限
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class CancelledError(Exception):
    """任务被用户取消。"""


class DownloadTask:
    """单个下载任务。所有可变状态都受 _lock 保护。"""

    def __init__(
        self,
        model_id: str,
        filename: str,
        source: str,
        target_dir: str,
        token: str = "",
    ):
        self.id: str = uuid.uuid4().hex[:12]
        self.model_id = model_id
        self.filename = filename
        self.source = source
        self.target_dir = target_dir
        self.token = token

        self.status: str = "pending"     # pending/downloading/completed/cancelled/error
        self.total_bytes: int = 0
        self.downloaded_bytes: int = 0
        self.error: str = ""
        self.started_at: float = time.time()
        self.finished_at: Optional[float] = None

        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=MAX_SAMPLES)  # (ts, cumulative_bytes)
        self._thread: Optional[threading.Thread] = None
        self.llamacpp_reload: Optional[Dict] = None   # 下载完成后的重载结果
        self.remote_last_modified: str = ""           # 远端 Last-Modified
        self.remote_etag: str = ""                    # 远端 ETag

        # 目标文件与临时文件
        self.final_path = Path(target_dir) / os.path.basename(filename)
        self.part_path = self.final_path.with_suffix(self.final_path.suffix + ".part")

        # 首帧就能反映断点位置（否则 POST 返回时线程可能还没读到 .part）
        if self.part_path.exists():
            try:
                self.downloaded_bytes = self.part_path.stat().st_size
                self._samples.append((time.time(), self.downloaded_bytes))
            except OSError:
                pass

    # ---------- 进度更新 ----------
    def _add_bytes(self, n: int) -> None:
        with self._lock:
            self.downloaded_bytes += n
            self._samples.append((time.time(), self.downloaded_bytes))

    def speed_bps(self) -> float:
        """滑动窗口内的平均速度（字节/秒）。"""
        with self._lock:
            samples = list(self._samples)
        if len(samples) < 2:
            return 0.0
        now = samples[-1][0]
        base_ts, base_bytes = samples[0]
        for ts, cum in samples:
            if ts >= now - SPEED_WINDOW:
                base_ts, base_bytes = ts, cum
                break
        dt = now - base_ts
        if dt <= 0.05:
            return 0.0
        return max(0.0, (samples[-1][1] - base_bytes) / dt)

    def eta_seconds(self) -> Optional[float]:
        if self.status != "downloading":
            return None
        speed = self.speed_bps()
        if speed <= 0 or not self.total_bytes:
            return None
        remain = max(0, self.total_bytes - self.downloaded_bytes)
        return remain / speed

    # ---------- 序列化 ----------
    def to_dict(self) -> Dict:
        speed = self.speed_bps() if self.status == "downloading" else 0.0
        total = self.total_bytes
        done = self.downloaded_bytes
        percent = (done / total * 100) if total else 0.0
        return {
            "id": self.id,
            "model_id": self.model_id,
            "filename": self.filename,
            "source": self.source,
            "target_dir": self.target_dir,
            "status": self.status,
            "total_bytes": total,
            "downloaded_bytes": done,
            "total_size_human": human_size(total) if total else "",
            "downloaded_size_human": human_size(done),
            "percent": round(percent, 1),
            "speed_bps": speed,
            "speed_human": human_size(speed) + "/s" if speed > 0 else "",
            "eta_seconds": self.eta_seconds(),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "final_path": str(self.final_path),
            "llamacpp_reload": self.llamacpp_reload,
        }

    # ---------- 控制 ----------
    def cancel(self) -> bool:
        if self.status in ("completed", "cancelled", "error"):
            return False
        self._cancel.set()
        if self.status == "pending":
            self.status = "cancelled"
            self.finished_at = time.time()
        return True

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---------- URL 与请求头 ----------
    def build_url(self) -> str:
        if self.source == "huggingface":
            endpoint = (
                settings.hf_endpoint
                or os.getenv("HF_ENDPOINT", "")
                or "https://huggingface.co"
            ).rstrip("/")
            # 路径逐段编码，保留 repo 中的 "/"
            repo = "/".join(quote(p, safe="") for p in self.model_id.split("/"))
            fname = "/".join(quote(p, safe="") for p in self.filename.split("/"))
            return f"{endpoint}/{repo}/resolve/main/{fname}"

        # ModelScope
        base = (os.getenv("MS_ENDPOINT", "") or "https://modelscope.cn").rstrip("/")
        mid = "/".join(quote(p, safe="") for p in self.model_id.split("/"))
        fpath = quote(self.filename, safe="/")
        return f"{base}/api/v1/models/{mid}/repo?Revision=master&FilePath={fpath}"

    def build_headers(self) -> Dict[str, str]:
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if self.source == "huggingface" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # ---------- 执行 ----------
    def run(self) -> None:
        if self.is_cancelled:
            return

        self.status = "downloading"
        url = self.build_url()

        try:
            Path(self.target_dir).mkdir(parents=True, exist_ok=True)

            offset = self.part_path.stat().st_size if self.part_path.exists() else 0
            headers = self.build_headers()
            if offset:
                headers["Range"] = f"bytes={offset}-"
                logger.info(f"[task {self.id}] resume from {offset} bytes")

            timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
            with httpx.Client(
                timeout=timeout, headers=headers, follow_redirects=True
            ) as client:
                with client.stream("GET", url) as resp:
                    if self.is_cancelled:
                        raise CancelledError()

                    resp.raise_for_status()

                    content_length = int(resp.headers.get("Content-Length") or 0)
                    content_range = resp.headers.get("Content-Range", "")

                    if resp.status_code == 206 and content_range:
                        # bytes 100-999/1234
                        m = re.search(r"/(\d+)$", content_range.strip())
                        self.total_bytes = int(m.group(1)) if m else (offset + content_length)
                        mode = "ab"
                    else:
                        # 服务端忽略 Range（返回 200）：从头重下
                        self.total_bytes = content_length
                        offset = 0
                        mode = "wb"

                    # ⭐ 记录远端版本标识，之后才能判断"远端是否出了新版本"
                    self.remote_last_modified = resp.headers.get("Last-Modified") or ""
                    self.remote_etag = (resp.headers.get("ETag") or "").strip('"')

                    self.downloaded_bytes = offset
                    self._samples.append((time.time(), offset))

                    logger.info(
                        f"[task {self.id}] start: url={url} total={self.total_bytes} mode={mode}"
                    )

                    with open(self.part_path, mode) as f:
                        for chunk in resp.iter_bytes(CHUNK_SIZE):
                            if self.is_cancelled:
                                raise CancelledError()
                            if not chunk:
                                continue
                            f.write(chunk)
                            self._add_bytes(len(chunk))

            os.replace(self.part_path, self.final_path)
            self.status = "completed"
            self.finished_at = time.time()
            self.downloaded_bytes = self.total_bytes or self.downloaded_bytes
            logger.info(f"[task {self.id}] completed -> {self.final_path}")

            # ⭐ 记录来源，供后续"检测更新"比对远端大小
            self._record_registry()
            # ⭐ 按配置决定是否让 llama.cpp 重新加载模型
            self._notify_llamacpp()

        except CancelledError:
            self.status = "cancelled"
            self.finished_at = time.time()
            logger.info(f"[task {self.id}] cancelled, kept {self.part_path}")

        except Exception as e:
            self.status = "error"
            self.error = str(e)
            self.finished_at = time.time()
            logger.exception(f"[task {self.id}] failed: {e}")

    # ---------- 完成后的钩子 ----------
    def _record_registry(self) -> None:
        """把来源信息写进注册表，之后才能判断远端是否有更新。"""
        try:
            from app.services.registry import registry, rel_path_of

            rel = rel_path_of(str(self.final_path))
            if not rel:
                logger.warning(
                    f"[task {self.id}] final path outside MODEL_DIR, skip registry"
                )
                return
            registry.put(
                rel_path=rel,
                model_id=self.model_id,
                source=self.source,
                filename=self.filename,
                remote_size=self.total_bytes or None,
                local_size=self.downloaded_bytes,
                remote_mtime=_parse_http_time(self.remote_last_modified),
                remote_etag=self.remote_etag,
            )
        except Exception as e:
            # 注册表写失败不应该让任务变成失败状态
            logger.warning(f"[task {self.id}] registry write failed: {e}")

    def _notify_llamacpp(self) -> None:
        """下载完成后按配置触发 llama.cpp 重载。"""
        try:
            from app.services import llamacpp

            res = llamacpp.maybe_reload_after_download(str(self.final_path))
            self.llamacpp_reload = res
            if res.get("ok"):
                logger.info(f"[task {self.id}] llama.cpp reloaded: {res}")
            elif res.get("skipped"):
                logger.info(f"[task {self.id}] llama.cpp skipped: {res.get('reason')}")
            else:
                logger.warning(f"[task {self.id}] llama.cpp reload failed: {res}")
        except Exception as e:
            logger.warning(f"[task {self.id}] llama.cpp hook error: {e}")


def _parse_http_time(value: str):
    """解析 HTTP Last-Modified 头（RFC 1123），失败返回 None。"""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        import calendar
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return calendar.timegm(dt.timetuple())
        return dt.timestamp()
    except Exception:
        return None


class DownloadManager:
    """内存任务表。单进程部署够用；重启后任务丢失（不持久化）。"""

    def __init__(self):
        self._tasks: Dict[str, DownloadTask] = {}
        self._lock = threading.Lock()

    def start(
        self,
        model_id: str,
        filename: str,
        source: str,
        target_dir: str,
        token: str = "",
    ) -> DownloadTask:
        task = DownloadTask(model_id, filename, source, target_dir, token)

        # 同一目标文件已在下载中，直接复用，避免重复写同一个 .part
        with self._lock:
            for t in self._tasks.values():
                if (
                    t.status in ("pending", "downloading")
                    and t.model_id == model_id
                    and t.filename == filename
                    and t.target_dir == target_dir
                ):
                    logger.info(f"[mgr] duplicate task, reuse {t.id}")
                    return t
            self._tasks[task.id] = task

        thread = threading.Thread(
            target=task.run, name=f"dl-{task.id}", daemon=True
        )
        task._thread = thread
        thread.start()
        logger.info(f"[mgr] task started: {task.id} {model_id}/{filename}")
        return task

    def get(self, task_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> List[DownloadTask]:
        with self._lock:
            return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        return task.cancel()

    def remove(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def clear_finished(self) -> int:
        with self._lock:
            dead = [
                tid for tid, t in self._tasks.items()
                if t.status in ("completed", "cancelled", "error")
            ]
            for tid in dead:
                del self._tasks[tid]
        if dead:
            logger.info(f"[mgr] cleared {len(dead)} finished tasks")
        return len(dead)

    def total_speed_bps(self) -> float:
        return sum(
            t.speed_bps() for t in self.list() if t.status == "downloading"
        )

    def active_count(self) -> int:
        return sum(1 for t in self.list() if t.status in ("pending", "downloading"))


manager = DownloadManager()
