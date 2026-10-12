"""
HuggingFace 源后端。

设计要点：
1) 超时/重试统一从配置读取，默认连接 15s / 读 60s。国内镜像普遍慢，10 秒必超时。
2) 指数退避 + 总预算（deadline）：降级链每档都重试满 N 次的话，
   最坏要等 9 × 读超时，用绝对时间戳兜底。
3) 搜索分级降级：full=True 大候选 → full=True 小候选 → full=False 服务端过滤。
   最后一步响应极小，体积交给前端调 /api/search/sizes 批量补全。
4) 实现 SourceBackend 协议，对外只暴露统一方法。

⚠️ 不要把 HF_HUB_ETAG_TIMEOUT 硬编码成 10 —— 那正是 10 秒读超时的来源。
"""

import os
import time
from typing import Optional, List, Dict

# 别设成 10：etag 请求在国内镜像同样容易超时，统一给足
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

import requests  # noqa: E402
from huggingface_hub import HfApi, configure_http_backend  # noqa: E402

from app.config import settings  # noqa: E402
from app.logger import get_logger  # noqa: E402
from app.sources import SourceBackend, TTLCache, normalize_model_card  # noqa: E402
from app.utils import coerce_timestamp, sum_sizes  # noqa: E402

logger = get_logger(__name__)


# 文件列表缓存。定义在模块级（而不是类属性）是因为 apply_endpoint() 在类定义
# 之前就要用到它 —— 切换镜像后必须清空，否则会拿到上一个端点缓存下来的结果。
# 场景：搜索补全体积和「查看」文件列表查的是同一个模型，一次交互里会重复两三次。
_FILES_CACHE = TTLCache(ttl=60.0, maxsize=512)


# ============================================================
# 给 huggingface_hub 的 requests.Session 注入默认超时
# ============================================================
def _session_factory() -> requests.Session:
    """注入可配置超时的 Session，避免 paginate 无限等待或过早超时。"""
    session = requests.Session()
    original_request = session.request
    timeout = (float(settings.hf_connect_timeout), float(settings.hf_read_timeout))

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    return session


configure_http_backend(backend_factory=_session_factory)
api = HfApi()


def apply_endpoint(endpoint: str = "") -> str:
    """
    运行时切换 HF 镜像地址。

    这里有三个坑，缺一个就会“设置了镜像但请求还打官方站”：
    1) huggingface_hub import 时就端点求值进了 constants，改 os.environ 无效，
       必须同步改 constants。
    2) 常量名随版本变化：0.26.x 是 `ENDPOINT`（`HF_ENDPOINT` 是 1.x 的名字，
       0.26 里压根不存在，赋值等于写了个没人读的变量）。
    3) 最要命：`HfApi.__init__` 里 `self.endpoint = endpoint or constants.ENDPOINT`
       把值固化成了实例属性。constants 改对了，早已创建的 `api` 对象仍是旧值，
       而 list_models 拼的是 `f"{self.endpoint}/api/models"`。
       所以必须显式改实例上的 endpoint。
    """
    endpoint = (endpoint or "").strip().rstrip("/")
    base = endpoint or "https://huggingface.co"

    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    else:
        os.environ.pop("HF_ENDPOINT", None)

    try:
        import huggingface_hub.constants as consts
        for name in ("ENDPOINT", "HF_ENDPOINT", "_HF_DEFAULT_ENDPOINT"):
            if hasattr(consts, name):
                setattr(consts, name, base)
        resolve_tpl = base + "/api/{repo_type}s/{repo_id}/resolve/{revision}/{filename}"
        for name in ("HF_HUB_URL", "HUGGINGFACE_CO_RESOLVE_ENDPOINT"):
            if hasattr(consts, name):
                setattr(consts, name, resolve_tpl)
    except Exception as e:
        logger.warning(f"[HF] cannot patch constants: {e}")

    try:
        api.endpoint = base
    except Exception as e:
        logger.warning(f"[HF] cannot patch HfApi instance: {e}")

    # 换了端点，旧缓存里的文件列表/大小都不再可信
    _FILES_CACHE.clear()

    logger.info(f"[HF] endpoint -> {base} (api.endpoint={getattr(api, 'endpoint', '?')})")
    return endpoint


def effective_endpoint() -> str:
    """返回当前真正会用于请求的端点（供日志/排查用）。"""
    return str(getattr(api, "endpoint", "") or "") or "https://huggingface.co"


