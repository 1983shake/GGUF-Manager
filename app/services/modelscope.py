# app/services/modelscope.py
"""
ModelScope 服务模块。

修复要点：
1) 搜索改用 /openapi/v1/models 的官方 snake_case 参数：
       search / page_size / page_number / sort
   旧代码用的 PascalCase（Name / PageSize / PageNumber / SortBy）属于
   /api/v1/ 旧版接口，OpenAPI 会直接忽略这些未知参数，
   等价于"不带关键词"返回默认列表。
2) 响应解析优先取官方字段 `id`（旧版才是 path/Path/name/Name）。
   旧代码 _get_field(m, "path","Path","name","Name") 取不到值，
   紧接着 `if not path or "/" not in path: continue` 会把每一个模型丢掉，
   导致搜索永远返回 0 条。
3) GGUF 判定兼容官方 `tasks` 字段（OpenAPI 返回的是 tasks 而非 tags），
   并把"仓库名是否含 gguf"提到最前面作为最可靠的判据。
4) 服务端搜索生效性校验由"只看第一条"改为"整页命中数"，
   避免第一条恰好不匹配就误判、只翻一页就 break。

体积信息：
   搜索接口不返回文件大小。为保证搜索响应速度，这里**不在搜索时**并发补拉，
   改由前端按需调 /api/search/size 懒加载（可见即加载，用户能看到明确状态）。
   文件列表走 /api/v1/models/{id}/repo/files，返回 Data.Files[].{Path,Size}。
"""

from typing import List, Dict, Optional

import httpx

from app.logger import get_logger  # noqa: E402
from app.models import coerce_timestamp, sum_sizes  # noqa: E402

logger = get_logger(__name__)


# ============================================================
# 常量
# ============================================================
MS_BASE = "https://modelscope.cn"
MS_SEARCH_URL = f"{MS_BASE}/openapi/v1/models"
MS_FILES_URL = f"{MS_BASE}/api/v1/models/{{model_id}}/repo/files"

MAX_PAGE_SIZE = 50        # OpenAPI 单页最大条数
MAX_PAGE_NUMBER = 20      # 最多翻 20 页 × 50 条
HTTP_TIMEOUT = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


# ============================================================
# 搜索
# ============================================================
def search_models(query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
    """
    通过 ModelScope OpenAPI 搜索模型。

    参数:
        query: 模糊搜索关键词（例如 "qwen2.5"）
        gguf_only: 是否只返回含 GGUF 的模型
        limit: 最多返回多少个结果
    """
    logger.info(
        f"[MS] search_models start: query={query!r}, "
        f"gguf_only={gguf_only}, limit={limit}"
    )

    if not query:
        logger.info("[MS] empty query, skip")
        return []

    results: List[Dict] = []
    seen: set = set()
    page_number = 1
    total_count: Optional[int] = None
    scanned = 0

    while len(results) < limit and page_number <= MAX_PAGE_NUMBER:
        # ⭐ 官方 OpenAPI 使用 snake_case 参数名
        params = {
            "search": query,
            "page_size": MAX_PAGE_SIZE,
            "page_number": page_number,
            "sort": "downloads",
        }

        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True
            ) as client:
                resp = client.get(MS_SEARCH_URL, params=params)
                logger.debug(
                    f"[MS] GET {resp.url} status={resp.status_code} "
                    f"body[:500]={resp.text[:500]!r}"
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"[MS] search page {page_number} HTTP error: "
                f"{e.response.status_code} - {e.response.text[:200]}"
            )
            raise
        except Exception as e:
            logger.exception(f"[MS] search page {page_number} failed: {e}")
            raise

        models_raw = _extract_models(data)
        if not models_raw:
            logger.info(f"[MS] page {page_number} returned 0 models, stop")
            break

        if total_count is None:
            total_count = _get_total_count(data)

        scanned += len(models_raw)

        # 服务端搜索生效性校验：整页都没有一条命中才降级为客户端过滤
        hits = [m for m in models_raw if _match_query(m, query)]
        search_effective = bool(hits)
        if page_number == 1 and not search_effective:
            logger.warning(
                f"[MS] server-side search looks ineffective for {query!r}; "
                f"fallback to client-side filter"
            )

        candidates = hits if search_effective else models_raw

        for m in candidates:
            if len(results) >= limit:
                break

            try:
                # ⭐ 官方字段是 id（旧版才是 path）
                model_id = _model_id(m)
                if not model_id or "/" not in model_id or model_id in seen:
                    continue

                has_gguf = _detect_gguf(m, model_id)
                if gguf_only and not has_gguf:
                    continue

                seen.add(model_id)
                results.append(
                    {
                        "id": model_id,
                        "author": model_id.split("/")[0],
                        "downloads": _get_field(m, "downloads", "Downloads") or 0,
                        "likes": _get_field(m, "likes", "Likes", "stars", "Stars") or 0,
                        "last_modified": str(
                            _get_field(
                                m,
                                "updated_at",
                                "UpdatedAt",
                                "last_updated_time",
                                "LastUpdatedTime",
                                "lastModified",
                            )
                            or ""
                        ),
                        "tags": _get_field(m, "tags", "Tags", "tasks", "Tasks") or [],
                        "has_gguf": has_gguf,
                        "gguf_count": 0,
                        "total_size": None,
                        "total_size_human": "",
                        "size_known": False,   # 由前端懒加载补齐
                        "source": "modelscope",
                    }
                )
            except Exception as e:
                logger.warning(f"[MS] skip model due to parse error: {e}")

        # 终止条件
        if len(models_raw) < MAX_PAGE_SIZE:
            break
        if total_count is not None and page_number * MAX_PAGE_SIZE >= total_count:
            break
        if not search_effective:
            logger.info("[MS] stop paging because server-side search is ineffective")
            break

        page_number += 1

    logger.info(
        f"[MS] search_models done: scanned={scanned}, results={len(results)}, "
        f"total_count={total_count}"
    )
    return results


