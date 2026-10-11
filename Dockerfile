# ============================================================
# GGUF-Manager Dockerfile
# ============================================================
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# ---------- apt 阿里云源 ----------
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list; \
    fi

# tzdata：日志时区功能依赖 zoneinfo，slim 镜像默认不带
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

RUN mkdir -p /app/models /app/config

# ---------- 环境变量 ----------
ENV MODEL_DIR=/app/models
ENV CONFIG_DIR=/app/config
ENV SEARCH_LIMIT=50
ENV HF_TOKEN=
ENV LOG_TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# 容器内端口改为 8200（与 CMD 保持一致）
EXPOSE 8200

# 数据卷：模型 + 配置
VOLUME ["/app/models", "/app/config"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8200/api/version || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
