"""
ModelScope 源后端。

接口与字段说明（易踩坑，逐条列明）：
1) 搜索走 /openapi/v1/models 的 snake_case 参数：search / page_size / page_number / sort。
   用 PascalCase（Name/PageSize/PageNumber/SortBy）会被 OpenAPI 当作未知参数忽略，
   等价于“不带关键词”返回默认列表。
2) 模型 ID 取官方字段 `id`；path/Path/name/Name 只出现在旧接口里。
   若取不到值又紧跟 `if not path or "/" not in path: continue`，
   会把每个模型都丢掉，搜索变成永远返回 0 条。
3) GGUF 判定兼容官方 `tasks` 字段（OpenAPI 返回 tasks 而非 tags），
   并把“仓库名是否含 gguf”放在最前作为最可靠的判据。
4) 服务端搜索生效性校验看“整页命中数”，不能只看第一条 ——
   否则第一条恰好不匹配就会误判，且只翻一页就 break。

体积信息：搜索接口不返回文件大小，为保证搜索响应速度不在搜索时并发补拉，
改由前端调 /api/search/sizes 批量补全。文件列表走
/api/v1/models/{id}/repo/files，返回 Data.Files[].{Path,Size}。
"""

from typing import List, Dict, Optional

import httpx

from app.logger import get_logger
from app.sources import SourceBackend, TTLCache, normalize_model_card
from app.utils import coerce_timestamp, sum_sizes

logger = get_logger(__name__)


# ============================================================
# 常量
# ============================================================
MS_BASE = "https://modelscope.cn"
MS_SEARCH_URL = f"{MS_BASE}/openapi/v1/models"
MS_FILES_URL = f"{MS_BASE}/api/v1/models/{{model_id}}/repo/files"

MAX_PAGE_SIZE = 50
MAX_PAGE_NUMBER = 20
HTTP_TIMEOUT = 30.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


