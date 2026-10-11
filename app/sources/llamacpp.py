"""
llama.cpp 集成。

支持三种服务端形态：
  router      llama.cpp 路由模式（支持模型列表、加载/卸载、重新扫描模型源）
  llamaswap   llama-swap 代理
  server      普通 llama-server（单模型）

v1.3.0-fix3（本次修复 - 关键）：
  · ⭐ 路由模式重载前必须先调 GET /models?reload=1 让服务端重新扫描磁盘！
    原因：llama.cpp 路由模式只在启动时扫描一次 LLAMA_ARG_MODELS_DIR，
    运行期间不监听磁盘变化。新下载的模型文件虽然在磁盘上，但服务端内存里
    没有它的记录，POST /models/load 会直接返回 404 File Not Found。
  · 在 _reload_by_http() 里对 router/llamaswap 增加 pre-refresh 步骤，
    随后再选 id、再 load，一气呵成。
  · 保留之前所有修复：候选 id 多形式尝试 / 400 already running 幂等 /
    500 记录响应体 / 大模型加载超时宽容处理。
"""

import json
import os
import subprocess
from typing import Optional, Dict, List

import httpx

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

HTTP_CONNECT_TIMEOUT = 10.0
HTTP_READ_TIMEOUT = 30.0
LOAD_READ_TIMEOUT = 120.0


MODELS_LOAD_ENDPOINTS = ("/models/load",)
MODELS_UNLOAD_ENDPOINTS = ("/models/unload",)
MODELS_REFRESH_ENDPOINTS = ("/models?reload=1", "/models/?reload=1")
SERVER_RELOAD_ENDPOINTS = ("/update",)


def _headers() -> dict:
    h = {"User-Agent": "GGUF-Manager/1.3.0", "Accept": "application/json"}
    key = (settings.llamacpp_api_key or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _client(read_timeout: float = HTTP_READ_TIMEOUT, connect_timeout: float = HTTP_CONNECT_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=30.0,
            pool=10.0,
        ),
        headers=_headers(),
        follow_redirects=True,
    )


def _base() -> str:
    return (settings.llamacpp_url or "").rstrip("/")


def _resolve_flavor() -> str:
    configured = settings.llamacpp_flavor or "auto"
    if configured != "auto":
        return configured

    base = _base()
    if not base:
        return "unknown"

    with _client() as c:
        # 1) /models 是路由模式专属端点
        try:
            r = c.get(f"{base}/models")
            if r.status_code == 200:
                try:
                    body = r.json()
                    if isinstance(body, dict) and isinstance(body.get("data"), list):
                        return "router"
                except (ValueError, json.JSONDecodeError):
                    pass
        except httpx.HTTPError:
            pass

        # 2) /props 普通 server 独有（路由模式也暴露，必须排第二）
        try:
            r = c.get(f"{base}/props")
            if r.status_code == 200:
                return "server"
        except httpx.HTTPError:
            pass

        # 3) /v1/models 兜底
        try:
            r = c.get(f"{base}/v1/models")
            if r.status_code == 200:
                return "unknown"
        except httpx.HTTPError:
            pass

    return "unknown"


def probe() -> dict:
    base = _base()
    if not base:
        return {"ok": False, "reachable": False, "error": "未配置 llama.cpp 地址"}

    flavor = _resolve_flavor()

    with _client() as c:
        if flavor == "router":
            try:
                r = c.get(f"{base}/models")
                if r.status_code == 200:
                    return {"ok": True, "reachable": True, "endpoint": "/models", "flavor": "router"}
            except httpx.HTTPError:
                pass

        try:
            r = c.get(f"{base}/props")
            if r.status_code == 200:
                return {"ok": True, "reachable": True, "endpoint": "/props", "flavor": "server"}
        except httpx.HTTPError:
            pass

        try:
            r = c.get(f"{base}/v1/models")
            if r.status_code == 200:
                return {"ok": True, "reachable": True, "endpoint": "/v1/models", "flavor": flavor}
        except httpx.HTTPError:
            pass

        for ep in ("/health", "/slots"):
            try:
                r = c.get(f"{base}{ep}")
                if r.status_code == 200:
                    return {"ok": True, "reachable": True, "endpoint": ep, "flavor": "server"}
            except httpx.HTTPError:
                continue

    return {"ok": False, "reachable": False, "error": f"{base} 不可达", "flavor": flavor}


def discover_flavor(force_refresh: bool = False) -> dict:
    flavor = _resolve_flavor()
    if flavor == "unknown":
        return {"flavor": "unknown", "reason": "无法识别"}
    return {"flavor": flavor, "reason": "探测完成"}


