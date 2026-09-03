"""Device-staleness email alerting.

A background task polls each device's last-report age and emails the operator
when a device that was reporting goes quiet (e.g. an SDR/LilyGO board hangs, a
sensor battery dies, a cloud API key expires). The decision logic is pure and
unit-tested (`decide`); the task layer adds DB-persisted state + SMTP delivery.

Off unless `alert_email_to` and `smtp_host` are configured (see Settings).
"""
import asyncio
import functools
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import apns, db, storm, storm_watch
from .config import settings

log = logging.getLogger("alerts")


# ───────────────────────── pure decision logic ─────────────────────────
@dataclass
class AlertDecision:
    state: str            # 'ok' | 'stale'  — the device's state this tick
    event: str | None     # None | 'stale' | 'recovered' | 'repeat'
    changed_ms: int       # when the state last flipped


def decide(prior: dict | None, last_seen_ms: int | None, now_ms: int,
           threshold_ms: int, repeat_ms: int) -> AlertDecision:
    """Decide whether to alert for one device this tick.

    `prior` is the persisted state (dict with 'state'/'changed_ms'/'notified_ms')
    or None on first sight. Transition-based: a device is *baselined* on first
    sight with no alert, so we never alert for devices that were already
    dead/removed when monitoring started. Thereafter:
      * OK→stale  → 'stale' event
      * stale→OK  → 'recovered' event
      * stays stale and repeat_ms>0 and that long since last notify → 'repeat'
    """
    is_stale = last_seen_ms is not None and (now_ms - last_seen_ms) > threshold_ms
    cur = "stale" if is_stale else "ok"

    if prior is None:
        return AlertDecision(cur, None, now_ms)        # baseline, no alert

    if cur != prior["state"]:
        return AlertDecision(cur, "stale" if cur == "stale" else "recovered", now_ms)

    # State unchanged. Optionally re-remind while still stale. Repeats key
    # off notified_ms ONLY: a device that was already dead at first sight is
    # baselined 'stale' with notified_ms=None (per the promise above, no
    # alert), and the old `or changed_ms` fallback made the repeat branch
    # fire "still not reporting" for it anyway once repeat_hours elapsed.
    # notified_ms is set exactly when a stale notification delivered, so a
    # never-notified device never "re"-notifies.
    if cur == "stale" and repeat_ms > 0:
        last_notified = prior.get("notified_ms")
        if last_notified is not None and now_ms - last_notified >= repeat_ms:
            return AlertDecision("stale", "repeat", prior["changed_ms"])

    return AlertDecision(cur, None, prior["changed_ms"])


def _fmt_ts(ms: int | None, tz_name: str) -> str:
    if not ms:
        return "never"
    try:
        zi = ZoneInfo(tz_name)
    except Exception:
        zi = ZoneInfo("UTC")
    return datetime.fromtimestamp(ms / 1000, zi).strftime("%Y-%m-%d %H:%M %Z")


# Canonical header scrub lives in storm._clean_name (alerts imports storm;
# the reverse would be a cycle). It grew as a verbatim copy in both modules
# — hardening one would have left the other alert channel breakable
# (2026-08-20 review). Local name kept for the many call sites.
_clean_name = storm._clean_name


def build_alert(event: str, name: str, mac: str, last_seen_ms: int | None,
                now_ms: int, threshold_min: float, tz_name: str) -> tuple[str, str]:
    """Build (subject, body) for an alert. Pure — unit-testable."""
    name = _clean_name(name)
    last = _fmt_ts(last_seen_ms, tz_name)
    if event in ("stale", "repeat"):
        age_min = (now_ms - last_seen_ms) / 60000 if last_seen_ms else None
        age_txt = f"{age_min:.0f} min" if age_min is not None else "an unknown time"
        still = "still " if event == "repeat" else ""
        subject = f"[Zasder Weather] {name} {still}not reporting"
        body = (
            f"Device '{name}' ({mac}) has not reported for {age_txt} "
            f"(threshold {threshold_min:.0f} min).\n\n"
            f"Last reading: {last}\n\n"
            "If this is an SDR/LilyGO receiver it may have hung — power-cycle "
            "or reset it. If it's a cloud feed, check the upstream service and "
            "your API credentials. The status page lists every device's "
            "last-seen time.\n"
        )
    else:  # recovered
        subject = f"[Zasder Weather] {name} is reporting again"
        body = (f"Device '{name}' ({mac}) is back online.\n\n"
                f"Latest reading: {last}\n")
    return subject, body


def build_push(event: str, name: str, last_seen_ms: int | None,
               now_ms: int, threshold_min: float) -> tuple[str, str]:
    """Short (title, body) for an APNs alert push. Pure — unit-testable."""
    name = _clean_name(name)
    if event in ("stale", "repeat"):
        age = (now_ms - last_seen_ms) / 60000 if last_seen_ms else None
        body = f"No data for {age:.0f} min (threshold {threshold_min:.0f})" if age is not None \
            else "Not reporting"
        return f"{name} is offline", body
    return f"{name} is back online", "Reporting again"


# ───────────────────────── threshold rules ─────────────────────────
# Field keys match the iOS AlertRule / observation JSON keys.
THRESHOLD_FIELDS = {
    "tempf", "feelsLike", "humidity", "dewPoint", "windspeedmph",
    "windgustmph", "dailyrainin", "hourlyrainin", "baromrelin", "uv",
}
THRESHOLD_COMPARATORS = {"above", "below", "equalTo"}
_FIELD_LABELS = {
    "tempf": "Temperature", "feelsLike": "Feels Like", "humidity": "Humidity",
    "dewPoint": "Dew Point", "windspeedmph": "Wind Speed", "windgustmph": "Wind Gust",
    "dailyrainin": "Rain Today", "hourlyrainin": "Rain Rate",
    "baromrelin": "Pressure", "uv": "UV Index",
}
_FIELD_UNITS = {
    "tempf": "°F", "feelsLike": "°F", "dewPoint": "°F", "humidity": "%",
    "windspeedmph": " mph", "windgustmph": " mph", "dailyrainin": " in",
    "hourlyrainin": " in/hr", "baromrelin": " inHg", "uv": "",
}
_COMPARATOR_SYM = {"above": ">", "below": "<", "equalTo": "="}

# Oldest a stored storm-counter baseline may be and still count as a
# tick-to-tick delta (see _check_storm_summaries). Generous next to the
# ~minute tick cadence, small next to the disabled-for-weeks gaps that
# fabricated back-dated storms.
_STORM_BASELINE_MAX_AGE_MS = 6 * 3_600_000
# How far before the first bucket tip the storm summary looks for its top
# gust. Storms lead with their wind: Doren's 23.81 mph front hit 18 minutes
# before the rain opened the window, and the summary's "17 mph" — true for
# the rain window — read as a wrong variable (2026-08-23).
_STORM_GUST_LEAD_MS = 30 * 60_000


def rule_triggered(comparator: str, threshold: float, value: float) -> bool:
    if comparator == "above":
        return value > threshold
    if comparator == "below":
        return value < threshold
    return abs(value - threshold) < 0.5   # equalTo — tolerance for noisy sensors


def evaluate_rule(comparator: str, threshold: float, value: float,
                  prev_triggered: int) -> tuple[bool, bool]:
    """(now_triggered, fire). Edge-triggered: fire only on clear→triggered."""
    now = rule_triggered(comparator, threshold, value)
    return now, (now and not prev_triggered)


# Re-arm deadband per field, in the stored API-native units (°F, mph, inHg,
# inches — see CLAUDE.md; a margin sized for a display unit would be a bug).
# Without hysteresis a sensor oscillating on the boundary (35.01 → 34.99 →
# 35.01°F against a 35° rule at a 16–60s cadence) re-armed on every dip and
# re-fired a push/email on every rise — dozens of alerts in one boundary night.
_REARM_MARGIN: dict[str, float] = {
    "tempf": 1.0, "feelsLike": 1.0, "dewPoint": 1.0,   # °F
    "humidity": 2.0,                                    # %
    "windspeedmph": 2.0, "windgustmph": 2.0,            # mph
    "dailyrainin": 0.02, "hourlyrainin": 0.02,          # in
    "baromrelin": 0.02,                                 # inHg
    "uv": 0.5,
}


def rule_cleared(comparator: str, threshold: float, value: float,
                 margin: float) -> bool:
    """Re-arm test: the value must clear the threshold by at least `margin`
    (a deadband) before the rule may fire again. Pure — unit-testable."""
    if comparator == "above":
        return value <= threshold - margin
    if comparator == "below":
        return value >= threshold + margin
    # equalTo triggers within ±0.5; require leaving that band by the margin.
    return abs(value - threshold) >= 0.5 + margin


