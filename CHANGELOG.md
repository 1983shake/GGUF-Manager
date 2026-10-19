# 更新记录

本文件记录 GGUF-Manager 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

使用说明与部署方式见 [README.md](./README.md)。

---

## [1.4.3] - 2026-10-19

### 修复

| 问题 | 影响 / 修复 |
| ---- | ---- |
| 更新检测“假更新”：有时亮“有更新”，手动再检测却变“已是最新” | 根因：`Registry.mark_checked()` 在**每次检测结束**时把远端当前 `remote_size / remote_mtime / remote_etag` 回写进注册表，覆盖了 `put()` 在下载时写入的“本地文件对应远端版本基线”。第一次检测出 `update_available` 后基线被刷成最新版，下次检测拿“新基线 vs 当前远端”自然判 `up_to_date`，本地其实仍是旧版，更新被“消费”一次后丢失。修复：`mark_checked()` 只更新 `last_checked_at / last_check_status`，**不再回写版本基线**；基线仅在 `put()`（下载/更新成功）时更新。同步去掉 `check_one()` 调用 `mark_checked` 时传入的 `remote_size / remote_mtime / remote_etag` 实参。`app/sources/local.py` |
| 模型下载完成后“下载完成”通知弹两次（任务面板出现两条完成提示） | 根因：一条任务结束同时被两条独立通道检出——SSE `task_updated(completed)` 事件（`applyTaskUpdated` 的 finished 分支）与 800ms 轮询 `/api/tasks` 快照（`refreshTasks` 的完成检测），两者各自 `toast('下载完成')`，原去重只靠 `__prevTasks` 且轮询路径未读它。修复：抽公共 `toastIfJustFinished()`，用模块级 `finishedToastSent` 集合做 SSE 与轮询**共用**去重（同 id 只弹一次）；保留 `wasRunning` 判定（任务曾在快照里处于进行中/排队）确保刷新页面 / SSE 重连回放的“历史已结束”事件仍**不弹**。任务 id 为 `uuid4().hex[:12]`，每次下载唯一，不误伤重复下载。`app/static/app.js` |
| 下载中点「取消」无反应，无法取消下载 | 根因：任务面板下载中每次进度变化整表重绘 `innerHTML`（≥800ms 一次），「取消 / 移除」按钮是**每次重绘后逐节点绑定** click——节点在 mousedown 与 mouseup 之间被替换，click 事件丢失。修复：改为**事件委托**，在 `#task-list` 容器上只绑一次（覆盖整表重绘与精准更新 `replaceWith` 两条替换路径），节点替换不再影响事件响应。`app/static/app.js` |
| 更新下载完成（已转新版）后，手动检测仍恒报“有更新” | 根因：**版本基线与检测值不在同一命名空间**。下载落库（`_record_registry`）写的是 HTTP 响应头 `ETag` / `Last-Modified`，而检测（`check_one` → `get_file_meta`）比较的是来源 API 元数据（HF `blob_id` / `lastModified` 等）——两套标识永远不相等，`etag_changed` 恒真 → 刚下载完的最新版被恒定误报。修复：① 落库改为优先取**与检测同源的 API 元数据**作基线（`get_backend(source).get_file_meta()`），失败才回退 HTTP 头并标记 `remote_ns="http"`；② 注册表新增 `remote_ns` 字段，`check_one` 只在 `remote_ns=="api"` 时比 etag/mtime，旧数据（无标记）降级只比 size（字节数跨命名空间稳定），避免历史记录恒定误报；旧记录下次下载更新后自动升级为 api 基线。`app/sources/downloader.py`、`app/sources/local.py` |
| 清理 `applyTaskUpdated` 死代码 toast 块 | 非结束分支（pending/downloading）内判断 completed/error/cancelled 并弹通知的代码块永远不可达（`finished` 已提前 return），删除以防误导后续维护，结束通知统一走 `toastIfJustFinished` 去重。`app/static/app.js` |

---

## [1.4.2] - 2026-10-19

### 修复

