# app/services/llamacpp.py
"""
llama.cpp 控制。

支持三种服务端形态（配置项 llamacpp_flavor，默认 auto 自动探测）：

  router    llama.cpp 路由模式（llama-server --models-preset / --models-dir）
              GET  /models            列出模型及其状态
              POST /models/load       {"model": name}  加载指定模型
              POST /models/unload     {"model": name}  卸载指定模型
              GET  /models?reload=1   重新读取模型源 —— 新增/删除模型文件后必须调用
  llamaswap llama-swap 代理
              GET  /v1/models         列出模型
              POST /api/models/unload/{id}  卸载（加载由推理请求自动触发）
  server    普通单模型 llama-server
              只能走 http / command / none 三种重载策略

为什么必须有"重新扫描"这一步：
  路由模式在启动时读取一次模型源（preset ini 或目录），之后不再监听磁盘。
  所以在 GGUF-Manager 里新增或删掉一个模型后，服务端列表不会变——
  必须请求 /models?reload=1 才会"changed or removed models are unloaded,
  new ones are added"。这正是"新增、删除模型后服务端没更新"的根因。

模型名怎么来：
  路由模式认的是 preset 里的 section 名 / 目录下文件名，不是本容器内的绝对路径。
  所以先从服务端拉列表（GET /models），再用本地文件名去匹配，
  匹配不到才回退到配置项推导的候选名依次尝试。
"""

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

# 路由模式重新扫描的等待时间：模型卸载/加载是异步的，给一点缓冲
RELOAD_SETTLE_SECONDS = 1.5


def _headers() -> Dict[str, str]:
    h = {"User-Agent": "GGUF-Manager/1.2", "Accept": "application/json"}
    if settings.llamacpp_api_key:
        h["Authorization"] = f"Bearer {settings.llamacpp_api_key}"
    return h


def _base_url() -> str:
    return str(settings.llamacpp_url or "").rstrip("/")


def _timeout() -> float:
    return float(settings.llamacpp_timeout or 10)


def _extract_error(resp) -> str:
    """从响应里抽取人类可读的错误信息（不同实现的错误格式不一样）。"""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:300]

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        for k in ("message", "detail", "msg"):
            if data.get(k):
                return str(data[k])[:300]
    return json.dumps(data)[:300]


def _client() -> httpx.Client:
    return httpx.Client(timeout=_timeout(), headers=_headers(), follow_redirects=True)


