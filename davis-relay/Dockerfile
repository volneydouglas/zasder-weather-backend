# Two-stage build:
#   1. builder — compiles lheijst/rtldavis from source (needs Go + librtlsdr-dev)
#   2. runtime — debian-slim + librtlsdr0 + python3, the binary copied across
#
# Reason for source build: no upstream prebuilts exist for rtldavis, and the
# Pi runs Debian arm64. Multi-stage keeps the final image lean (~120 MB)
# without dragging Go's ~600 MB toolchain into the runtime.

FROM golang:1.23-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    librtlsdr-dev git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN go install github.com/lheijst/rtldavis@latest && \
    cp /go/bin/rtldavis /build/rtldavis

# ── runtime ──
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    librtlsdr0 python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/rtldavis /usr/local/bin/rtldavis
WORKDIR /app
COPY davis_relay.py davis_iss.py /app/
# rtldavis claims the USB SDR via libusb; container needs to be started
# with the host's /dev/bus/usb passed through (see docker-compose.yml).
CMD ["python3", "-u", "/app/davis_relay.py"]
