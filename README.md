# GGUF-Manager

基于 Docker 的 GGUF 模型管理器：搜索 HuggingFace / ModelScope 上的 GGUF 模型，在线下载、本地管理、检测更新，并可在下载完成后让 llama.cpp 自动切换模型。

- **搜索**：关键词模糊搜索，结果带文件大小与 GGUF 文件数
- **下载**：流式下载，含实时进度、速度、剩余时间，可随时取消并支持断点续传
- **管理**：本地模型列表、有效性校验、缓存清理、单模型 / 批量更新检测
- **集成**：对接 llama.cpp 路由模式与 llama-swap，新增 / 删除模型后自动同步

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

| 目录 | 用途 |
|---|---|
| `./models` | 模型文件 |
| `./config` | 配置与来源注册表（**必须挂载，否则重建容器后设置会丢**） |

#### 关键配置说明

**端口、模型目录、配置目录、时区**属于部署层配置，写在 `docker-compose.yml`：

```yaml
ports:
  - "8200:8200"          # 宿主:容器
volumes:
  - ./models:/app/models
  - ./config:/app/config
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

| 分组 | 说明 |
|---|---|
| 数据源与日志 | HuggingFace 镜像、日志级别、日志保留条数、模型扫描路径 |
| 任务面板 | 面板与通知的展示时长（默认 3 秒） |
| 网络 | 连接 / 读取超时、重试次数、搜索总时限 |
| llama.cpp | 服务地址、服务端类型、下载完成后自动重载 |
| 更新检测 | 定期检测开关与间隔 |

设置保存后立即生效，无需重启容器。

### llama.cpp 集成

支持三种服务端形态：

| 类型 | 说明 |
|---|---|
| llama.cpp 路由模式 | 支持模型列表、加载 / 卸载、重新扫描模型源 |
| llama-swap | 通过 `/v1/models` 管理，加载由推理请求自动触发 |
| 普通 llama-server | 单模型，可用 HTTP 请求或自定义命令重载 |

在设置页点「测试连接」确认连通，再点「模型列表」核对服务端识别到的模型。建议把「服务端类型」从自动探测改为显式指定，省掉每次探测。

---

## 三、注意事项

1. **配置必须挂载**：`./config` 不挂卷时容器重建会丢失全部设置。启动日志会检测并提示是否挂载成功。
2. **国内网络务必配置镜像**：容器内通常无法直连 `huggingface.co`，请在「设置 / 数据源与日志 / HuggingFace 镜像」填 `https://hf-mirror.com`。
3. **更新检测只对通过本工具下载的模型生效**：手动拷入 `models/` 的文件没有来源记录，状态显示「未知来源」，无法检测更新。
4. **取消下载会保留 `.part` 文件**：这是为断点续传设计的，重新下载同一文件会自动从断点继续；不再需要时用「清理缓存」删除。
5. **任务列表存于内存**：容器重启后列表清空，但磁盘上的 `.part` 仍在，重新下载会自动续传。
6. **更新采用原子替换**：下载写入临时文件后整体替换，过程中旧模型保持可用，失败也不会损坏现有文件。
7. **llama.cpp 需能访问模型路径**：若 llama.cpp 在另一个容器中，需将其模型目录挂载到同一份文件，或在设置里配置「模型名前缀」使其匹配服务端的模型名。

---

## 四、许可

本项目基于 **GNU General Public License v3.0（GPL-3.0）** 开源。

- 你可以自由使用、修改和分发本项目
- 二次分发时必须同样以 GPL-3.0 开源
- 本项目不提供任何担保

完整条款见 <https://www.gnu.org/licenses/gpl-3.0.html>

---

## 五、致谢

- [llama.cpp](https://github.com/ggml-org/llama.cpp) —— GGUF 格式与推理服务
- [HuggingFace Hub](https://huggingface.co) 与 [ModelScope](https://modelscope.cn) —— 模型来源
- [hf-mirror.com](https://hf-mirror.com) —— 国内镜像加速
- [llama-swap](https://github.com/mostlygeek/llama-swap) —— 多模型热切换代理
- [FastAPI](https://fastapi.tiangolo.com) —— Web 框架
- 所有提交 issue 与建议的用户

项目地址：<https://github.com/1983shake/GGUF-Manager>
