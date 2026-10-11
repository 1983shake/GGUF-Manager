# GGUF-Manager

基于 Docker 的 GGUF 模型管理器：搜索 HuggingFace / ModelScope 上的 GGUF 模型，在线下载、本地管理、检测更新，并在下载完成后让 llama.cpp **无需重启容器即可热切换模型**。

- **搜索**：关键词模糊搜索，结果带文件大小与 GGUF 文件数
- **下载**：流式下载，含实时进度、速度、剩余时间，可随时取消并支持断点续传
- **管理**：本地模型列表、有效性校验、缓存清理、单模型 / 批量更新检测
- **集成**：对接 llama.cpp 路由模式与 llama-swap，新增 / 删除模型后自动同步
- **实时推送**：基于 SSE 的事件驱动架构，下载完成 / 检测结束 / 配置变更无需手动刷新

---

## 一、部署

三种方式，按推荐程度排序。日常使用选第一种即可。

### 方式一：docker-compose（推荐）

一条命令启动，配置与模型都通过挂载持久化，升级也最省事。

#### 1. 新建目录

```bash
mkdir gguf-manager && cd gguf-manager
```

#### 2. 创建 docker-compose.yml

```yaml
# docker-compose.yml
# 首次启动后 ./config/settings.json 会自动生成，也可直接编辑该文件。

services:
  gguf-manager:
    # 本地构建（用同目录下的 Dockerfile）
    #build: .
    # 使用已发布镜像时改为下面这行，并注释掉上面的 build
    image: docker.cnb.cool/1983shake/gguf-manager:latest
    container_name: gguf-manager
    restart: unless-stopped

    ports:
      - "8200:8200"

    volumes:
      # 模型文件（大文件，建议放空间充足的盘）
      - ./models:/app/models

      # ⭐ 配置持久化卷：Web 设置面板的所有改动都存这里，
      - ./config:/app/config

      # Docker socket：用于"重载方式 = Shell 命令"时执行 docker restart
      - /var/run/docker.sock:/var/run/docker.sock

      # 可选：额外的只读模型目录，在 Web 的"扫描路径"里填 /app/external-models
      # - /mnt/nas/models:/app/external-models:ro

    environment:
      # 容器内模型目录（改挂载路径时同步调整）
      - MODEL_DIR=/app/models

      # 配置目录：指向上面挂载的卷
      - CONFIG_DIR=/app/config

      # 日志时区。容器默认 UTC，这里设成北京时间
      - LOG_TZ=Asia/Shanghai
```

#### 3. 启动并访问

```bash
docker compose up -d
```

浏览器打开 **http://<宿主IP>:8200**。

#### 4. 常用命令

```bash
docker compose logs -f                       # 查看日志
docker compose restart                       # 重启
docker compose down                          # 停止并移除容器（数据保留）
docker compose pull && docker compose up -d  # 拉取新镜像并更新
```

#### 目录与数据

启动后自动生成两个目录，**请勿删除**：

| 目录       | 用途                                                     |
| ---------- | -------------------------------------------------------- |
| `./models` | 模型文件                                                 |
| `./config` | 配置与来源注册表（**必须挂载，否则重建容器后设置会丢**） |

#### 关键配置说明

**端口、模型目录、配置目录、时区**属于部署层配置，写在 `docker-compose.yml`：

```yaml
ports:
  - "8200:8200" # 宿主:容器
volumes:
  - ./models:/app/models
  - ./config:/app/config
  - /var/run/docker.sock:/var/run/docker.sock # 用 Docker 命令重载时必填
environment:
  - MODEL_DIR=/app/models
  - CONFIG_DIR=/app/config
  - LOG_TZ=Asia/Shanghai
  # - HF_TOKEN=hf_xxx    # 下载需授权的模型时填写
```

> ⚠️ **改端口时三处必须一致**：`Dockerfile` 的 `EXPOSE`、`Dockerfile` 的 `CMD --port`、`docker-compose.yml` 的 `ports`。不一致会导致宿主转发到容器内没有服务监听的端口，浏览器直接连不上。

以下三项**在 Web「设置」面板配置**，不要写进 compose（面板值会持久化到 config 卷并优先于环境变量）：

- HuggingFace 镜像加速
- 日志级别
- 日志保留条数

---

### 方式二：命令行部署

适合不装 compose、或要把启动命令写进脚本的场景。