| 问题 | 影响 |
| ---- | ---- |
| 更新/下载完成后刷新页面弹出任务面板 | 三条路径叠加：① **页面加载路径**——`boot()` 时 `refreshTasks()` 会把服务端残留的已结束任务拉回任务列表（如完成后几秒内就刷新/关掉页面，3 秒自动收起还没轮到清理，或清理请求失败），而 `updatePanelVisibility()` 对非空任务列表一律 `showTaskPanel()`；② **SSE 回放已完成事件**——事件总线保留最近 200 条历史事件，新页面连接 `/api/events` 时从头回放，其中包含以往任务结束时发出的 `task_updated`，前端对已结束的任务也无条件弹面板；③ **SSE 回放下载中进度事件（本次主因）**——下载中以 0.4s 节流广播进度，一次下载能在 backlog 里留下几十上百条 `task_updated(downloading)`，回放时走“进行中”分支弹出面板并插入幽灵下载中卡片，回放的 `completed` 事件还会误弹一条“下载完成”通知，使面板因消息常驻约 3 秒。修复：**SSE 首次连接（无 `Last-Event-ID`）不再回放历史 backlog**，页面的现状由前端启动时的全量拉取覆盖，断线重连才按 `Last-Event-ID` 补回放；同时修复 `Last-Event-ID` 只读 query 参数不读 HTTP 头的问题（浏览器重连时带头，导致永远全量回放）；前端增加武装门控与幽灵过滤兜底（轮询真相里不存在的“进行中”任务是历史幽灵，不弹面板、不插卡片）。面板收起时服务端残留的已结束任务直接清理（本地 + 服务端）；进行中的任务仍会正常弹出 |
| 手动关闭面板时残留本地任务缓存 | 点 × 关闭只隐藏面板、不清 `__lastTasks`，残留缓存会在通知消息到期回调 `updatePanelVisibility()` 时被误判成“服务端僵尸任务”，触发一次多余的服务端清理。现在关闭统一走 `hideTaskPanel({ clearTasks: true })`，本地缓存、服务端已结束任务、面板通知一次清完（`clear_finished` 不动进行中的任务，下载中关闭是安全的） |

---

## [1.4.1] - 2026-10-13

### 新增

- **更新检测支持选择检测源**（设置 → 更新检测 → 检测源）：默认「按模型记录的下载来源」，
  可强制 `huggingface` / `modelscope`。此前检测源固定为下载来源，国内访问 HuggingFace
  不稳定时无法改走 ModelScope。强制源上没有该文件（或网络异常）时自动回退到记录来源，
  不会误报「远端已无此文件」；检测到更新后重新下载也走同一套源解析。
- 新配置项 `update_check_source`（环境变量 `UPDATE_CHECK_SOURCE` 或设置面板修改，
  非法值回退 `record`）；定期检测徽标显示当前生效的检测源。

---

## [1.4.0] - 2026-10-12

### 新增

- 增加logo文件

---

## [1.3.0] - 2026-10-12

### 新增

- **本地管理新增排序与过滤**：表头 6 列（文件名 / 大小 / 有效性 / 来源 / 更新状态 / 修改时间）点击即排序，
  配合关键字过滤、状态过滤（7 种）与排序下拉框（10 种）。全部在浏览器端完成，不重新扫盘、不发新请求。
- 本地列表新增「修改时间」列。
- 新增批量体积查询端点 `GET /api/search/sizes`，一次补齐整页模型的体积。

### 修复

| 问题                                                  | 影响                                                                              |
| ----------------------------------------------------- | --------------------------------------------------------------------------------- |
| `config_changed` 事件回调引用了作用域外的变量         | 抛 `ReferenceError`，设置保存后页面徽标与扫描路径不刷新                           |
| `format_timestamp()` 引用未导入的 `datetime`          | 异常被 `except` 吞掉后**永远返回空串**，「远端更新时间」从未显示过                |
| `utils` 中时间解析的模块级依赖缺失                    | 同上，属静默失效：不报错，但功能完全没生效                                        |
| `/api/llamacpp/proxy` 的 `except` 用到未导入的 `json` | 上游返回非 JSON 时抛 `NameError`，把 500 变成 500 + 堆栈                          |
| 扫描「外部挂载目录」时按“相对扫描根”查来源注册表      | 外部目录里的同名文件会**错配**到模型目录中同名文件的来源与更新状态                |
| 相对路径口径混用（Windows 反斜杠 vs 正斜杠）          | 可能出现“明确有来源记录却显示未知来源”“点检测报无记录”                            |
| `search_limit` 未列入整数键                           | 环境变量注入时以字符串进入配置                                                    |
| `hf_search_budget` 被写成 0 / 负数                    | 搜索第 1 档直接跳过，最后 `raise None` 变成 `TypeError`                           |
| 前端未使用已实现的批量体积接口，仍逐个模型请求        | 50 条搜索结果产生 50 次 HTTP + 50 次上游文件列表拉取，实测耗时约 5 秒、日志被刷满 |
| 上游文件列表没有缓存                                  | 同一次交互里（补体积、打开「查看」）对同一模型重复拉取完整文件列表                |
| llama.cpp 重载盲试候选 id                             | 每次重载都白扔 2 个必然失败的 `POST /models/load`，每个都要等一次 HTTP 往返       |
| 重载时重复拉取 `/models`                              | pre-refresh 已带回模型列表，选 id 时又请求一次                                    |

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