# ============================================================
# 服务端类型探测
# ============================================================
def probe() -> Dict:
    """
    探测 llama.cpp server 是否可达。

    依次尝试 /props、/health、/，任一返回非 5xx 即认为在线。

    ⚠️ 这个函数曾在重构时被整个删掉，而 router 里还在调用它，
       结果抛 AttributeError → FastAPI 返回 HTML 的 Internal Server Error →
       前端 res.json() 报 "Unexpected token 'I', "Internal S"..."。
       所以这里对非 JSON 响应做了充分兜底：解析失败就按纯文本返回。
    """
    base = _base_url()
    if not base:
        return {"ok": False, "reachable": False, "error": "未配置 llama.cpp 地址"}

    last_err = None

    for path in ("/props", "/health", "/"):
        try:
            with _client() as c:
                resp = c.get(f"{base}{path}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

        # 探活只关心"能不能连上"，4xx 也说明服务活着（例如 /props 需要 POST）
        if resp.status_code < 500:
            return {
                "ok": 200 <= resp.status_code < 300,
                "reachable": True,
                "status_code": resp.status_code,
                "endpoint": path,
                "flavor": current_flavor(),
                "detail": _extract_error(resp) or (resp.text or "")[:200],
            }
        last_err = f"HTTP {resp.status_code}: {_extract_error(resp)}"

    return {
        "ok": False,
        "reachable": False,
        "error": last_err or "unreachable",
        "hint": "检查服务地址与端口，以及容器网络是否可达",
    }


def _looks_like_model_list(data) -> Optional[List[Dict]]:
    """判断响应是不是 OpenAI 风格的模型列表。"""
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    return None


def discover_flavor(force_refresh: bool = False) -> Dict:
    """
    探测对面到底是哪种服务端。

    顺序很关键：先用 /models 与 /v1/models 判断是不是路由模式/代理，
    都拿不到像样的模型列表，才认为是普通 llama-server。
    """
    base = _base_url()
    if not base:
        return {"flavor": "unknown", "error": "未配置 llama.cpp 地址"}

    if not force_refresh:
        configured = str(settings.llamacpp_flavor or "auto").lower()
        if configured in ("router", "llamaswap", "server"):
            return {"flavor": configured, "source": "configured"}

    probes = [
        # (候选 flavor, 路径)
        ("router", "/models"),
        ("llamaswap", "/v1/models"),
        ("router", "/v1/models"),
    ]

    for flavor, path in probes:
        try:
            with _client() as c:
                resp = c.get(f"{base}{path}")
            if resp.status_code != 200:
                continue
            items = _looks_like_model_list(resp.json())
            if items is None:
                continue
            names = [str(i.get("id") or i.get("name") or "") for i in items
                     if isinstance(i, dict)]
            names = [n for n in names if n]
            if names:
                return {"flavor": flavor, "source": "probe", "endpoint": path,
                        "models": names}
        except Exception as e:
            logger.debug(f"[llamacpp] probe {path} failed: {e}")
            continue

    # 有 /props 但没有模型列表 → 普通 llama-server
    try:
        with _client() as c:
            resp = c.get(f"{base}/props")
        if resp.status_code < 500:
            return {"flavor": "server", "source": "probe", "endpoint": "/props"}
    except Exception as e:
        logger.debug(f"[llamacpp] probe /props failed: {e}")

    return {"flavor": "unknown", "error": "无法识别服务端类型"}


def current_flavor() -> str:
    """返回生效的服务端类型（配置优先，auto 才探测）。"""
    configured = str(settings.llamacpp_flavor or "auto").lower()
    if configured in ("router", "llamaswap", "server"):
        return configured
    return str(discover_flavor().get("flavor") or "unknown")


# ============================================================
# 模型列表
# ============================================================
def list_models(refresh: bool = False) -> Dict:
    """
    拉取服务端当前的模型列表。

    refresh=True 时请求 /models?reload=1，让路由模式重新读取磁盘上的
    模型源——新增或删除 GGUF 之后必须走这一步，否则列表不会更新。
    """
    base = _base_url()
    if not base:
        return {"ok": False, "error": "未配置 llama.cpp 地址", "models": []}

    flavor = current_flavor()
    paths = {
        "router": ["/models", "/v1/models"],
        "llamaswap": ["/v1/models", "/models"],
        "server": ["/v1/models", "/models"],
    }.get(flavor, ["/models", "/v1/models"])

    last_err = None
    for p in paths:
        url = f"{base}{p}"
        if refresh and p == "/models":
            url = f"{base}/models?reload=1"
        try:
            with _client() as c:
                resp = c.get(url)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {_extract_error(resp)}"
                continue
            items = _looks_like_model_list(resp.json())
            if items is None:
                last_err = f"{p} 未返回模型列表"
                continue

            models = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("id") or it.get("name") or "")
                if not name:
                    continue
                status = it.get("status")
                if isinstance(status, dict):
                    status = status.get("value") or status.get("state") or ""
                models.append({"id": name, "status": str(status or "")})

            return {
                "ok": True,
                "flavor": flavor,
                "endpoint": p,
                "refreshed": bool(refresh),
                "models": models,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

    return {"ok": False, "flavor": flavor, "error": last_err or "无法获取模型列表",
            "models": []}


def refresh_models() -> Dict:
    """
    让路由模式重新读取模型源（新增/删除模型后调用）。

    返回重新扫描后的模型列表，便于界面直接刷新展示。
    """
    result = list_models(refresh=True)
    if not result.get("ok"):
        return result

    # 路由模式的卸载/加载是异步的，稍等一下再取一次，状态才稳定。
    # 二次查询不带 reload，所以要把"确实扫描过"这个事实单独记下来，
    # 否则最终返回的 refreshed=False 会让界面误以为没扫描。
    time.sleep(RELOAD_SETTLE_SECONDS)
    settled = list_models(refresh=False)
    if settled.get("ok"):
        settled["scanned"] = True
        return settled
    result["scanned"] = True
    return result


# ============================================================
# 加载 / 卸载
# ============================================================
def _post_json(path: str, payload: Dict, ok_codes=(200, 201, 202, 204)) -> Dict:
    base = _base_url()
    url = f"{base}{path}"
    try:
        with _client() as c:
            resp = c.post(url, json=payload)
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}

    if resp.status_code in ok_codes:
        return {"ok": True, "url": url, "status_code": resp.status_code,
                "detail": _extract_error(resp) or resp.text[:200]}
    return {"ok": False, "url": url, "status_code": resp.status_code,
            "error": _extract_error(resp) or resp.text[:200]}


