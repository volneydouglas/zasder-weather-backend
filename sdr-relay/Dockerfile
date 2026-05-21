FROM debian:trixie-slim

# rtl-433 (the decoder) + rtl-sdr (libraries + rtl_test/rtl_eeprom tools).
# soapysdr-module-rtlsdr is REQUIRED for rtl_433's `-d "serial=NAME"` syntax —
# that syntax routes the device-open through SoapySDR which then dispatches
# to the rtlsdr backend. Without it: `Failed to open sdr device matching
# 'serial=…'` and rtl_433 exits immediately. usbutils gives us `lsusb`
# inside the container for diagnostics.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      rtl-433 rtl-sdr soapysdr-module-rtlsdr soapysdr-tools \
      usbutils python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sdr_relay.py .

# All config via env vars (see .env.example). No persistent storage —
# we forward immediately and don't buffer to disk.
CMD ["python3", "-u", "sdr_relay.py"]
