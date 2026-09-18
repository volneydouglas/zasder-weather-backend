# Digest-pinned, the same index as backend/Dockerfile: a floating tag can
# change under a rebuild without a diff. This is the multi-arch index for
# python:3.12-slim on Docker Hub (verified against the registry
# 2026-09-15; it matched the backend's 2026-09-06 pin). To move it, fetch
# the new one and change it here on purpose:
#   TOK=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
#   curl -sI -H "Authorization: Bearer $TOK" -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/python/manifests/3.12-slim | grep -i docker-content-digest
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app \
            --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV DATABASE_PATH=/data/map.db
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /data && chown app:app /data
EXPOSE 8080
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