# The deadband alone couldn't hold against wind: instantaneous samples swing
# 2→10 mph within a single minute (Doren's station, verified in his
# observations 2026-08-23), so a ">10 mph" rule fired, re-armed through the
# 2 mph margin one tick later, and fired again on the next poke above 10 —
# an alert every ~8 minutes all afternoon, reported as "duplicate
# notifications". Value says WHERE the reading is; only time says the event
# is OVER. Re-arm now additionally requires the value to stay clear for this
# long, continuously — one breezy afternoon is one alert.
_REARM_DWELL_MS = 15 * 60_000


def rearm_transition(cleared: bool, clear_since_ms: int | None, now_ms: int,
                     dwell_ms: int = _REARM_DWELL_MS) -> tuple[bool, int | None]:
    """(rearm_now, new_clear_since_ms) for a currently-triggered rule.
    Clearance must be CONTINUOUS: any tick that isn't clear (re-triggered or
    merely back inside the deadband) resets the clock to none. Pure."""
    if not cleared:
        return False, None
    if clear_since_ms is None:
        return False, now_ms
    if now_ms - clear_since_ms >= dwell_ms:
        return True, None
    return False, clear_since_ms


def build_threshold_message(device_name: str, field: str, value: float,
                            comparator: str, threshold: float) -> tuple[str, str]:
    """(title, body) for a tripped threshold rule. Pure — unit-testable."""
    device_name = _clean_name(device_name)
    label = _FIELD_LABELS.get(field, field)
    unit = _FIELD_UNITS.get(field, "")
    sym = _COMPARATOR_SYM.get(comparator, comparator)
    def fmt(v: float) -> str: return f"{v:g}{unit}"
    return (f"{device_name}: {label} alert",
            f"{label} is {fmt(value)} ({sym} {fmt(threshold)})")


# ───────────────────────── smart (derived) alerts ─────────────────────────
# Built-in weather-intelligent alerts that need no per-metric threshold: frost
# risk, dangerous heat, and a rapid pressure drop. Edge-triggered like the
# threshold rules (fire once on clear→triggered, re-arm when the condition
# clears). Pure — the monitor supplies already-computed values.
# 1.8 additions: the rate-of-change family (temp_drop / wind_ramp join
# pressure_drop), a duration alert (pipe_freeze), and single-station
# outflow detection. "front" is not in this list — it is the GROUPED
# delivery when several rate alerts fire on the same tick (see
# _check_smart_alerts), never an independently-evaluated condition.
# ── severity tiers (1.8, Pillar A; 'major' added 1.9) ───────────────────
# The ops-world lesson applied to weather: the CHANNEL is part of the
# signal. 'warning' = act now (breaks through quiet hours AND arrives
# Time Sensitive on iOS, punching Focus modes); 'major' = the 1.9 middle
# ground — delivered during quiet hours but as a NORMAL notification, so
# the phone's own Focus rules still apply (Volney: "for people that want
# to know during quiet hours but not always time sensitive related");
# 'watch' = worth a normal push, muted overnight; 'info' = after-the-fact
# color that a digest can carry. Unknown kinds default to watch — new
# alerts should have to EARN silence, not legibility.
ALERT_SEVERITY: dict[str, str] = {
    "lightning": "warning",
    "outflow": "warning",
    "pipe_freeze": "warning",
    "nws": "warning",
    "lightning_clear": "info",
    "first_frost": "info",
    "digest": "info",
    "sensor_recovered": "info",
    "device_recovered": "info",
    "battery_recovered": "info",
    "disk_recovered": "info",
}


def severity_of(kind: str) -> str:
    return ALERT_SEVERITY.get(kind, "watch")


# Per-rule urgency (1.8, Volney: "high wind urgent, temp over 100 minor";
# 'major' added 1.9). User-facing levels map onto the delivery tiers:
# urgent breaks quiet hours AND arrives Time Sensitive (punches Focus);
# major breaks quiet hours as a normal notification; standard is a normal
# push; minor pushes by day, is MUTED overnight (not queued — history and
# the morning digest carry it), and rides the digest. Default MINOR — new
# and existing rules start at the quiet end and get promoted deliberately.
RULE_SEVERITIES = ("minor", "standard", "major", "urgent")
_RULE_TIER = {"minor": "info", "standard": "watch",
              "major": "major", "urgent": "warning"}

# Tiers that the quiet-hours gate lets through. 'warning' additionally
# rides Time Sensitive — see _deliver.
_QUIET_HOURS_EXEMPT = ("warning", "major")


def rule_tier(severity: str | None) -> str:
    return _RULE_TIER.get(severity or "minor", "info")


def in_quiet_hours(now_ms: int, tz_name: str,
                   start_min: int | None, end_min: int | None) -> bool:
    """Local-time quiet window, minutes-of-day, wrap-around aware
    (22:00→07:00 is the normal shape). None = no quiet hours. Pure."""
    if start_min is None or end_min is None or start_min == end_min:
        return False
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    m = local.hour * 60 + local.minute
    if start_min < end_min:
        return start_min <= m < end_min
    return m >= start_min or m < end_min


SMART_KINDS = ("frost", "heat", "pressure_drop", "temp_drop", "wind_ramp",
               "pipe_freeze", "outflow")


def smart_condition(kind: str, *, tempf: float | None = None,
                    feels: float | None = None,
                    pressure_delta_3h: float | None = None,
                    frost_f: float, heat_f: float, drop_inhg: float,
                    dew_point: float | None = None,
                    wind: float | None = None,
                    gust: float | None = None,
                    temp_delta_1h: float | None = None,
                    wind_delta_1h: float | None = None,
                    temp_1h_ago: float | None = None,
                    pressure_delta_10m: float | None = None,
                    temp_delta_10m: float | None = None,
                    temp_drop_f: float = 12.0,
                    wind_ramp_mph: float = 12.0,
                    pipe_freeze_f: float = 20.0) -> bool:
    if kind == "frost":
        # 1.8 science refinement (Pillar A): radiative frost needs moist-
        # enough air depositing on a calm night — the published best signal
        # is cold + dew point near/below freezing + light wind. A windy or
        # bone-dry cold snap is real cold but not FROST; suppressors only
        # apply when the reading exists (absent is not a suppressor).
        if tempf is None or tempf > frost_f:
            return False
        if dew_point is not None and dew_point > 37.0:
            return False
        if wind is not None and wind > 8.0:
            return False
        return True
    if kind == "heat":
        return feels is not None and feels >= heat_f
    if kind == "pressure_drop":
        return (pressure_delta_3h is not None
                and pressure_delta_3h <= -abs(drop_inhg))
    if kind == "temp_drop":
        return (temp_delta_1h is not None
                and temp_delta_1h <= -abs(temp_drop_f))
    if kind == "wind_ramp":
        return (wind is not None and wind_delta_1h is not None
                and wind >= 15.0 and wind_delta_1h >= abs(wind_ramp_mph))
    if kind == "pipe_freeze":
        # Sustained, not instantaneous: both ends of the trailing hour at
        # or below the burst threshold reads as an hour of hard freeze.
        return (tempf is not None and temp_1h_ago is not None
                and tempf <= pipe_freeze_f and temp_1h_ago <= pipe_freeze_f)
    if kind == "outflow":
        # Single-station gust-front signature: an abrupt pressure JUMP
        # with a temperature break and strong gusts, all inside ~10
        # minutes. Detection of what just hit, never a forecast.
        return (pressure_delta_10m is not None and temp_delta_10m is not None
                and gust is not None
                and pressure_delta_10m >= 0.015      # ≈0.5 hPa jump
                and temp_delta_10m <= -3.0
                and gust >= 20.0)
    return False


# Smart-alert re-arm deadbands, API-native units (°F / inHg) — same rationale
# as _REARM_MARGIN: a temp hovering at exactly smart_alert_frost_f must not
# re-fire the frost alert on every 0.1° wobble.
_SMART_REARM_MARGIN_F = 1.0
_SMART_REARM_MARGIN_INHG = 0.01


