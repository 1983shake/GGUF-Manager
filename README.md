# GGUF 模型管理器

一个基于 **Python 3.12 + FastAPI** 的 llama.cpp GGUF 模型管理工具，提供 Web UI 界面，支持从 **HuggingFace** 和 **ModelScope** 搜索、下载 GGUF 模型，并对本地模型文件进行管理。

项目通过 Docker 部署，模型文件可通过 Volume 挂载持久化保存。

---

## ✨ 功能特性

- 🔍 **多来源模型搜索**：支持 HuggingFace 与 ModelScope 两大模型源
- 🔎 **模型名称模糊查找**：输入关键词即可搜索相关模型
- 🎯 **GGUF 过滤选项**：可一键过滤掉不含 GGUF 文件的模型
- ⬇️ **GGUF 模型下载**：支持从远程仓库下载 `.gguf` 文件到本地指定目录
- 📁 **本地模型管理**：扫描指定路径下的 GGUF 模型，支持查看与删除
- 🌐 **Web UI 界面**：基于浏览器操作，无需命令行
- 🐳 **Docker 部署**：一键构建启动，模型目录挂载持久化
- 🚀 **镜像加速支持**：支持 `hf-mirror.com` 等镜像端点，加速国内下载
- 💾 **断点续传**：基于 `hf_transfer`，下载中断可恢复

---

## 📸 界面预览

Web UI 主要包含两个页面：

| 页面         | 功能                                                 |
| ------------ | ---------------------------------------------------- |
| **模型搜索** | 选择来源、输入关键词、勾选 GGUF 过滤、查看结果并下载 |
| **本地管理** | 扫描指定路径、查看已下载模型、删除模型文件           |

---

## 🏗️ 技术栈

| 层         | 技术                        |
| ---------- | --------------------------- |
| 后端框架   | FastAPI + Uvicorn           |
| 前端       | HTML + Vanilla JS + CSS     |
| 模型源 SDK | huggingface_hub、modelscope |
| 下载加速   | hf_transfer                 |
| 容器化     | Docker + docker-compose     |
| Python     | 3.12                        |

---

## 📁 项目结构
```

gguf-manager/
├── app/
│ ├── **init**.py
│ ├── main.py # FastAPI 入口
│ ├── config.py # 配置管理
│ ├── models.py # Pydantic 数据模型
│ ├── routers/
│ │ ├── **init**.py
│ │ ├── search.py # 模型搜索 API
│ │ ├── download.py # 模型下载 API
│ │ └── manage.py # 本地模型管理 API
│ ├── services/
│ │ ├── **init**.py
│ │ ├── huggingface.py # HuggingFace 搜索/下载
│ │ ├── modelscope.py # ModelScope 搜索/下载
│ │ └── local_scanner.py # 本地 GGUF 文件扫描
│ └── static/
│ ├── index.html # 主页面
│ ├── style.css # 样式
│ └── app.js # 前端逻辑
├── models/ # 默认模型存储目录（Docker 挂载）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── LICENSE # GPL-3.0 许可证文件
└── README.md

````

---

## 🚀 快速开始

### 前置要求

- Docker 20.10+
- Docker Compose v2+
- 可选：HuggingFace Token（下载受限模型时需要）

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd gguf-manager
````

### 2. （可选）设置 HuggingFace Token

如果计划下载需要授权的模型（如 Llama 系列），请先配置 Token：

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
```

Token 可在 https://huggingface.co/settings/tokens 获取。

### 3. 构建并启动

```bash
docker compose up -d --build
```

### 4. 访问 Web UI

浏览器打开：

```
http://localhost:8200
```

### 5. 查看日志

```bash
docker compose logs -f
```

### 6. 停止服务

```bash
docker compose down
```

---

## ⚙️ 配置项

所有配置通过 `docker-compose.yml` 的 `environment` 段或环境变量传递。

| 环境变量                    | 默认值        | 说明                                                          |
| --------------------------- | ------------- | ------------------------------------------------------------- |
| `MODEL_DIR`                 | `/app/models` | 容器内模型存储目录                                            |
| `HF_ENDPOINT`               | （空）        | HuggingFace 端点，可设为 `https://hf-mirror.com` 使用镜像加速 |
| `HF_TOKEN`                  | （空）        | HuggingFace 访问令牌，下载 gated 模型时需要                   |
| `SEARCH_LIMIT`              | `50`          | 每次搜索返回的最大结果数量                                    |
| `HF_HUB_ENABLE_HF_TRANSFER` | `1`           | 启用 hf_transfer 加速下载（默认开启）                         |

### 修改默认端口

如需修改端口（例如改为 `9000`），请同步修改以下两处：

1. `Dockerfile` 中的 `EXPOSE` 和 `CMD` 的 `--port`
2. `docker-compose.yml` 中的 `ports` 映射

---

## 📖 使用说明

### 一、搜索模型

1. 切换到 **模型搜索** 标签页
2. 选择来源：`HuggingFace` 或 `ModelScope`
3. 在输入框中输入关键词（如 `qwen2.5`、`llama-3`、`gemma`）进行模糊搜索
4. 可选：勾选/取消 **"仅显示含 GGUF 的模型"**
   - 勾选：只展示确实包含 `.gguf` 文件的模型
   - 取消：展示所有匹配的模型（便于查看是否有其他格式）
5. 点击 **搜索** 按钮

### 二、下载模型