```bash
docker run -d --name gguf-manager --restart unless-stopped \
  -p 8200:8200 \
  -v ./models:/app/models \
  -v ./config:/app/config \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e MODEL_DIR=/app/models \
  -e CONFIG_DIR=/app/config \
  -e LOG_TZ=Asia/Shanghai \
  docker.cnb.cool/1983shake/gguf-manager:latest
```

常用命令：

```bash
docker logs -f gguf-manager    # 查看日志
docker restart gguf-manager    # 重启
docker stop gguf-manager       # 停止
docker rm -f gguf-manager      # 移除容器（数据保留）
```

#### 从源码构建镜像

需要同目录下的 `Dockerfile`、`requirements.txt`、`app/`：

```bash
docker build -t gguf-manager .
docker run -d --name gguf-manager --restart unless-stopped \
  -p 8200:8200 \
  -v ./models:/app/models \
  -v ./config:/app/config \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e MODEL_DIR=/app/models \
  -e CONFIG_DIR=/app/config \
  -e LOG_TZ=Asia/Shanghai \
  gguf-manager
```

---

### 方式三：本地代码部署

适合开发调试或二次开发，需要 **Python 3.12**。

```bash
git clone https://github.com/1983shake/GGUF-Manager.git
cd GGUF-Manager

# 建议用虚拟环境
python -m venv .venv && source .venv/bin/activate

pip install -r requirements.txt

export MODEL_DIR=./models CONFIG_DIR=./config
uvicorn app.main:app --host 0.0.0.0 --port 8200
```

开发时可用 `--reload` 让代码改动自动生效：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8200 --reload
```

> 不装 `modelscope` SDK 是刻意为之：ModelScope 搜索走 HTTP API，装它会额外拉进几百 MB 的 torch 依赖。

---

## 二、使用说明

### 搜索与下载

1. 选择来源（HuggingFace / ModelScope），输入关键词，勾选「仅显示含 GGUF 的模型」
2. 点「查看」展开 GGUF 文件列表，选择量化版本后点「下载」
3. 右下角任务面板显示进度、速度、剩余时间，可随时取消

### 本地管理

- 查看本地模型、有效性（有效 / 未完成 / 损坏）与来源
- 支持单个或批量的检测更新、更新、删除
- 「清理缓存」用于删除取消或失败下载残留的 `.part` 文件

### 设置面板

| 分组         | 说明                                                   |
| ------------ | ------------------------------------------------------ |
| 数据源与日志 | HuggingFace 镜像、日志级别、日志保留条数、模型扫描路径 |
| 任务面板     | 面板与通知的展示时长（默认 3 秒）                      |
| 网络         | 连接 / 读取超时、重试次数、搜索总时限                  |
| llama.cpp    | 服务地址、服务端类型、下载完成后自动重载               |
| 更新检测     | 定期检测开关与间隔                                     |

设置保存后立即生效，无需重启容器。

---

## 三、llama.cpp 集成（重点）

### 3.1 三种服务端形态

| 类型                   | 说明                                                | 支持热切换        |
| ---------------------- | --------------------------------------------------- | ----------------- |
| **llama.cpp 路由模式** | 官方 router 模式，支持模型列表、加载/卸载、重新扫描 | ✅ 无需重启       |
| **llama-swap**         | 通过 `/v1/models` 管理，加载由推理请求自动触发      | ✅ 无需重启       |
| 普通 llama-server      | 单模型，可用 HTTP `/update` 或自定义命令重载        | ⚠️ 通常需重启容器 |

**强烈推荐使用路由模式**。

### 3.2 服务端配置（docker-compose.yml）

路由模式的关键是**不指定 `-m / LLAMA_ARG_MODEL`**，改为用 `LLAMA_ARG_MODELS_DIR` 指定模型目录：

```yaml
services:
  llama-cpp-server:
    image: ghcr.io/ggml-org/llama.cpp:server
    container_name: llama-cpp-server
    restart: always
    ports:
      - "8085:8080"
    volumes:
      # ⭐ 与 GGUF-Manager 挂载同一个模型目录（路径可以不同，但要指向同一份文件）
      - /vol2/1000/docker_cache/llama-cpp_models:/models
      - /vol2/1000/docker_cache/llama-cpp_cache:/root/.cache
    environment:
      - TZ=Asia/Shanghai
      - LLAMA_ARG_HOST=0.0.0.0
      - LLAMA_ARG_PORT=8080
      - LLAMA_ARG_THREADS=3.5
      - LLAMA_ARG_BATCH=512
      - LLAMA_ARG_CTX_SIZE=65536
      - LLAMA_ARG_N_GPU_LAYERS=0

      # ⭐⭐⭐ 路由模式三件套（缺一不可）
      - LLAMA_ARG_MODELS_DIR=/models # 扫描模型目录
      - LLAMA_ARG_MODELS_MAX=1 # 同时只加载 1 个模型
      - LLAMA_ARG_MODELS_AUTOLOAD=true # 首次请求时自动加载


      # ⚠️ 千万不要设置 LLAMA_ARG_MODEL / -m，否则退化为单模型模式，
      #    路由端点 /models/load 会直接返回 404