def smart_cleared(kind: str, *, tempf: float | None = None,
                  feels: float | None = None,
                  pressure_delta_3h: float | None = None,
                  frost_f: float, heat_f: float, drop_inhg: float,
                  wind: float | None = None,
                  temp_delta_1h: float | None = None,
                  pressure_delta_10m: float | None = None,
                  temp_drop_f: float = 12.0,
                  pipe_freeze_f: float = 20.0,
                  **_ignore) -> bool:
    """Re-arm test for a smart alert: the condition must clear by a deadband
    (None = no data = not cleared). Pure — unit-testable."""
    if kind == "frost":
        return tempf is not None and tempf > frost_f + _SMART_REARM_MARGIN_F
    if kind == "heat":
        return feels is not None and feels < heat_f - _SMART_REARM_MARGIN_F
    if kind == "temp_drop":
        return (temp_delta_1h is not None
                and temp_delta_1h > -abs(temp_drop_f) / 2)
    if kind == "wind_ramp":
        return wind is not None and wind < 12.0
    if kind == "pipe_freeze":
        return (tempf is not None
                and tempf > pipe_freeze_f + 4.0)
    if kind == "outflow":
        return (pressure_delta_10m is not None
                and pressure_delta_10m < 0.005)
    if kind == "pressure_drop":
        return (pressure_delta_3h is not None
                and pressure_delta_3h > -abs(drop_inhg) + _SMART_REARM_MARGIN_INHG)
    return False


def build_smart_message(kind: str, device_name: str, *,
                        tempf: float | None = None, feels: float | None = None,
                        pressure_delta_3h: float | None = None,
                        **extra) -> tuple[str, str]:
    """(title, body) for a smart alert. Pure — unit-testable."""
    device_name = _clean_name(device_name)
    if kind == "frost":
        return (f"{device_name}: Frost/freeze risk",
                f"Temperature is {tempf:g}°F — frost or freeze possible. "
                f"Protect sensitive plants.")
    if kind == "heat":
        return (f"{device_name}: Dangerous heat",
                f"Feels like {feels:g}°F — heat is dangerous. Limit time "
                f"outdoors and stay hydrated.")
    if kind == "pressure_drop":
        drop = abs(pressure_delta_3h) if pressure_delta_3h is not None else 0.0
        return (f"{device_name}: Pressure falling fast",
                f"Pressure fell {drop:.2f} inHg in 3h — a storm may be "
                f"approaching.")
    if kind == "temp_drop":
        drop = abs(extra.get("temp_delta_1h") or 0.0)
        return (f"{device_name}: Temperature dropping fast",
                f"Down {drop:.0f}°F in the last hour — a front or outflow "
                f"is moving through.")
    if kind == "wind_ramp":
        return (f"{device_name}: Wind picking up fast",
                f"Sustained wind jumped to {extra.get('wind') or 0:.0f} mph "
                f"within the hour.")
    if kind == "pipe_freeze":
        return (f"{device_name}: Hard freeze — pipe risk",
                f"At or below {extra.get('pipe_freeze_f') or 20:.0f}°F for "
                f"a sustained stretch. Drip vulnerable faucets and check "
                f"exposed pipes.")
    if kind == "outflow":
        return (f"{device_name}: Storm outflow just hit",
                f"Pressure jumped and the temperature broke with gusts to "
                f"{extra.get('gust') or 0:.0f} mph — strong winds likely "
                f"in the next few minutes.")
    if kind == "front":
        parts = extra.get("front_parts") or []
        return (f"{device_name}: Front passage",
                "Several things moved at once — " + ", ".join(parts) + ". "
                "One notification instead of a pile.")
    return (f"{device_name}: alert", "")


# ───────────────────────── effective config ─────────────────────────
@dataclass
class EffectiveAlertConfig:
    enabled: bool                 # transport + recipients + not turned off
    transport_configured: bool    # an SMTP host is set (DB or env)
    recipients: list[str]         # DB prefs override env
    default_threshold_min: float  # DB prefs override env
    repeat_hours: float           # DB prefs override env
    # Resolved SMTP transport (app-managed DB value over env secret).
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_tls: bool
    smtp_ssl: bool
    # 'all' | 'device_down' — which alert kinds may EMAIL. Push is always
    # unscoped. Defaulted so positional construction elsewhere stays valid.
    email_scope: str = "all"
    # Storm summary, resolved DB-over-env like everything above. Defaulted so
    # existing positional construction (and tests) keep working.
    storm_summary: bool = True
    storm_quiet_minutes: float = 30.0
    storm_min_total_in: float = 0.05
    # Rain-start nowcast (1.7), resolved the same way.
    rain_start: bool = False
    # 1.7: 'push' | 'email' | 'both' — which channels carry storm summaries.
    # None = legacy (push always, email iff email_scope='all'), so existing
    # installs keep their exact delivery until the user picks.
    storm_channels: str | None = None
    # 1.8 heat-day Live Activity: opt-in, with its trigger threshold (°F,
    # API-native like every stored constant).
    heat_day: bool = False
    heat_day_threshold_f: float = 100.0
    # 1.8 quiet hours (minutes of local day; None = off) + daily digest
    # hour (local hour to send; None = off).
    quiet_start_min: int | None = None
    quiet_end_min: int | None = None
    digest_hour: int | None = None
    # 2.0: minute past the hour (0..59; absent = :00). "An email that is
    # sent at 7:29 will likely be seen before one at 7am" (Volney): the
    # on-the-hour report lands in the same minute as everyone else's.
    digest_minute: int | None = None


