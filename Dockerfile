ARG PYTHON_IMAGE=af.hikvision.com.cn/docker/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=http://af.hikvision.com.cn/artifactory/api/pypi/pypi/simple
ARG PIP_TRUSTED_HOST=af.hikvision.com.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt ./
# 使用已锁定版本的 Linux wheels；缺少内网缓存时明确失败，不回退公网或在线编译。
RUN python -m pip --isolated --disable-pip-version-check install \
    --index-url "${PIP_INDEX_URL}" \
    --trusted-host "${PIP_TRUSTED_HOST}" \
    --only-binary=:all: --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 analyzer \
    && useradd --uid 10001 --gid analyzer --create-home analyzer \
    && mkdir -p /app/runtime && chown 10001:10001 /app/runtime

COPY analyzer ./analyzer
COPY examples ./examples
COPY frontend/dist ./frontend/dist
USER analyzer
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8100/healthz', timeout=3)"

CMD ["uvicorn", "analyzer.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
