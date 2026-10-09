# app/config.py
"""
运行时配置。

优先级：环境变量（docker-compose） < 持久化文件（Web 设置面板改过的值）。
持久化文件默认存在 CONFIG_DIR（缺省 /app/config），与模型目录分离，
可单独挂 volume 持久化；重启容器后仍然生效；删除该文件即回退到环境变量。

目录优先级：CONFIG_DIR 环境变量 > /app/config > ~/.gguf-manager
           > MODEL_DIR/.gguf-manager（旧布局兼容，启动时自动迁移）

所有 *_int / *_float 解析都做了兜底，环境变量写错不会导致启动崩溃。
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULTS: Dict[str, Any] = {
    # ---------- 基础 ----------
    "model_dir": "/app/models",
    "hf_endpoint": "",
    "hf_token": "",
    "search_limit": 50,
    "log_level": "INFO",
    "log_buffer_size": 1000,

    # ---------- UI ----------
    # 弹窗（Toast）展示时长，单位秒，默认 3 秒
    # ⭐ 原「弹窗展示时长」与「面板自动收起」两项已合并为 task_panel_autohide。
    #    这两个值默认都是 3 秒，实际是同一件事的两个名字，保留一个即可。
    #    toast_duration 仅作旧配置兼容，保存时会自动与 task_panel_autohide 同步。
    "toast_duration": 3.0,
    # 右下角任务面板：全部任务结束后自动隐藏的延迟（秒），默认 3 秒
    "task_panel_autohide": 3.0,
    # 本地管理的默认扫描路径，留空表示用 MODEL_DIR
    "scan_path": "",

    # ---------- 网络（超时 / 重试）----------
    "hf_connect_timeout": 15.0,
    "hf_read_timeout": 60.0,
    "hf_max_retries": 3,        # 总尝试次数（含首次）
    "hf_retry_backoff": 1.5,    # 指数退避基数
    "hf_search_budget": 45.0,   # 搜索总时间预算（秒），耗尽即降级/报错
    "ms_connect_timeout": 15.0,
    "ms_read_timeout": 60.0,
    "ms_max_retries": 3,

    # ---------- llama.cpp 控制 ----------
    "llamacpp_enabled": False,
    "llamacpp_url": "http://127.0.0.1:8080",
    # ⭐ 服务端类型：
    #   auto      自动探测
    #   router    llama.cpp 路由模式（llama-server --models-preset / --models-dir）
    #             支持 /models 列表、/models/load、/models/unload、/models?reload=1
    #   llamaswap llama-swap 代理（/v1/models 列表、/api/models/unload）
    #   server    普通 llama-server（单模型，只能靠 http/command 重载）
    "llamacpp_flavor": "auto",
    "llamacpp_api_key": "",
    "llamacpp_timeout": 10.0,
    # 下载完成后是否自动让 llama.cpp 重新加载模型
    "llamacpp_reload_on_download": True,
    # 重载方式：http（请求 llama.cpp 的某个端点）| command（执行 shell 命令）| none
    "llamacpp_reload_mode": "http",
    "llamacpp_reload_path": "/props",
    "llamacpp_reload_cmd": "",
    # ⭐ 发给 llama.cpp 的"模型名"怎么从本地文件推导。
    #    默认 stem（去掉 .gguf，如 Qwen3-1.7B-Q4_K_M）。
    #    很多部署（尤其是 llama-swap）要求的是它自己配置里的模型名，
    #    而不是本容器内的绝对路径——传 /app/models/xxx.gguf 会 400 model not found。
    #    stem     : Qwen3-1.7B-Q4_K_M
    #    filename : Qwen3-1.7B-Q4_K_M.gguf
    #    relative : 相对 MODEL_DIR 的路径
    #    path     : 本容器内的绝对路径
    "llamacpp_model_name_mode": "stem",
    # 可选前缀，会拼在模型名前面（如 llama.cpp 容器内挂载点是 /models 时填 /models）
    "llamacpp_model_prefix": "",
    # POST 提交时用的字段别名（不同实现字段名不同）
    "llamacpp_model_param": "model",

    # ---------- 本地模型更新检测 ----------
    "update_check_enabled": False,
    "update_check_interval_minutes": 360,

    # ---------- 日志时区 ----------
    # 留空则跟随系统（容器默认多为 UTC）。建议设为 Asia/Shanghai
    "timezone": "",
}

# 允许 Web 面板修改的键。
# hf_endpoint / log_level / log_buffer_size 都在 Web 设置里调整（改完即时生效），
# 不再依赖 docker-compose.yml；只有 model_dir 属于部署级配置，不暴露给面板。
EDITABLE_KEYS = {
    "hf_endpoint",
    "log_level", "log_buffer_size",
    "toast_duration", "task_panel_autohide", "timezone", "scan_path",
    "hf_connect_timeout", "hf_read_timeout", "hf_max_retries",
    "hf_retry_backoff", "hf_search_budget",
    "ms_connect_timeout", "ms_read_timeout", "ms_max_retries",
    "llamacpp_enabled", "llamacpp_url", "llamacpp_api_key", "llamacpp_timeout",
    "llamacpp_reload_on_download", "llamacpp_reload_mode",
    "llamacpp_reload_path", "llamacpp_reload_cmd",
    "llamacpp_model_name_mode", "llamacpp_model_prefix", "llamacpp_model_param",
    "llamacpp_flavor",
    "update_check_enabled", "update_check_interval_minutes",
    "search_limit",
}

_BOOL_TRUE = {"1", "true", "yes", "on", "enabled"}

# 允许在 Web 面板选择的日志级别
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_MODEL_NAME_MODES = ("stem", "filename", "relative", "path")
_LLAMACPP_FLAVORS = ("auto", "router", "llamaswap", "server")
_RELOAD_MODES = ("http", "command", "none")

SETTINGS_FILENAME = "settings.json"
REGISTRY_FILENAME = "registry.json"

# 配置的默认落点（相对于应用根目录）。独立于 MODEL_DIR，
# 这样换模型挂载点、或把模型挂到 NAS 时，配置都不会跟着丢。
DEFAULT_CONFIG_DIR = "/app/config"


def _env(key: str, cast):
    """从环境变量读配置，未设置或解析失败则返回 None（交给默认值）。"""
    raw = os.getenv(key.upper(), "")
    if raw == "":
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return None


def _is_writable_dir(path: Path) -> bool:
    """目录存在（或能创建）且可写。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_config_dir(model_dir: "Path | None" = None) -> Path:
    """
    确定配置目录，按优先级依次尝试：

      1. 环境变量 CONFIG_DIR 指定
      2. 默认 /app/config（建议单独挂 volume）
      3. ~/.gguf-manager（容器以非 root 运行、/app 只读时的兜底）
      4. MODEL_DIR/.gguf-manager（最后兜底，兼容旧版本布局）

    目录不可写会自动降级到下一个候选，保证启动不会因为权限问题失败。
    """
    candidates: list[Path] = []

    env_dir = os.getenv("CONFIG_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.append(Path(DEFAULT_CONFIG_DIR))
    candidates.append(Path.home() / ".gguf-manager")

    if model_dir is not None:
        candidates.append(Path(model_dir) / ".gguf-manager")

    for c in candidates:
        try:
            c = c.expanduser()
        except (RuntimeError, OSError):
            pass
        if _is_writable_dir(c):
            return c

    # 理论上到不了这里（至少 home 或 tmp 可写），仍给一个不崩溃的落点
    return Path(tempfile.gettempdir()) / "gguf-manager"


def is_persistent_dir(path) -> bool:
    """
    判断目录是否位于独立挂载点（docker volume 或 bind mount）。

    容器里的 /app、/app/config 如果不挂卷，容器一重建配置就没了。
    这个函数让启动日志能明确提醒，而不是让用户等到重启后才发现配置丢了。
    """
    try:
        target = str(Path(path).resolve())
    except OSError:
        return False

    # 这些是容器运行时注入的文件/伪文件系统，不算持久化目录
    ignore = {"/", "/proc", "/sys", "/dev", "/tmp", "/run", "/etc/hostname",
              "/etc/hosts", "/etc/resolv.conf"}

    try:
        with open("/proc/mounts", encoding="utf-8", errors="ignore") as f:
            mounts = [line.split()[1] for line in f if len(line.split()) > 1]
    except OSError:
        return False  # 非 Linux（如 Windows 开发机）无法判断，不误报

    for mp in mounts:
        if mp in ignore or mp.startswith(("/proc", "/sys", "/dev")):
            continue
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            return True
    return False


def _migrate_legacy(model_dir: Path, config_dir: Path, filenames) -> None:
    """
    旧版本把配置放在 MODEL_DIR/.gguf-manager/ 下，升级后自动搬到新目录。

    只在目标文件不存在时搬，绝不覆盖已有配置。
    """
    legacy_dir = Path(model_dir) / ".gguf-manager"
    if legacy_dir == config_dir or not legacy_dir.exists():
        return

    for name in filenames:
        src, dst = legacy_dir / name, config_dir / name
        if not src.exists() or dst.exists():
            continue
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            pass


class Settings:
    """可通过属性访问的配置对象，改动会同步持久化。"""

    def __init__(self):
        self._lock = threading.RLock()

        # 1) 默认值
        self._data: Dict[str, Any] = dict(_DEFAULTS)

        # 2) 环境变量覆盖
        for k, default in _DEFAULTS.items():
            if isinstance(default, bool):
                raw = os.getenv(k.upper(), "")
                if raw != "":
                    self._data[k] = raw.lower() in _BOOL_TRUE
            elif isinstance(default, int):
                v = _env(k, int)
                if v is not None:
                    self._data[k] = v
            elif isinstance(default, float):
                v = _env(k, float)
                if v is not None:
                    self._data[k] = v
            else:
                v = os.getenv(k.upper(), "")
                if v != "":
                    self._data[k] = v

        # 3) 持久化文件覆盖（Web 面板改过的值）
        self.MODEL_DIR = Path(os.getenv("MODEL_DIR", self._data["model_dir"]))
        try:
            self.MODEL_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # ⭐ 配置独立于模型目录存放，可单独挂 volume 持久化
        self._store_dir = resolve_config_dir(self.MODEL_DIR)
        self._store_file = self._store_dir / SETTINGS_FILENAME

        _migrate_legacy(self.MODEL_DIR, self._store_dir, (SETTINGS_FILENAME, REGISTRY_FILENAME))

        self._load_persisted()

        # ⭐ 首次部署自动生成配置文件；已存在则不覆盖用户的手改内容
        self.created_config_file = self._ensure_initial_file()

        # 同步 HF_ENDPOINT 给 huggingface_hub / requests
        if self.hf_endpoint:
            os.environ["HF_ENDPOINT"] = self.hf_endpoint

    def _ensure_initial_file(self) -> bool:
        """配置文件不存在时用当前生效值（环境变量 + 默认值）落盘，返回是否新建。"""
        if self._store_file.exists():
            return False
        self._persist()
        return True

    # ---------- 属性访问 ----------
    def __getattr__(self, name: str):
        data = self.__dict__.get("_data")
        if data is not None and name in data:
            return data[name]
        raise AttributeError(f"Settings has no attribute {name!r}")

    @property
    def config_persistent(self) -> bool:
        """配置文件所在目录是否已挂载（重启容器后能保留）。"""
        return is_persistent_dir(self._store_dir)

    @property
    def config_path(self) -> Path:
        """配置文件绝对路径，供日志与接口展示。"""
        return self._store_file

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """只更新白名单内的键，并做类型/范围校验。"""
        applied, rejected = {}, {}
        with self._lock:
            for k, v in (patch or {}).items():
                if k not in EDITABLE_KEYS:
                    rejected[k] = "不可修改"
                    continue
                coerced = self._coerce(k, v)
                if coerced is None:
                    rejected[k] = "取值非法"
                    continue
                self._data[k] = coerced
                applied[k] = coerced

            # ⭐ 两个时长设置已合并：改任意一个都让另一个跟着变。
            #    否则旧配置里的 toast_duration 还留着 8，界面上只改了
            #    task_panel_autohide，消息却还是按 8 秒消失，看着像没生效。
            for key_a, key_b in (("task_panel_autohide", "toast_duration"),
                                 ("toast_duration", "task_panel_autohide")):
                if key_a in applied:
                    self._data[key_b] = applied[key_a]
                    applied.setdefault(key_b, applied[key_a])

            if applied:
                self._persist()
        return {"applied": applied, "rejected": rejected}

    def reset(self) -> None:
        """清掉持久化文件并回退到环境变量。"""
        with self._lock:
            try:
                self._store_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._data = self._rebuild_from_env()

    # ---------- 校验 ----------
    def _coerce(self, key: str, value):
        default = _DEFAULTS[key]
        try:
            if isinstance(default, bool):
                if isinstance(value, bool):
                    return value
                return str(value).lower() in _BOOL_TRUE
            if isinstance(default, int):
                v = int(value)
                if v < 0:
                    return None
                return self._clamp_int(key, v)
            if isinstance(default, float):
                v = float(value)
                return v if v >= 0 else None
            text = str(value).strip()
            # 日志级别是枚举，写错会让所有日志静默消失，必须校验
            if key == "log_level":
                return text.upper() if text.upper() in _LOG_LEVELS else None
            if key == "llamacpp_flavor":
                return text.lower() if text.lower() in _LLAMACPP_FLAVORS else None
            if key == "llamacpp_model_name_mode":
                return text.lower() if text.lower() in _MODEL_NAME_MODES else None
            if key == "llamacpp_reload_mode":
                return text.lower() if text.lower() in _RELOAD_MODES else None
            if False:
                return text.upper() if text.upper() in _LOG_LEVELS else None
            # 镜像地址必须带协议，否则 requests 会报难以理解的错
            if key == "hf_endpoint" and text:
                return text if text.startswith(("http://", "https://")) else None
            return text
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clamp_int(key: str, value: int) -> Optional[int]:
        """数值型配置的范围校验，超出范围返回 None（表示该值非法）。"""
        if key == "log_buffer_size":
            # 太少看不到东西，太多会吃掉内存（每条日志约 200 字节）
            return value if 100 <= value <= 20000 else None
        if key == "update_check_interval_minutes":
            return value if 1 <= value <= 100000 else None
        if key == "search_limit":
            return value if 1 <= value <= 500 else None
        if key.endswith("_max_retries"):
            return value if 1 <= value <= 10 else None
        return value if value >= 0 else None

    # ---------- 持久化 ----------
    def _load_persisted(self) -> None:
        try:
            if not self._store_file.exists():
                return
            saved = json.loads(self._store_file.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if k in EDITABLE_KEYS:
                    coerced = self._coerce(k, v)
                    if coerced is not None:
                        self._data[k] = coerced
        except Exception:
            # 配置文件损坏不应该影响启动
            pass

    def _persist(self) -> None:
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            payload = {k: self._data[k] for k in EDITABLE_KEYS}
            self._store_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _rebuild_from_env(self) -> Dict[str, Any]:
        data = dict(_DEFAULTS)
        for k, default in _DEFAULTS.items():
            raw = os.getenv(k.upper(), "")
            if raw == "":
                continue
            if isinstance(default, bool):
                data[k] = raw.lower() in _BOOL_TRUE
            elif isinstance(default, int):
                v = _env(k, int)
                data[k] = v if v is not None else default
            elif isinstance(default, float):
                v = _env(k, float)
                data[k] = v if v is not None else default
            else:
                data[k] = raw
        return data


settings = Settings()
