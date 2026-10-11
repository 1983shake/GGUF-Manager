"""
运行时配置层。

设计：
  · 配置只在此处一个入口，其他模块一律读 settings.xxx，不再散落 os.getenv。
  · 启动时合并：环境变量 → 持久化 JSON（settings.json，优先级更高）。
  · Web 面板改设置 → config.py 的 update() → 写盘 + 全量生效。
  · 只有 EDITABLE_KEYS 里的键允许 Web 面板修改，其余是部署层（compose/环境变量），
    避免“两处配置打架”。

持久化目录：CONFIG_DIR 环境变量指向的位置，与模型目录分离，便于单独挂 volume。
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from app.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 可调白名单：只有这些键允许 Web 面板修改。
# 新增面板字段时，记得同步维护这里与默认值字典。
# ============================================================
EDITABLE_KEYS = {
    "hf_endpoint",
    "hf_connect_timeout",
    "hf_read_timeout",
    "hf_max_retries",
    "hf_retry_backoff",
    "hf_search_budget",
    "log_buffer_size",
    "log_level",
    "task_panel_autohide",
    "search_limit",
    "scan_path",
    "llamacpp_enabled",
    "llamacpp_flavor",
    "llamacpp_url",
    "llamacpp_api_key",
    "llamacpp_reload_mode",
    "llamacpp_reload_path",
    "llamacpp_reload_cmd",
    "llamacpp_reload_on_download",
    "hf_token",
    "update_check_enabled",
    "update_check_interval_minutes",
}

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

DEFAULTS = {
    "hf_endpoint": "",
    "hf_connect_timeout": 15,
    "hf_read_timeout": 60,
    "hf_max_retries": 3,
    "hf_retry_backoff": 1.0,
    "hf_search_budget": 45,
    "log_buffer_size": 1000,
    "hf_token": "",
    "log_level": "INFO",
    "task_panel_autohide": 3,
    "search_limit": 50,
    "scan_path": "",
    "llamacpp_enabled": False,
    "llamacpp_flavor": "auto",
    "llamacpp_url": "",
    "llamacpp_api_key": "",
    "llamacpp_reload_mode": "http",
    "llamacpp_reload_path": "/props",
    "llamacpp_reload_cmd": "",
    "llamacpp_reload_on_download": False,
    "update_check_enabled": True,
    "update_check_interval_minutes": 1440,
}

REGISTRY_FILENAME = "registry.json"
SETTINGS_FILENAME = "settings.json"

_int_keys = {
    "hf_connect_timeout", "hf_read_timeout", "hf_max_retries",
    "log_buffer_size", "task_panel_autohide", "update_check_interval_minutes",
}
_float_keys = {"hf_retry_backoff", "hf_search_budget"}
_bool_keys = {
    "llamacpp_enabled", "llamacpp_reload_on_download",
}


def _coerce(key: str, value: Any) -> Any:
    """按白名单类型强制转换。转换失败回退默认值，避免脏数据把服务打挂。"""
    if value is None:
        return DEFAULTS.get(key)
    if key in _bool_keys:
        return bool(value)
    if key in _int_keys:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(DEFAULTS.get(key, 0))
    if key in _float_keys:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULTS.get(key, 0.0))
    if key == "log_level":
        v = str(value).upper()
        return v if v in _LOG_LEVELS else "INFO"
    return value


def resolve_config_dir(model_dir: Optional[Path] = None) -> Path:
    """
    解析配置持久化目录。

    优先级：环境变量 CONFIG_DIR > 模型目录下的 .gguf-manager/。返回绝对路径。
    """
    env = os.getenv("CONFIG_DIR", "").strip()
    if env:
        return Path(env).resolve()
    base = model_dir or Path(os.getenv("MODEL_DIR", "/app/models"))
    return base.resolve() / ".gguf-manager"


class Settings:
    """
    配置单例。所有读取走这里，保证“环境变量 / 文件 / 默认值”三层合并逻辑只有一份。

    字段做了两种暴露方式：
      settings.log_level           —— 驼峰属性（兼容旧代码）
      settings["log_level"]        —— 字典式（as_dict / update 里用）
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._values: Dict[str, Any] = {}
        self._store_dir: Path = Path("/app/config")
        self._store_file: Path = self._store_dir / SETTINGS_FILENAME
        self._created = False
        self._load()

    # ---------- 加载 / 持久化 ----------
    def _load(self) -> None:
        with self._lock:
            merged = dict(DEFAULTS)
            # 环境变量只提供部署层的默认值，settings.json 可覆盖
            for key in EDITABLE_KEYS:
                env_key = key.upper()
                if env_key in os.environ:
                    merged[key] = _coerce(key, os.environ[env_key])

            self._store_dir = resolve_config_dir()
            self._store_file = self._store_dir / SETTINGS_FILENAME

            try:
                self._store_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f"[config] cannot create config dir {self._store_dir}: {e}")

            if self._store_file.exists():
                try:
                    persisted = json.loads(self._store_file.read_text(encoding="utf-8"))
                    if isinstance(persisted, dict):
                        for k, v in persisted.items():
                            if k in EDITABLE_KEYS:
                                merged[k] = _coerce(k, v)
                except Exception as e:
                    logger.warning(f"[config] settings.json 读取失败，使用默认值：{e}")
            else:
                # ⭐ 首次启动把当前生效值落盘，避免下次重启时被默认值覆盖
                try:
                    self._store_dir.mkdir(parents=True, exist_ok=True)
                    self._store_file.write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    self._created = True
                    logger.info(f"[config] 已生成默认配置：{self._store_file}")
                except OSError as e:
                    logger.warning(f"[config] 无法写入默认配置：{e}")

            self._values = merged

    def save(self) -> None:
        """把当前值写回 settings.json。"""
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            self._store_file.write_text(
                json.dumps(self._values, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"[config] 保存配置失败：{e}")

    # ---------- 查询 ----------
    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def get(self, key: str, default=None):
        with self._lock:
            return self._values.get(key, default)

    def __getitem__(self, key: str):
        return self._values[key]

    def __contains__(self, key: str) -> bool:
        return key in self._values

    # ---------- 更新 ----------
    def update(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Web 面板修改入口。

        返回 {applied: {...}, rejected: {...}}，前端据此提示哪些没生效。
        只接受白名单内的键；写盘后再做全量生效（见 routers/config.py），
        避免“改过又改回 / reset”导致运行时与文件不一致。
        """
        applied, rejected = {}, {}
        with self._lock:
            for k, v in (values or {}).items():
                if k not in EDITABLE_KEYS:
                    rejected[k] = "not editable"
                    continue
                new_val = _coerce(k, v)
                if self._values.get(k) == new_val:
                    # 值没变也放进 applied，让调用方知道已确认
                    applied[k] = new_val
                    continue
                self._values[k] = new_val
                applied[k] = new_val

        if applied:
            self.save()
        return {"applied": applied, "rejected": rejected}

    def reset(self) -> None:
        """清空持久化值，回退到环境变量/默认值。"""
        with self._lock:
            self._values = dict(DEFAULTS)
            for key in EDITABLE_KEYS:
                env_key = key.upper()
                if env_key in os.environ:
                    self._values[key] = _coerce(key, os.environ[env_key])
        self.save()

    # ---------- 派生属性 ----------
    @property
    def MODEL_DIR(self) -> Path:
        return Path(os.getenv("MODEL_DIR", "/app/models"))

    @property
    def created_config_file(self) -> bool:
        return self._created

    @property
    def config_path(self) -> Path:
        return self._store_file

    # 白名单字段直接以属性形式暴露，调用方无需区分驼峰/下划线
    def __getattr__(self, name: str):
        with self._lock:
            if name in self._values:
                return self._values[name]
            if name in DEFAULTS:
                return DEFAULTS[name]
        raise AttributeError(f"Settings has no attribute {name!r}")


settings = Settings()