# 启动时按配置初始化一次。读 settings 而非环境变量：镜像地址可存在配置文件，
# 只从环境变量取的话，重启后配置里的镜像会被忽略。
apply_endpoint(getattr(settings, "hf_endpoint", "") or os.getenv("HF_ENDPOINT", ""))


def _is_retryable(e: Exception) -> bool:
    """
    只有网络/超时/5xx 才重试，4xx（401/404）重试没意义。

    拿不到 response 的 HTTPError 一律不重试 —— 无法确认是 5xx，
    盲重试只会让 401 之类错误拖满整个预算。
    """
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        if resp is None:
            return False
        return getattr(resp, "status_code", 0) >= 500

    if isinstance(e, requests.exceptions.RequestException):
        return True

    name = type(e).__name__.lower()
    return "timeout" in name or "connection" in name or "network" in name


def _call_with_retry(fn, *args, deadline: Optional[float] = None, **kwargs):
    """
    带指数退避的重试包装。

    deadline 为仅关键字参数。之前它是第二个位置参数，于是
        _call_with_retry(api.model_info, model_id, files_metadata=True)
    里的 model_id 会被 deadline 吃掉 → "missing 1 required positional argument"，
    正是“能搜出结果但体积全拿不到”的原因。keyword-only 后漏传会直接报参数错误。
    """
    attempts = max(1, int(settings.hf_max_retries))
    backoff = float(settings.hf_retry_backoff) or 1.0
    last_err = None

    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt >= attempts or not _is_retryable(e):
                raise
            if deadline is not None and time.time() >= deadline:
                logger.warning("[HF] retry budget exhausted, stop retrying")
                raise
            wait = backoff ** (attempt - 1)
            logger.warning(
                f"[HF] attempt {attempt}/{attempts} failed "
                f"({type(e).__name__}: {e}), retry in {wait:.1f}s"
            )
            time.sleep(wait)

    raise last_err


def _gguf_total_from_meta(model) -> Optional[int]:
    """
    从 ModelInfo.gguf["total"] 取 GGUF 文件总字节数。
    Hub 返回形如 {"architecture": "llama", "total": 1234, "quantizations": [...]}
    """
    gguf_meta = getattr(model, "gguf", None)
    if isinstance(gguf_meta, dict):
        for key in ("total", "total_size", "totalSize", "size"):
            v = gguf_meta.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return None


def _collect(iterator, limit: int, gguf_only: bool) -> List[Dict]:
    """把 list_models 的迭代器收敛成统一结构的列表。"""
    results: List[Dict] = []
    scanned = 0

    try:
        for model in iterator:
            scanned += 1
            if len(results) >= limit:
                break

            try:
                siblings = getattr(model, "siblings", None) or []
                gguf_files = [
                    s.rfilename for s in siblings
                    if getattr(s, "rfilename", None) and s.rfilename.endswith(".gguf")
                ]

                has_gguf = bool(gguf_files) or (
                    not siblings and "gguf" in str(getattr(model, "id", "")).lower()
                )
                if gguf_only and not has_gguf:
                    continue

                total_size = _gguf_total_from_meta(model)
                if total_size is None:
                    total_size = sum_sizes(
                        getattr(s, "size", None)
                        for s in siblings
                        if getattr(s, "rfilename", None)
                        and s.rfilename.endswith(".gguf")
                    )

                results.append(normalize_model_card(
                    model_id=model.id,
                    source="huggingface",
                    downloads=model.downloads or 0,
                    likes=model.likes or 0,
                    last_modified=str(model.lastModified) if model.lastModified else "",
                    tags=model.tags or [],
                    has_gguf=has_gguf,
                    gguf_count=len(gguf_files),
                    total_size=total_size,
                ))
            except Exception as e:
                logger.warning(f"[HF] skip model due to parse error: {e}")

    except Exception as e:
        logger.exception(f"[HF] iterate models error: {e}")
        raise

    logger.info(f"[HF] collected scanned={scanned}, results={len(results)}")
    return results