```

### 3.3 GGUF-Manager 端设置

打开 **设置 → llama.cpp**：

| 配置项                     | 推荐值                                                |
| -------------------------- | ----------------------------------------------------- |
| 启用 llama.cpp 集成        | ✅ 勾选                                               |
| 服务端类型                 | `llama.cpp 路由模式`（若不确定先选"自动探测"）        |
| 服务地址                   | `http://192.168.1.100:8080`（改为你的服务端 IP:端口） |
| 模型下载完成后自动重新加载 | ✅ 勾选                                               |
| 重载方式                   | `HTTP 请求`                                           |

保存后，点 **「测试连接」** 应显示 `✓ 在线（/models）`，点 **「模型列表」** 应能看到服务端已识别的所有 `.gguf` 文件名。

### 3.4 热切换工作原理（v1.3.0 关键修复）

llama.cpp 路由模式有一个**关键行为**：

> **只在启动时扫描一次 `LLAMA_ARG_MODELS_DIR` 目录，运行期间不监听磁盘变化。**

这意味着新下载的模型文件虽然已经写到磁盘，但**服务端内存里没有它的记录**，直接 `POST /models/load` 会返回：

```json
{
  "error": {
    "message": "File Not Found",
    "type": "not_found_error",
    "code": 404
  }
}
```

**正确流程**（GGUF-Manager v1.3.0 已自动化）：

```
下载完成 → 写入 /models/xxx.gguf
   ↓
① GET  /models?reload=1        ← ⭐ 让服务端重新扫描磁盘（必做！）
   ↓
② POST /models/load {"model":"xxx.gguf"}   ← 加载新模型，不重启容器
   ↓
服务端自动卸载旧模型 → 加载新模型 → 秒级完成
```

日志中会看到：

```
[llamacpp] router flavor detected, pre-refresh via GET /models?reload=1 before load
[llamacpp] pre-refresh result: ok=True, models_count=4    ← 服务端识别到 4 个模型
[llamacpp] POST .../models/load payload={'model': 'qwen2.5-coder-7b-instruct-q4_k_m.gguf'}
[llamacpp] reload OK: /models/load id=qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

### 3.5 手动诊断（可选）

遇到问题时，可在命令行手动验证全链路：

```bash
# ① 让服务端重新扫描磁盘，观察模型数量变化
curl 'http://192.168.1.100:8080/models?reload=1' | jq '.data | length'

# ② 查看服务端当前识别到的所有模型 id
curl 'http://192.168.1.100:8080/models' | jq '.data[].id'

# ③ 手动加载某个模型（id 用上一步返回的）
curl -X POST 'http://192.168.1.100:8080/models/load' \
     -H 'Content-Type: application/json' \
     -d '{"model":"qwen2.5-coder-7b-instruct-q4_k_m.gguf"}'

# ④ 看服务端日志确认加载成功
docker logs --tail 30 llama-cpp-server
```

只要 ① 能返回新的模型 id，说明磁盘和扫描都正常；③ 成功则说明热切换链路完全打通。

### 3.6 常见问题

#### Q: 点「立即重载」报 404 File Not Found？

- 检查服务端是否真的运行在路由模式：`curl http://<服务端>/models` 应返回 `{"data":[...]}` 结构
- 检查服务端 docker-compose 是否**误设置了** `LLAMA_ARG_MODEL`（应删除）
- 检查 GGUF-Manager 和 llama.cpp **是否挂载了同一个模型目录**（宿主机上应该是同一份文件）

#### Q: 报 400 `model is already running`？

这是**幂等成功**——目标模型已经在运行，无需重复加载。v1.3.0 会自动识别并提示成功。

#### Q: 报 500 `model limit reached`？

服务端已加载的模型数达到 `LLAMA_ARG_MODELS_MAX`。v1.3.0 会自动尝试「先 unload 再 load」兜底。或改用：

- 方式 A：卸载其他模型后再试
- 方式 B：把 `LLAMA_ARG_MODELS_MAX` 调大

#### Q: 普通 llama-server（用 `-m` 启动）怎么办？

