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

from . import apns, db, storm
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
SMART_KINDS = ("frost", "heat", "pressure_drop")


def smart_condition(kind: str, *, tempf: float | None = None,
                    feels: float | None = None,
                    pressure_delta_3h: float | None = None,
                    frost_f: float, heat_f: float, drop_inhg: float) -> bool:
    if kind == "frost":
        return tempf is not None and tempf <= frost_f
    if kind == "heat":
        return feels is not None and feels >= heat_f
    if kind == "pressure_drop":
        return (pressure_delta_3h is not None
                and pressure_delta_3h <= -abs(drop_inhg))
    return False


# Smart-alert re-arm deadbands, API-native units (°F / inHg) — same rationale
# as _REARM_MARGIN: a temp hovering at exactly smart_alert_frost_f must not
# re-fire the frost alert on every 0.1° wobble.
_SMART_REARM_MARGIN_F = 1.0
_SMART_REARM_MARGIN_INHG = 0.01


def smart_cleared(kind: str, *, tempf: float | None = None,
                  feels: float | None = None,
                  pressure_delta_3h: float | None = None,
                  frost_f: float, heat_f: float, drop_inhg: float) -> bool:
    """Re-arm test for a smart alert: the condition must clear by a deadband
    (None = no data = not cleared). Pure — unit-testable."""
    if kind == "frost":
        return tempf is not None and tempf > frost_f + _SMART_REARM_MARGIN_F
    if kind == "heat":
        return feels is not None and feels < heat_f - _SMART_REARM_MARGIN_F
    if kind == "pressure_drop":
        return (pressure_delta_3h is not None
                and pressure_delta_3h > -abs(drop_inhg) + _SMART_REARM_MARGIN_INHG)
    return False


def build_smart_message(kind: str, device_name: str, *,
                        tempf: float | None = None, feels: float | None = None,
                        pressure_delta_3h: float | None = None) -> tuple[str, str]:
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
    return EffectiveAlertConfig(
        enabled, transport, recipients, float(default_thr), float(repeat),
        smtp_host, smtp_port, smtp_username, smtp_password, smtp_from,
        smtp_tls, smtp_ssl, email_scope,
        storm_on, float(storm_quiet), float(storm_min), rain_on,
        storm_channels=storm_channels)


# ───────────────────────── SMTP delivery ─────────────────────────
def _send_sync(subject: str, body: str, to_list: list[str],
               cfg: EffectiveAlertConfig) -> None:
    """Blocking SMTP send — run via asyncio.to_thread. Uses the resolved
    transport (DB over env). STARTTLS (587), implicit SSL (465), or plain."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from or cfg.smtp_username or "zasder-weather@localhost"
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)

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
async def _deliver(cfg: EffectiveAlertConfig, subject: str, body: str,
                   push_title: str, push_body: str,
                   email_ok: bool = True, *,
                   push_ok: bool = True,
                   kind: str = "alert", mac: str | None = None) -> bool:
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
            res = await apns.send_to_all(push_title, push_body)
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
    if not attempted and not delivered:
        log.info("alert had no willing channel (muted by scope / no "
                 "recipients) — treating as handled: %s", push_title)
    handled = delivered or not attempted
    if handled:
        # History rides the HANDLED outcome, not the delivered one: callers
        # retry unhandled alerts next tick (logging those would duplicate),
        # while a muted alert still happened — it lands with delivered=0 and
        # the app's Recent list becomes the only place it's visible at all.
        try:
            await db.log_alert(int(time.time() * 1000), kind, mac,
                               push_title, push_body, delivered)
        except Exception as e:
            log.exception("alert history write failed: %s", e)
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
            try:
                await self._tick()
            except Exception as e:
                log.exception("alert tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

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
        cfg = await effective_config()
        # Run if EITHER channel can deliver — email (cfg.enabled) or push
        # (local APNs key or a configured relay, env or app-managed).
        if not cfg.enabled and not await apns.push_configured():
            return
        now_ms = int(time.time() * 1000)
        repeat_ms = int(cfg.repeat_hours * 3600 * 1000)
        devices = await db.list_devices()
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
                        kind="rule", mac=d["mac"])
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
                        log.info("storm summary sent for %s: %.2fin over %.1fh",
                                 dname, summary.total_in, summary.duration_hours)
                    else:
                        log.warning("storm summary delivery failed for %s; "
                                    "will retry next tick", mac)
                    continue
                # Not worth reporting: close it silently so a drizzle does
                # not leave an event open forever.
                await db.upsert_storm_state(mac, None, None, field, value, obs_ms)
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

    async def _check_smart_alerts(self, cfg, devices, now_ms: int) -> None:
        """Frost / heat / rapid-pressure-drop alerts, edge-triggered per device."""
        states = await db.get_smart_alert_states()
        cutoff_ms = now_ms - 3 * 3600 * 1000   # 3h ago, for pressure tendency

        def _f(v) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        for d in devices:
            mac = d["mac"]
            last = d.get("lastData") or {}
            tempf = _f(last.get("tempf"))
            feels = _f(last.get("feelsLike"))
            cur_p = _f(last.get("baromrelin"))
            # Pressure change over the last 3h (needs a historical reading).
            delta = None
            if cur_p is not None:
                past_p = await db.value_at_or_before(mac, "baromrelin", cutoff_ms)
                if past_p is not None:
                    delta = cur_p - past_p

            for kind in SMART_KINDS:
                cond = smart_condition(
                    kind, tempf=tempf, feels=feels, pressure_delta_3h=delta,
                    frost_f=settings.smart_alert_frost_f,
                    heat_f=settings.smart_alert_heat_f,
                    drop_inhg=settings.smart_alert_pressure_drop_inhg)
                prev = states.get((mac, kind), 0)
                if cond and not prev:
                    dname = d.get("name") or mac
                    title, body = build_smart_message(
                        kind, dname, tempf=tempf, feels=feels,
                        pressure_delta_3h=delta)
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
                elif not cond and prev and smart_cleared(
                        kind, tempf=tempf, feels=feels, pressure_delta_3h=delta,
                        frost_f=settings.smart_alert_frost_f,
                        heat_f=settings.smart_alert_heat_f,
                        drop_inhg=settings.smart_alert_pressure_drop_inhg):
                    # Re-arm only once the condition clears by a deadband, so a
                    # value wobbling on the boundary can't re-fire every tick.
                    await db.upsert_smart_alert_state(mac, kind, 0, now_ms)


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