def load_model(name: str) -> Dict:
    """要求服务端加载指定模型（路由模式 / llama-swap）。"""
    if not name:
        return {"ok": False, "error": "模型名为空"}

    flavor = current_flavor()

    if flavor == "llamaswap":
        # llama-swap 没有独立的 load 端点，通过 /upstream/{id}/load 触发
        r = _post_json(f"/upstream/{name}/load", {})
        if r.get("ok"):
            return {**r, "model": name, "flavor": flavor}
        # 回退：发一个极短的推理请求，靠自动加载把它拉起来
        try:
            with _client() as c:
                resp = c.post(
                    f"{_base_url()}/v1/chat/completions",
                    json={"model": name, "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 1},
                )
            if resp.status_code == 200:
                return {"ok": True, "model": name, "flavor": flavor,
                        "method": "chat-completion", "status_code": 200}
            return {"ok": False, "model": name, "flavor": flavor,
                    "status_code": resp.status_code,
                    "error": _extract_error(resp) or resp.text[:200]}
        except Exception as e:
            return {"ok": False, "model": name, "flavor": flavor,
                    "error": f"{type(e).__name__}: {e}"}

    # router / server：POST /models/load
    r = _post_json("/models/load", {"model": name})
    if r.get("ok"):
        return {**r, "model": name, "flavor": flavor}

    # 有些版本挂在 /v1/models/load 下
    r2 = _post_json("/v1/models/load", {"model": name})
    if r2.get("ok"):
        return {**r2, "model": name, "flavor": flavor}

    return {**r, "model": name, "flavor": flavor, "tried": ["/models/load", "/v1/models/load"]}


def unload_model(name: str) -> Dict:
    """卸载指定模型（删除本地文件后用它把服务端上的实例也停掉）。"""
    if not name:
        return {"ok": False, "error": "模型名为空"}

    flavor = current_flavor()

    if flavor == "llamaswap":
        r = _post_json(f"/api/models/unload/{name}", {})
        if r.get("ok"):
            return {**r, "model": name, "flavor": flavor}
        r2 = _post_json("/api/models/unload", {"model": name})
        return {**r2, "model": name, "flavor": flavor}

    r = _post_json("/models/unload", {"model": name})
    if r.get("ok"):
        return {**r, "model": name, "flavor": flavor}
    r2 = _post_json("/v1/models/unload", {"model": name})
    return {**r2, "model": name, "flavor": flavor}


# ============================================================
# 模型名推导与匹配
# ============================================================
def local_name_candidates(model_path: Optional[str]) -> List[str]:
    """从本地文件路径推导出可能的模型名候选（按优先级）。"""
    if not model_path:
        return []

    p = Path(model_path)
    filename = p.name

    stem = filename
    for suffix in (".gguf.part", ".gguf"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        stem = p.stem

    try:
        rel = str(p.resolve().relative_to(Path(str(settings.MODEL_DIR)).resolve()))
    except Exception:
        rel = filename

    mode = str(settings.llamacpp_model_name_mode or "stem").lower()
    primary = {
        "stem": stem,
        "filename": filename,
        "relative": rel,
        "path": str(p),
    }.get(mode, stem)

    prefix = str(settings.llamacpp_model_prefix or "").strip()

    def _join(pref: str, name: str) -> str:
        return f"{pref.rstrip('/')}/{name.lstrip('/')}" if pref else name

    raw: List[str] = []
    if prefix:
        raw.append(_join(prefix, primary))
    raw.append(primary)
    raw.append(filename)
    if stem not in raw:
        raw.append(stem)

    out, seen = [], set()
    for c in raw:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def match_remote_name(model_path: Optional[str]) -> Optional[str]:
    """
    拿本地文件去服务端的模型列表里找对应名字。

    优先精确匹配（忽略大小写与分隔符差异），再退回包含匹配。
    这样 preset 里的 section 名和文件名不一致时也能对上。
    """
    if not model_path:
        return None

    remote = list_models().get("models") or []
    if not remote:
        return None

    ids = [m["id"] for m in remote]
    for cand in local_name_candidates(model_path):
        nc = _norm(cand)
        for rid in ids:
            if _norm(rid) == nc:
                return rid

    # 包含匹配：如服务端叫 qwen3-1.7b，本地叫 Qwen3-1.7B-Q4_K_M.gguf
    for cand in local_name_candidates(model_path):
        nc = _norm(cand)
        if not nc:
            continue
        for rid in ids:
            nr = _norm(rid)
            if nr and (nr in nc or nc in nr):
                return rid
    return None


# ============================================================
# 通用 http / command 重载（单模型 llama-server 用）
# ============================================================
def _reload_http(model_path: Optional[str] = None) -> Dict:
    base = _base_url()
    path = str(settings.llamacpp_reload_path or "/props")
    url = f"{base}{path}"
    param = str(settings.llamacpp_model_param or "model").strip() or "model"

    names = local_name_candidates(model_path)
    if not names:
        # ⭐ 没给模型名就别发空名字——路由模式会回 400
        #   "model name is missing from the request"
        return {"ok": False, "mode": "http", "url": url,
                "error": "未指定模型名，且无法从路径推导；请在设置里指定模型名"}

    attempts: List[Dict] = []
    for name in names:
        payload = {param: name}
        for alias in ("model", "model_path", "model_path_or_url"):
            if alias != param:
                payload[alias] = name

        try:
            with _client() as c:
                resp = c.post(url, json=payload)
        except Exception as e:
            attempts.append({"model": name, "method": "POST", "error": f"{type(e).__name__}: {e}"})
            continue

        status = resp.status_code
        detail = _extract_error(resp)
        if 200 <= status < 300:
            return {"ok": True, "mode": "http", "method": "POST", "url": url,
                    "model": name, "status_code": status, "detail": detail,
                    "attempts": attempts}

        attempts.append({"model": name, "method": "POST", "status_code": status,
                         "error": detail})
        if status in (404, 405):
            try:
                with _client() as c:
                    resp = c.get(url)
                if 200 <= resp.status_code < 300:
                    return {"ok": True, "mode": "http", "method": "GET", "url": url,
                            "model": name, "status_code": resp.status_code,
                            "detail": _extract_error(resp), "attempts": attempts}
            except Exception as e:
                attempts.append({"model": name, "method": "GET",
                                 "error": f"{type(e).__name__}: {e}"})

    last = attempts[-1] if attempts else {}
    return {"ok": False, "mode": "http", "url": url, "model": names[0],
            "tried_models": names, "status_code": last.get("status_code"),
            "error": last.get("error") or "重载请求未返回成功状态", "attempts": attempts,
            "hint": "请确认设置里的「模型名推导方式 / 前缀」与服务端登记的模型名一致"}


def _reload_command(model_path: Optional[str] = None) -> Dict:
    cmd_tpl = str(settings.llamacpp_reload_cmd or "").strip()
    if not cmd_tpl:
        return {"ok": False, "mode": "command", "error": "未配置重载命令"}

    names = local_name_candidates(model_path)
    name = names[0] if names else (model_path or "")
    cmd = cmd_tpl.replace("{model_path}", model_path or "").replace("{model}", name)
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                              timeout=_timeout())
        ok = proc.returncode == 0
        result = {"ok": ok, "mode": "command", "command": cmd, "model": name,
                  "returncode": proc.returncode,
                  "stdout": (proc.stdout or "")[:500],
                  "stderr": (proc.stderr or "")[:500]}
        if not ok:
            result["error"] = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}")[:300]
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "mode": "command", "command": cmd, "error": "执行超时"}
    except Exception as e:
        return {"ok": False, "mode": "command", "command": cmd,
                "error": f"{type(e).__name__}: {e}"}