# ============================================================
# 体积查询
# ============================================================
def get_model_size(model_id: str) -> Dict:
    """查询单个模型的 GGUF 总大小（前端懒加载用）。"""
    logger.info(f"[MS] get_model_size: model_id={model_id!r}")
    files = get_gguf_files(model_id)   # 失败会抛异常，交给 router 转成 error
    total = sum_sizes(f.get("size") for f in files)
    return {"gguf_count": len(files), "total_size": total}


# ============================================================
# 解析辅助
# ============================================================
def _get_field(obj: dict, *keys):
    """按顺序尝试多个字段名，返回第一个非空值。"""
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "":
            return v
    return None


def _model_id(m: dict) -> str:
    """取模型 ID：官方 OpenAPI 为 `id`，旧版 /api/v1/ 为 path/Path/name/Name。"""
    return str(_get_field(m, "id", "Id", "path", "Path", "name", "Name") or "").strip()


def _match_query(m: dict, query: str) -> bool:
    mid = _model_id(m)
    return bool(mid) and query.lower() in mid.lower()


def _get_total_count(data) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("total_count", "totalCount", "total"):
            v = inner.get(key)
            if isinstance(v, int):
                return v
    for key in ("total_count", "totalCount", "TotalCount", "total"):
        v = data.get(key)
        if isinstance(v, int):
            return v
    return None


def _detect_gguf(m: dict, model_id: str) -> bool:
    """判断模型是否含 GGUF。"""
    # 1) 仓库名/路径命中（最可靠）
    if "gguf" in model_id.lower():
        return True

    # 2) tags / tasks 命中（OpenAPI 返回的是 tasks）
    for key in ("tags", "Tags", "tasks", "Tasks", "labels", "Labels",
                "categories", "Categories"):
        v = m.get(key)
        if isinstance(v, (list, tuple)):
            if any("gguf" in str(t).lower() for t in v):
                return True
        elif v and "gguf" in str(v).lower():
            return True

    # 3) 元字段命中
    for key in ("library_name", "LibraryName", "pipeline_tag", "PipelineTag",
                "model_type", "ModelType", "framework", "Framework"):
        v = m.get(key)
        if v and "gguf" in str(v).lower():
            return True

    return False


