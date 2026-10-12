# 更新记录

本文件记录 GGUF-Manager 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

使用说明与部署方式见 [README.md](./README.md)。

---

## [1.3.0] - 2026-10-12

### 新增

- **本地管理新增排序与过滤**：表头 6 列（文件名 / 大小 / 有效性 / 来源 / 更新状态 / 修改时间）点击即排序，
  配合关键字过滤、状态过滤（7 种）与排序下拉框（10 种）。全部在浏览器端完成，不重新扫盘、不发新请求。
- 本地列表新增「修改时间」列。
- 新增批量体积查询端点 `GET /api/search/sizes`，一次补齐整页模型的体积。

### 修复

| 问题 | 影响 |
| ---- | ---- |
| `config_changed` 事件回调引用了作用域外的变量 | 抛 `ReferenceError`，设置保存后页面徽标与扫描路径不刷新 |
| `format_timestamp()` 引用未导入的 `datetime` | 异常被 `except` 吞掉后**永远返回空串**，「远端更新时间」从未显示过 |
| `utils` 中时间解析的模块级依赖缺失 | 同上，属静默失效：不报错，但功能完全没生效 |
| `/api/llamacpp/proxy` 的 `except` 用到未导入的 `json` | 上游返回非 JSON 时抛 `NameError`，把 500 变成 500 + 堆栈 |
| 扫描「外部挂载目录」时按“相对扫描根”查来源注册表 | 外部目录里的同名文件会**错配**到模型目录中同名文件的来源与更新状态 |
| 相对路径口径混用（Windows 反斜杠 vs 正斜杠） | 可能出现“明确有来源记录却显示未知来源”“点检测报无记录” |
| `search_limit` 未列入整数键 | 环境变量注入时以字符串进入配置 |
| `hf_search_budget` 被写成 0 / 负数 | 搜索第 1 档直接跳过，最后 `raise None` 变成 `TypeError` |
| 前端未使用已实现的批量体积接口，仍逐个模型请求 | 50 条搜索结果产生 50 次 HTTP + 50 次上游文件列表拉取，实测耗时约 5 秒、日志被刷满 |
| 上游文件列表没有缓存 | 同一次交互里（补体积、打开「查看」）对同一模型重复拉取完整文件列表 |
| llama.cpp 重载盲试候选 id | 每次重载都白扔 2 个必然失败的 `POST /models/load`，每个都要等一次 HTTP 往返 |
| 重载时重复拉取 `/models` | pre-refresh 已带回模型列表，选 id 时又请求一次 |

### 优化

- 体积补全改走批量接口，并按 20 个一批分片、片间并发 3 —— `model_id` 里的 `/` 编码后膨胀，
  50 个拼进 query string 会超过 2000 字符，容易触发 414 或被中间代理截断。
- 新增通用 `TTLCache`（60 秒 TTL + LRU，存取走副本，避免调用方就地修改污染缓存），
  ModelScope / HuggingFace 的文件列表与体积查询共用；切换 HF 镜像时自动清空。
- llama.cpp 候选 id 排序把「服务端 `/models` 实际报出的 id」提到最前，本地路径写法降级为兜底。
- 上游查询日志由 INFO 降到 DEBUG，一次搜索不再刷出上百行日志。
- 下载进度事件按 0.4s 节流（原先每个 256KB 分片都推送一次，5GB 模型会产生约 2 万条事件与 2 万次 DOM 重绘）。
- SSE 连接不再每条起一个搬运线程，改为事件回调 + `call_soon_threadsafe`，断开时回收订阅。
- llama.cpp 服务端类型探测加 30 秒缓存，配置变更时失效。
- 删除模型时不再为了判断布尔值先白跑一次 `list_models()`。
- `@app.on_event("startup")` 迁移到 lifespan，消除 DeprecationWarning，并补上关机时停掉定期检测线程。
- 代码整理：模块由 27 个文件精简到 23 个。
  - `sources/scanner.py` + `sources/registry.py` + `sources/updater.py` 合并为 `sources/local.py`
  - `sources/base.py` 并入 `sources/__init__.py`
  - `routers/sources.py` 并入 `routers/downloads.py`
  - `app/models.py` 更名为语义更准确的 `app/utils.py`
- 删除死代码：未被引用的 Pydantic 模型（`SearchResult` / `LocalModel` / `GGUFFile` / `ModelSize` / `DownloadPatch`）、
  `delete_models`、`_get_source_backend`、前端 `formatSpeed` / `clearFinishedTasks`、日志路由里重复的过滤实现。

### 安全 / 健壮性

- 下载的 `target_subdir` 限制在模型目录内，拒绝 `../` 越界写入。
- 更新检测、删除等接口的异常路径统一收口，失败信息不再吞成 `undefined`。

### 实测效果

- 搜索 50 条结果的体积补全：**50 次请求 → 1~3 次**，相关日志 **约 250 行 → 2 行**。
- 上游文件列表重复查询：60 秒缓存命中，**0.58s → 0.01s**。
- llama.cpp 重载：**5 次 HTTP → 3 次**，且不再有必然失败的请求。

---

## [1.2.0]

### 新增

- llama.cpp 路由模式热切换自动化：下载完成后自动 `GET /models?reload=1` 让服务端重新扫描磁盘，
  再 `POST /models/load` 加载新模型，无需重启容器。
- 识别 `400 model is already running` 为幂等成功。
- `500 model limit reached` 时自动尝试「先 unload 再 load」兜底。
- 基于 SSE 的事件驱动架构：下载完成 / 检测结束 / 配置变更主动推送到前端。