def _parse_recipients(raw: str | None) -> list[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


def _pick(dbv, envv):
    """DB value wins unless it's NULL/empty, then fall back to env."""
    return dbv if dbv not in (None, "") else envv


async def effective_config() -> EffectiveAlertConfig:
    """Merge app-managed DB prefs over env defaults. DB value wins when set;
    NULL falls back to env — including the SMTP transport, so the app can
    configure mail end-to-end without touching server env/secrets."""
    p = await db.get_alert_prefs()
    smtp_host = _pick(p["smtp_host"], settings.smtp_host)
    smtp_port = int(p["smtp_port"]) if p["smtp_port"] is not None else settings.smtp_port
    smtp_username = _pick(p["smtp_username"], settings.smtp_username)
    smtp_password = _pick(p["smtp_password"], settings.smtp_password)
    smtp_from = _pick(p["smtp_from"], settings.alert_email_from)
    smtp_tls = bool(p["smtp_tls"]) if p["smtp_tls"] is not None else settings.smtp_tls
    smtp_ssl = bool(p["smtp_ssl"]) if p["smtp_ssl"] is not None else settings.smtp_ssl
    transport = bool(smtp_host)
    recipients = (_parse_recipients(p["recipients"]) if p["recipients"]
                  else settings.alert_recipients)
    default_thr = (p["default_threshold_min"] if p["default_threshold_min"] is not None
                   else settings.alert_stale_minutes)
    repeat = (p["repeat_hours"] if p["repeat_hours"] is not None
              else settings.alert_repeat_hours)
    enabled = transport and bool(recipients) and (p["enabled"] != 0)
    scope = p.get("email_scope")
    email_scope = scope if scope in ("all", "device_down") else "all"
    storm_on = (bool(p["storm_summary"]) if p["storm_summary"] is not None
                else settings.storm_summary)
    storm_quiet = (p["storm_quiet_minutes"] if p["storm_quiet_minutes"] is not None
                   else settings.storm_summary_quiet_minutes)
    storm_min = (p["storm_min_total_in"] if p["storm_min_total_in"] is not None
                 else settings.storm_summary_min_total_in)
    rain_on = (bool(p["rain_start"]) if p.get("rain_start") is not None
               else settings.rain_start_alerts)
    chan = p.get("storm_channels")
    storm_channels = chan if chan in ("push", "email", "both") else None
    heat_on = bool(p["heat_day"]) if p.get("heat_day") is not None else False
    try:
        heat_thr = float(p.get("heat_day_threshold_f") or 100.0)
    except (TypeError, ValueError):
        heat_thr = 100.0
    def _int_or_none(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return EffectiveAlertConfig(
        enabled, transport, recipients, float(default_thr), float(repeat),
        smtp_host, smtp_port, smtp_username, smtp_password, smtp_from,
        smtp_tls, smtp_ssl, email_scope,
        storm_on, float(storm_quiet), float(storm_min), rain_on,
        storm_channels=storm_channels,
        heat_day=heat_on, heat_day_threshold_f=heat_thr,
        quiet_start_min=_int_or_none(p.get("quiet_start_min")),
        quiet_end_min=_int_or_none(p.get("quiet_end_min")),
        digest_hour=_int_or_none(p.get("digest_hour")),
        digest_minute=_int_or_none(p.get("digest_minute")))


# ───────────────────────── SMTP delivery ─────────────────────────
def _send_sync(subject: str, body: str, to_list: list[str],
               cfg: EffectiveAlertConfig, html: str | None = None) -> None:
    """Blocking SMTP send — run via asyncio.to_thread. Uses the resolved
    transport (DB over env). STARTTLS (587), implicit SSL (465), or plain.
    `html` (1.9, the weather-report digest) rides as a multipart
    alternative: text stays the fallback for clients that refuse HTML."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from or cfg.smtp_username or "zasder-weather@localhost"
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    host, port = cfg.smtp_host, cfg.smtp_port
    user, pw = cfg.smtp_username, cfg.smtp_password
    if cfg.smtp_ssl:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                              timeout=30) as s:
            if user:
                s.login(user, pw or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if cfg.smtp_tls:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if user:
                s.login(user, pw or "")
            s.send_message(msg)


# ───────────────────────── delivery ─────────────────────────
# Strong refs for in-flight webhook dispatch tasks (fire-and-forget).
_WEBHOOK_TASKS: set = set()


def _reap_webhook_task(task) -> None:
    """Discard AND consume the exception — an unretrieved task exception
    logs asyncio noise at GC time (R8 S7)."""
    _WEBHOOK_TASKS.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("webhook dispatch task failed: %s", exc)


async def _deliver(cfg: EffectiveAlertConfig, subject: str, body: str,
                   push_title: str, push_body: str,
                   email_ok: bool = True, *,
                   push_ok: bool = True,
                   kind: str = "alert", mac: str | None = None,
                   severity: str | None = None) -> bool:
    """Send an alert through every configured channel (email + push). Returns
    True when the alert is HANDLED: at least one channel delivered, or no
    channel had anything to attempt. Shared by device-down + threshold.

    `email_ok` scopes the EMAIL channel only (cfg.email_scope='device_down'
    keeps rule/smart alerts out of the inbox); `push_ok` scopes push the
    same way (1.7 storm_channels='email' — every other caller leaves it on).

    "Nothing to deliver" must NOT read as a transient failure: with
    email_scope='device_down' and push unconfigured (or push configured but
    zero registered tokens matching a live channel), a fired threshold/smart
    rule used to come back False every tick — state never persisted, a
    retry-WARNING logged every tick forever, and the long-past crossing
    fired as fresh if push was enabled weeks later. Muted is handled, not
    failed; only an ATTEMPTED channel that failed should trigger the
    caller's retry-next-tick path."""
    # 1.8 quiet hours (+ 'major' 1.9): below-major pushes hold their
    # tongue at night; major and warning go through. Email is inherently
    # quiet and unaffected; the alert still lands in history, so the
    # Recently Triggered list carries the overnight story.
    eff_severity = severity or severity_of(kind)
    if (push_ok and eff_severity not in _QUIET_HOURS_EXEMPT
            and in_quiet_hours(int(time.time() * 1000), settings.timezone,
                               getattr(cfg, "quiet_start_min", None),
                               getattr(cfg, "quiet_end_min", None))):
        push_ok = False
    delivered = False
    attempted = False
    if cfg.enabled and email_ok:
        attempted = True
        try:
            await asyncio.to_thread(_send_sync, subject, body, cfg.recipients, cfg)
            delivered = True
        except Exception as e:
            log.exception("alert email send failed: %s", e)
    if push_ok and await apns.push_configured():
        try:
            # Urgent (warning tier) rides Time Sensitive (1.9): the server
            # already refuses to hold it back; this makes the PHONE treat
            # it the same way (breaks Focus, needs the app entitlement).
            # 'major' deliberately does not — it bypasses quiet hours only.
            level = ("time-sensitive" if eff_severity == "warning"
                     else None)
            res = await apns.send_to_all(push_title, push_body,
                                         interruption_level=level)
            if res.get("sent"):
                delivered = True
            elif res.get("failed"):
                # There were recipients and the send failed — retriable.
                attempted = True
            # sent == 0 with nothing failed: no registered token matched a
            # live channel ({"sent": 0, "skipped": ...} / {"total": 0}) —
            # nothing to deliver on this channel, not a failure.
        except Exception as e:
            attempted = True
            log.exception("alert push send failed: %s", e)
    # 1.8 webhooks are their own channel: with enabled hooks registered,
    # scheduling the dispatch counts as delivery — otherwise a failing SMTP
    # server made the tick retry forever and the perfectly healthy webhook
    # never heard about the alert at all (CodeRabbit, PR #32 round 2).
    webhook_channel = False
    try:
        from . import webhooks as _wh
        webhook_channel = bool(await db.list_webhooks(enabled_only=True))
    except Exception:
        pass
    if webhook_channel:
        delivered = True
    if not attempted and not delivered:
        log.info("alert had no willing channel (muted by scope / no "
                 "recipients) — treating as handled: %s", push_title)
    handled = delivered or not attempted
    if handled:
        # History rides the HANDLED outcome, not the delivered one: callers
        # retry unhandled alerts next tick (logging those would duplicate),
        # while a muted alert still happened — it lands with delivered=0 and
        # the app's Recent list becomes the only place it's visible at all.
        ts_ms = int(time.time() * 1000)
        try:
            await db.log_alert(ts_ms, kind, mac,
                               push_title, push_body, delivered,
                               severity=eff_severity)
        except Exception as e:
            log.exception("alert history write failed: %s", e)
        # 1.8 webhooks (Pillar B): every HANDLED alert fans out to the
        # registered endpoints — muted-by-scope alerts included, because
        # the automation a webhook feeds is its own delivery channel.
        try:
            from . import webhooks
            # Fire-and-forget (R7): the two-attempt HTTP delivery can take
            # ~22s and was blocking the 60s monitor tick. Strong ref so the
            # task isn't GC'd mid-flight; errors land on the hook's row.
            task = asyncio.create_task(
                webhooks.dispatch_alert(kind, mac, push_title,
                                        push_body, ts_ms,
                                        severity=eff_severity))
            _WEBHOOK_TASKS.add(task)
            task.add_done_callback(_reap_webhook_task)
        except Exception as e:
            log.exception("webhook dispatch failed: %s", e)
    return handled


# ───────────────────────── monitor task ─────────────────────────
class AlertMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="alert-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        interval = max(15, settings.alert_check_interval_seconds)
        log.info("alert monitor running every %ds (transport configured=%s)",
                 interval, settings.transport_configured)
        while not self._stop.is_set():
            # Sleep FIRST: a tick at t=0 raced every short-lived process —
            # at boot no data has arrived yet, and in tests the startup
            # tick ran on a loop that died mid-flight, orphaning its
            # aiosqlite threads (the suite's exit hang) and racing
            # monkeypatches. Nothing time-critical lives in the first 60s.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._tick()
            except Exception as e:
                log.exception("alert tick failed: %s", e)

    async def _tick(self) -> None:
        # Persist guest-token last-used stamps every tick, not only when the
        # owner opens the share list: memory-only stamps die with the process,
        # and a deploy between someone's use and the owner's look read as
        # "never used" (Volney's work-phone test, 2026-08-23). No-op when
        # nothing is pending; never allowed to break alerting.
        try:
            await db.flush_guest_last_used()
        except Exception as e:
            log.warning("guest last-used flush failed: %s", e)
        # Same deal for the per-device ingest tokens — a board posts every
        # few seconds, so unflushed stamps pile up fast and a restart would
        # eat them.
        try:
            await db.flush_ingest_last_used()
        except Exception as e:
            log.warning("ingest last-used flush failed: %s", e)
        # Write-share audit rows (1.9): same buffered-sync-dep shape as the
        # stamps above, same per-tick persistence so a restart loses at
        # most one minute of attribution.
        try:
            await db.flush_write_audit()
        except Exception as e:
            log.warning("write-audit flush failed: %s", e)
        cfg = await effective_config()
        now_ms = int(time.time() * 1000)
        devices = await db.list_devices()
        # Gate decided HERE, applied after the egress block below — the
        # decision must not drift from the tick's start (a background tick
        # racing a test's monkeypatch found the widened window).
        alerts_open = (cfg.enabled or await apns.push_configured()
                       or bool(await db.list_webhooks(enabled_only=True)))
        # ── Pillar B egress + widget nudges run FIRST, independent of the
        # alert-transport gate below (R7 R1): community uploads, forecast
        # snapshots and widget refreshes are their own delivery surfaces —
        # a PWSWeather-only setup with email and push unconfigured used to
        # get zero uploads, silently. Each is try/except-bounded so a bad
        # one can't take alerting down with it.
        from . import widget_push
        try:
            await widget_push.check(devices, now_ms)
        except Exception:
            log.exception("widget push failed")
        from . import forecast_snapshots
        try:
            await forecast_snapshots.check(devices, now_ms)
        except Exception:
            log.exception("forecast snapshot failed")
        # The Zambretti daily ledger (2.0): one slide-rule call per station
        # per local day, filed at the first tick after 09:00 local. Same
        # rule as the snapshots above — its own delivery surface, bounded
        # so it can never take the tick down.
        from . import zambretti_ledger
        try:
            await zambretti_ledger.check(devices, now_ms)
        except Exception:
            log.exception("zambretti ledger failed")
        from . import share_targets
        try:
            await share_targets.check(devices, now_ms)
        except Exception:
            log.exception("share fan-out failed")
        # Run the ALERT sections when any alert channel can deliver: email,
        # push, or an enabled webhook (1.8 — webhooks carry every handled
        # alert, so they count as a channel; _deliver treats muted email+push
        # as handled and the dispatch still fans out).
        if not alerts_open:
            return
        repeat_ms = int(cfg.repeat_hours * 3600 * 1000)
        states = await db.get_alert_states()
        dev_prefs = await db.get_device_alert_prefs()
        for d in devices:
            mac = d["mac"]
            name = d.get("name") or mac
            thr_min = _device_threshold(mac, dev_prefs, cfg.default_threshold_min)
            if thr_min is None or thr_min <= 0:    # monitoring disabled for device
                continue
            threshold_ms = int(thr_min * 60 * 1000)
            prior = states.get(mac)
            last_seen = d.get("lastSeen")
            dec = decide(prior, last_seen, now_ms, threshold_ms, repeat_ms)

            notified_ms = (prior or {}).get("notified_ms")
            if dec.event:
                subject, bodytext = build_alert(
                    dec.event, name, mac, last_seen, now_ms, thr_min, settings.timezone)
                ptitle, pbody = build_push(dec.event, name, last_seen, now_ms, thr_min)
                delivered = await _deliver(cfg, subject, bodytext, ptitle, pbody,
                                           kind=f"device_{dec.event}", mac=mac)
                if delivered:
                    notified_ms = now_ms     # advance re-notify clock only on delivery
                elif prior is not None and dec.state != prior["state"]:
                    # Same treatment the threshold rules got (Reviewer P2): if
                    # the INITIAL stale/recovered event fails on every channel,
                    # do NOT persist the new state — decide() only fires on a
                    # state change (repeat is off by default), so persisting
                    # here would drop the most important alert forever. Keep
                    # the prior state so the next tick re-detects the
                    # transition and retries the send. ('repeat' events keep
                    # their existing retry: notified_ms simply doesn't
                    # advance.) No duplicate risk: a successful delivery
                    # persists the new state and re-fires nothing.
                    log.warning("device-down alert %s delivery failed for %s; "
                                "will retry next tick", dec.event, mac)
                    continue
                log.info("device-down alert %s for %s (%s)", dec.event, name, mac)

            if prior is None or dec.state != prior["state"] or dec.event:
                await db.upsert_alert_state(
                    mac, dec.state, last_seen, dec.changed_ms, notified_ms)

        # ── threshold rules: fire when a device's latest reading crosses a rule
        await self._check_threshold_rules(cfg, devices, now_ms)
        # ── smart (derived) alerts: frost / heat / rapid pressure drop
        if settings.smart_alerts:
            await self._check_smart_alerts(cfg, devices, now_ms)
            # 1.8 seasonal one-shots (first frost of the season).
            await self._check_seasonal_events(cfg, devices, now_ms)
            # 1.8 lightning proximity + the NWS 30-minute all-clear.
            # Guarded like nws_watch below: an exception here must not
            # abort the digest/storm/nowcast half of the tick.
            from . import lightning_watch
            try:
                await lightning_watch.check(cfg, devices, now_ms, _deliver)
            except Exception:
                log.exception("lightning watch failed")
            # 1.8 station health: battery flags, sensors gone quiet,
            # flatlined readings (Pillar D).
            from . import health_watch
            try:
                await health_watch.check(cfg, devices, now_ms, _deliver)
            except Exception:
                log.exception("health watch failed")
        # ── NWS relay (1.8): severe weather through OUR channels, not
        # just the foregrounded app. Warning tier — breaks quiet hours.
        from . import nws_watch
        try:
            await nws_watch.check(cfg, devices, now_ms, _deliver)
        except Exception:
            log.exception("nws watch failed")
        # ── disk space (1.9): server-level, not per-device, and not gated
        # on smart_alerts — a filling volume is an operational fact, not a
        # derived-weather opinion. It IS behind the alerts_open gate above
        # (R11), deliberately: with zero channels the tier must NOT latch
        # silently, or the operator who configures email next week never
        # hears the edge that already fired. Edge-triggered inside, so
        # every-tick is one cheap statvfs, not a nag.
        from . import disk_watch
        try:
            await disk_watch.check(cfg, now_ms, _deliver)
        except Exception:
            log.exception("disk watch failed")
        # ── daily digest (1.8; 1.9 turned it into the morning WEATHER
        # REPORT — yesterday's numbers per station + outlook + alert log).
        # Guarded like every sibling watch (R14): the rollup gathering +
        # forecast fetch grew the failure surface, and an exception here
        # was aborting storm summaries and the nowcast for the tick.
        try:
            await self._maybe_send_digest(cfg, devices, now_ms)
        except Exception:
            log.exception("morning report failed")
        # ── storm summary: one report per event, after the rain stops. Not
        # gated on smart_alerts — it is a different kind of thing, and the
        # rain counter it watches needs no derived inputs.
        await self._check_storm_summaries(cfg, devices, now_ms, dev_prefs)
        # ── rain-start nowcast (1.7): the leading edge to the storm
        # summary's trailing one. Internally throttled + opt-in; passing
        # _deliver keeps nowcast.py import-cycle-free. The partial stamps the
        # history kind — nowcast's callback signature stays five-positional.
        from . import nowcast
        await nowcast.check(cfg, devices, now_ms,
                            functools.partial(_deliver, kind="rain_start"))
        # ── heat-day Live Activity (1.8): all-day island presence on hot
        # days. Opt-in, internally throttled, best-effort like the storm
        # watch — a push failure never touches the alert pipeline.
        from . import heat_watch
        await heat_watch.check(cfg, devices, now_ms)

    async def _check_threshold_rules(self, cfg, devices, now_ms: int) -> None:
        rules = await db.list_alert_rules(enabled_only=True)
        if not rules:
            return
        rstates = await db.get_rule_states_full()
        for d in devices:
            last = d.get("lastData") or {}
            for rule in rules:
                if rule["target_mac"] and rule["target_mac"] != d["mac"]:
                    continue
                raw = last.get(rule["field"])
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                prev, clear_since = rstates.get((rule["id"], d["mac"]), (0, None))
                now_trig, fire = evaluate_rule(rule["comparator"], rule["threshold"], val, prev)
                if fire:
                    # Reviewer P2: only persist triggered=1 AFTER delivery succeeds.
                    # If SMTP/APNs/relay fails, leave state at 0 so the next tick
                    # retries — the alternative (mark first, deliver second) silently
                    # drops the alert until the reading clears and re-crosses.
                    dname = d.get("name") or d["mac"]
                    title, body = build_threshold_message(
                        dname, rule["field"], val, rule["comparator"], rule["threshold"])
                    delivered = await _deliver(
                        cfg, f"[Zasder Weather] {title}", body, title, body,
                        email_ok=cfg.email_scope == "all",
                        kind="rule", mac=d["mac"],
                        severity=rule_tier(rule.get("severity")))
                    if delivered:
                        await db.upsert_rule_state(rule["id"], d["mac"], 1, now_ms)
                        log.info("threshold alert fired: rule %s (%s) on %s value=%.3f",
                                 rule["id"], rule["field"], dname, val)
                    else:
                        log.warning(
                            "threshold alert delivery failed for rule %s on %s; "
                            "will retry next tick", rule["id"], d["mac"])
                elif prev:
                    # Triggered and not (re)firing: decide whether to re-arm.
                    # Two gates, both required — the value must clear the
                    # threshold by the field's deadband (_REARM_MARGIN) AND
                    # stay clear for the whole dwell window (_REARM_DWELL_MS).
                    # A re-triggered or deadband tick breaks the clearance
                    # and the clock starts over.
                    margin = _REARM_MARGIN.get(rule["field"], 0.0)
                    cleared = (not now_trig) and rule_cleared(
                        rule["comparator"], rule["threshold"], val, margin)
                    rearm, new_since = rearm_transition(cleared, clear_since, now_ms)
                    if rearm:
                        await db.upsert_rule_state(rule["id"], d["mac"], 0, now_ms)
                    elif new_since != clear_since:
                        await db.set_rule_clear_since(rule["id"], d["mac"], new_since)

    async def _check_storm_summaries(self, cfg, devices, now_ms: int,
                                     dev_prefs: dict | None = None) -> None:
        """One summary per storm, delivered once the rain has stopped.

        Unlike every other alert here this is trailing-edge and stateful, so
        the tracker keeps only enough to know an event is open — the numbers
        come from stored history when it closes (see storm.py).
        """
        if not cfg.storm_summary:
            return
        if dev_prefs is None:            # direct callers (tests) skip _tick
            dev_prefs = await db.get_device_alert_prefs()
        # 1.7 channel choice for summaries. None = legacy delivery, so
        # nobody's setup changes until they pick in the app.
        if cfg.storm_channels is not None:
            email_ok = cfg.storm_channels in ("email", "both")
            push_ok = cfg.storm_channels in ("push", "both")
        else:
            email_ok = cfg.email_scope == "all"
            push_ok = True
        for d in devices:
            mac = d["mac"]
            # Per-station mute (1.7, Doren: haptic Tempest feet from the
            # Davis reported every storm twice). Skipping the TRACKER too is
            # deliberate: the stale-baseline rebaseline below handles the
            # gap if the station is ever unmuted mid-storm.
            if (dev_prefs.get(mac) or {}).get("storm_summary") is False:
                continue
            last = d.get("lastData") or {}
            reading = storm.counter_value(last)
            state = await db.get_storm_state(mac) or {}
            started = state.get("started_ms")
            last_rain = state.get("last_rain_ms")
            prev_field = state.get("counter_field")
            prev_value = state.get("counter_value")

            prev_ms = state.get("counter_ms")
            obs_ms = last.get("dateutc")
            obs_ms = int(obs_ms) if isinstance(obs_ms, (int, float)) else now_ms

            field, value = reading if reading else (prev_field, prev_value)
            # `counter_value` holds the running PEAK, not merely the last
            # reading — some sources revise the day's total downward and then
            # climb back over the same ground, which comparing consecutive
            # readings counts twice. See storm.counter_progress.
            #
            # Only compare like with like: yearly and daily are different
            # scales, so a source that switched fields between ticks would
            # otherwise fabricate an enormous increment and open a storm.
            if reading and prev_field == field:
                # A baseline this old is history, not a tick-to-tick delta.
                # The checker stamps counter_ms every tick, so a large gap
                # means it was OFF (toggle disabled, no alert channel, or
                # downtime) — and the counter's rise across that gap is
                # accumulated weather, not a storm. Counting it opened a
                # "storm" back-dated weeks whose summary spanned the whole
                # gap (2026-08-20 review). Rebaseline silently; a real storm
                # in progress loses only its opening increment.
                if (prev_ms is not None
                        and now_ms - prev_ms > _STORM_BASELINE_MAX_AGE_MS):
                    increment = 0.0
                else:
                    increment, value = storm.counter_progress(prev_value, value)
            else:
                increment = 0.0

            if increment > 0:
                if started is None:
                    # Back-date to the PREVIOUS reading. The counter rises one
                    # reading after the rain began, so starting at "now" would
                    # drop the very increment that opened the storm from both
                    # the total and the temperature range.
                    started = prev_ms if prev_ms is not None else obs_ms
                    log.info("storm opened on %s", d.get("name") or mac)
                last_rain = obs_ms

            if started is not None and storm.should_close(
                    last_rain, now_ms, cfg.storm_quiet_minutes):
                stats = await db.storm_window_stats(
                    mac, started, last_rain or started, field or "yearlyrainin")
                # Gusts lead the rain: widen the GUST window (only) to the
                # half hour before the first tip, so the summary reports the
                # storm's headline wind rather than just the wind-while-wet.
                lead = await db.max_windgust_in_window(
                    mac, started - _STORM_GUST_LEAD_MS, started)
                if lead is not None and (stats.get("max_gust_mph") is None
                                         or lead > stats["max_gust_mph"]):
                    stats["max_gust_mph"] = lead
                summary = storm.StormSummary(
                    started_ms=started, ended_ms=last_rain or started, **stats)
                if storm.worth_reporting(summary, cfg.storm_min_total_in):
                    dname = d.get("name") or mac
                    title, body = storm.build_storm_message(
                        dname, summary, settings.timezone)
                    # Clear state only after delivery, same as the other
                    # alerts, so a transport failure retries next tick rather
                    # than losing the storm entirely.
                    if await _deliver(cfg, f"[Zasder Weather] {title}", body,
                                      title, body,
                                      email_ok=email_ok, push_ok=push_ok,
                                      kind="storm", mac=mac):
                        await db.upsert_storm_state(mac, None, None, field,
                                                    value, obs_ms)
                        # 1.9 Storm Report card: keep the structured stats
                        # this summary was built from. Best-effort — a
                        # failed history write must never unsend a summary.
                        #
                        # 2.0 storm-close capture: the before/after
                        # readings ride the same write, and THIS is the
                        # only moment they can be taken. History thinning
                        # ages the minute-by-minute rows either side of the
                        # storm down to one per bucket, so the pair that
                        # makes "108°F before, 84°F after" a story is gone
                        # within days of the storm. Measured now or never
                        # measured — see db._storm_close_capture for the
                        # windows and why they are permanent.
                        #
                        # The capture is enrichment and the row is the
                        # record: they fail separately. The storm state
                        # was cleared above, so nothing retries this tick
                        # — a capture that raises must not take the
                        # total, peak rate and gust down with it
                        # (CodeRabbit, PR #35).
                        capture: dict = {}
                        try:
                            capture = await db.storm_close_capture(
                                mac, summary.started_ms, summary.ended_ms,
                                now_ms)
                        except Exception:
                            log.exception("storm close capture failed; "
                                          "recording the storm without it")
                        try:
                            await db.record_storm(mac, {
                                "started_ms": summary.started_ms,
                                "ended_ms": summary.ended_ms,
                                "total_in": summary.total_in,
                                "peak_rate_in_hr": summary.peak_rate_in_hr,
                                "max_gust_mph": summary.max_gust_mph,
                                "min_tempf": summary.min_tempf,
                                "max_tempf": summary.max_tempf,
                                **capture})
                        except Exception:
                            log.exception("storm history write failed")
                        log.info("storm summary sent for %s: %.2fin over %.1fh",
                                 dname, summary.total_in, summary.duration_hours)
                        # 1.8 Storm Watch: final Activity beat, silent —
                        # the summary notification just rang.
                        await storm_watch.on_closed(
                            cfg, d, started, last_rain, now_ms, field,
                            reported=True)
                    else:
                        log.warning("storm summary delivery failed for %s; "
                                    "will retry next tick", mac)
                    continue
                # Not worth reporting: close it silently so a drizzle does
                # not leave an event open forever.
                await db.upsert_storm_state(mac, None, None, field, value, obs_ms)
                await storm_watch.on_closed(cfg, d, started, last_rain,
                                            now_ms, field, reported=False)
                continue

            # R6: with storm_summary default-on this unconditional upsert
            # committed one write per device per minute forever — storm or
            # no storm, pure WAL churn on a 256MB machine. Skip when this
            # tick changed nothing (the freshly-read `state` is the
            # baseline; any real transition differs in at least one field).
            if (started, last_rain, field, value, obs_ms) != (
                    state.get("started_ms"), state.get("last_rain_ms"),
                    state.get("counter_field"), state.get("counter_value"),
                    state.get("counter_ms")):
                await db.upsert_storm_state(mac, started, last_rain, field,
                                            value, obs_ms)
            # 1.8 Storm Watch: an open episode drives the Live Activity
            # (start once per episode, then throttled updates). Runs after
            # the close paths above, so a closing tick never re-opens it.
            if started is not None:
                await storm_watch.on_open_tick(cfg, d, started, now_ms, field)

    async def _maybe_send_digest(self, cfg, devices, now_ms: int) -> None:
        """Daily digest (1.8; rebuilt 1.9 as the morning WEATHER REPORT —
        Volney: "like a weather report you see on the nightly news").
        One email per local day at/after the configured hour: an anchor's
        headline, yesterday's hi/lo/rain/gust per station off the rollups,
        today's outlook (best-effort Open-Meteo), and the alert log —
        which now includes the quiet days, because "nothing fired" is a
        good report too. HTML in the share-card aesthetic with a plain
        alternative; both built by app/digest.py."""
        # digest_hour is the only hard gate now (1.9): a push-only install
        # (no SMTP) still gets the phone half of the morning report. The
        # email piece keeps its own transport guard below.
        if cfg.digest_hour is None:
            return
        email_ready = bool(cfg.enabled and cfg.recipients)
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            # UTC, not system-local (R14): rollup day keys fall back to
            # UTC (insights._tz), and "yesterday" must agree with them.
            tz = ZoneInfo("UTC")
        local = datetime.fromtimestamp(now_ms / 1000, tz)
        # Hour AND minute (2.0): the report goes on the first tick at or
        # after the chosen clock time. getattr: test fixtures and 1.9 rows
        # predate the minute and mean :00.
        minute = int(getattr(cfg, "digest_minute", 0) or 0)
        if local.hour * 60 + local.minute < int(cfg.digest_hour) * 60 + minute:
            return
        raw = await db.get_kv("alerts.digest.last_ms")
        try:
            last = int(raw) if raw else 0
        except ValueError:
            last = 0
        email_done = False
        if last:
            last_local = datetime.fromtimestamp(last / 1000, tz)
            email_done = last_local.date() >= local.date()
        # Each half retries on its OWN stamp (R15): the email stamp alone
        # gated everything here, so a transient APNs outage during the
        # phone send could never retry — the email's success stamped the
        # whole day closed.
        phone_done = (await db.get_kv("alerts.digest.phone_day")
                      == local.date().isoformat())
        if email_done and phone_done:
            return                            # both halves delivered today

        from . import digest as dg

        # On a phone-retry tick the email stamp has already moved to this
        # morning (R16): windowing from it would give the retried banner a
        # different alert count than the email it accompanies. Use the
        # same trailing day the original report described.
        window_start = (now_ms - 86_400_000) if email_done \
            else (last or (now_ms - 86_400_000))
        rows = await db.alerts_since(window_start)
        rows = [r for r in rows if r["kind"] != "digest"]
        alert_lines = [
            dg.AlertLine(
                when=datetime.fromtimestamp(r["ts_ms"] / 1000, tz)
                    .strftime("%a %H:%M"),
                title=r["title"],
                severity=r.get("severity") or severity_of(r["kind"]))
            for r in rows]

        # Yesterday, per WEATHER station, off the daily rollups — the same
        # rows the year charts and climate reports trust.
        from datetime import timedelta as _td
        from .climate import _rollup_rows
        yday = (local.date() - _td(days=1)).isoformat()
        stations: list = []
        primary_coords: tuple[float, float] | None = None
        for d in devices:
            if db.is_air_monitor_device(d):
                continue
            rrows = await _rollup_rows(d["mac"], yday, yday)
            if primary_coords is None:
                c = (((d.get("info") or {}).get("coords") or {})
                     .get("coords") or {})
                if isinstance(c.get("lat"), (int, float)) \
                        and isinstance(c.get("lon"), (int, float)):
                    primary_coords = (float(c["lat"]), float(c["lon"]))
            if not rrows:
                continue
            r = rrows[0]
            stations.append(dg.StationDay(
                name=d.get("name") or d["mac"],
                tmax_f=r["tempf_max"], tmin_f=r["tempf_min"],
                rain_in=r["rain_total"], gust_mph=r["windgustmph_max"],
                humidity_lo=r["humidity_min"], humidity_hi=r["humidity_max"],
                uv_max=r["uv_max"]))

        # Nothing to say at all (no alerts, no rollups — INSIGHTS off on a
        # quiet day): stamp and skip, exactly like the 1.8 behavior.
        if not rows and not stations:
            # Both stamps: with per-half retry gating (R15) an unstamped
            # phone half would re-enter — and re-gather rollups — every
            # tick for the rest of the day.
            await db.set_kv("alerts.digest.last_ms", str(now_ms))
            await db.set_kv("alerts.digest.phone_day",
                            local.date().isoformat())
            return

        # Today's outlook — best-effort, ten seconds, the report stands
        # without it. Same Open-Meteo daily fields the forecast route uses.
        outlook = None
        if primary_coords is None and settings.forecast_lat is not None \
                and settings.forecast_lon is not None:
            primary_coords = (settings.forecast_lat, settings.forecast_lon)
        if primary_coords is not None:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={"latitude": primary_coords[0],
                                "longitude": primary_coords[1],
                                "daily": "temperature_2m_max,"
                                         "temperature_2m_min,"
                                         "precipitation_probability_max",
                                "temperature_unit": "fahrenheit",
                                "forecast_days": 1,
                                "timezone": settings.timezone})
                    daily = resp.json().get("daily") or {}

                    def _first(key):
                        v = daily.get(key) or []
                        return v[0] if v and isinstance(v[0], (int, float)) \
                            else None
                    outlook = dg.Outlook(
                        hi_f=_first("temperature_2m_max"),
                        lo_f=_first("temperature_2m_min"),
                        precip_pct=(int(_first(
                            "precipitation_probability_max"))
                            if _first("precipitation_probability_max")
                            is not None else None))
            except Exception:
                outlook = None

        report = dg.Report(
            date_label=local.strftime("%A, %B %-d"),
            stations=stations, alerts=alert_lines, outlook=outlook)

        # ── the phone half (1.9, Volney: "a daily digest for your phone
        # first thing in the morning... like what we do with the storm").
        # Its OWN daily stamp, separate from the email's: a failed email
        # retries next tick, and re-sending the Live Activity + push each
        # retry would stack morning cards on the lock screen.
        await self._send_morning_phone(report, stations, local, now_ms)

        if email_done:
            return                  # only the phone half needed a retry
        if not email_ready:
            # No SMTP: the phone half above was the whole delivery. Stamp
            # so the day is done.
            await db.set_kv("alerts.digest.last_ms", str(now_ms))
            return
        subject = ("[Zasder Weather] Morning report · "
                   + local.strftime("%a %b %-d")
                   + (f" · {len(rows)} alert{'s' if len(rows) != 1 else ''}"
                      if rows else ""))
        try:
            await asyncio.to_thread(_send_sync, subject,
                                    dg.build_text(report),
                                    cfg.recipients, cfg,
                                    dg.build_html(report))
            await db.set_kv("alerts.digest.last_ms", str(now_ms))
            log.info("morning report sent: %d station(s), %d alert(s)",
                     len(stations), len(rows))
        except Exception:
            log.exception("digest send failed; will retry next tick")

    async def _send_morning_phone(self, report, stations, local,
                                  now_ms: int) -> None:
        """The morning report's lock-screen half: a push-to-start Live
        Activity (yesterday's numbers + today's outlook, self-dismissing
        by mid-morning — a newspaper, not a ticker) plus a compact push
        banner. Best-effort and once per local day; the email is the
        durable record."""
        from . import apns, digest as dg
        stamp = await db.get_kv("alerts.digest.phone_day")
        today_key = local.date().isoformat()
        if stamp == today_key:
            return
        lead = stations[0] if stations else None
        # lead None (R15): an alerts-only day (station down overnight but
        # the log has entries) still gets the compact push — push_text
        # renders the no-station form — just no Live Activity card, which
        # needs a station's numbers to draw.
        title, body = dg.push_text(report)
        sent_any = False
        had_targets = False
        if lead is not None:
            try:
                state = {"dateMs": now_ms,
                         "hiF": lead.tmax_f, "loF": lead.tmin_f,
                         "rainIn": lead.rain_in, "gustMph": lead.gust_mph,
                         "todayHiF": report.outlook.hi_f
                         if report.outlook else None,
                         "todayLoF": report.outlook.lo_f
                         if report.outlook else None,
                         "precipPct": report.outlook.precip_pct
                         if report.outlook else None}
                # stale/dismiss ride the SEND time, not the digest hour
                # (R16, deliberate): a report recovered at 14:00 still
                # earns its few hours on the lock screen — a late paper
                # is still that day's paper.
                payload = apns.build_live_activity_start(
                    "MorningReportActivityAttributes",
                    {"stationName": lead.name}, state, title, body,
                    now_s=now_ms // 1000,
                    stale_s=(now_ms + 3 * 3_600_000) // 1000,
                    dismiss_s=(now_ms + 5 * 3_600_000) // 1000)
                res = await apns.send_live_activity_start(
                    payload, title, body, activity="morning")
                sent_any = bool(res.get("sent"))
                had_targets = bool(res.get("sent") or res.get("failed")
                                   or res.get("dead"))
            except Exception:
                had_targets = True     # the attempt itself died — retry
                log.exception("morning live activity failed")
        try:
            if await apns.push_configured():
                res = await apns.send_to_all(title, body)
                sent_any = sent_any or bool(res.get("sent"))
                had_targets = had_targets or bool(res.get("total"))
        except Exception:
            had_targets = True
            log.exception("morning push failed")
        # Stamp when something was DELIVERED, or when there was nothing to
        # deliver to (no tokens — a token-less server must not retry the
        # same no-op every tick all morning). A total delivery failure
        # leaves the stamp unset so the next tick retries, matching the
        # email half and the storm-watch sibling (R15: the old
        # unconditional stamp lost the whole morning to one transient
        # APNs outage).
        if sent_any or not had_targets:
            await db.set_kv("alerts.digest.phone_day", today_key)
        if sent_any:
            log.info("morning phone report sent (%s)",
                     lead.name if lead else "alerts only")

    async def _check_seasonal_events(self, cfg, devices, now_ms: int) -> None:
        """One-shot seasonal markers (1.8, Pillar A): the first freezing
        reading after August 1 fires exactly once per station per season —
        trivially cheap, and the gardeners' favorite. Season key rolls on
        Aug 1 so a January frost belongs to the season that began the
        previous fall."""
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = None
        local = datetime.fromtimestamp(now_ms / 1000, tz)
        season = local.year if local.month >= 8 else local.year - 1
        for d in devices:
            if db.is_air_monitor_device(d):
                continue
            last = d.get("lastData") or {}
            # Freshness gate (the health-watcher rule): a station that died
            # on a cold night keeps a freezing lastData forever, and firing
            # off it days later would also burn the once-per-season key.
            obs_ms = last.get("dateutc")
            # Inverted guard on purpose: a blob with NO numeric timestamp is
            # unverifiable and must also skip — otherwise arbitrarily old
            # data fires and burns the once-per-season key (R7 finding 3).
            # abs(): a FUTURE timestamp (broken device clock that slipped
            # past ingest's skew clamp via a poller path) must not read as
            # "fresh" — it would fire and burn the seasonal key early.
            if (not isinstance(obs_ms, (int, float))
                    or abs(now_ms - obs_ms) > 30 * 60_000):
                continue
            try:
                tempf = float(last.get("tempf"))
            except (TypeError, ValueError):
                continue
            if tempf > 32.5:
                continue
            mac = d["mac"]
            key = f"seasonal.first_frost.{mac}.{season}"
            if await db.get_kv(key):
                continue
            dname = _clean_name(d.get("name") or mac)
            title = f"{dname}: First frost of the season"
            body = (f"{tempf:g}°F — the season's first freezing reading. "
                    f"Tender plants and hoses, this is your notice.")
            if await _deliver(cfg, f"[Zasder Weather] {title}", body,
                              title, body,
                              email_ok=cfg.email_scope == "all",
                              kind="first_frost", mac=mac):
                await db.set_kv(key, str(now_ms))
                log.info("first frost of season %s on %s", season, dname)

    async def _check_smart_alerts(self, cfg, devices, now_ms: int) -> None:
        """Frost / heat / rapid-pressure-drop alerts, edge-triggered per device."""
        states = await db.get_smart_alert_states()
        # Weather smart alerts skip air monitors: the outdoor AirGradient
        # reports a real tempf, and without this every frost/heat/front
        # would fire twice for the same yard.
        devices = [d for d in devices if not db.is_air_monitor_device(d)]
        cutoff_ms = now_ms - 3 * 3600 * 1000   # 3h ago, for pressure tendency

        def _f(v) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        # The rate family groups: when several of these newly fire on the
        # SAME tick for the SAME station, that is one weather event (a
        # front) — deliver one notification, not a pile (Pillar A).
        _FRONT_FAMILY = ("pressure_drop", "temp_drop", "wind_ramp", "outflow")
        _FRONT_LABEL = {"pressure_drop": "pressure falling fast",
                        "temp_drop": "temperature dropping",
                        "wind_ramp": "wind ramping up",
                        "outflow": "a pressure-jump outflow"}
        for d in devices:
            mac = d["mac"]
            last = d.get("lastData") or {}
            tempf = _f(last.get("tempf"))
            feels = _f(last.get("feelsLike"))
            dew = _f(last.get("dewPoint"))
            wind = _f(last.get("windspeedmph"))
            gust = _f(last.get("windgustmph"))
            cur_p = _f(last.get("baromrelin"))
            # Pressure change over the last 3h (needs a historical reading).
            delta = None
            if cur_p is not None:
                # max_age 2× the window (R7 R4): after a station outage the
                # anchor would otherwise be days old and the "3h" delta a lie.
                past_p = await db.value_at_or_before(
                    mac, "baromrelin", cutoff_ms, max_age_ms=3 * 3_600_000)
                if past_p is not None:
                    delta = cur_p - past_p
            # 1.8 rate windows: 1h for fronts, 10min for outflow signatures.
            h1 = now_ms - 3_600_000
            m10 = now_ms - 600_000
            temp_1h_ago = await db.value_at_or_before(
                mac, "tempf", h1, max_age_ms=3_600_000) \
                if tempf is not None else None
            temp_delta_1h = (tempf - temp_1h_ago
                             if tempf is not None and temp_1h_ago is not None
                             else None)
            wind_1h_ago = await db.value_at_or_before(
                mac, "windspeedmph", h1, max_age_ms=3_600_000) \
                if wind is not None else None
            wind_delta_1h = (wind - wind_1h_ago
                             if wind is not None and wind_1h_ago is not None
                             else None)
            p_10m_ago = await db.value_at_or_before(
                mac, "baromrelin", m10, max_age_ms=1_200_000) \
                if cur_p is not None else None
            pressure_delta_10m = (cur_p - p_10m_ago
                                  if cur_p is not None and p_10m_ago is not None
                                  else None)
            t_10m_ago = await db.value_at_or_before(
                mac, "tempf", m10, max_age_ms=1_200_000) \
                if tempf is not None else None
            temp_delta_10m = (tempf - t_10m_ago
                              if tempf is not None and t_10m_ago is not None
                              else None)

            kw = dict(
                tempf=tempf, feels=feels, pressure_delta_3h=delta,
                frost_f=settings.smart_alert_frost_f,
                heat_f=settings.smart_alert_heat_f,
                drop_inhg=settings.smart_alert_pressure_drop_inhg,
                dew_point=dew, wind=wind, gust=gust,
                temp_delta_1h=temp_delta_1h, wind_delta_1h=wind_delta_1h,
                temp_1h_ago=temp_1h_ago,
                pressure_delta_10m=pressure_delta_10m,
                temp_delta_10m=temp_delta_10m,
                temp_drop_f=settings.smart_alert_temp_drop_f,
                wind_ramp_mph=settings.smart_alert_wind_ramp_mph,
                pipe_freeze_f=settings.smart_alert_pipe_freeze_f)

            # Evaluate everything first so grouping can see the whole tick.
            fresh: list[str] = []
            for kind in SMART_KINDS:
                cond = smart_condition(kind, **kw)
                prev = states.get((mac, kind), 0)
                if cond and not prev:
                    fresh.append(kind)
                elif not cond and prev and smart_cleared(kind, **kw):
                    # Re-arm only once the condition clears by a deadband, so
                    # a value wobbling on the boundary can't re-fire per tick.
                    await db.upsert_smart_alert_state(mac, kind, 0, now_ms)

            dname = d.get("name") or mac
            front = [k for k in fresh if k in _FRONT_FAMILY]
            if len(front) >= 2:
                title, body = build_smart_message(
                    "front", dname,
                    front_parts=[_FRONT_LABEL[k] for k in front])
                # The group inherits its LOUDEST member's tier: outflow is
                # warning ("strong winds in minutes") and grouping it with
                # a watch-tier sibling must not demote it below the
                # quiet-hours line (R7 R5).
                tier = ("warning" if any(severity_of(k) == "warning"
                                         for k in front) else None)
                if await _deliver(cfg, f"[Zasder Weather] {title}", body,
                                  title, body,
                                  email_ok=cfg.email_scope == "all",
                                  kind="front", mac=mac,
                                  severity=tier):
                    for k in front:
                        await db.upsert_smart_alert_state(mac, k, 1, now_ms)
                    log.info("front passage (%s) on %s",
                             "+".join(front), dname)
                fresh = [k for k in fresh if k not in front]

            for kind in fresh:
                title, body = build_smart_message(
                    kind, dname, tempf=tempf, feels=feels,
                    pressure_delta_3h=delta, temp_delta_1h=temp_delta_1h,
                    wind=wind, gust=gust,
                    pipe_freeze_f=settings.smart_alert_pipe_freeze_f)
                # Persist triggered=1 only after delivery (same as threshold
                # rules) so a transport failure retries next tick.
                if await _deliver(cfg, f"[Zasder Weather] {title}", body,
                                  title, body,
                                  email_ok=cfg.email_scope == "all",
                                  kind=kind, mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
                    log.info("smart alert fired: %s on %s", kind, dname)
                else:
                    log.warning("smart alert %s delivery failed for %s; "
                                "will retry next tick", kind, mac)


def _device_threshold(mac: str, dev_prefs: dict, default_min: float) -> float | None:
    """Effective stale-threshold (minutes) for a device, or None if it's
    explicitly not monitored. Precedence: app per-device pref > env per-MAC
    override > global default."""
    dp = dev_prefs.get(mac)
    if dp is not None:
        if not dp.get("monitor", True):
            return None
        if dp.get("threshold_min") is not None:
            return float(dp["threshold_min"])
    return float(settings.alert_stale_minutes_by_mac.get(mac, default_min))