1. 在搜索结果中找到目标模型，点击 **下载** 按钮
2. 弹窗会列出该仓库中所有可下载的 GGUF 文件及大小
3. 选择需要的量化版本（如 `Q4_K_M`、`Q5_K_M`、`Q8_0` 等），点击对应的 **下载**
4. 下载任务在后台执行，完成后可在 **本地管理** 页面查看

> 💡 **提示**：不同量化版本大小和精度不同。通常推荐 `Q4_K_M`（平衡）或 `Q5_K_M`（更高质量）。

### 三、本地模型管理

1. 切换到 **本地管理** 标签页
2. 默认扫描 `MODEL_DIR`（`/app/models`）
3. 可在输入框中指定其他已挂载到容器内的路径
4. 点击 **扫描本地模型**，展示所有 `.gguf` 文件
5. 点击 **删除** 可移除指定模型文件

> ⚠️ **安全限制**：删除操作被限制在 `MODEL_DIR` 目录范围内，防止越权删除宿主机文件。

---

## 🔌 API 接口

后端提供以下 REST API，方便与其他系统集成。

| 方法     | 路径                  | 说明                       |
| -------- | --------------------- | -------------------------- |
| `GET`    | `/api/search`         | 搜索模型                   |
| `GET`    | `/api/download/files` | 获取仓库中的 GGUF 文件列表 |
| `POST`   | `/api/download`       | 启动下载任务               |
| `GET`    | `/api/models`         | 扫描本地 GGUF 模型         |
| `DELETE` | `/api/models`         | 删除指定模型               |
| `GET`    | `/api/config`         | 获取服务端配置             |

### 示例：搜索模型

```bash
curl "http://localhost:8200/api/search?q=qwen2.5&source=huggingface&gguf_only=true&limit=10"
```

### 示例：下载模型

```bash
curl -X POST http://localhost:8200/api/download \
  -H "Content-Type: application/json" \
  -d '{
    "model_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
    "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
    "source": "huggingface"
  }'
```

### 示例：查看本地模型

```bash
curl "http://localhost:8200/api/models"
```

---

## 🐳 Docker 部署详解

### 挂载模型目录

默认 `docker-compose.yml` 将宿主机的 `./models` 挂载到容器的 `/app/models`：

```yaml
volumes:
  - ./models:/app/models
```

如需挂载到宿主机其他路径（例如 NAS 存储）：

```yaml
volumes:
  - /mnt/nas/gguf-models:/app/models
```

### 挂载额外路径供本地管理扫描

如需扫描多个目录，可添加多个挂载：

```yaml
volumes:
  - ./models:/app/models
  - /mnt/external/models:/app/external-models
```

之后在 Web UI 的"扫描路径"输入 `/app/external-models` 即可。

> ⚠️ 注意：删除操作仍受 `MODEL_DIR` 限制。若希望外部路径也可删除，需调整 `app/services/local_scanner.py` 中的安全校验逻辑。

### 使用国内镜像加速

`docker-compose.yml` 中默认已启用 HuggingFace 镜像：

```yaml
environment:
  - HF_ENDPOINT=https://hf-mirror.com
```

如需使用官方源，将此环境变量设为空即可：

```yaml
environment:
  - HF_ENDPOINT=
```

---

## ❓ 常见问题

### Q1: 搜索 HuggingFace 时很慢或超时？

**A**: 配置 `HF_ENDPOINT=https://hf-mirror.com` 使用国内镜像。

### Q2: 下载模型时报 401/403？

**A**: 该模型为 gated 模型，需要在 HuggingFace 上申请权限并配置 `HF_TOKEN`。

### Q3: 下载中断怎么办？

**A**: 本工具默认启用 `hf_transfer` 与断点续传，重新点击下载即可从上次进度继续。

### Q4: 如何修改端口？

**A**: 修改 `Dockerfile` 与 `docker-compose.yml` 中的端口配置（详见"修改默认端口"一节），然后重新构建：

```bash
docker compose up -d --build
```

### Q5: 下载的模型在哪里？

**A**: 默认保存到宿主机的 `./models` 目录（相对 `docker-compose.yml` 所在位置）。可在 `docker-compose.yml` 中修改挂载路径。

### Q6: 如何与 llama.cpp 联动？

**A**: 下载的 GGUF 文件可直接用于 llama.cpp：

```bash
./llama-server -m /path/to/models/your-model.gguf --port 8080
```

或使用 `llama-cli`、`llama-server` 等工具加载。

### Q7: 支持 Ollama 吗？

**A**: 本工具仅提供 GGUF 文件的下载与管理，不直接集成 Ollama。但下载的 GGUF 文件可通过 `Modelfile` 导入 Ollama：

```
FROM /app/models/your-model.gguf
```

---

## 🛠️ 本地开发（非 Docker）

```bash
# 1. 创建虚拟环境
python3.12 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 设置环境变量
export MODEL_DIR=./models
export HF_ENDPOINT=https://hf-mirror.com

# 4. 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8200 --reload
```

---

## 📄 License

本项目采用 **GNU General Public License v3.0 (GPL-3.0)** 许可协议发布。

Copyright (C) 2024 GGUF Manager Contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

完整的许可证文本请参见项目根目录下的 [LICENSE](./LICENSE) 文件，或访问
<https://www.gnu.org/licenses/gpl-3.0.html>。

---

## 🙏 致谢

- [llama.cpp](https://github.com/ggerganov/llama.cpp) — GGUF 格式与推理引擎
- [HuggingFace Hub](https://huggingface.co/) — 模型托管
- [ModelScope](https://modelscope.cn/) — 模型托管
- [FastAPI](https://fastapi.tiangolo.com/) — Web 框架

---