def _extract_models(data) -> List[Dict]:
    """
    兼容新版 OpenAPI（snake_case：{"data": {"models": [...]}}）
    与旧版 API（PascalCase：{"Data": {"Model": {"Models": [...]}}}）。
    """
    if not isinstance(data, dict):
        logger.warning(f"[MS] unexpected response type: {type(data).__name__}")
        return []

    # 新版 OpenAPI
    if "data" in data:
        inner = data["data"]
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            for key in ("models", "model", "items", "list", "results"):
                val = inner.get(key)
                if isinstance(val, list):
                    return val
                if isinstance(val, dict):
                    for sub_key in ("models", "model", "items", "list", "results"):
                        sub_val = val.get(sub_key)
                        if isinstance(sub_val, list):
                            return sub_val
            logger.warning(
                f"[MS] cannot locate models in data, data.keys={list(inner.keys())}"
            )
            return []

    # 旧版 PascalCase
    d = data.get("Data")
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        inner = d.get("Model")
        if isinstance(inner, dict) and isinstance(inner.get("Models"), list):
            return inner["Models"]
        if isinstance(inner, list):
            return inner
        if isinstance(d.get("Models"), list):
            return d["Models"]
        if isinstance(d.get("Models"), dict):
            inner_models = d["Models"].get("Model")
            if isinstance(inner_models, list):
                return inner_models

    logger.warning(f"[MS] cannot locate models, keys={list(data.keys())}")
    return []


# ============================================================
# 文件列表
# ============================================================
def get_gguf_files(model_id: str) -> List[Dict]:
    """
    获取 ModelScope 模型仓库中的 GGUF 文件列表（含大小）。

    接口：GET /api/v1/models/{model_id}/repo/files?Revision=master&Recursive=true
    返回：{"Data": {"Files": [{"Name","Path","Size","Type","Sha256"}, ...]}}
    """
    logger.info(f"[MS] get_gguf_files: model_id={model_id!r}")

    url = MS_FILES_URL.format(model_id=model_id)
    params = {"Revision": "master", "Recursive": "true"}

    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True
        ) as client:
            resp = client.get(url, params=params)
            logger.debug(
                f"[MS] GET {resp.url} status={resp.status_code} "
                f"body[:500]={resp.text[:500]!r}"
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.exception(f"[MS] get_gguf_files HTTP failed: {e}")
        raise

    files = _extract_files(data)
    gguf_files: List[Dict] = []

    for f in files:
        # 目录项 Type='tree' 没有 Size，跳过
        if str(_get_field(f, "type", "Type") or "").lower() == "tree":
            continue

        # 兼容新版 path/size 与旧版 Path/Size/Name
        name = _get_field(f, "path", "Path", "name", "Name")
        if not name or not name.endswith(".gguf"):
            continue

        size = _get_field(f, "size", "Size")
        gguf_files.append(
            {
                "filename": name,
                "size": int(size) if isinstance(size, (int, float)) else None,
                "mtime": coerce_timestamp(
                    _get_field(
                        f, "last_modified", "LastModified", "updated_at",
                        "UpdatedAt", "lastModified", "mtime", "Mtime",
                    )
                ),
                "etag": str(
                    _get_field(f, "sha256", "Sha256", "sha", "Sha", "etag", "ETag") or ""
                ),
            }
        )

    gguf_files.sort(key=lambda x: x["filename"])
    logger.info(f"[MS] get_gguf_files: found {len(gguf_files)} gguf files")
    return gguf_files


def _extract_files(data) -> List[Dict]:
    if not isinstance(data, dict):
        return []

    # 新版：{"data": {"files": [...]}}
    if "data" in data and isinstance(data["data"], dict):
        inner = data["data"]
        for key in ("files", "items", "list", "results"):
            val = inner.get(key)
            if isinstance(val, list):
                return val
        return []

    # 旧版：{"Data": {"Files": [...]}}
    d = data.get("Data")
    if isinstance(d, dict) and isinstance(d.get("Files"), list):
        return d["Files"]
    if isinstance(d, list):
        return d
    return []





def get_file_meta(model_id: str, filename: str) -> Dict:
    """
    查询远端单个文件的元信息（大小 / 最后修改时间 / 内容标识）。

    更新检测不只比大小 —— 有些仓库重新量化后大小不变（例如修了 bad token），
    只比 size 会漏判。这里同时返回 mtime 与 sha，任一变化都算"有更新"。
    """
    for f in get_gguf_files(model_id):
        if f.get("filename") == filename:
            return {
                "size": f.get("size"),
                "mtime": f.get("mtime"),
                "etag": f.get("etag") or "",
            }
    return {"size": None, "mtime": None, "etag": ""}
