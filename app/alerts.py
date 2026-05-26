"""Device-staleness email alerting.

A background task polls each device's last-report age and emails the operator
when a device that was reporting goes quiet (e.g. an SDR/LilyGO board hangs, a
sensor battery dies, a cloud API key expires). The decision logic is pure and
unit-tested (`decide`); the task layer adds DB-persisted state + SMTP delivery.

Off unless `alert_email_to` and `smtp_host` are configured (see Settings).
"""
import asyncio
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import db
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

    # State unchanged. Optionally re-remind while still stale.
    if cur == "stale" and repeat_ms > 0:
        last_notified = prior.get("notified_ms") or prior.get("changed_ms") or 0
        if now_ms - last_notified >= repeat_ms:
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


def build_alert(event: str, name: str, mac: str, last_seen_ms: int | None,
                now_ms: int, threshold_min: float, tz_name: str) -> tuple[str, str]:
    """Build (subject, body) for an alert. Pure — unit-testable."""
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


def build_threshold_message(device_name: str, field: str, value: float,
                            comparator: str, threshold: float) -> tuple[str, str]:
    """(title, body) for a tripped threshold rule. Pure — unit-testable."""
    label = _FIELD_LABELS.get(field, field)
    unit = _FIELD_UNITS.get(field, "")
    sym = _COMPARATOR_SYM.get(comparator, comparator)
    def fmt(v: float) -> str: return f"{v:g}{unit}"
    return (f"{device_name}: {label} alert",
            f"{label} is {fmt(value)} ({sym} {fmt(threshold)})")


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
    return EffectiveAlertConfig(
        enabled, transport, recipients, float(default_thr), float(repeat),
        smtp_host, smtp_port, smtp_username, smtp_password, smtp_from,
        smtp_tls, smtp_ssl)


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
                   push_title: str, push_body: str) -> bool:
    """Send an alert through every configured channel (email + push). Returns
    True if at least one channel delivered. Shared by device-down + threshold."""
    delivered = False
    if cfg.enabled:
        try:
            await asyncio.to_thread(_send_sync, subject, body, cfg.recipients, cfg)
            delivered = True
        except Exception as e:
            log.exception("alert email send failed: %s", e)
    if settings.apns_configured:
        try:
            from . import apns
            res = await apns.send_to_all(push_title, push_body)
            if res.get("sent"):
                delivered = True
        except Exception as e:
            log.exception("alert push send failed: %s", e)
    return delivered


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
        cfg = await effective_config()
        # Run if EITHER channel can deliver — email (cfg.enabled) or push.
        if not cfg.enabled and not settings.apns_configured:
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
                if await _deliver(cfg, subject, bodytext, ptitle, pbody):
                    notified_ms = now_ms     # advance re-notify clock only on delivery
                log.info("device-down alert %s for %s (%s)", dec.event, name, mac)

            if prior is None or dec.state != prior["state"] or dec.event:
                await db.upsert_alert_state(
                    mac, dec.state, last_seen, dec.changed_ms, notified_ms)

        # ── threshold rules: fire when a device's latest reading crosses a rule
        await self._check_threshold_rules(cfg, devices, now_ms)

    async def _check_threshold_rules(self, cfg, devices, now_ms: int) -> None:
        rules = await db.list_alert_rules(enabled_only=True)
        if not rules:
            return
        rstates = await db.get_rule_states()
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
                prev = rstates.get((rule["id"], d["mac"]), 0)
                now_trig, fire = evaluate_rule(rule["comparator"], rule["threshold"], val, prev)
                if int(now_trig) != prev:
                    await db.upsert_rule_state(rule["id"], d["mac"], int(now_trig), now_ms)
                if fire:
                    dname = d.get("name") or d["mac"]
                    title, body = build_threshold_message(
                        dname, rule["field"], val, rule["comparator"], rule["threshold"])
                    await _deliver(cfg, f"[Zasder Weather] {title}", body, title, body)
                    log.info("threshold alert fired: rule %s (%s) on %s value=%.3f",
                             rule["id"], rule["field"], dname, val)


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