# ============================================================
# 统一入口
# ============================================================
def reload(model_path: Optional[str] = None, reason: str = "manual") -> Dict:
    """
    让服务端加载/切换到指定模型。

    路由模式下会先在服务端列表里匹配模型名，匹配到就走 /models/load；
    匹配不到才退回配置项推导的候选名依次尝试。
    """
    if not settings.llamacpp_enabled:
        return {"ok": False, "skipped": True, "reason": "llama.cpp 集成未启用"}

    mode = str(settings.llamacpp_reload_mode or "none").lower()
    started = time.time()

    if mode == "none":
        result = {"ok": True, "skipped": True, "mode": "none", "reason": "重载方式设为 none"}
    elif mode == "command":
        result = _reload_command(model_path)
    else:
        # 下载/更新场景要"先刷新再加载"，否则新文件还没进路由模式的模型表
        result = _reload_for_flavor(
            model_path, refresh_first=(reason in ("download", "update"))
        )

    result["elapsed"] = round(time.time() - started, 2)
    result["trigger"] = reason

    if result.get("skipped"):
        logger.info(f"[llamacpp] reload skipped: {result}")
    elif result.get("ok"):
        logger.info(f"[llamacpp] reload ok: {result}")
    else:
        # ⭐ 真正的状态码在 attempts 里，顶层通常没有，
        #    之前直接取 result['status_code'] 会打出一堆 status=None，看着像别的问题
        status = result.get("status_code")
        if status is None:
            for a in reversed(result.get("attempts") or []):
                if a.get("status_code") is not None:
                    status = a.get("status_code")
                    break
        logger.error(f"[llamacpp] reload FAILED: model={result.get('model')!r}, "
                     f"status={status}, error={result.get('error')}")
        result["status_code"] = status
    return result