# ============================================================
# 模型列表 / 刷新
# ============================================================
def _normalize_status(it: dict) -> str:
    st = it.get("status")
    if isinstance(st, dict):
        return st.get("value", "") or ""
    if isinstance(st, str):
        return st
    return ""


def _fetch_router_entries() -> List[dict]:
    base = _base()
    if not base:
        return []
    with _client() as c:
        try:
            r = c.get(f"{base}/models")
            if r.status_code == 200:
                items = ((r.json() or {}).get("data")) or []
                if isinstance(items, list):
                    return [it for it in items if isinstance(it, dict)]
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            pass
    return []


def list_models() -> dict:
    base = _base()
    if not base:
        return {"ok": False, "models": [], "error": "未配置地址"}

    flavor = _resolve_flavor()

    if flavor == "router":
        items = _fetch_router_entries()
        models = []
        for it in items:
            mid = it.get("id", "")
            if mid:
                models.append(
                    {
                        "id": mid,
                        "object": "model",
                        "status": _normalize_status(it),
                        "raw_id": mid,
                    }
                )
        return {"ok": True, "models": models, "flavor": "router"}

    with _client() as c:
        try:
            r = c.get(f"{base}/v1/models")
            if r.status_code == 200:
                body = r.json()
                items = (body or {}).get("data") or []
                models = []
                for it in items:
                    mid = it.get("id", "")
                    if mid:
                        clean_id = mid
                        for prefix in ("/models/", "./", "/"):
                            if clean_id.startswith(prefix):
                                clean_id = clean_id[len(prefix) :]
                        models.append(
                            {
                                "id": clean_id,
                                "object": it.get("object", "model"),
                                "created": it.get("created"),
                                "owned_by": it.get("owned_by"),
                                "raw_id": mid,
                            }
                        )
                if models:
                    return {"ok": True, "models": models, "flavor": flavor}
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            pass

        try:
            r = c.get(f"{base}/slots")
            if r.status_code == 200:
                slots = r.json()
                if isinstance(slots, list) and slots:
                    models = []
                    for slot in slots:
                        if isinstance(slot, dict):
                            mid = slot.get("model") or slot.get("model_filename") or ""
                            if mid:
                                models.append({"id": mid, "object": "model"})
                    if models:
                        return {"ok": True, "models": models, "flavor": flavor}
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            pass

    return {"ok": False, "models": [], "error": "无法获取模型列表"}


def refresh_models() -> dict:
    """让路由模式重新读取模型源。官方方式：GET /models?reload=1"""
    base = _base()
    if not base:
        return {"ok": False, "error": "未配置地址"}

    flavor = _resolve_flavor()

    if flavor == "server":
        return {
            "ok": False,
            "flavor": "server",
            "error": (
                "当前为普通 llama-server，不支持「刷新模型源」操作。\n"
                "方案 A：改用路由模式（移除 -m/--model，保留 --models-dir）\n"
                "方案 B：用 docker restart 命令重启"
            ),
            "hint": "use_docker_restart",
        }

    results = {}
    with _client() as c:
        for path in MODELS_REFRESH_ENDPOINTS:
            try:
                r = c.get(base + path)
                results[path] = r.status_code
                if r.status_code == 200:
                    try:
                        body = r.json()
                        items = (body or {}).get("data") or []
                        models = [{"id": it.get("id", ""), "status": _normalize_status(it)} for it in items if isinstance(it, dict)]
                    except (ValueError, json.JSONDecodeError):
                        models = []
                    return {
                        "ok": True,
                        "method": "refresh",
                        "endpoint": path,
                        "flavor": flavor,
                        "models": models,
                        "models_count": len(models),
                    }
            except httpx.HTTPError as e:
                results[path] = f"error: {e}"
                continue

    return {"ok": False, "flavor": flavor, "tried": results, "error": f"刷新失败（{flavor}）：{results}"}


# ============================================================
# 重载
# ============================================================
def reload(model_path: Optional[str] = None, reason: str = "manual") -> dict:
    """
    按配置的方式触发重载。

    🔥 路由模式：先 refresh 后 load（_reload_by_http 内部处理）
    🖥️ 普通 server：尝试 /update，失败降级到 docker restart
    🔀 llama-swap：同 router
    """
    base = _base()
    mode = settings.llamacpp_reload_mode or "http"

    if mode == "none":
        return {"ok": True, "skipped": True, "reason": "已配置为不重载"}

    if mode == "command":
        return _reload_by_command(model_path)

    if mode == "http":
        result = _reload_by_http(base, model_path)
        if not result.get("ok") and _has_docker_cmd():
            logger.info("[llamacpp] HTTP reload failed, trying docker restart as fallback")
            docker_result = _reload_by_command(model_path)
            if docker_result.get("ok"):
                return {**docker_result, "fallback": True, "fallback_reason": result.get("error", "HTTP 重载失败")}
        return result

    if mode == "auto":
        http_result = _reload_by_http(base, model_path)
        if http_result.get("ok"):
            return http_result
        if _has_docker_cmd():
            cmd_result = _reload_by_command(model_path)
            if cmd_result.get("ok"):
                return {**cmd_result, "fallback": True}
        return http_result

    return {"ok": False, "error": f"未知的重载方式：{mode}"}


