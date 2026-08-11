# Pure-stdlib service — base just needs a recent Python and nothing else.
FROM python:3.12-slim

WORKDIR /app
COPY poller.py .

# Run as non-root inside the container.
RUN useradd -r -u 10001 -s /sbin/nologin wll && chown -R wll:wll /app
USER wll

ENV PYTHONUNBUFFERED=1

# Liveness: the poller touches /tmp/wll-heartbeat once per loop iteration.
# A wedged process (loop stuck, thread hung) stops touching it and flips the
# container to `unhealthy` instead of sitting "Up" forever while posting
# nothing. Threshold scales with the configured poll interval.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import os,sys,time; poll=int(os.environ.get('WLL_POLL_SECONDS','10') or 10); sys.exit(0 if time.time()-os.path.getmtime(os.environ.get('HEARTBEAT_FILE','/tmp/wll-heartbeat')) < max(120, 6*poll) else 1)"]

ENTRYPOINT ["python3", "/app/poller.py"]