普通 server 的 `/update` 端点**不是所有编译版本都有**，Docker 默认镜像经常不带。此时唯一可靠的方式是重启容器：

1. GGUF-Manager 设置 → 「重载方式」改为 **`Shell 命令`**
2. 命令填：`docker restart llama-cpp-server`
3. 确保 GGUF-Manager 容器挂载了 `/var/run/docker.sock`

#### Q: 服务端容器名不叫 `llama-cpp-server`？

改命令里的容器名即可。`docker ps --format '{{.Names}}'` 可以查看实际名字。

---

## 四、注意事项

1. **配置必须挂载**：`./config` 不挂卷时容器重建会丢失全部设置。启动日志会检测并提示是否挂载成功。
2. **国内网络务必配置镜像**：容器内通常无法直连 `huggingface.co`，请在「设置 / 数据源与日志 / HuggingFace 镜像」填 `https://hf-mirror.com`。
3. **更新检测只对通过本工具下载的模型生效**：手动拷入 `models/` 的文件没有来源记录，状态显示「未知来源」，无法检测更新。
4. **取消下载会保留 `.part` 文件**：这是为断点续传设计的，重新下载同一文件会自动从断点继续；不再需要时用「清理缓存」删除。
5. **任务列表存于内存**：容器重启后列表清空，但磁盘上的 `.part` 仍在，重新下载会自动续传。
6. **更新采用原子替换**：下载写入临时文件后整体替换，过程中旧模型保持可用，失败也不会损坏现有文件。
7. **llama.cpp 必须挂载同一份模型文件**：GGUF-Manager 和 llama.cpp 是两个独立容器，各自挂载的 `/models` 必须**指向宿主机上的同一个目录**。否则 GGUF-Manager 下载完，llama.cpp 的目录里根本没有这个文件。
8. **路由模式必须移除 `-m / LLAMA_ARG_MODEL`**：只要设置了这个变量，llama.cpp 就会退化为单模型模式，`/models/load` 返回 404。

---

## 五、v1.3.0 更新日志

### 核心变更

- **事件驱动架构**：引入 SSE 事件总线，状态变化主动推送到前端，彻底解决"更新状态不及时刷新"问题
- **实时状态同步**：下载完成、检测结束、配置变更等操作完成后自动刷新界面，无需手动检测
- **代码优化**：从 19 个 Python 文件重构为更清晰的模块化架构（routers + sources 分层）
- **合并冗余路由**：将 download.py + tasks.py 合并为 downloads.py，config.py + logs.py + llamacpp.py 合并为 system.py

### llama.cpp 集成改进

- ⭐ **路由模式热切换修复**：新增 `GET /models?reload=1` 前置刷新步骤，彻底解决"新下载的模型无法被服务端识别"问题
- ⭐ **候选 id 多形式尝试**：自动尝试带/不带 `.gguf` 后缀、大小写归一、模糊匹配等多种 id 形式
- ⭐ **400 / 500 智能处理**：`model is already running` 视为幂等成功；500 时自动尝试 unload + load 兜底
- ⭐ **响应体完整记录**：失败时把每次请求的完整响应体返回给前端，便于排查

### 事件类型

| 事件名            | 触发时机                      |
| ----------------- | ----------------------------- |
| `model_updated`   | 模型记录（来源/更新状态）变化 |
| `check_completed` | 一轮更新检测结束              |
| `task_updated`    | 下载任务状态变化              |
| `config_changed`  | 运行时配置被修改              |
| `scheduler_tick`  | 定期检测调度器状态变化        |

---

## 六、许可

本项目基于 **GNU General Public License v3.0（GPL-3.0）** 开源。

- 你可以自由使用、修改和分发本项目
- 二次分发时必须同样以 GPL-3.0 开源
- 本项目不提供任何担保

完整条款见 <https://www.gnu.org/licenses/gpl-3.0.html>

---

## 七、致谢

- [llama.cpp](https://github.com/ggml-org/llama.cpp) —— GGUF 格式与推理服务
- [HuggingFace Hub](https://huggingface.co) 与 [ModelScope](https://modelscope.cn) —— 模型来源
- [hf-mirror.com](https://hf-mirror.com) —— 国内镜像加速
- [llama-swap](https://github.com/mostlygeek/llama-swap) —— 多模型热切换代理
- [FastAPI](https://fastapi.tiangolo.com) —— Web 框架
- 所有提交 issue 与建议的用户

项目地址：<https://github.com/1983shake/GGUF-Manager>