def _has_docker_cmd() -> bool:
    cmd = (settings.llamacpp_reload_cmd or "").strip()
    return bool(cmd) and ("docker" in cmd or "podman" in cmd or "restart" in cmd)


def _pick_model_ids(model_path: Optional[str]) -> List[str]:
    """
    返回候选 model id 列表（按优先级）。

    ⚠️ 必须在 pre-refresh 之后调用，这样 _fetch_router_entries()
       才能拿到包含新下载模型的 id 列表。
    """
    entries = _fetch_router_entries()
    available = [it.get("id", "") for it in entries if it.get("id")]

    result: List[str] = []
    seen: set = set()

    def add(x: str) -> None:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            result.append(x)

    if model_path:
        from pathlib import Path

        p = Path(model_path)
        stem = p.stem
        name = p.name

        add(model_path)
        add(name)
        add(stem)
        if not stem.lower().endswith(".gguf"):
            add(stem + ".gguf")
        if not name.lower().endswith(".gguf"):
            add(name + ".gguf")

        # 在 /models 返回的 id 里做模糊匹配，把命中的提前
        lower_stem = stem.lower()
        lower_name = name.lower()
        for mid in available:
            mid_lower = mid.lower()
            mid_stem = mid[:-5] if mid_lower.endswith(".gguf") else mid
            mid_stem_lower = mid_stem.lower()
            if (
                mid == stem
                or mid == name
                or mid_stem == stem
                or mid_stem == name
                or mid_stem_lower == lower_stem
                or mid_stem_lower == lower_name
                or mid_lower == lower_stem
                or lower_stem in mid_stem_lower
                or mid_stem_lower in lower_name
            ):
                add(mid)
    else:
        # 未指定：优先选 unloaded（换一个跑，避免 already running）
        unloaded = [it for it in entries if _normalize_status(it) in ("unloaded", "not_loaded", "idle")]
        if unloaded:
            add(unloaded[0].get("id", ""))
        for it in entries:
            add(it.get("id", ""))
        for it in list(entries):
            mid = it.get("id", "")
            if mid and not mid.lower().endswith(".gguf"):
                add(mid + ".gguf")

    return result


def _parse_server_error(text: str) -> str:
    try:
        obj = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return ""
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")
        if isinstance(err, str):
            return err
        if "message" in obj:
            return str(obj["message"])
    return ""