class HuggingFaceBackend(SourceBackend):
    name = "huggingface"

    def search_models(self, query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
        """
        搜索模型。三级降级共享一个时间预算（HF_SEARCH_BUDGET 秒，默认 45），
        避免“每档都重试满 N 次”导致最坏要等十分钟才报错。
        """
        logger.info(
            f"[HF] search_models: query={query!r}, gguf_only={gguf_only}, "
            f"limit={limit}, endpoint={effective_endpoint()}"
        )

        # max(5, ...)：配置被写坏（0/负数）时也不会让 deadline 落在过去，
        # 否则第 1 档就直接 break，最后 `raise None` 会变成 TypeError。
        budget = max(5.0, float(getattr(settings, "hf_search_budget", 45) or 45))
        deadline = time.time() + budget

        plans = [
            dict(full=True, fetch=min(limit * 2, 100)),
            dict(full=True, fetch=min(max(limit, 10), 30)),
            dict(full=False, fetch=limit),
        ]

        last_err = None
        for idx, plan in enumerate(plans, 1):
            if time.time() >= deadline:
                logger.warning(f"[HF] budget {budget}s exhausted before plan {idx}")
                break

            kwargs: Dict = dict(
                search=query if query else None,
                sort="downloads",
                direction=-1,
                limit=plan["fetch"],
                full=plan["full"],
            )
            if gguf_only:
                kwargs["filter"] = ["gguf"]

            try:
                iterator = _call_with_retry(api.list_models, deadline=deadline, **kwargs)
                results = _collect(iterator, limit, gguf_only)
                logger.info(
                    f"[HF] plan {idx} succeeded (full={plan['full']}, "
                    f"fetch={plan['fetch']}) -> {len(results)} results"
                )
                return results
            except Exception as e:
                last_err = e
                logger.warning(f"[HF] plan {idx} failed: {type(e).__name__}: {e}")
                if not _is_retryable(e):
                    logger.info("[HF] error is not retryable, stop degrading")
                    break
                if idx < len(plans):
                    logger.info(f"[HF] degrading to plan {idx + 1}")

        logger.error(f"[HF] all plans failed: endpoint={effective_endpoint()}, err={last_err}")
        if last_err is None:
            raise RuntimeError(f"搜索未执行（时间预算 {budget:.0f}s 已耗尽）")
        raise last_err

    def get_model_size(self, model_id: str, deadline: Optional[float] = None) -> Dict:
        """
        查单模型 GGUF 总大小（前端懒加载兜底）。

        直接复用 get_gguf_files：两者统计的是同一批 .gguf 文件，
        合流后既省一次 api.model_info，也共享同一份缓存。
        """
        logger.debug(f"[HF] get_model_size: model_id={model_id!r}")
        files = self.get_gguf_files(model_id, deadline=deadline)
        return {"gguf_count": len(files), "total_size": sum_sizes(f.get("size") for f in files)}

    def get_gguf_files(self, model_id: str, deadline: Optional[float] = None) -> List[Dict]:
        """
        列出仓库内 GGUF 文件（含大小/修改时间/内容指纹）。

        带 60s TTL 缓存：搜索补全体积与「查看」文件列表会查同一个模型，
        没必要重复走一遍 api.model_info。
        """
        cached, hit = _FILES_CACHE.get(model_id)
        if hit:
            logger.debug(f"[HF] get_gguf_files: cache hit, {len(cached)} files")
            return cached

        logger.debug(f"[HF] get_gguf_files: model_id={model_id!r}")
        info = _call_with_retry(
            api.model_info, model_id, files_metadata=True, deadline=deadline
        )
        repo_mtime = coerce_timestamp(getattr(info, "lastModified", None))
        gguf_files = []
        for s in info.siblings or []:
            if not s.rfilename or not s.rfilename.endswith(".gguf"):
                continue
            gguf_files.append({
                "filename": s.rfilename,
                "size": s.size,
                "mtime": repo_mtime,
                # blob_id 随文件内容变化，是判断“内容变了”最可靠的标识
                "etag": str(getattr(s, "blob_id", "") or ""),
            })
        gguf_files.sort(key=lambda x: x["filename"])
        logger.debug(f"[HF] get_gguf_files: found {len(gguf_files)} files")
        _FILES_CACHE.set(model_id, gguf_files)
        return gguf_files

    def get_file_meta(self, model_id: str, filename: str) -> Dict:
        """
        取远端单文件元信息。

        blob_id 随文件内容改变，因此即使大小不变（如重新量化后体积刚好一致）
        也能检出更新。
        """
        for f in self.get_gguf_files(model_id):
            if f.get("filename") == filename:
                return {"size": f.get("size"), "mtime": f.get("mtime"), "etag": f.get("etag") or ""}
        return {"size": None, "mtime": None, "etag": ""}


backend = HuggingFaceBackend()
