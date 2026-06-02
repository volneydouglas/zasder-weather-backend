# Pure-stdlib service — base just needs a recent Python and nothing else.
FROM python:3.12-slim

WORKDIR /app
COPY poller.py .

# Run as non-root inside the container.
RUN useradd -r -u 10001 -s /sbin/nologin wll && chown -R wll:wll /app
USER wll

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/poller.py"]