def _shorten(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _reload_by_http(base: str, model_path: Optional[str] = None) -> dict:
    """
    通过 HTTP 端点触发重载。

    ⭐ 核心改进（v1.3.0-fix3）：
       路由模式下 **先 refresh，再 load**。
       llama.cpp 路由模式只在启动时扫描一次 LLAMA_ARG_MODELS_DIR，
       运行期间不监听磁盘变化。新下载的模型必须通过
       GET /models?reload=1 让服务端重新扫描，否则 POST /models/load
       永远报 404 File Not Found。
    """
    if not base:
        return {"ok": False, "error": "未配置地址"}

    flavor = _resolve_flavor()

    # ⭐⭐⭐ 关键：路由模式先 refresh，让服务端重新扫描磁盘
    pre_refresh: Optional[dict] = None
    if flavor in ("router", "llamaswap"):
        logger.info(f"[llamacpp] router flavor detected, " f"pre-refresh via GET /models?reload=1 before load")
        try:
            pre_refresh = refresh_models()
            logger.info(f"[llamacpp] pre-refresh result: ok={pre_refresh.get('ok')}, " f"models_count={pre_refresh.get('models_count', '?')}")
        except Exception as e:
            logger.warning(f"[llamacpp] pre-refresh failed: {e}")
            pre_refresh = {"ok": False, "error": str(e)}

    # ⭐ 在 refresh 之后再选 id（此时 /models 已包含新下载的模型）
    candidate_ids = _pick_model_ids(model_path)
    logger.info(f"[llamacpp] reload: flavor={flavor}, model_path={model_path!r}, " f"candidates={candidate_ids}")

    if not candidate_ids and flavor in ("router", "llamaswap"):
        return {
            "ok": False,
            "flavor": flavor,
            "hint": "empty_model_id",
            "error": (
                "无法解析要加载的模型 id。\n"
                f"  1. 手动验证：curl '{base}/models'\n"
                f"  2. 若 data 为空，先执行「重新扫描」\n"
                f"  3. 确认新模型已在服务端 LLAMA_ARG_MODELS_DIR 中"
            ),
            "method": "http",
            "pre_refresh": pre_refresh,
        }

    all_responses: List[Dict] = []
    first_500_id: Optional[str] = None

    with _client(read_timeout=LOAD_READ_TIMEOUT) as c:
        for ep in MODELS_LOAD_ENDPOINTS:
            for mid in candidate_ids or [""]:
                payload = {"model": mid} if flavor in ("router", "llamaswap") else ({"path": model_path} if model_path else {})
                if not payload.get("model") and not payload.get("path"):
                    continue

                logger.info(f"[llamacpp] POST {base}{ep} payload={payload}")
                try:
                    r = c.post(base + ep, json=payload)
                    body = r.text or ""
                    all_responses.append(
                        {
                            "endpoint": ep,
                            "model_id": mid,
                            "status": r.status_code,
                            "body": _shorten(body, 500),
                        }
                    )

                    # 成功
                    if r.status_code in (200, 202):
                        logger.info(f"[llamacpp] reload OK: {ep} id={mid}")
                        return {
                            "ok": True,
                            "status_code": r.status_code,
                            "response": _shorten(body, 300),
                            "method": "http",
                            "endpoint": ep,
                            "flavor": flavor,
                            "model_id": mid,
                            "no_restart": True,
                            "pre_refresh": pre_refresh,
                        }

                    # 400：可能是 already running（幂等成功），也可能是 id 不对
                    if r.status_code == 400:
                        msg = _parse_server_error(body)
                        if "already running" in msg.lower() or "already loaded" in msg.lower():
                            logger.info(f"[llamacpp] {mid} already running -> success")
                            return {
                                "ok": True,
                                "status_code": 200,
                                "response": msg,
                                "method": "http",
                                "endpoint": ep,
                                "flavor": flavor,
                                "model_id": mid,
                                "no_restart": True,
                                "already_running": True,
                                "pre_refresh": pre_refresh,
                            }
                        logger.warning(f"[llamacpp] {ep} 400 id={mid} msg={msg!r}")
                        continue

                    # 500 / 其他：记下，试下一个
                    if r.status_code >= 500 and first_500_id is None:
                        first_500_id = mid
                        logger.warning(f"[llamacpp] {ep} {r.status_code} id={mid} " f"body={_shorten(body, 200)!r}")
                    else:
                        logger.debug(f"[llamacpp] {ep} {r.status_code} id={mid}, try next")

                except httpx.ReadTimeout:
                    logger.warning(f"[llamacpp] {ep} read timeout id={mid} " f"(server may still be loading)")
                    return {
                        "ok": True,
                        "status_code": 202,
                        "response": "(read timeout; server may still be loading)",
                        "method": "http",
                        "endpoint": ep,
                        "flavor": flavor,
                        "model_id": mid,
                        "no_restart": True,
                        "timeout_but_accepted": True,
                        "pre_refresh": pre_refresh,
                    }

                except httpx.HTTPError as e:
                    all_responses.append(
                        {
                            "endpoint": ep,
                            "model_id": mid,
                            "status": None,
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                    continue

    # 全部候选都失败了 → 若首次失败是 500，试试 unload + load 兜底
    if first_500_id:
        logger.info(f"[llamacpp] all candidates failed, trying unload+load for {first_500_id!r}")
        retry = _try_unload_then_load(base, first_500_id)
        if retry:
            return {**retry, "flavor": flavor, "pre_refresh": pre_refresh}

    # 拼详细错误
    detail_lines = []
    for rr in all_responses:
        ep = rr.get("endpoint", "?")
        mid = rr.get("model_id", "")
        if rr.get("status") is not None:
            msg = _parse_server_error(rr.get("body", ""))
            extra = f"（{msg}）" if msg else ""
            detail_lines.append(f"  · id={mid!r} → {ep} HTTP {rr['status']}{extra}\n" f"      响应体：{rr.get('body') or '(空)'}")
        else:
            detail_lines.append(f"  · id={mid!r} → {ep} {rr.get('error', 'unknown')}")
    detail_text = "\n".join(detail_lines) or "  (无)"

    any_404 = any(rr.get("status") == 404 for rr in all_responses)

    hint = ""
    if any_404:
        hint = (
            "\n\n⚠️ 收到 404 File Not Found —— 服务端不认识这个模型 id。\n"
            "  常见原因：\n"
            "    1. 服务端模型源尚未刷新（本程序已在 load 前调用 "
            "GET /models?reload=1，若仍 404 则说明刷新也失败）\n"
            "    2. 新模型文件不在服务端的 LLAMA_ARG_MODELS_DIR 目录下\n"
            f"    3. 手动验证：curl '{base}/models?reload=1' 再看返回的 id 列表"
        )

    avail = [it.get("id") for it in _fetch_router_entries() if it.get("id")]
    ids_hint = ""
    if avail:
        ids_hint = "\n\n服务端当前可用的模型 id：\n" + "\n".join(f"  · {mid}" for mid in avail)

    return {
        "ok": False,
        "method": "http",
        "flavor": flavor,
        "model_id": candidate_ids[0] if candidate_ids else "",
        "tried_ids": candidate_ids,
        "raw_responses": all_responses,
        "pre_refresh": pre_refresh,
        "error": (f"HTTP 重载失败（flavor={flavor}）。\n\n" f"尝试的候选 id 与响应：\n{detail_text}" f"{hint}{ids_hint}"),
    }


def _try_unload_then_load(base: str, model_id: str) -> Optional[dict]:
    """兜底：先 unload 再 load。用于应对 models-max 满/状态卡死。"""
    with _client(read_timeout=LOAD_READ_TIMEOUT) as c:
        for ep in MODELS_UNLOAD_ENDPOINTS:
            try:
                r = c.post(base + ep, json={"model": model_id})
                logger.info(f"[llamacpp] unload {ep} model={model_id} -> {r.status_code}")
            except httpx.HTTPError as e:
                logger.debug(f"[llamacpp] unload {ep} error: {e}")
            break

        import time as _t

        _t.sleep(1.0)

        for ep in MODELS_LOAD_ENDPOINTS:
            try:
                r = c.post(base + ep, json={"model": model_id})
                if r.status_code in (200, 202):
                    return {
                        "ok": True,
                        "status_code": r.status_code,
                        "response": _shorten(r.text, 300),
                        "method": "http",
                        "endpoint": ep,
                        "model_id": model_id,
                        "no_restart": True,
                        "retried_after_unload": True,
                    }
            except httpx.HTTPError as e:
                logger.debug(f"[llamacpp] load-after-unload {ep} error: {e}")
    return None


def _reload_by_command(model_path: Optional[str] = None) -> dict:
    cmd_template = settings.llamacpp_reload_cmd
    if not cmd_template:
        return {
            "ok": False,
            "hint": "configure_docker_cmd",
            "error": "未配置重载命令（设置 → 重载方式 → Shell 命令）",
        }

    model_name = os.path.basename(model_path) if model_path else ""
    container = ""
    parts = cmd_template.split()
    for i, p in enumerate(parts):
        if p in ("restart", "stop", "start") and i + 1 < len(parts):
            container = parts[i + 1]
            break

    cmd = cmd_template.format(
        model=model_name,
        model_path=model_path or "",
        model_name=model_name,
        container=container,
    )
    logger.info(f"[llamacpp] executing reload command: {cmd}")

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return {
            "ok": result.returncode == 0,
            "method": "command",
            "command": cmd,
            "container": container,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
            "error": ("" if result.returncode == 0 else f"命令退出码 {result.returncode}"),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "command", "command": cmd, "error": "命令执行超时（60秒）", "container": container}
    except Exception as e:
        return {"ok": False, "method": "command", "command": cmd, "error": str(e), "container": container}


# ============================================================
# 名称推断
# ============================================================
def match_remote_name(local_path: str) -> Optional[str]:
    try:
        res = list_models()
        if not res.get("ok"):
            return None
        from pathlib import Path

        stem = Path(local_path).stem
        for m in res.get("models", []):
            mid = m.get("id", "")
            if stem and (stem in mid or mid == stem):
                return mid
    except Exception:
        pass
    return None


def local_name_candidates(local_path: str) -> List[str]:
    from pathlib import Path

    p = Path(local_path)
    return [p.stem, p.name, str(p)]


def maybe_reload_after_download(local_path: str) -> dict:
    if not settings.llamacpp_enabled:
        return {"ok": True, "skipped": True, "reason": "llama.cpp 集成未启用"}
    if not settings.llamacpp_reload_on_download:
        return {"ok": True, "skipped": True, "reason": "未开启下载后自动重载"}
    return reload(local_path, reason="download")
