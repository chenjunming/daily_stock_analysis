# syntax=docker/dockerfile:1.7
# ===================================
# A股自选股智能分析系统 - Docker 镜像
# ===================================
# 基于 Python 3.11 slim 镜像，体积小、启动快

FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海
ENV TZ=Asia/Shanghai
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装系统依赖
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl

# 复制依赖文件
COPY requirements.txt .

# 可选：通过 build-arg 指定 pip 镜像/额外源
ARG PIP_INDEX_URL=""
ARG PIP_EXTRA_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

# 安装 Python 依赖
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    PIP_ARGS="--prefer-binary -r requirements.txt"; \
    if [ -n "$PIP_INDEX_URL" ]; then PIP_ARGS="$PIP_ARGS --index-url $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_EXTRA_INDEX_URL" ]; then PIP_ARGS="$PIP_ARGS --extra-index-url $PIP_EXTRA_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then PIP_ARGS="$PIP_ARGS --trusted-host $PIP_TRUSTED_HOST"; fi; \
    pip install $PIP_ARGS

# 复制应用代码
COPY *.py ./
COPY data_provider/ ./data_provider/
COPY web/ ./web/
COPY bot/ ./bot/
COPY src/ ./src/

# 创建数据目录
RUN mkdir -p /app/data /app/logs /app/reports

# 设置环境变量默认值
ENV PYTHONUNBUFFERED=1
ENV LOG_DIR=/app/logs
ENV DATABASE_PATH=/app/data/stock_analysis.db
# WebUI应用
ENV WEBUI_HOST=0.0.0.0
ENV WEBUI_PORT=8000

# 暴露 WebUI 端口
EXPOSE 8000

# 数据卷（持久化数据）
VOLUME ["/app/data", "/app/logs", "/app/reports"]

# 健康检查（支持 WebUI 模式）
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || python -c "import sys; sys.exit(0)"

# 默认命令（可被覆盖）
CMD ["python", "main.py", "--schedule"]