def _reload_for_flavor(model_path: Optional[str], refresh_first: bool = False) -> Dict:
    """
    按服务端类型分派重载逻辑。

    refresh_first=True 时先让路由模式重新读盘，再加载。
    ⭐ 这是"刚下载完就 404 File Not Found"的关键：
       路由模式的模型表是启动时读一次的，新下载的文件不在表里，
       POST /models/load 必然 404。必须先 /models?reload=1 刷新，再加载。
    """
    flavor = current_flavor()

    if flavor in ("router", "llamaswap"):
        # ⭐ 手动点「立即重载」时往往没带模型名。以前会拿着空名字去发请求，
        #    路由模式直接回 400 "model name is missing from the request"。
        #    这种情况下正确的动作是重新扫描模型源，并把列表返回给界面。
        if not model_path:
            refreshed = refresh_models()
            return {
                "ok": bool(refreshed.get("ok")),
                "flavor": flavor,
                "mode": "refresh",
                "status": "refreshed",
                "models": refreshed.get("models") or [],
                "error": refreshed.get("error"),
                "hint": "未指定模型，已重新扫描模型源；要加载具体模型请在列表行点「重载」",
            }

        if refresh_first:
            refresh_models()

        attempts: List[Dict] = []

        def _try_all(candidates):
            for name in candidates:
                r = load_model(name)
                if r.get("ok"):
                    return r
                attempts.append({"model": name, "error": r.get("error"),
                                 "status_code": r.get("status_code")})
            return None

        # 先从服务端列表里找名字，preset 的 section 名往往和文件名不同
        matched = match_remote_name(model_path)
        candidates = ([matched] if matched else []) + local_name_candidates(model_path)

        ok = _try_all(candidates)
        if ok:
            return {**ok, "mode": "load", "matched_from_server": True,
                    "attempts": attempts}

        # ⭐ 还没刷新过的话，刷新一次再重试：
        #    刚下载完的文件在路由模式刷新之前是不存在的
        if not refresh_first:
            refresh_models()
            matched2 = match_remote_name(model_path)
            retry = ([matched2] if matched2 and matched2 != matched else []) + candidates
            ok = _try_all(retry)
            if ok:
                return {**ok, "mode": "load", "refresh_retried": True,
                        "matched_from_server": matched2 is not None,
                        "attempts": attempts}

        return {"ok": False, "flavor": flavor, "mode": "load",
                "model": candidates[0] if candidates else "",
                "tried_models": candidates, "attempts": attempts,
                "error": (attempts[-1].get("error") if attempts else "无可用模型名"),
                "hint": "服务端列表里没有匹配到该模型；确认模型目录已挂载给 llama.cpp，"
                        "且 preset/目录里确实有这个文件"}

    return _reload_http(model_path)