class ModelScopeBackend(SourceBackend):
    name = "modelscope"

    # 文件列表缓存：同一个模型在一次「搜索 → 浏览 → 点开下载」里会被查两三次，
    # 而每次都要全量拉一遍 /repo/files（大仓库有上百个文件）。
    _files_cache = TTLCache(ttl=60.0, maxsize=512)

    # ============================================================
    # 搜索
    # ============================================================
    def search_models(self, query: str, gguf_only: bool = True, limit: int = 50) -> List[Dict]:
        logger.info(
            f"[MS] search_models: query={query!r}, gguf_only={gguf_only}, limit={limit}"
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

            data = self._get_json(MS_SEARCH_URL, params)
            models_raw = self._extract_models(data)
            if not models_raw:
                logger.info(f"[MS] page {page_number} returned 0 models, stop")
                break

            if total_count is None:
                total_count = self._get_total_count(data)

            scanned += len(models_raw)

            # 服务端搜索生效性校验：整页都没一条命中才降级为客户端过滤
            hits = [m for m in models_raw if self._match_query(m, query)]
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
                    model_id = self._model_id(m)
                    if not model_id or "/" not in model_id or model_id in seen:
                        continue

                    has_gguf = self._detect_gguf(m, model_id)
                    if gguf_only and not has_gguf:
                        continue

                    seen.add(model_id)
                    results.append(normalize_model_card(
                        model_id=model_id,
                        source="modelscope",
                        downloads=self._get_field(m, "downloads", "Downloads") or 0,
                        likes=self._get_field(m, "likes", "Likes", "stars", "Stars") or 0,
                        last_modified=str(
                            self._get_field(
                                m, "updated_at", "UpdatedAt", "last_updated_time",
                                "LastUpdatedTime", "lastModified",
                            ) or ""
                        ),
                        tags=self._get_field(m, "tags", "Tags", "tasks", "Tasks") or [],
                        has_gguf=has_gguf,
                    ))
                except Exception as e:
                    logger.warning(f"[MS] skip model due to parse error: {e}")

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
    def get_model_size(self, model_id: str) -> Dict:
        """查单模型 GGUF 总大小（前端懒加载）。"""
        logger.debug(f"[MS] get_model_size: model_id={model_id!r}")
        files = self.get_gguf_files(model_id)
        return {"gguf_count": len(files), "total_size": sum_sizes(f.get("size") for f in files)}

    # ============================================================
    # 文件列表
    # ============================================================
    def get_gguf_files(self, model_id: str) -> List[Dict]:
        """
        获取 ModelScope 模型仓库中的 GGUF 文件列表（含大小）。

        接口：GET /api/v1/models/{model_id}/repo/files?Revision=master&Recursive=true
        返回：{"Data": {"Files": [{"Name","Path","Size","Type","Sha256"}, ...]}}

        带 60s TTL 缓存：搜索补全体积和「查看」文件列表会查同一个模型，
        没必要拉两遍。
        """
        cached, hit = self._files_cache.get(model_id)
        if hit:
            logger.debug(f"[MS] get_gguf_files: cache hit, {len(cached)} files")
            return cached

        logger.debug(f"[MS] get_gguf_files: model_id={model_id!r}")

        url = MS_FILES_URL.format(model_id=model_id)
        params = {"Revision": "master", "Recursive": "true"}

        data = self._get_json(url, params)
        files = self._extract_files(data)
        gguf_files: List[Dict] = []

        for f in files:
            if str(self._get_field(f, "type", "Type") or "").lower() == "tree":
                continue

            name = self._get_field(f, "path", "Path", "name", "Name")
            if not name or not name.endswith(".gguf"):
                continue

            size = self._get_field(f, "size", "Size")
            gguf_files.append({
                "filename": name,
                "size": int(size) if isinstance(size, (int, float)) else None,
                "mtime": coerce_timestamp(self._get_field(
                    f, "last_modified", "LastModified", "updated_at",
                    "UpdatedAt", "lastModified", "mtime", "Mtime",
                )),
                "etag": str(self._get_field(
                    f, "sha256", "Sha256", "sha", "Sha", "etag", "ETag"
                ) or ""),
            })

        gguf_files.sort(key=lambda x: x["filename"])
        logger.debug(f"[MS] get_gguf_files: found {len(gguf_files)} gguf files")
        self._files_cache.set(model_id, gguf_files)
        return gguf_files

    def get_file_meta(self, model_id: str, filename: str) -> Dict:
        """
        取远端单文件元信息。

        更新检测不只比大小 —— 有些仓库重新量化后大小不变（如修了 bad token），
        只比 size 会漏判。这里同时返回 mtime 与 sha，任一变化都算“有更新”。
        """
        for f in self.get_gguf_files(model_id):
            if f.get("filename") == filename:
                return {"size": f.get("size"), "mtime": f.get("mtime"), "etag": f.get("etag") or ""}
        return {"size": None, "mtime": None, "etag": ""}

    # ============================================================
    # 内部工具
    # ============================================================
    def _get_json(self, url: str, params: dict) -> dict:
        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True
            ) as client:
                resp = client.get(url, params=params)
                logger.debug(f"[MS] GET {resp.url} status={resp.status_code}")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"[MS] HTTP {e.response.status_code} - {e.response.text[:200]}"
            )
            raise
        except Exception:
            logger.exception("[MS] request failed")
            raise

    @staticmethod
    def _get_field(obj: dict, *keys):
        """按顺序尝试多个字段名，返回第一个非空值。"""
        for k in keys:
            v = obj.get(k)
            if v is not None and v != "":
                return v
        return None

    @staticmethod
    def _model_id(m: dict) -> str:
        """取模型 ID：官方 OpenAPI 为 `id`，旧版 /api/v1/ 为 path/Path/name/Name。"""
        return str(ModelScopeBackend._get_field(
            m, "id", "Id", "path", "Path", "name", "Name"
        ) or "").strip()

    @staticmethod
    def _match_query(m: dict, query: str) -> bool:
        mid = ModelScopeBackend._model_id(m)
        return bool(mid) and query.lower() in mid.lower()

    @staticmethod
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

    @staticmethod
    def _detect_gguf(m: dict, model_id: str) -> bool:
        """判断模型是否含 GGUF。"""
        if "gguf" in model_id.lower():
            return True
        for key in ("tags", "Tags", "tasks", "Tasks", "labels", "Labels",
                    "categories", "Categories"):
            v = m.get(key)
            if isinstance(v, (list, tuple)):
                if any("gguf" in str(t).lower() for t in v):
                    return True
            elif v and "gguf" in str(v).lower():
                return True
        for key in ("library_name", "LibraryName", "pipeline_tag", "PipelineTag",
                    "model_type", "ModelType", "framework", "Framework"):
            v = m.get(key)
            if v and "gguf" in str(v).lower():
                return True
        return False

    @staticmethod
    def _extract_models(data) -> List[Dict]:
        """兼容新版 OpenAPI（{"data": {"models": [...]}}）与旧版（{"Data": {...}}）。"""
        if not isinstance(data, dict):
            logger.warning(f"[MS] unexpected response type: {type(data).__name__}")
            return []

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
                    f"[MS] cannot locate models in data, keys={list(inner.keys())}"
                )
                return []

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

    @staticmethod
    def _extract_files(data) -> List[Dict]:
        if not isinstance(data, dict):
            return []
        if "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            for key in ("files", "items", "list", "results"):
                val = inner.get(key)
                if isinstance(val, list):
                    return val
            return []
        d = data.get("Data")
        if isinstance(d, dict) and isinstance(d.get("Files"), list):
            return d["Files"]
        if isinstance(d, list):
            return d
        return []


backend = ModelScopeBackend()
