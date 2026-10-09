# app/services/huggingface.py
"""
HuggingFace 服务模块。

超时/重试相关的修复（针对 "Read timed out. (read timeout=10.0)"）：
1) 之前顶部硬编码 HF_HUB_ETAG_TIMEOUT=10，这是 10 秒读超时的直接来源。
   国内镜像（hf-mirror.com 等）响应普遍较慢，10 秒必然超时。
   现在所有超时统一从 settings 读取且可调，默认连接 15s / 读 60s。
2) 加入指数退避重试（默认 3 次）。镜像站抖动是常态，单次失败就报错体验太差。
3) 关键降级：full=True 会带上所有 siblings，响应体常达数 MB，是超时的主因。
   重试时逐步降级 —— 先降 limit，再退到 full=False（响应极小），
   宁可少带体积信息也要先把结果搜出来（体积可由 /api/search/size 懒加载补齐）。
"""

import os
import time
from typing import Optional, List, Dict

# ⚠️ 不要设成 10：etag 请求在国内镜像下同样容易超时，统一给足
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

import requests  # noqa: E402
from huggingface_hub import HfApi, configure_http_backend  # noqa: E402

from app.config import settings  # noqa: E402
from app.logger import get_logger  # noqa: E402
from app.models import sum_sizes, coerce_timestamp  # noqa: E402

logger = get_logger(__name__)


# ============================================================
# 给 huggingface_hub 的 requests.Session 注入默认超时
# ============================================================
def _session_factory() -> requests.Session:
    """注入可配置超时的 Session，避免 paginate 无限等待或过早超时。"""
    session = requests.Session()
    original_request = session.request
    timeout = (
        float(settings.hf_connect_timeout),
        float(settings.hf_read_timeout),
    )

    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    return session


configure_http_backend(backend_factory=_session_factory)

api = HfApi()


