"""Davis ISS over-the-air packet parser.

Takes the 8 raw bytes of a Davis ISS FHSS packet (as decoded by
rtldavis from the 915 MHz radio) and returns a dict of named fields.

Protocol references:
  https://github.com/dekay/DavisRFM69/wiki/Message-Protocol
  https://github.com/lheijst/weewx-rtldavis/blob/master/bin/user/rtldavis.py

Packet layout (8 bytes):
  Byte 0: header
    bits 7-4 = message type (0x2..0xE rotating)
    bit 3    = battery-low flag
    bits 2-0 = transmitter id (0..7) → "channel" 1..8 in Davis UI
  Byte 1: wind speed (mph, raw 0-255)
  Byte 2: wind direction (raw 0-255 → 0-360°)
  Bytes 3-6: rotating payload (selected by message type)
  Byte 7: CRC-16 (low byte; we don't re-validate here — rtldavis already did)

The rotating message types we decode (others are ignored / sensor-suite-
specific):
  0x2 = supercap voltage              (solar-powered ISS — house bat for ISS-W)
  0x4 = UV index
  0x6 = solar radiation
  0x8 = temperature (digital path)
  0x9 = wind gust (10-min max, mph)
  0xA = humidity (digital path)
  0xE = rain bucket tip count (rolling 7-bit counter)

This module is intentionally framework-free — no logging deps, no
weewx imports. parse() returns a dict; caller decides what to do.
"""
from __future__ import annotations


WIND_DIR_OFFSET_DEG = 0
"""Davis VP2 wind direction is broadcast as 0-255 → 0-360°. Some
sensor revisions need a small calibration offset (typically a few
degrees). If yours points slightly off true north, tweak via the
DAVIS_WIND_DIR_OFFSET env var rather than hand-editing this."""


def parse(raw: bytes, wind_dir_offset_deg: float = 0.0) -> dict[str, float | int]:
    """Decode one Davis ISS 8-byte FHSS packet.

    Returns a dict containing whatever fields this specific packet
    carried — wind is always present, plus exactly one rotating field
    per packet (or zero, for message types we don't handle yet).

    Caller is expected to merge consecutive parse() results into a
    single observation since Davis only sends one rotating field per
    packet (~10 packet types, ~2.5s cycle = full update every ~25s)."""
    if len(raw) != 8:
        raise ValueError(f"expected 8 bytes, got {len(raw)}")

    out: dict[str, float | int] = {}

    # Byte 0 — header
    header = raw[0]
    msg_type = (header >> 4) & 0x0F
    transmitter_id = header & 0x07          # 0..7 (UI shows "channel" = id+1)
    battery_low = bool(header & 0x08)
    out["transmitter_id"] = transmitter_id
    out["battery_low"] = battery_low

    # Wind — present in EVERY packet (bytes 1 and 2).
    # Byte 1 is mph directly (0-255). Sentinel: 0xFF means "no data".
    wspd_raw = raw[1]
    if wspd_raw != 0xFF:
        out["wind_speed_mph"] = float(wspd_raw)
    # Byte 2 is wind direction. Linear 0-255 → 0-360°. Wraps at 360.
    wdir_raw = raw[2]
    deg = (wdir_raw * 360.0 / 256.0) + wind_dir_offset_deg
    out["wind_dir_deg"] = deg % 360.0

    # Bytes 3-6 — rotating payload (dispatched by message type).
    if msg_type == 0x2:
        # Supercap voltage (ISS solar-powered models). Useful battery
        # health signal — drops below ~2.5 V indicates a dead supercap
        # that won't survive cloudy days.
        raw_v = ((raw[3] << 2) + (raw[4] >> 6)) & 0x3FF
        out["supercap_v"] = raw_v * 0.005859375

    elif msg_type == 0x4:
        # UV index. 0x3FF is the "no sensor" sentinel.
        raw_uv = ((raw[3] << 2) + (raw[4] >> 6)) & 0x3FF
        if raw_uv != 0x3FF:
            out["uv_index"] = raw_uv / 50.0

    elif msg_type == 0x6:
        # Solar radiation in W/m². 0x3FE = "no sensor".
        raw_sol = ((raw[3] << 2) + (raw[4] >> 6)) & 0x3FF
        if raw_sol < 0x3FE:
            out["solar_w_m2"] = raw_sol * 1.757936

    elif msg_type == 0x8:
        # Temperature, digital path (10ths of °F, signed).
        # 0xFFC = "no sensor" sentinel.
        raw_t = (raw[3] << 4) + (raw[4] >> 4)
        if raw_t != 0xFFC:
            # Handle two's-complement-ish: top bit set means negative.
            if raw_t & 0x800:
                raw_t -= 0x1000
            out["temp_f"] = raw_t / 10.0

    elif msg_type == 0x9:
        # Wind gust — 10-minute peak mph, raw 0-255.
        gust_raw = raw[3]
        if gust_raw != 0xFF:
            out["wind_gust_mph"] = float(gust_raw)

    elif msg_type == 0xA:
        # Humidity %, digital path. Raw is 10ths of percent in a
        # 12-bit field straddling bytes 3-4 (high nibble of byte 4
        # is the top 4 bits).
        raw_h = ((raw[4] >> 4) << 8) + raw[3]
        if raw_h:
            out["humidity_pct"] = raw_h / 10.0

    elif msg_type == 0xE:
        # Rain bucket tip count — a 7-bit free-running counter that
        # wraps at 128. Caller is responsible for delta-tracking
        # against the previous reading to compute new tips since
        # last sample. 0x80 sentinel = no sensor.
        rc = raw[3]
        if rc != 0x80:
            out["rain_count"] = rc & 0x7F

    # Message types 0x0, 0x1, 0x3, 0x5, 0x7, 0xB, 0xC, 0xD, 0xF carry
    # sensor-suite-specific data we don't need for a stock VP2 ISS
    # (extra temp/humidity stations, leaf wetness, soil moisture).
    # They're silently ignored.

    return out


def parse_line(line: str) -> tuple[bytes, dict[str, int]] | None:
    """Pull the 8-byte payload out of one rtldavis stdout line.

    rtldavis emits lines like:
        20:30:15.123456 80123456789ABCDEF0 1 2 3 4
    where the first token is a wall-clock timestamp, the second is
    16 hex chars = 8 bytes, and the trailing integers are channel
    counters (curr_cnt0..3 = packets received per channel slot).

    Returns (raw_bytes, counters_dict) or None if the line doesn't
    look like a data packet (CHANNELPacket / status / log lines).
    """
    parts = line.strip().split()
    if len(parts) < 2:
        return None
    hex_blob = parts[1]
    if len(hex_blob) != 16:
        return None
    try:
        raw = bytes.fromhex(hex_blob)
    except ValueError:
        return None
    counters: dict[str, int] = {}
    for i, tok in enumerate(parts[2:6]):
        try:
            counters[f"curr_cnt{i}"] = int(tok)
        except ValueError:
            break
    return raw, counters
