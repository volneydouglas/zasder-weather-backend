# Digest-pinned (2.1 pre-release review, INF-8): a floating tag can change
# under a rebuild without a diff. This is the multi-arch index for
# python:3.12-slim as published on Docker Hub on 2026-09-06; to move it,
# fetch the new one and change it here on purpose:
#   TOK=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
#   curl -sI -H "Authorization: Bearer $TOK" -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/python/manifests/3.12-slim | grep -i docker-content-digest
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The server runs as this user, not root (2.1). Fixed uid/gid so a file on
# the data volume keeps the same owner across image rebuilds.
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app \
            --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Persistent SQLite lives on a Fly volume mounted at /data. The volume
# arrives root-owned; docker-entrypoint.sh chowns it to `app` at boot and
# drops privileges before uvicorn starts. The mkdir here is for images run
# without a volume at all (a local smoke test).
ENV DATABASE_PATH=/data/weather.db
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /data && chown app:app /data

EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