def apply_endpoint(endpoint: str = "") -> str:
    """
    运行时切换 HF 镜像地址（Web 设置面板改"镜像加速"后调用），返回生效地址。

    ⭐ 这里有三个坑，缺一个就会出现"设置里选了镜像，请求却还打官方站"：

    1) huggingface_hub 在 import 时就端点求值进了 constants，改 os.environ
       对已加载的模块无效，必须同步改 constants。
    2) 常量名随版本变化：0.26.x 是 `ENDPOINT`（`HF_ENDPOINT` 是 1.x 的名字，
       在 0.26 里压根不存在，给它赋值等于写了个没人读的变量）。
       —— 这正是本 bug 的直接原因：只改了 HF_ENDPOINT，请求照样走官方站。
    3) 最要命的：`HfApi.__init__` 里 `self.endpoint = endpoint or constants.ENDPOINT`
       把值固化成了**实例属性**。即便 constants 改对了，早已创建好的 `api`
       对象仍然留着旧值，而 list_models 拼的正是 `f"{self.endpoint}/api/models"`。
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

        # 覆盖各版本可能的常量名，逐个尝试，改不动的跳过
        for name in ("ENDPOINT", "HF_ENDPOINT", "_HF_DEFAULT_ENDPOINT"):
            if hasattr(consts, name):
                setattr(consts, name, base)

        resolve_tpl = base + "/api/{repo_type}s/{repo_id}/resolve/{revision}/{filename}"
        for name in ("HF_HUB_URL", "HUGGINGFACE_CO_RESOLVE_ENDPOINT"):
            if hasattr(consts, name):
                setattr(consts, name, resolve_tpl)
    except Exception as e:
        logger.warning(f"[HF] cannot patch constants: {e}")

    # ⭐ 决定性的一步：改实例属性。list_models / model_info 都读 self.endpoint
    try:
        api.endpoint = base
    except Exception as e:
        logger.warning(f"[HF] cannot patch HfApi instance: {e}")

    logger.info(f"[HF] endpoint -> {base} (api.endpoint={getattr(api, 'endpoint', '?')})")
    return endpoint


def effective_endpoint() -> str:
    """返回当前真正会用于请求的端点（供日志/排查用）。"""
    return str(getattr(api, "endpoint", "") or "") or "https://huggingface.co"


# 启动时按配置初始化一次。
# ⭐ 读 settings.hf_endpoint 而不是环境变量：镜像地址可以存在配置文件里，
#    只从环境变量取的话，重启后配置文件里配的镜像会被忽略。
apply_endpoint(getattr(settings, "hf_endpoint", "") or os.getenv("HF_ENDPOINT", ""))


def _is_retryable(e: Exception) -> bool:
    """
    只有网络/超时/5xx 才重试，4xx（401/404 等）重试没有意义。

    注意：拿不到 response 的 HTTPError 一律不重试 —— 无法确认是 5xx，
    盲重试只会让 401 之类的错误拖满整个预算。
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

    deadline: 绝对时间戳（time.time()）。降级链上每一档都重试 3 次的话，
    最坏情况要等 9 × 读超时 才会报错，体验不可接受。因此用总预算兜底：
    预算耗尽就直接抛，让上层尽快降级或报错。

    ⭐ deadline 改为仅关键字参数（keyword-only）。
       之前它是第二个位置参数，于是
         _call_with_retry(api.model_info, model_id, files_metadata=True)
       里的 model_id 会被 deadline 吃掉，真正调用变成
         api.model_info(files_metadata=True)
       → "missing 1 required positional argument: 'repo_id'"。
       这正是"镜像能搜出结果、但体积全拿不到"的原因：搜索那条调用传了
       deadline 所以没事，体积这条漏传了才炸。改成 keyword-only 后，
       再漏传会直接报"缺少参数"，不会再静默错位。
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

    Hub 返回的 gguf 形如：
        {"architecture": "llama", "context_length": ..., "total": 123456789012,
         "quantizations": [...]}
    其中 total 即该仓库所有 .gguf 文件的字节数之和。
    """
    gguf_meta = getattr(model, "gguf", None)
    if isinstance(gguf_meta, dict):
        for key in ("total", "total_size", "totalSize", "size"):
            v = gguf_meta.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    return None


def _collect(iterator, limit: int, gguf_only: bool) -> List[Dict]:
    """把 list_models 的迭代器收敛成结果列表。"""
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
                    s.rfilename
                    for s in siblings
                    if getattr(s, "rfilename", None) and s.rfilename.endswith(".gguf")
                ]

                # full=False 时没有 siblings，退化为按仓库名判断
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

                results.append(
                    {
                        "id": model.id,
                        "author": model.author or "",
                        "downloads": model.downloads or 0,
                        "likes": model.likes or 0,
                        "last_modified": str(model.lastModified) if model.lastModified else "",
                        "tags": model.tags or [],
                        "has_gguf": has_gguf,
                        "gguf_count": len(gguf_files),
                        "total_size": total_size,
                        "total_size_human": "",   # 由 router 统一格式化
                        "size_known": total_size is not None,
                        "source": "huggingface",
                    }
                )
            except Exception as e:
                logger.warning(f"[HF] skip model due to parse error: {e}")

    except Exception as e:
        logger.exception(f"[HF] iterate models error: {e}")
        raise

    logger.info(f"[HF] collected scanned={scanned}, results={len(results)}")
    return results


def search_models(query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
    """
    搜索 HuggingFace 模型。

    分级降级策略（保证在网络差时也能搜出结果）：
      第 1 档：full=True  + 放大候选（能直接拿到体积）
      第 2 档：full=True  + 缩小候选（响应体减小）
      第 3 档：full=False + 服务端过滤（响应极小，体积交给 /size 懒加载）

    整个降级链共享一个时间预算（HF_SEARCH_BUDGET 秒，默认 45），
    避免"每档都重试满 N 次"导致最坏情况要等十分钟才报错。
    """
    logger.info(
        f"[HF] search_models start: query={query!r}, gguf_only={gguf_only}, "
        f"limit={limit}, endpoint={effective_endpoint()}"
    )

    budget = float(getattr(settings, "hf_search_budget", 45) or 45)
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
            # ⭐ 服务端过滤，响应体大幅缩小
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
            # 4xx 之类"明确被拒绝"的错误，换档位重试也没有意义，直接放弃
            if not _is_retryable(e):
                logger.info(f"[HF] error is not retryable, stop degrading")
                break
            if idx < len(plans):
                logger.info(f"[HF] degrading to plan {idx + 1}")

    # ⭐ 报错时把实际用的端点带上：
    #    如果这里显示的还是 huggingface.co，就说明镜像没生效，
    #    一眼能定位，不用去猜请求到底打到哪。
    logger.error(
        f"[HF] all plans failed: endpoint={effective_endpoint()}, err={last_err}"
    )
    raise last_err


def get_model_size(model_id: str, deadline: Optional[float] = None) -> Dict:
    """
    查询单个模型的 GGUF 总大小（前端懒加载兜底用）。

    走 model_info(files_metadata=True)，这是唯一能拿到真实 size 的接口。
    """
    logger.info(f"[HF] get_model_size: model_id={model_id!r}")
    try:
        info = _call_with_retry(
            api.model_info, model_id, files_metadata=True, deadline=deadline
        )
    except Exception as e:
        logger.warning(f"[HF] get_model_size failed for {model_id}: {e}")
        raise

    files = [
        (s.rfilename, getattr(s, "size", None))
        for s in (info.siblings or [])
        if getattr(s, "rfilename", None) and s.rfilename.endswith(".gguf")
    ]
    total = sum_sizes(size for _, size in files)
    return {"gguf_count": len(files), "total_size": total}


def get_gguf_files(model_id: str, deadline: Optional[float] = None) -> List[Dict]:
    """获取指定模型仓库中所有 GGUF 文件（含大小）。"""
    logger.info(f"[HF] get_gguf_files: model_id={model_id!r}")
    try:
        info = _call_with_retry(
            api.model_info, model_id, files_metadata=True, deadline=deadline
        )
        # 仓库级最后修改时间：文件级时间接口不统一，用它作为兜底判据
        repo_mtime = coerce_timestamp(getattr(info, "lastModified", None))
        gguf_files = []
        for s in info.siblings or []:
            if not s.rfilename or not s.rfilename.endswith(".gguf"):
                continue
            gguf_files.append(
                {
                    "filename": s.rfilename,
                    "size": s.size,
                    "mtime": repo_mtime,
                    # blob_id 随文件内容变化，是判断"内容变了"最可靠的标识
                    "etag": str(getattr(s, "blob_id", "") or ""),
                }
            )
        gguf_files.sort(key=lambda x: x["filename"])
        logger.info(f"[HF] get_gguf_files: found {len(gguf_files)} files")
        return gguf_files
    except Exception as e:
        logger.exception(f"[HF] get_gguf_files failed: {e}")
        raise


def get_file_meta(model_id: str, filename: str) -> Dict:
    """
    查询远端单个文件的元信息（大小 / 最后修改时间 / 内容标识）。

    blob_id 会随文件内容改变，因此即使大小不变（例如重新量化后体积刚好一致）
    也能检出更新。
    """
    for f in get_gguf_files(model_id):
        if f.get("filename") == filename:
            return {
                "size": f.get("size"),
                "mtime": f.get("mtime"),
                "etag": f.get("etag") or "",
            }
    return {"size": None, "mtime": None, "etag": ""}
