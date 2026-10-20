# 工作台项目 · 单镜像交付（SPEC.md 第 8 节）
# 构建：docker build -t workbench:latest .
# 运行：docker compose up -d（推荐，见 docker-compose.yml）
FROM python:3.12-slim

# ---- 运行时基础环境 ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    VAULT_DIR=/vault \
    PORT=8080

WORKDIR /app

# tzdata：zoneinfo 解析 Asia/Shanghai 需要系统时区库
# sqlite3：容器内可直接执行 scripts/backup.sh 的 VACUUM INTO 备份
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# 先装依赖：只改业务代码时不会击穿这一层缓存
# PIP_INDEX_URL 可选构建参数：默认空 = 官方 PyPI；国内网络可用 --build-arg 指定镜像加速
ARG PIP_INDEX_URL=""
COPY requirements.txt ./
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
COPY scripts ./scripts
COPY SPEC.md README.md ./

# 非 root 运行：uid 10001 固定，便于在 NAS 上对齐目录属主
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin workbench \
 && mkdir -p /data /vault /backup \
 && chown -R workbench:workbench /app /data /backup

USER workbench

EXPOSE 8080

# 健康检查不依赖 curl：容器内用 python 打 /healthz（该路径不鉴权）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
