# Dockerfile
# ============================================================
# 基础镜像（可通过 --build-arg BASE_IMAGE=... 覆盖）
# ============================================================
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# ---------- apt 阿里云源 ----------
# Debian 12+ 用 deb822 格式的 debian.sources，旧版用 sources.list，两种都处理
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list; \
    fi

# tzdata：日志时区功能依赖 zoneinfo，slim 镜像默认不带，
# 缺失时只能回退到固定偏移，涉及夏令时的时区会算错时间。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# ---------- pip 阿里云源 ----------
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# 模型目录与配置目录分开：配置单独挂卷，换模型挂载点时设置不会丢
RUN mkdir -p /app/models /app/config

# ---------- 环境变量 ----------
ENV MODEL_DIR=/app/models

# ⭐ 配置持久化目录。首次启动会在这里生成 settings.json，
#    Web 设置面板的所有改动都保存在此，必须挂载 volume 才能跨容器保留。
ENV CONFIG_DIR=/app/config

# ⭐ 日志级别只在 Web「设置」面板配置（改完即时生效，不用重启）。
#    这里刻意不设 LOG_LEVEL，避免"两处配置互相打架"。
#    同理 HF_ENDPOINT / LOG_BUFFER_SIZE 也请在面板设置，
#    这里只保留 SEARCH_LIMIT 这类与部署相关的项。
ENV SEARCH_LIMIT=50

# 需要下载 gated 模型（如 Llama 系列）时在这里或 compose 里填
ENV HF_TOKEN=

# 日志时区（也可在 Web 设置面板改）
ENV LOG_TZ=Asia/Shanghai

ENV PYTHONUNBUFFERED=1

# ⚠️ 原文件里的 HF_HUB_ENABLE_HF_TRANSFER=1 已移除：
#    它需要额外安装 hf_transfer 包，没装的情况下该变量无效且会刷警告。
#    本项目已改用自实现的 httpx 流式下载（支持进度/速度/取消/断点续传），
#    不需要 hf_transfer。若确实想开启，请在 requirements.txt 加上该包。

# 容器内端口改为 8200（与 CMD 保持一致）
EXPOSE 8200

# 数据卷：模型 + 配置
VOLUME ["/app/models", "/app/config"]

# 健康检查：端口随 EXPOSE 同步为 8200
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8200/api/version || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
