FROM python:3.13-slim

# openssl is needed for the first-boot self-signed cert generation; everything
# else this image needs is in the Python stdlib.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY relay.py .

EXPOSE 80 443
CMD ["python", "-u", "relay.py"]