def maybe_reload_after_download(model_path: str) -> Dict:
    """下载完成后的钩子：刷新模型源 + 加载新模型。"""
    if not settings.llamacpp_enabled:
        return {"ok": False, "skipped": True, "reason": "llama.cpp 集成未启用"}
    if not settings.llamacpp_reload_on_download:
        return {"ok": False, "skipped": True, "reason": "未在下载后自动重载"}

    # ⭐ reload() 内部对 download 场景已经"先刷新再加载"，这里只需把刷新结果
    #    附到返回值上供前端展示——不要在这里再刷一次，否则会白白多一轮
    #    卸载/加载，反而可能把刚加载好的模型又顶掉。
    result = reload(model_path, reason="download")

    if current_flavor() in ("router", "llamaswap"):
        listing = list_models()
        result["refreshed"] = listing
        result["server_models"] = [m["id"] for m in (listing.get("models") or [])]
    return result


def notify_models_changed(reason: str = "delete", names: Optional[List[str]] = None) -> Dict:
    """
    本地模型增删后通知服务端（删除场景专用）。

    路由模式不会监听磁盘，删掉的文件在服务端的列表里依然存在，
    必须先卸载再重新扫描，否则客户端还能"加载"一个已经不存在的模型。
    """
    if not settings.llamacpp_enabled:
        return {"ok": False, "skipped": True, "reason": "llama.cpp 集成未启用"}

    flavor = current_flavor()
    out: Dict = {"flavor": flavor, "reason": reason}

    if flavor in ("router", "llamaswap"):
        unloaded = []
        for n in names or []:
            if not n:
                continue
            r = unload_model(n)
            unloaded.append({"model": n, "ok": bool(r.get("ok")),
                             "error": r.get("error")})
        out["unloaded"] = unloaded

        refreshed = refresh_models()
        out["refreshed"] = refreshed
        out["server_models"] = [m["id"] for m in (refreshed.get("models") or [])]
        out["ok"] = True
    else:
        # 单模型 server：删除后没有可用的"重新扫描"概念，沿用重载策略
        out.update(_reload_http(None) if str(settings.llamacpp_reload_mode) == "http"
                   else {"ok": True, "skipped": True})
        out.setdefault("ok", True)

    logger.info(f"[llamacpp] models changed ({reason}): {out}")
    return out
