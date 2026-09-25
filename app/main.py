import asyncio
import base64
import hashlib
import json
import html as _html
import math
import logging
import os
import uuid
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .limits import BodySizeLimitMiddleware
from .updates import UpdateChecker
from .version import __version__

from . import db
from . import build_guard
from . import changes
from . import nws_families as _nws_families
from . import email_card as _email_card
from .alerts import AlertMonitor
from .capture import router as capture_router
from .config import settings, tokens_match
from . import source_status
from . import forecast_skill
from . import config_backup
from . import restore as _restore
from . import public_dashboard as _pd
from .discovery import router as discovery_router
from .mcp import router as mcp_router
from . import oauth
from .oauth import router as oauth_router
from .ingest import router as ingest_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs every request at INFO with the FULL URL — and the WU import,
# TWC forecast, AWN and WeatherLink clients all carry their API keys as
# query parameters (those APIs accept them nowhere else). Left at INFO,
# every forecast refresh and each of the ~1400 calls of a WU import writes
# the plaintext key into the server logs / Fly log drain. The modules scrub
# their own exception messages; this closes httpx's channel too.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("api")


def attach_file_log(db_path: str) -> str | None:
    """Keep the process log on the data volume, rotated (5 x 10 MB), so a
    boot's lines survive Fly's short `fly logs` window (Volney, 2026-09-02:
    the boot log that would have explained a stuck writer had already
    rolled off). LOG_FILE overrides the path; LOG_FILE="" disables."""
    import os
    from logging.handlers import RotatingFileHandler
    path = os.environ.get("LOG_FILE")
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                            "logs", "zasder.log")
    if not path:
        return None
    root = logging.getLogger()
    for h in list(root.handlers):
        if not getattr(h, "_zasder_file_log", False):
            continue
        if getattr(h, "baseFilename", None) == path:
            return path
        # A previous boot in this process (the test suite boots the app
        # per test with a fresh temp database) pointed at another file.
        root.removeHandler(h)
        h.close()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=10 * 2**20, backupCount=5)
    except OSError as e:
        log.warning("file log disabled (%s): %s", path, e)
        return None
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h._zasder_file_log = True          # type: ignore[attr-defined]
    root.addHandler(h)
    return path


_WRITE_LOCK_PROBE_S = 30.0
_WRITE_LOCK_STRIKES = 3
_WRITE_LOCK_DUMP_EVERY_S = 600.0


def _probe_write_lock() -> bool:
    """True when the writer is free. A one-second BEGIN IMMEDIATE on a
    fresh connection, rolled back at once — never holds anything itself."""
    import sqlite3
    if not db.is_open():
        return True        # a restore holds the file; that is not a stuck writer
    conn = sqlite3.connect(settings.database_path, timeout=1.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except sqlite3.OperationalError as e:
        if db.is_lock_error(e):
            return False
        raise
    finally:
        conn.close()


def dump_all_threads(reason: str) -> None:
    """Every thread's stack, to stderr AND to threads.log beside the
    database: Fly's log window is a hundred lines and rolled the first
    dump straight off (2026-09-02 05:39)."""
    import faulthandler
    import os
    import sys
    log.error("write lock held for %s — dumping every thread's stack",
              reason)
    # 2.1: the statements, not just the stacks. A thread dump names the
    # function that holds or waits; these lines name the SQL, how long
    # its transaction has been open, and where a running rollup rebuild
    # is (phase, station, cursor).
    lines = [f"open transaction {tx['open_s']}s: {tx['sql']}"
             for tx in db.open_transactions()]
    try:
        from . import insights as _insights
        if (flight := _insights.in_flight()):
            lines.append(flight)
    except Exception:       # never let the diagnostic break the dump
        pass
    for line in lines:
        log.error("%s", line)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(
            settings.database_path)), "logs", "threads.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("\n=== %s write lock held for %s ===\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), reason))
            for line in lines:
                f.write(line + "\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except OSError as e:
        log.warning("threads.log not written: %s", e)


async def _write_lock_watchdog(probe=None, dump=None,
                               sleep=asyncio.sleep) -> None:
    probe = probe or (lambda: asyncio.to_thread(_probe_write_lock))
    dump = dump or dump_all_threads
    strikes = 0
    last_dump = -_WRITE_LOCK_DUMP_EVERY_S
    while True:
        await sleep(_WRITE_LOCK_PROBE_S)
        try:
            free = await probe()
        except Exception:
            log.exception("write-lock probe failed")
            continue
        if free:
            if strikes >= _WRITE_LOCK_STRIKES:
                log.warning("write lock free again after %d probes", strikes)
            strikes = 0
            continue
        strikes += 1
        now = time.monotonic()
        if (strikes >= _WRITE_LOCK_STRIKES
                and now - last_dump >= _WRITE_LOCK_DUMP_EVERY_S):
            last_dump = now
            dump("%d probes (~%ds)" % (strikes,
                                       int(strikes * _WRITE_LOCK_PROBE_S)))


# Between chart-index rebuild attempts. Module-level so a test can shrink
# it; production waits a minute, then two.
_CHART_INDEX_RETRY_DELAY_S = 60.0
_CHART_INDEX_ATTEMPTS = 3


async def _heal_rollups_after(app: FastAPI) -> None:
    """The boot's background rollup rebuild, reusable after a restore:
    waits for a pending chart-index build first (they contend for the
    writer), then rebuilds and clears the dirty flag."""
    from . import insights as _insights
    idx = getattr(app.state, "chart_index_task", None)
    if idx is not None and not idx.done():
        await idx
    try:
        stats = await _insights.rebuild()
        log.info("background rollup rebuild done: %s", stats)
    except Exception:
        log.exception("background rollup rebuild failed — records stay on "
                      "the raw path until one succeeds")


async def _chart_index_job() -> None:
    """The deferred big-archive chart-index rebuild, with the ingest
    write-behind drained after EVERY attempt (the queue fills only while
    a CREATE is running, and the readings must land before the next
    attempt parks more behind them).

    A few in-process retries (R11): a transient failure — disk-pressure
    hiccup, an unrelated exception — used to leave the rebuild flag set
    with nothing re-attempting until the next boot, so charts ran
    uncovered for the process's whole life. NOT retried: a lock timeout.
    That means another long writer (the rollup healer, a WU import) held
    the database for the full busy_timeout, and re-queueing behind it
    only stretches the ingest outage; the next boot's probe defers again
    with the old index still serving (build-then-swap, 2.0)."""
    from . import ingest as _ingest
    for attempt in range(_CHART_INDEX_ATTEMPTS):
        try:
            if attempt:
                await asyncio.sleep(_CHART_INDEX_RETRY_DELAY_S * attempt)
            t0 = time.time()
            log.info("chart index rebuild starting in background "
                     "(large archive, attempt %d)", attempt + 1)
            await db.rebuild_chart_index()
            log.info("chart index rebuilt in %.0fs", time.time() - t0)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if db.is_lock_error(e):
                log.warning("background chart index rebuild lost the lock "
                            "race (%s); not retrying — the previous index "
                            "still serves and the next boot defers again", e)
                return
            log.exception("background chart index rebuild failed "
                          "(attempt %d/%d)", attempt + 1,
                          _CHART_INDEX_ATTEMPTS)
        finally:
            try:
                await _ingest.drain_write_behind()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ingest write-behind drain failed; %d "
                              "reading(s) still parked",
                              _ingest.write_behind_depth())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if the stripped public build was deployed onto a host that is
    # supposed to serve the relay. Deliberately BEFORE anything else, and
    # deliberately not marked PRIVATE — this line must survive into the mirror,
    # where it is inert unless REQUIRE_RELAY is set. See app/build_guard.py.
    build_guard.assert_build_variant()
    # A restore killed between its two renames leaves a marker beside the
    # database; put the pre-restore copy back BEFORE init_db, which would
    # otherwise create an empty database under the vanished name (R21-02).
    try:
        if (note := await asyncio.to_thread(_restore.recover_at_boot)):
            log.warning("restore recovery at boot: %s", note)
    except Exception:
        log.exception("restore recovery at boot failed; continuing with the file on disk")
    await db.init_db()
    app.state.started_at = time.time()
    attach_file_log(settings.database_path)
    # The server's identity for OAuth, said once where an operator looks
    # first (round-three review SEC-G2): which origin, and why that one.
    try:
        origin, why = oauth.describe_origin()
        log.info("oauth origin: %s (%s)", origin or "the request's Host", why)
    except Exception:
        log.exception("could not describe the OAuth origin")
    # Orphaned snapshot sweep (2.0, 2026-09-01): a `.dbbackup-*.db` was
    # deleted only after a SUCCESSFUL download, so every timed-out or
    # abandoned backup left a database-sized file on the volume — Volney's
    # box held three (5.3 GB) beside a 2.07 GB database and the next
    # backup refused with 507. At boot nothing can be in flight, so any
    # age goes; an hourly pass below catches the rest.
    try:
        swept = await asyncio.to_thread(_sweep_orphan_snapshots, at_boot=True)
        # 2.1: a restore upload a killed process left beside the database.
        if (n := await asyncio.to_thread(_restore.sweep_leftovers)):
            log.info("boot sweep removed %d abandoned restore upload(s)", n)
        if swept["deleted"]:
            log.info("boot sweep removed %d orphaned database snapshot(s), "
                     "%d MB freed", len(swept["deleted"]),
                     swept["bytes_freed"] // 2**20)
    except Exception:
        log.exception("orphaned snapshot sweep at boot failed")
    # Big-archive chart-index rebuild runs AFTER startup (v1.8.1): inline
    # at boot it outlived Fly's health-check window on a 1.15M-row box and
    # the 1.8.0 upgrade crash-looped. Strong ref on app.state; charts are
    # slower-but-correct until it completes, and an interrupted run just
    # defers again next boot.
    # 2.2: a stable identity for THIS server's data, minted once and kept
    # in the database, so an app holding several connections can key its
    # caches and per-station preferences by server rather than by URL
    # (a renamed host or a second address is still the same server; a
    # snapshot restored elsewhere brings its identity with its data).
    # BEFORE the chart-index rebuild is kicked off: that job takes the
    # SQLite write lock for minutes on a big archive, and a first-boot
    # set_kv behind it would sit on busy_timeout past Fly's health check
    # (CodeRabbit, PR #37).
    await ensure_server_id()
    app.state.chart_index_task = None
    if db.chart_index_rebuild_needed():
        app.state.chart_index_task = asyncio.create_task(_chart_index_job())
    # Declare every source up front, configured or not. "Not set up" and "set
    # up but broken" are the two answers a self-hoster needs to tell apart,
    # and they look identical from outside.
    source_status.reset()
    source_status.declare("custom-ingest", True,
                          note="LilyGO boards, SDR relays and the WeatherLink "
                               "Live poller POST here; health is per-device "
                               "last-seen, see /api/devices")
    # Cloud pollers (AmbientWeather, Davis WeatherLink, Tempest) — owned by
    # the IntegrationManager so credentials configured FROM THE APP
    # (/api/integrations → server_kv, kv-over-env like the WU key) apply at
    # boot and on change without a redeploy. Each provider's source_status
    # is declared inside apply(), configured or not: "not set up" and "set
    # up but broken" are the two answers a self-hoster must be able to tell
    # apart. AcuRite-only deploys configure none of them and rely entirely
    # on /ingest/custom.
    from .integrations import IntegrationManager
    integration_manager = IntegrationManager()
    await integration_manager.start_all()
    app.state.integration_manager = integration_manager

    async def _reconcile_after_restore() -> None:
        """Bring the running process in line with a database that was just
        swapped in (round-four review R21-03): the pollers re-apply from the
        restored credentials, every process cache that mirrors the database
        is dropped, the deferred jobs a fresh boot would schedule are
        scheduled, and the report backfill runs. Only after this does the
        restore report done."""
        global _PUBLIC_DASH_CACHE
        from . import wu_upload as _wu, share_targets as _st, alerts as _al
        db._YEAR_PRIOR_CACHE.clear()
        db._DAILY_ROLLUP_CACHE.clear()
        _RECORDS_CACHE.clear()
        _OBS_COUNT_CACHE.clear()
        _PUBLIC_DASH_CACHE = None
        _SHARE_TEST_LAST.clear()
        _wu._stats.clear()
        _st._last_send_ms.clear()
        from . import map_beacon as _mb
        _mb._last_publish_ms.clear()
        source_status.reset()
        source_status.declare("custom-ingest", True,
                              note="LilyGO boards, SDR relays and the WeatherLink "
                                   "Live poller POST here; health is per-device "
                                   "last-seen, see /api/devices")
        # Stop, rebuild and start every provider from the restored kv: a
        # provider the restored file configures starts, one it does not
        # stops, a changed credential is a new client.
        await integration_manager.start_all()
        if settings.insights:
            import time as _time
            await db.set_kv("rollups_dirty", str(_time.time_ns()))
            heal = getattr(app.state, "rollup_heal_task", None)
            if heal is None or heal.done():
                app.state.rollup_heal_task = asyncio.create_task(_heal_rollups_after(app))
        if db.chart_index_rebuild_needed():
            idx = getattr(app.state, "chart_index_task", None)
            if idx is None or idx.done():
                app.state.chart_index_task = asyncio.create_task(_chart_index_job())
        try:
            added = await _al.backfill_storm_reports()
            if added:
                log.info("post-restore storm report backfill: %d row(s)", added)
        except Exception:
            log.exception("post-restore storm report backfill failed")

    _restore.POST_SWAP_HOOKS.append(_reconcile_after_restore)

    # Device-staleness email alerts — independent of any poller; watches ALL
    # devices (cloud + SDR) for going quiet. ALWAYS started: it re-reads the
    # effective config each tick and no-ops unless alerts are enabled with a
    # transport + recipients. Gating on env SMTP at boot would miss transport
    # configured later from the app (PUT /api/alerts → DB), so the monitor
    # must already be running to pick that up without a redeploy.
    alert_monitor = AlertMonitor()
    await alert_monitor.start()
    app.state.alert_monitor = alert_monitor
    log.info("staleness alert monitor started (active once alerts are configured)")

    # Self-healing rollups: a repair or column backfill marks the ledgers
    # dirty (records() serves raw — correct but slow — while the flag is
    # set), and this background rebuild clears it without an operator having
    # to know POST /api/insights/rebuild exists (CODE_REVIEW_R5 R5-14).
    # BACKGROUND on purpose: a full rebuild of a 1M-row archive takes
    # minutes, and running it inline here failed a deploy's health checks
    # once already (2026-08-20).
    # 1.9: Insights defaults ON (Volney, field test 2026-08-27 — "why is
    # it not on by default?"), so a server that just gained it over
    # existing history must also SELF-backfill: empty rollup tables beside
    # a populated observations table get the dirty nonce here, and the
    # same background heal below runs the one-time rebuild nobody should
    # have to know about. Fresh installs skip it (no observations yet).
    if settings.insights and not await db.get_kv("rollups_dirty"):
        async with db.connect() as _c:
            has_rollups = await (await _c.execute(
                "SELECT 1 FROM daily_rollups LIMIT 1")).fetchone()
            has_obs = await (await _c.execute(
                "SELECT 1 FROM observations LIMIT 1")).fetchone()
        if has_obs and not has_rollups:
            import time as _time
            await db.set_kv("rollups_dirty", str(_time.time_ns()))
            log.info("insights enabled over existing history — scheduling "
                     "the one-time background rollup rebuild")
    if settings.insights and await db.get_kv("rollups_dirty"):
        from . import insights as _insights

        async def _heal_rollups() -> None:
            # AFTER the chart-index rebuild when one is pending (2.0): the
            # two used to start together and contend for the write lock —
            # the healer's BEGIN IMMEDIATE transactions waiting out
            # busy_timeout behind a minutes-long CREATE INDEX, or the
            # CREATE losing the race to them and failing. Unchanged when
            # no rebuild is pending (the task is None).
            idx = getattr(app.state, "chart_index_task", None)
            if idx is not None:
                await idx
            try:
                stats = await _insights.rebuild()   # clears the flag itself
                log.info("background rollup rebuild done: %s", stats)
            except Exception:
                log.exception("background rollup rebuild failed — records "
                              "stay on the raw path until one succeeds")
        app.state.rollup_heal_task = asyncio.create_task(_heal_rollups())

    # 2.1 Reports: every storm summary storm_history already holds becomes
    # a report row, once, in the background (the list was empty on day
    # one otherwise). Delayed so ingest and the health checks go first.
    async def _backfill_storm_reports() -> None:
        await asyncio.sleep(20)
        try:
            from . import alerts as _al
            added = await _al.backfill_storm_reports()
            if added:
                log.info("reports: %d storm summar%s backfilled from "
                         "storm_history", added, "y" if added == 1 else "ies")
        except Exception:
            log.exception("storm report backfill failed; will retry next boot")
    app.state.report_backfill_task = asyncio.create_task(
        _backfill_storm_reports())

    # 1.9 column backfill: when the migration added the field-survey
    # columns to an existing database, fill them from data_json in
    # background chunks — a foreground full-table UPDATE at boot is
    # minutes of lock + a database-sized WAL (the chart-index lesson).
    app.state.colfill_task = None
    if await db.colfill_pending():
        async def _colfill() -> None:
            await asyncio.sleep(30)
            log.info("1.9 column backfill starting (chunked, background)")
            while True:
                try:
                    more = await db.backfill_extra_columns_chunk()
                except Exception:
                    log.exception("column backfill chunk failed — retrying "
                                  "in 10 minutes")
                    await asyncio.sleep(600)
                    continue
                if not more:
                    log.info("1.9 column backfill complete")
                    return
                await asyncio.sleep(1)     # yield; ingest goes first
        app.state.colfill_task = asyncio.create_task(_colfill())

    # History retention (1.9, opt-in; quiet-hour window 2.0): the nightly
    # pass runs BOTH aging jobs inside the operator's window — see
    # _retention_daily. Always started: the knobs are app-managed at
    # runtime (env is only the fallback), so they are re-read at every
    # window rather than frozen at boot.
    app.state.history_thin_task = asyncio.create_task(_retention_daily())

    # Hourly orphaned-snapshot sweep (2.0): a backup job whose client went
    # away mid-poll, or a ready snapshot nobody fetched inside the fresh
    # window, would otherwise sit until the next boot. Keeps the live
    # job's file; see _sweep_orphan_snapshots.
    async def _snapshot_sweep_hourly() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                swept = await asyncio.to_thread(_sweep_orphan_snapshots)
                if swept["deleted"]:
                    log.info("hourly sweep removed %d orphaned database "
                             "snapshot(s), %d MB freed",
                             len(swept["deleted"]),
                             swept["bytes_freed"] // 2**20)
            except Exception:
                log.exception("orphaned snapshot sweep failed")
    app.state.snapshot_sweep_task = asyncio.create_task(_snapshot_sweep_hourly())

    # OAuth housekeeping (2.1): expired codes and tokens, idle clients. At
    # boot and hourly.
    from . import oauth as _oauth
    try:
        await _oauth.sweep_expired()
    except Exception:
        log.exception("oauth sweep at boot failed")

    async def _oauth_sweep_hourly() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await _oauth.sweep_expired()
            except Exception:
                log.exception("oauth sweep failed")
    app.state.oauth_sweep_task = asyncio.create_task(_oauth_sweep_hourly())

    # Write-lock watchdog (2026-09-02): a connection held the writer for
    # four minutes without appending a WAL frame and every write 503'd;
    # nothing in the log said WHO. SQLite cannot name the holder, but the
    # process can: when BEGIN IMMEDIATE fails three probes in a row, dump
    # every thread's stack (aiosqlite runs each connection on its own
    # thread, so the stuck statement is in there) — once per ten minutes.
    app.state.write_lock_task = asyncio.create_task(_write_lock_watchdog())

    # Daily "is there a newer release?" check → status-page banner + /api/version.
    update_checker = UpdateChecker(app)
    update_checker.start()
    app.state.update_checker = update_checker

    # Opt-in self-update rides on the checker's result (AUTO_UPDATE=1 +
    # FLY_API_TOKEN — inert without both; see app/self_update.py).
    from .self_update import SelfUpdater
    self_updater = SelfUpdater(app)
    self_updater.start()
    app.state.self_updater = self_updater

    # MQTT publisher (Home Assistant discovery) — only if a broker is configured.
    mqtt_pub = None
    if settings.mqtt_host:
        from .mqtt_publish import MqttPublisher
        mqtt_pub = MqttPublisher()
        await mqtt_pub.start()
        app.state.mqtt_pub = mqtt_pub
        log.info("MQTT publisher started (broker %s:%s)",
                 settings.mqtt_host, settings.mqtt_port)

    # AirGradient LAN poller (1.9): env-only — a backend that can see the
    # monitors' LAN is a local install, where env is the native config.
    # Independent of the IntegrationManager (which owns the app-managed
    # cloud credentials).
    ag_local = None
    if (settings.airgradient_local_hosts or "").strip():
        from .airgradient_client import AirGradientLocalClient
        from .airgradient_poller import AirGradientLocalPoller
        ag_local = AirGradientLocalPoller(
            AirGradientLocalClient(),
            settings.airgradient_local_hosts.split(","),
            settings.airgradient_poll_interval_seconds)
        await ag_local.start()
        app.state.airgradient_local = ag_local

    try:
        yield
    finally:
        # rollup_heal_task: cancellation mid-scan clears the partial tables
        # (the _rebuild_locked safeguard) and LEAVES the dirty flag set, so
        # the next boot retries — never a silently truncated ledger. It is
        # cancelled AND awaited below with the rest (R18 finding 12: it
        # used to be cancelled here and dropped, so its aiosqlite teardown
        # could land after the loop closed).
        await integration_manager.stop_all()
        await alert_monitor.stop()
        from . import wu_import
        await wu_import.stop()
        await update_checker.stop()
        await self_updater.stop()
        if mqtt_pub is not None: await mqtt_pub.stop()
        if ag_local is not None: await ag_local.stop()
        # Cancel AND await: a cancelled-but-unawaited task finalizes at GC,
        # which can land after the event loop closes — every test boot then
        # logs "Event loop is closed" from the task's teardown (CI noise,
        # and the same would greet every real shutdown).
        # chart_index_task (2.0): a cancelled CREATE INDEX rolls back and
        # the old index still serves (build-then-swap); readings parked
        # in the ingest write-behind are gone with the process, which
        # the drain's log line at shutdown makes visible.
        # _STORAGE_TASK (2.0): the storage measurement job — a read-only
        # scan under a progress handler; cancelling it mid-walk loses
        # nothing but the report, and the next GET starts it over.
        # Every app-owned task, in one list (R18 finding 12): the public
        # dashboard refresh, a database snapshot in progress (its partial
        # file is the sweep's to remove), the records warmers and the
        # per-station recounts were never reaped and could still own an
        # aiosqlite operation when the loop went away.
        reapers = [t for t in (getattr(app.state, "rollup_heal_task", None),
                               getattr(app.state, "history_thin_task", None),
                               getattr(app.state, "snapshot_sweep_task", None),
                               getattr(app.state, "write_lock_task", None),
                               getattr(app.state, "colfill_task", None),
                               getattr(app.state, "chart_index_task", None),
                               getattr(app.state, "report_backfill_task", None),
                               getattr(app.state, "oauth_sweep_task", None),
                               _STORAGE_TASK,
                               _PUBLIC_DASH_REFRESH_TASK,
                               _DB_BACKUP_TASK,
                               *list(_WARM_TASKS),
                               *list(_OBS_COUNT_TASKS.values()))
                   # Alive, and OURS: a handle left over from another loop
                   # (a test's, or a previous lifespan's) cannot be awaited
                   # here and would raise at the gather.
                   if t is not None and not t.done()
                   and t.get_loop() is asyncio.get_running_loop()]
        for t in reapers:
            t.cancel()
        if reapers:
            await asyncio.gather(*reapers, return_exceptions=True)
        # 2.3: the hand-kept list above cannot know every fire-and-forget
        # task (a delivery's webhook post, a WU upload, a restore swap, a
        # write-behind flush). Any task still alive whose coroutine lives
        # in this package is ours: cancel and await it, then wait for the
        # last aiosqlite connection to close, so no worker thread can
        # call into a loop that is already gone. That call was the
        # eleven CI-only "Event loop is closed" thread warnings
        # (2026-09-14): a slow runner closed the test loop with an app
        # task still mid-query.
        await _reap_stray_app_tasks()


_APP_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _is_app_task(t: "asyncio.Task") -> bool:
    """A task whose coroutine was written in this package (not starlette's,
    anyio's or a test's) — the only ones a shutdown may cancel."""
    try:
        code = t.get_coro().cr_code          # type: ignore[union-attr]
    except AttributeError:
        return False
    return os.path.abspath(code.co_filename).startswith(_APP_DIR)


async def _reap_stray_app_tasks(drain_s: float = 5.0) -> None:
    me = asyncio.current_task()
    stray = [t for t in asyncio.all_tasks()
             if t is not me and not t.done() and _is_app_task(t)]
    for t in stray:
        t.cancel()
    if stray:
        await asyncio.gather(*stray, return_exceptions=True)
        log.info("shutdown reaped %d stray task(s): %s", len(stray),
                 ", ".join(t.get_name() for t in stray))
    deadline = time.monotonic() + drain_s
    while db.active_connections() > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


# How often the retention scheduler re-reads its knobs while waiting for
# the window: the start time is app-managed, so a sleep is never longer
# than this before the next start is recomputed.
_RETENTION_RECHECK_S = 3600.0


async def _retention_daily() -> None:
    """History retention: row thinning past the detail window and
    data_json trimming past the (usually shorter) payload window, run
    inside the station-local quiet-hour window and NEVER at boot — the
    scheduler waits for the next window start, strictly after now, so a
    restart inside tonight's window waits for tomorrow's (2026-09-02:
    every boot resumed the heavy pass two minutes in while ingest 503'd).
    Both jobs share the window's minute budget (thinning first, the JSON
    trim with what remains) and resume from their watermarks the next
    night. Off-thread (sqlite3 sync, short bounded transactions); guarded
    inside (rollups, colfill, chart-index rebuild, lock backoff)."""
    from . import maintenance
    while True:
        try:
            eff = await maintenance.effective_retention()
            delay = maintenance.seconds_until_thin_window(
                eff["thin_window_start"])
        except Exception:
            log.exception("history retention: could not schedule the "
                          "window — retrying in an hour")
            delay = _RETENTION_RECHECK_S
        if delay > _RETENTION_RECHECK_S:
            await asyncio.sleep(_RETENTION_RECHECK_S)
            continue
        await asyncio.sleep(delay)
        try:
            eff = await maintenance.effective_retention()
            if (eff["detail_days"] > 0 or eff["json_days"] > 0) \
                    and eff["thin_window_minutes"] > 0:
                await asyncio.to_thread(maintenance.run_thin_night, eff)
        except Exception:
            log.exception("history retention pass failed — nothing "
                          "ages out until a pass succeeds")


# /docs, /redoc, /openapi.json are exposed by default in FastAPI and
# advertise the shapes of every route — including /ingest/* and
# /ingest/capture/* — to anyone who can hit the URL. They also load
# CDN scripts (Swagger UI), which exacerbates the missing CSP. Disable
# in production; set DEBUG=1 (or any truthy value) to re-enable for
# local development.
_DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(
    title="zasder weather",
    lifespan=lifespan,
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    openapi_url="/openapi.json" if _DEBUG else None,
)

@app.exception_handler(RequestValidationError)
async def _validation_error_no_echo(request: Request,
                                    exc: RequestValidationError) -> JSONResponse:
    """422 bodies minus the "input" echo. Pydantic's default includes the
    rejected value verbatim — for credential-carrying routes (wu-station
    upload_key, the WU api key) the app renders that body in a persistent
    label, redisplaying a just-typed secret the SecureField hid."""
    errors = [{k: v for k, v in e.items() if k != "input"}
              for e in exc.errors()]
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder(errors)})


# Throttled "database busy" logging, one line per path per minute — the
# same shape as _log_auth_failure below. Process-global; mutated only on
# the event loop thread.
_DB_BUSY_LOG_TS: dict[str, float] = {}
_DB_BUSY_LOG_INTERVAL_S = 60.0
_DB_BUSY_LOG_MAX_PATHS = 256


def _log_db_busy(path: str) -> None:
    now = time.monotonic()
    last = _DB_BUSY_LOG_TS.get(path)
    if last is not None and now - last < _DB_BUSY_LOG_INTERVAL_S:
        return
    if len(_DB_BUSY_LOG_TS) >= _DB_BUSY_LOG_MAX_PATHS:
        _DB_BUSY_LOG_TS.clear()
    _DB_BUSY_LOG_TS[path] = now
    log.warning("database busy on %s — answered 503 Retry-After (throttled: "
                "1 line/min/path)", path)


@app.exception_handler(db.DatabaseUnavailable)
async def _database_unavailable(request: Request, exc: db.DatabaseUnavailable):
    """The maintenance lease is held (a restore is swapping files): a
    request that could not get a connection in time is told to retry,
    never served from the wrong file (R21-01)."""
    return JSONResponse(status_code=503, content={"detail": str(exc)},
                        headers={"Retry-After": "5"})


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_busy_to_503(request: Request,
                              exc: sqlite3.OperationalError):
    """A writer that waited out busy_timeout behind a long job (the
    chart-index build, a rollup rebuild) used to surface as a 500 — which
    a LilyGO counts toward its wipe heuristic and a relay client treats as
    "the server is broken". It is neither: 503 + Retry-After says "come
    back in a moment", and every client here already retries. Any OTHER
    OperationalError is a real fault and still 500s exactly as before."""
    if not db.is_lock_error(exc):
        raise exc
    _log_db_busy(request.url.path)
    return JSONResponse(status_code=503,
                        content={"detail": "database busy, retry"},
                        headers={"Retry-After": "5"})


app.include_router(capture_router)
app.include_router(discovery_router)
if settings.mcp_enabled:
    # 2.1: read-only MCP at POST /mcp, and the OAuth 2.1 authorization
    # server + discovery documents that let connectors reach it. One
    # switch for both (SEC-2): MCP_ENABLED=0 and none of these routes
    # exist, the well-known documents included.
    app.include_router(mcp_router)
    app.include_router(oauth_router)
app.include_router(ingest_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ───────────────────────── security middleware ─────────────────────────
# Two layers of hardening recommended by an external code review:
#   1. TrustedHostMiddleware — reject requests whose Host header doesn't
#      match an allow-list. Defends against Host-header poisoning where we
#      generate absolute URLs from the request: since 2.1 the OAuth
#      metadata documents and the /mcp WWW-Authenticate challenge do
#      (app/oauth.py `issuer`). Their first line of defence is
#      PUBLIC_BASE_URL, which fixes the origin regardless of Host; their
#      second is a Host sanity check that refuses anything but
#      host[:port]. This allow-list is the third. Configurable via the
#      ALLOWED_HOSTS env var (comma-separated). Defaults to "*" (accept
#      anything) so the public template works out-of-box; set this in
#      Fly secrets for production deploys (e.g.
#      ALLOWED_HOSTS="weather.example.com,*.fly.dev").
#   2. Browser security headers — CSP, HSTS, X-Content-Type-Options,
#      X-Frame-Options, Referrer-Policy. Especially important on the
#      public HTML status page; documents loading CDN scripts (Swagger UI
#      in DEBUG mode) need a CSP that allows them.

_allowed_raw = os.environ.get("ALLOWED_HOSTS", "*").strip()
_ALLOWED_HOSTS = [h.strip() for h in _allowed_raw.split(",") if h.strip()] or ["*"]
if _ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

# 3. Global body-size cap (added last → outermost middleware → runs FIRST):
#    bounds every request body before FastAPI parses JSON or checks auth, so
#    an anonymous malformed/chunked request can't stream unbounded data into
#    memory. See app/limits.py. Covers the /static mount too.
app.add_middleware(BodySizeLimitMiddleware)


class EcowittTokenScrub:
    """Pure-ASGI scrub for /ingest/ecowitt's query-string credential
    (CodeRabbit, PR #33): Ecowitt hardware can only carry the token in the
    URL, and uvicorn's default access logger prints the query string — so
    the live ingest token would land in the server logs on every upload.
    This moves the token into the X-Ingest-Token header (the route's
    preferred channel) and redacts it in scope['query_string'] BEFORE the
    app runs; uvicorn formats its access line from this same scope dict at
    response time, so the logged line shows token=REDACTED."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # rstrip: a gateway configured with a trailing slash 307s, and
        # uvicorn logs the ORIGINAL query string of that redirect — the
        # exact leak this middleware exists to prevent, triggered by the
        # most likely misconfiguration (R11).
        if (scope.get("type") == "http"
                and scope.get("path", "").rstrip("/") == "/ingest/ecowitt"
                and b"token=" in scope.get("query_string", b"")):
            from urllib.parse import parse_qsl, urlencode
            pairs = parse_qsl(scope["query_string"].decode("latin-1"),
                              keep_blank_values=True)
            token = next((v for k, v in pairs if k == "token"), "")
            if token:
                try:
                    encoded = token.encode("latin-1")
                except UnicodeEncodeError:
                    # A non-latin-1 token can't be a valid credential; skip
                    # the header (the route will 401) instead of 500ing
                    # before auth (CodeRabbit).
                    encoded = None
                if encoded is not None:
                    scope["headers"] = list(scope.get("headers", [])) + [
                        (b"x-ingest-token", encoded)]
            scope["query_string"] = urlencode(
                [(k, "REDACTED" if k == "token" else v)
                 for k, v in pairs]).encode("latin-1", errors="replace")
        await self.app(scope, receive, send)


app.add_middleware(EcowittTokenScrub)


# ── Inline-script CSP hashes ─────────────────────────────────────────────
# script-src deliberately carries no 'unsafe-inline': the public pages are
# the one place untrusted text (station names, location) meets HTML, so a
# blanket allowance would hand any markup slip a script foothold. But the
# pages DO run two inline scripts of our own — the embed's height
# broadcast and the spinner settle — and 'self' does not cover inline
# bodies, so browsers silently dropped both for as long as this CSP has
# existed (the eternally-spinning anemometer, and every auto-height
# "blank band" report: the messages were simply never posted). Allow
# exactly those two bodies by sha256 hash; anything else stays blocked.

_EMBED_HEIGHT_SCRIPT = """
/* Auto-height (1.7): tell the embedding page how tall this content really
   is, so its iframe can fit exactly instead of guessing a magic number.
   scrolling="no" clips anything below the frame's bottom edge, and a
   clipped bottom looks identical to content that never loaded — the
   50/50 missing-cards report (Doren, 2026-08-23). Fires on load and on
   every content resize; a no-op when the page isn't framed. The height
   is not sensitive, so the wildcard target is fine; the companion
   listener verifies the SOURCE frame before trusting the number. */
(function () {
  if (window.parent === window) return;
  var last = 0;
  /* Measure the CONTENT, never the frame (2026-09-23): inside an iframe
     documentElement.scrollHeight is at least the frame's own height, so
     it echoed the embedding page's fallback (1750px) straight back and a
     frame could grow but never shrink, leaving a tall empty band under
     the cards. The body's box is its content plus padding, whatever the
     frame's size; the corner spinner is position:fixed and adds nothing. */
  function contentHeight() {
    var b = document.body;
    var cs = getComputedStyle(b);
    return Math.ceil(b.getBoundingClientRect().height +
                     (parseFloat(cs.marginTop) || 0) +
                     (parseFloat(cs.marginBottom) || 0));
  }
  function post() {
    var h = contentHeight();
    if (Math.abs(h - last) < 2) return;
    last = h;
    window.parent.postMessage({ type: "zasder-embed-height", height: h }, "*");
  }
  if (window.ResizeObserver) {
    new ResizeObserver(post).observe(document.body);
  }
  window.addEventListener("load", post);
  post();
  /* Re-broadcast on a timer (field test 2026-08-28): WordPress/Divi
     "delay JS until interaction" optimizers attach the parent's
     listener LATE, after every post above already fired — and with
     static content ResizeObserver never fires again, so the iframe
     stayed at its fallback height forever (Doren's blank band under
     the records). Re-posting is a few bytes; the parent ignores
     repeats. Every 3s for the first 30s catches late listeners, then
     every 60s as a keep-alive until the page's own auto-refresh. */
  var ticks = 0;
  var timer = setInterval(function () {
    ticks += 1;
    last = 0;
    post();
    if (ticks === 10) {
      clearInterval(timer);
      setInterval(function () { last = 0; post(); }, 60000);
    }
  }, 3000);
})();
"""


def _csp_hash(script_body: str) -> str:
    """CSP source token for one inline script: sha256 over the exact bytes
    between the <script> tags, base64 as the spec requires."""
    digest = hashlib.sha256(script_body.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


_CSP_SCRIPT_HASHES: str | None = None


def _csp_script_hashes() -> str:
    # public_dashboard imports lazily elsewhere in this module (import-order
    # dance at startup); mirror that and cache after the first request.
    global _CSP_SCRIPT_HASHES
    if _CSP_SCRIPT_HASHES is None:
        from . import public_dashboard as _pd
        _CSP_SCRIPT_HASHES = (_csp_hash(_EMBED_HEIGHT_SCRIPT) + " "
                              + _csp_hash(_pd.SPINNER_SCRIPT))
    return _CSP_SCRIPT_HASHES


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Add a baseline set of browser security headers to every response.
    These mostly matter for HTML responses (the /status page and FastAPI's
    /docs when DEBUG=1) but cost nothing to set on JSON responses too."""
    response = await call_next(request)
    # Conservative CSP — page renders inline styles + same-origin images.
    # When DEBUG=1 and /docs is enabled, Swagger UI also needs cdn.jsdelivr.net
    # for its script and style assets; we allow that selectively.
    # /embed is the ONE page other sites may iframe (the operator's public
    # dashboard on their own homepage — the WeatherLink-embeddablePage use
    # case). It is anonymous, read-only, and has no actions, so there is
    # nothing for a framing page to clickjack. Everything else stays DENY.
    framable = request.url.path == "/embed"
    fa = "frame-ancestors *" if framable else "frame-ancestors 'none'"
    if _DEBUG:
        csp = ("default-src 'self'; "
               "img-src 'self' data:; "
               "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
               f"script-src 'self' {_csp_script_hashes()} https://cdn.jsdelivr.net; "
               f"connect-src 'self'; {fa}")
    else:
        csp = ("default-src 'self'; "
               "img-src 'self' data:; "
               "style-src 'self' 'unsafe-inline'; "
               f"script-src 'self' {_csp_script_hashes()}; "
               f"connect-src 'self'; {fa}")
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("Strict-Transport-Security",
                                 "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if not framable:
        response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy",
                                 "geolocation=(), microphone=(), camera=()")
    return response


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid token")
    return authorization.removeprefix("Bearer ")


# Throttled 401 logging: one line per source IP per minute, so failed bearer
# auth is at least VISIBLE (probes used to be completely silent) without a
# flood turning the log itself into the problem. Process-global like the other
# caches here; plain dict is fine (mutated only on the event loop thread).
_AUTH_FAIL_LOG_TS: dict[str, float] = {}
_AUTH_FAIL_LOG_INTERVAL_S = 60.0
_AUTH_FAIL_LOG_MAX_IPS = 1024


def _log_auth_failure(request: Request | None) -> None:
    host = (request.client.host if request and request.client else "?")[:64]
    now = time.monotonic()
    last = _AUTH_FAIL_LOG_TS.get(host)
    if last is not None and now - last < _AUTH_FAIL_LOG_INTERVAL_S:
        return
    if len(_AUTH_FAIL_LOG_TS) >= _AUTH_FAIL_LOG_MAX_IPS:
        _AUTH_FAIL_LOG_TS.clear()      # cheap bound; worst case one extra line per IP
    _AUTH_FAIL_LOG_TS[host] = now
    log.warning("rejected bearer auth from %s (throttled: 1 line/min/IP)", host)


def require_token(request: Request,
                  authorization: Annotated[str | None, Header()] = None) -> None:
    """READ-allowing dep: accepts api_token, reviewer_api_token, an env
    guest token, or an app-minted share token (db.guest_token_cache). Use
    on GETs."""
    try:
        presented = _extract_bearer(authorization)
        ok = tokens_match(presented,
                          settings.valid_api_tokens | db.guest_token_cache())
    except HTTPException:
        _log_auth_failure(request)
        raise
    if not ok:
        _log_auth_failure(request)
        raise HTTPException(status_code=401, detail="invalid token")
    # Last-used for the share screen. Second digest pass over the guest set
    # only (tokens_match doesn't say WHICH member matched); memory-only —
    # this dep is sync on the hot path, persistence happens on list.
    if tokens_match(presented, db.guest_token_cache()):
        db.touch_guest_token(presented)


def require_write_token(request: Request,
                        authorization: Annotated[str | None, Header()] = None) -> None:
    """MUTATING dep: only api_token. The reviewer/demo token is read-only,
    so it can't alter user state if the reviewer hits a write route. Use on
    every POST/PUT/PATCH/DELETE under /api/*."""
    try:
        ok = tokens_match(_extract_bearer(authorization), settings.write_tokens)
    except HTTPException:
        _log_auth_failure(request)
        raise
    if not ok:
        # Distinguish "wrong token" from "valid but read-only". The reviewer
        # token IS valid — telling its holder "invalid token" is factually
        # wrong, reads like broken demo credentials during App Review, and
        # gives a real read-only user no clue that a fuller token exists.
        # 403 (authenticated, not permitted) vs 401 (not authenticated).
        token = _extract_bearer(authorization)
        if tokens_match(token,
                        settings.valid_api_tokens | db.guest_token_cache()):
            raise HTTPException(
                status_code=403,
                detail="this access token is read-only — backups, restores and "
                       "other changes need the server's full-access API token")
        _log_auth_failure(request)
        raise HTTPException(status_code=401, detail="invalid token")


def require_shared_write(request: Request,
                         authorization: Annotated[str | None, Header()] = None) -> None:
    """STATION-OPS dep (1.9 write-share tier): the owner's api_token OR an
    app-minted WRITE share token (zww_, guest_tokens.can_write=1).

    Scope discipline: routes opt IN to this dependency one by one, and the
    security invariants pin the exact set — station operations only
    (rename/relocate a device, per-device alert toggles, threshold rules,
    push registration for the holder's own phone, a storm-watch start).
    Everything administrative — token minting, credentials, backups,
    restores, updates, retention, webhooks, integrations, device deletion
    — stays on require_write_token (owner only). A new mutating route
    defaults to OWNER; widening it to this tier is a deliberate, reviewed
    edit, never an accident. One route splits FIELDS by tier:
    /wu-station admits shared writers for the station-ID mapping but
    refuses its credential fields in-handler (R12 W1).

    Every write-share authentication is ATTRIBUTED: the token's last-used
    stamp advances and a write_audit row (label + token tail + method +
    path) is queued — the per-device attribution the tier requires."""
    try:
        presented = _extract_bearer(authorization)
    except HTTPException:
        _log_auth_failure(request)
        raise
    if tokens_match(presented, settings.write_tokens):
        return                                   # the owner; not audited
    if tokens_match(presented, db.write_guest_token_cache()):
        db.touch_guest_token(presented)
        db.record_write_audit(presented, request.method,
                              request.url.path)
        return
    if tokens_match(presented,
                    settings.valid_api_tokens | db.guest_token_cache()):
        raise HTTPException(
            status_code=403,
            detail="this access token is read-only — ask the station's "
                   "owner for a link that can make changes")
    _log_auth_failure(request)
    raise HTTPException(status_code=401, detail="invalid token")


def _is_reviewer(authorization: str | None) -> bool:
    """True when the presented bearer is the read-only reviewer/demo token."""
    return tokens_match(_extract_bearer(authorization), settings.reviewer_api_token)


def _is_limited_read(authorization: str | None) -> bool:
    """True for any read-only token that is NOT the operator's own.

    Covers the reviewer/demo token and every guest share token. Both can read
    /api/* but neither should see the operator's infrastructure: the alerts
    response carries SMTP host, username and sender, which is the maintainer's
    mail setup. Sharing a station with family must not also share that.

    Guest tokens were admitted to the read surface without being added here,
    so this generalises the reviewer check rather than adding a second one.
    App-minted share tokens (db.guest_token_cache) are the same read-only
    contract as the env guests and MUST be limited identically — they were
    admitted to require_token's union without being added here, which handed
    every share-link recipient the operator view: SMTP identity from
    /api/alerts and the un-stripped home coordinates from /api/devices
    (found by the 2026-08-20 review, same day the minting shipped).
    """
    if _is_reviewer(authorization):
        return True
    presented = _extract_bearer(authorization)
    return tokens_match(presented,
                        settings.guest_tokens | db.guest_token_cache())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness for Fly's checks and deploy.sh. Two additions in 2.1:
    `uid` (the effective uid, so a container whose entrypoint fell back
    to root is visible from outside -- deploy.sh's VERIFY asserts it) and
    a one-statement READ of the database with a short timeout, so a
    server whose SQLite file has gone away or whose connection hangs
    drops out of rotation instead of answering "ok" forever. It is a
    read: a held WRITE lock does not fail it (WAL readers never wait on
    the writer), and it must not, or a long rebuild would look like an
    outage."""
    import os as _os
    db_ok = await _database_healthy()
    body = {"status": "ok" if db_ok else "degraded", "version": __version__,
            "uid": _os.geteuid(), "db": db_ok}
    return JSONResponse(body, status_code=200 if db_ok else 503)


# (path, expires_monotonic, ok): the route is unauthenticated, so one
# probe answers every caller for a few seconds. Keyed by path so a test
# that swaps the database between calls is not answered from the last one.
_HEALTHZ_CACHE: tuple[str, float, bool] | None = None
HEALTHZ_CACHE_S = 5.0
# Single-flight (round-three review SEC-G4): the route is unauthenticated
# and the cache was written AFTER the probe, so concurrent anonymous hits
# during a slow probe each took a thread on the shared executor. Built
# lazily like _PUBLIC_DASH_LOCK (a Lock binds to the loop that first
# awaits it) and reset per test in conftest.
_HEALTHZ_LOCK: asyncio.Lock | None = None


def _probe_database(path: str) -> bool:
    """One READ of the schema table through a read-only URI. `mode=ro`
    refuses to create a file, so a volume that remounted empty answers
    "missing" instead of gaining a schemaless weather.db from the
    liveness check; reading sqlite_master touches the file, so a corrupt
    one fails here instead of passing a SELECT 1 that never opened it
    (round-two review, SEC-F2)."""
    import os as _os
    import sqlite3 as _sqlite3
    from urllib.parse import quote as _quote
    uri = "file:" + _quote(_os.path.abspath(path)) + "?mode=ro"
    conn = _sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()
    return True


def _healthz_cached(path: str, now: float) -> bool | None:
    hit = _HEALTHZ_CACHE
    if hit is not None and hit[0] == path and now < hit[1]:
        return hit[2]
    return None


async def _database_healthy() -> bool:
    global _HEALTHZ_CACHE, _HEALTHZ_LOCK
    path = settings.database_path
    cached = _healthz_cached(path, time.monotonic())
    if cached is not None:
        return cached
    if _HEALTHZ_LOCK is None:
        _HEALTHZ_LOCK = asyncio.Lock()
    async with _HEALTHZ_LOCK:
        # Whoever held the lock may have answered for everyone.
        now = time.monotonic()
        cached = _healthz_cached(path, now)
        if cached is not None:
            return cached
        try:
            ok = bool(await asyncio.wait_for(
                asyncio.to_thread(_probe_database, path), timeout=3.0))
        except Exception:
            ok = False
        _HEALTHZ_CACHE = (path, now + HEALTHZ_CACHE_S, ok)
        return ok


@app.get("/metrics")
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus exposition of every device's latest reading. Opt-in via
    PROMETHEUS_METRICS=1 (404 otherwise); open when enabled — same data class
    as the public dashboard. Point Prometheus/Grafana here for dashboards +
    alerting. See app/metrics.py."""
    if not settings.prometheus_metrics:
        raise HTTPException(status_code=404, detail="metrics not enabled")
    from . import metrics as _metrics
    devices = await db.list_devices()
    text = _metrics.render_prometheus(devices, int(time.time() * 1000))
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/version")
async def api_version() -> JSONResponse:
    """Running version + (if the daily check has run) the latest published
    release and whether an update is available. Open — version info is not a
    secret in an open-source project, and the app / monitoring read it to
    surface an update hint. See app/updates.py (opt-out with UPDATE_CHECK=0)."""
    info = getattr(app.state, "update_info", {"version": __version__,
                                              "latest": None,
                                              "update_available": False,
                                              "checked_ms": None, "enabled": False})
    # 1.9: volume used/free for the disk holding the database, so the apps
    # can put "· 2.1 GB free" next to the version and tint it when space
    # runs short. Same openness class as the version itself — how full a
    # disk is says nothing about the weather data on it. Computed per
    # request (one statvfs); null when the path can't be statted.
    from . import disk_watch
    return JSONResponse({**info, "disk": disk_watch.snapshot()})


# ── 2.1 server recommendations: more disk / memory, one tap ──────────
# Reads this machine and its volume through the Machines API the backend
# already holds a deploy token for (self_update.py) and says whether a
# bigger volume or more memory would help, with Fly's list price for the
# difference. `available: false` (with the reason) on a box without the
# token — a guest instance, local Docker — and the apps hide the card.
# See app/machine_advice.py.

@app.get("/api/server/advice", dependencies=[Depends(require_write_token)])
async def api_server_advice(refresh: bool = Query(False)) -> JSONResponse:
    """Write-tier like `apply` (2.1 review, SEC-6): the answer names the
    machine size, live RSS, OOM history and the volume id, which is the
    operator's infrastructure, not weather. A share-link guest or the App
    Store reviewer token gets 401 and the apps hide the card."""
    from . import machine_advice
    return JSONResponse(await machine_advice.advice(refresh=refresh))


class ServerAdviceApplyIn(BaseModel):
    kind: str = Field(pattern=r"^(volume|memory)$")
    target: int = Field(ge=1, le=100_000)


@app.post("/api/server/advice/apply",
          dependencies=[Depends(require_write_token)])
async def api_server_advice_apply(body: ServerAdviceApplyIn) -> JSONResponse:
    """Extend the volume (online) or set the machine's memory (Fly reboots
    the machine, so the app confirms first and then waits for it to come
    back). Validated against the machine as it is NOW, never the cached
    advice: a stale card cannot shrink anything."""
    from . import machine_advice
    try:
        out = await machine_advice.apply(body.kind, body.target)
    except machine_advice.ApplyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001 — Fly said no; say what it said
        log.warning("server advice apply failed: %s", e)
        raise HTTPException(status_code=502,
                            detail=f"Fly did not accept the change: {e}")
    return JSONResponse(out)


@app.get("/embed", response_class=HTMLResponse)
async def embed_page(
    theme: str | None = Query(None, pattern="^(light|dark|auto)$"),
) -> HTMLResponse:
    """The public dashboard ALONE — no status chrome — served with framing
    allowed, so an operator can put their weather inline on their own site
    with one iframe (the way WeatherLink's embeddablePage works; asked for
    by the first user who did exactly that with WeatherLink). Exists only
    when the operator opted into PUBLIC_DASHBOARD; 404s otherwise, so a
    non-public instance exposes nothing new. Same 100s-cached fragment the
    front page uses — an embedded page adds no extra load."""
    if not (await _pd_effective())["enabled"]:
        raise HTTPException(status_code=404, detail="public dashboard is off")
    from . import public_dashboard as _pd
    devices = await db.list_devices()
    now_ms = int(time.time() * 1000)
    dash = await _cached_public_dashboard(devices, now_ms)
    # ?theme=light|dark pins the palette (an embedding page's theme matters
    # more than the visitor's OS); auto/absent follows prefers-color-scheme.
    attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    page = f"""<!doctype html>
<html lang="en"{attr}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Zasder Weather</title>
  <style>
{_pd.THEME_CSS}
    body {{ font-family: system-ui, -apple-system, sans-serif;
            margin: 0; padding: 16px; line-height: 1.4; }}
    .wrap {{ max-width: 720px; margin: 0 auto; }}
{_pd.DASHBOARD_CSS}
  </style>
</head>
<body><div class="wrap">
{dash}
</div>
<script>{_EMBED_HEIGHT_SCRIPT}</script>
{_pd.SPINNER_HTML}
</body>
</html>"""
    return HTMLResponse(page)


@app.get("/", response_class=HTMLResponse)
@app.get("/status", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    """Public read-only status page. No secrets exposed — just enough to
    verify the deploy is alive and ingesting data. Anyone can hit this; we
    only show device names + counts + last-poll timestamp."""
    devices = await db.list_devices()
    now_ms = int(time.time() * 1000)
    rows = []
    total_obs = 0
    # (Counts served via _cached_observation_count below — the raw COUNT(*)
    # per device cost ~6s of cold index I/O on a 1.7GB DB, on EVERY
    # anonymous hit of `/`. Live-measured on Volney's box, 2026-08-26.)
    # Find the freshest non-null tempf across all devices for the sanity-check
    # tile. "Freshest" = highest dateutc_ms in the observations table, scoped
    # to rows that actually have a tempf value (a few SDR-coalesced posts can
    # land without it if the message-type cycle hasn't seen temp yet).
    latest_temp: dict | None = None
    for d in devices:
        n = await _cached_observation_count(d["mac"])
        total_obs += n
        last_seen_ms = d.get("lastSeen")
        last_seen_label = "—"
        last_seen_class = "stale"
        if last_seen_ms:
            age = (now_ms - last_seen_ms) / 1000
            last_seen_label = _humanize_age(age)
            last_seen_class = "fresh" if age < 600 else ("warm" if age < 3600 else "stale")
        # Latest observation may or may not include tempf — pick best.
        # Coerce through pd._num, never bare float(): stored values are not
        # guaranteed numeric (rows written before the ingest-boundary scrub
        # can hold strings), and a ValueError here 500s the ANONYMOUS `/`
        # page — same hardening the wind-rose loop already has.
        obs = await db.latest_observation(d["mac"])
        tval = _pd._num(obs.get("tempf")) if obs else None
        if tval is not None:
            obs_ms = obs.get("dateutc")
            if obs_ms and (latest_temp is None or obs_ms > latest_temp["ts_ms"]):
                latest_temp = {
                    "tempf": tval,
                    "ts_ms": obs_ms,
                    "device": d.get("name") or d["mac"],
                }
        # Public page: mask the MAC to its last 2 bytes and DON'T publish the
        # operator's free-text location label (it can name a home). Device
        # name + counts + freshness stay — enough to eyeball "the deploy is
        # alive and ingesting" without disclosing who/where.
        raw_mac = d["mac"]
        masked_mac = ("··:" * 4 + raw_mac[-5:]) if len(raw_mac) >= 5 else "··"
        rows.append({
            "name": d.get("name") or masked_mac,
            "mac": masked_mac,
            "count": n,
            "last_seen": last_seen_label,
            "last_seen_class": last_seen_class,
        })

    uptime = time.time() - getattr(app.state, "started_at", time.time())
    update_info = getattr(app.state, "update_info", None)

    # Optional public dashboard: current conditions + 24h charts for the
    # operator's station(s), rendered in place of the app screenshots.
    dashboard_html = ""
    if (await _pd_effective())["enabled"] and devices:
        dashboard_html = await _cached_public_dashboard(devices, now_ms)

    return HTMLResponse(_render_status_html(
        rows, total_obs, uptime, latest_temp, now_ms, update_info,
        dashboard_html=dashboard_html))


# The public dashboard is rendered for ANONYMOUS requests on `/`, and the page
# carries a 2-minute auto-refresh — so every visitor (and every refresh, and any
# crawler) drove a fresh 24h history aggregation per device. Cache the rendered
# HTML for slightly less than the refresh interval: the page can't show anything
# newer than its own refresh cadence anyway, so this costs no freshness and makes
# the only unauthenticated compute path flat under load.
# Row counts for the status page: cosmetic inventory stats that cost real
# I/O (COUNT(*) over a 300k-row mac walks cold index pages every request —
# each request opens a fresh SQLite connection, so the page cache never
# warms). Stale-while-refresh: past the TTL the OLD count is served
# instantly and one background task recounts; only a mac never counted
# blocks. Reset by the test fixture like its dashboard sibling.
_OBS_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_OBS_COUNT_TTL_S = 600
# One in-flight recount per station (R18 finding 7): the set this replaced
# only kept tasks alive, so every anonymous hit past the TTL spawned its
# own COUNT(*) — 25 simultaneous refreshes made 25 six-second scans of the
# same station. Keyed by mac; a station whose recount is still running
# gets the stale value and no new task. Reaped at shutdown with the rest.
_OBS_COUNT_TASKS: dict[str, asyncio.Task] = {}


async def _cached_observation_count(mac: str) -> int:
    now = time.time()
    hit = _OBS_COUNT_CACHE.get(mac)
    if hit is not None and now - hit[0] < _OBS_COUNT_TTL_S:
        return hit[1]
    if hit is not None:
        running = _OBS_COUNT_TASKS.get(mac)
        if running is None or running.done():
            async def _recount() -> None:
                try:
                    _OBS_COUNT_CACHE[mac] = (time.time(),
                                             await db.observation_count(mac))
                except Exception:
                    log.exception("count refresh failed for %s", mac)
            task = asyncio.create_task(_recount())
            _OBS_COUNT_TASKS[mac] = task
            task.add_done_callback(
                lambda t, m=mac: _OBS_COUNT_TASKS.pop(m, None)
                if _OBS_COUNT_TASKS.get(m) is t else None)
        return hit[1]
    n = await db.observation_count(mac)
    _OBS_COUNT_CACHE[mac] = (time.time(), n)
    return n


_PUBLIC_DASH_CACHE: tuple[float, str] | None = None
_PUBLIC_DASH_TTL_S = 100
# Serve-stale ceiling. A cold build takes ~7s on a large history (measured
# on Doren's 1.15M-row box, 2026-08-25 — his "embed takes 10sec" report),
# and with only the 100s TTL the first visitor after every quiet spell paid
# it. Between TTL and this ceiling the stale page is served instantly and a
# single background task rebuilds. The ceiling was 15 minutes on the
# theory that an older "live" page is a lie — but a sporadically-visited
# embed lands PAST 15 minutes on nearly every visit, so its owner ate the
# blocking build almost every time ("cards not loading", Doren, live
# 2026-08-28: broken at 9:49pm, loaded on the 9:50 retry — the build had
# just finished). The page prints "updated Xm ago" and auto-refreshes
# every 2 minutes, so a stale page is HONEST and self-healing; a blank
# frame with a spinner reads as broken. Ceiling now 24h: blocking builds
# happen only on the first visit after a restart.
_PUBLIC_DASH_STALE_MAX_S = 24 * 3600
_PUBLIC_DASH_REFRESHING = False
# Strong ref: asyncio only weakly holds tasks, and a GC'd refresh task
# would silently never fill the cache.
_PUBLIC_DASH_REFRESH_TASK: "asyncio.Task | None" = None
# Serializes cache MISSES, mirroring _RECORDS_LOCKS. The TTL alone only flattens
# load once the cache is warm: every anonymous request arriving during a cold
# build started its own full 24h aggregation per device, so a burst on `/` (the
# 2-minute auto-refresh syncing up, or a crawler) multiplied the one compute this
# cache exists to avoid — on the only unauthenticated compute path.
# Built lazily, and reset alongside the cache by the test fixture: an
# asyncio.Lock binds to the first loop that awaits it, so a module-level
# instance raises "bound to a different event loop" once a second loop uses it
# (the suite runs asyncio.run() per test). Same reason _RECORDS_LOCKS is cleared.
_PUBLIC_DASH_LOCK: asyncio.Lock | None = None


async def _invalidate_public_dashboard_cache() -> None:
    """Clear the dashboard cache WITHOUT racing an in-flight build: the
    builder assigns to the cache while holding _PUBLIC_DASH_LOCK, so a
    lock-held clear is ordered strictly after any build that already
    started — the stale html can never land after the clear."""
    global _PUBLIC_DASH_CACHE, _PUBLIC_DASH_LOCK
    if _PUBLIC_DASH_LOCK is None:
        _PUBLIC_DASH_CACHE = None
        return
    async with _PUBLIC_DASH_LOCK:
        _PUBLIC_DASH_CACHE = None


async def _pd_effective() -> dict:
    """Public-dashboard config, app-stored value winning field-by-field over
    env (the WU-key/integrations precedent). This is the groundwork for the
    1.7 apps' sharing screen: the flag the apps flip lives in server_kv, so
    an operator can turn the public page on/off and choose primary-station
    vs mirror-everything without touching env vars (Volney, 2026-08-21)."""
    raw_en = await db.get_kv("public_dashboard.enabled")
    enabled = (raw_en == "1") if raw_en in ("0", "1") else settings.public_dashboard
    macs = await db.get_kv("public_dashboard.macs")
    if macs is None:
        macs = settings.public_dashboard_macs
    loc = await db.get_kv("public_dashboard.location")
    if loc is None:
        loc = settings.public_dashboard_location
    return {"enabled": enabled, "macs": macs or None, "location": loc or None,
            "enabled_source": "app" if raw_en in ("0", "1") else "env"}


def _refresh_public_dashboard_soon(devices: list[dict], now_ms: int) -> None:
    """Fire one background rebuild of the dashboard cache. At most one runs
    at a time (the flag, not the lock, is the gate — stale requests must
    never queue behind the build they exist to avoid)."""
    global _PUBLIC_DASH_REFRESHING, _PUBLIC_DASH_REFRESH_TASK, _PUBLIC_DASH_LOCK
    if _PUBLIC_DASH_REFRESHING:
        return
    # Create the lock BEFORE scheduling (sync, no await): invalidation has a
    # no-lock fast path, and it must never coexist with a pending refresh or
    # its clear would be unordered against _run's build+write. In practice a
    # stale cache already implies a lock-holding build created the lock —
    # this line makes that invariant local instead of archaeological
    # (CodeRabbit, PR #29).
    if _PUBLIC_DASH_LOCK is None:
        _PUBLIC_DASH_LOCK = asyncio.Lock()
    _PUBLIC_DASH_REFRESHING = True

    async def _run() -> None:
        global _PUBLIC_DASH_CACHE, _PUBLIC_DASH_LOCK, _PUBLIC_DASH_REFRESHING
        try:
            if _PUBLIC_DASH_LOCK is None:
                _PUBLIC_DASH_LOCK = asyncio.Lock()
            async with _PUBLIC_DASH_LOCK:
                hit = _PUBLIC_DASH_CACHE
                if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_TTL_S:
                    return          # someone else already rebuilt
                html = await _build_public_dashboard(devices, now_ms)
                _PUBLIC_DASH_CACHE = (time.time(), html)
        except Exception:
            log.exception("background public-dashboard refresh failed")
        finally:
            _PUBLIC_DASH_REFRESHING = False

    _PUBLIC_DASH_REFRESH_TASK = asyncio.get_running_loop().create_task(_run())


async def _cached_public_dashboard(devices: list[dict], now_ms: int) -> str:
    """The cached section, with its "updated Xm ago" recomputed for this
    serve: the cache may be up to a day old (see the stale ceiling), and
    the phrase used to be frozen at build time."""
    from . import public_dashboard as _pd
    return _pd.restamp_ages(await _cached_public_dashboard_raw(devices, now_ms))


async def _cached_public_dashboard_raw(devices: list[dict], now_ms: int) -> str:
    global _PUBLIC_DASH_CACHE, _PUBLIC_DASH_LOCK
    hit = _PUBLIC_DASH_CACHE
    if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_TTL_S:
        return hit[1]
    # Stale-while-revalidate: between the TTL and the stale ceiling, the
    # visitor gets the old page NOW and the rebuild happens behind them.
    if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_STALE_MAX_S:
        _refresh_public_dashboard_soon(devices, now_ms)
        return hit[1]
    if _PUBLIC_DASH_LOCK is None:      # no await between test and assignment
        _PUBLIC_DASH_LOCK = asyncio.Lock()
    async with _PUBLIC_DASH_LOCK:
        # Re-check under the lock: a caller that queued behind an in-flight
        # build takes its result rather than running an identical second one.
        hit = _PUBLIC_DASH_CACHE
        if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_TTL_S:
            return hit[1]
        html = await _build_public_dashboard(devices, now_ms)
        _PUBLIC_DASH_CACHE = (time.time(), html)
        return html


async def _build_public_dashboard(devices: list[dict], now_ms: int) -> str:
    """Gather current + 24h history for the selected station(s) and render the
    dashboard section. Selection: PUBLIC_DASHBOARD_MACS ('all' | csv | unset →
    primary/first device)."""
    from . import public_dashboard as pd
    eff = await _pd_effective()
    if not devices:
        # A fresh deployment with the dashboard already on: the selection
        # below indexes devices[0], which 500'd /embed before any station
        # ever posted (CodeRabbit on the 1.6.1 retro-review, 2026-08-21).
        return ('<div class="station"><div class="chart-empty">'
                'No stations reporting yet — data appears here as soon as '
                'the first reading arrives.</div></div>')
    fields = pd.resolve_fields(settings.public_dashboard_fields)
    sel = (eff["macs"] or "").strip()
    by_mac = {d["mac"]: d for d in devices}
    # Air monitors render as their own card (2.0, pd.render_air_station):
    # they are selectable like any device, by explicit mac or "all". Only
    # the PRIMARY fallback still prefers a weather station, so a fleet
    # whose first device is a CO2 sensor opens with the weather.
    # (Before the air card existed the page filtered monitors out
    # unconditionally, 2026-08-26.)
    weather = [d for d in devices if not db.is_air_monitor_device(d)]
    if sel.lower() == "all":
        macs = [d["mac"] for d in devices]
    elif sel:
        # Match on the separator-stripped uppercase form so the operator can
        # write the MAC colonized or compact, lower or upper case. Walk the
        # CSV, not the devices list: the csv's order IS the page order — the
        # app's sharing screen writes it in the user's chosen ranking
        # (Volney, 2026-08-21). dict.fromkeys dedups while preserving order.
        def _compact(m: str) -> str:
            return m.upper().replace("-", "").replace(":", "")
        by_compact = {_compact(d["mac"]): d["mac"] for d in devices}
        want = dict.fromkeys(_compact(m) for m in sel.split(",") if m.strip())
        macs = ([by_compact[w] for w in want if w in by_compact]
                or [(weather or devices)[0]["mac"]])
    else:
        # primary = first WEATHER station (monitor-first fleets fall back).
        macs = [(weather or devices)[0]["mac"]]

    start_ms = now_ms - 24 * 3600 * 1000
    stations = []
    for mac in macs:
        d = by_mac.get(mac)
        if not d:
            continue
        obs = await db.latest_observation(mac)
        rows = await db.history(mac, start_ms, now_ms, limit=5000)
        if db.is_air_monitor_device(d):
            # The air card wants one series (PM2.5; db.history carries it
            # in both the raw and the bucketed SELECT) and none of the
            # weather machinery: no wind rose, no records scan kicked off
            # for a monitor, no rain periods, no summary board.
            pm_pts, co2_pts = [], []
            for r in rows:
                t = r.get("dateutc")
                if t is None:
                    continue
                v = pd._num(r.get("pm25"))
                if v is not None:
                    pm_pts.append((int(t), v))
                c = pd._num(r.get("co2"))
                if c is not None:
                    co2_pts.append((int(t), c))
            stations.append({"name": d.get("name") or mac, "obs": obs,
                             "series": {"pm25": pm_pts, "co2": co2_pts},
                             "air": True})
            continue
        # Always carry feelsLike too (overlaid on the temp chart), regardless
        # of the selected fields.
        series: dict[str, list] = {}
        for key in list(fields) + ["feelsLike"]:
            pts = []
            for r in rows:
                t = r.get("dateutc")
                # pd._num, not bare float(): raw-window rows come from
                # data_json and can carry non-numeric junk stored before the
                # ingest scrub — a ValueError here 500s the anonymous `/`
                # page (same guard as the wind-samples loop below).
                v = pd._num(r.get(key))
                if t is not None and v is not None:
                    pts.append((int(t), v))
            series[key] = pts
        # Paired (direction, speed) samples for the wind rose. Both values must
        # be finite: the cloud pollers write lastData straight through, and a
        # NaN direction reaching int() in the rose renderer 500s the public
        # status page for anonymous visitors.
        wind_samples = []
        for r in rows:
            wd, ws = pd._num(r.get("winddir")), pd._num(r.get("windspeedmph"))
            if wd is not None and ws is not None:
                wind_samples.append((wd, ws))
        # NON-blocking: never run the full-history records scan inside the
        # status-page request. Use the cache if warm; otherwise kick off a
        # background warm and render without the strip (it appears on a later
        # auto-refresh). Keeps the public page fast regardless of history size.
        recs = _records_cached_or_warm(mac)
        # The app main page's summary boards: 24h stats from the rows already
        # fetched, and the rain-periods row from the same enrichment /current
        # serves — so the public page replaces a screenshot of the app rather
        # than approximating one.
        if obs:
            await _fill_rain_periods(mac, obs)
        stations.append({"name": d.get("name") or mac, "obs": obs,
                         "series": series, "wind_samples": wind_samples,
                         "records": recs, "summary": pd.summary_stats(rows)})
    return pd.render_dashboard(stations, fields, tz_name=settings.timezone,
                               app_url=settings.public_dashboard_app_url,
                               location=eff["location"])


def _humanize_age(seconds: float) -> str:
    if seconds < 60:    return f"{int(seconds)}s ago"
    if seconds < 3600:  return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


_DEFAULT_HERO_HTML = """<div class="hero">
      <div class="hero-shots">
        <div class="hero-shot">
          <img src="/static/dashboard.png" alt="Zasder Weather iOS app — Dashboard tab showing current conditions, 24h temperature chart, and stat tiles" loading="lazy">
          <div class="cap">Dashboard</div>
        </div>
        <div class="hero-shot">
          <img src="/static/charts.png" alt="Zasder Weather iOS app — Charts tab showing temperature time series with selectable field and time-range pickers" loading="lazy">
          <div class="cap">Charts</div>
        </div>
      </div>
      <div class="hero-copy">
        <p>A clean, dark, fast iOS app for personal weather stations. Bring your own backend (this one) and your station data is yours, end to end. No ads, no tracking, no subscriptions.</p>
        <p>Supports AmbientWeather and AcuRite Atlas out of the box. Multi-device dashboard, history charts across six fields, threshold-based local alerts, and a 7-day Open-Meteo forecast.</p>
      </div>
    </div>"""


def _render_status_html(rows: list[dict], total_obs: int, uptime_s: float,
                        latest_temp: dict | None = None,
                        now_ms: int | None = None,
                        update_info: dict | None = None,
                        dashboard_html: str = "") -> str:
    started = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    # Public dashboard on ⇒ swap the app screenshots for the live charts + an
    # App Store link, add its CSS, and auto-refresh the page.
    from . import public_dashboard as _pd
    _spinner = _pd.SPINNER_HTML
    theme_css = _pd.THEME_CSS
    if dashboard_html:
        dashboard_css = _pd.DASHBOARD_CSS
        refresh_meta = '<meta http-equiv="refresh" content="120">'
        # No banner: the App Store link rides beside the first station's
        # temperature now (Volney: the full-width row "takes up too much
        # room and looks strange"). render_dashboard placed it.
        hero_html = dashboard_html
    else:
        dashboard_css = ""
        refresh_meta = ""
        hero_html = _DEFAULT_HERO_HTML
    # Version line + "update available" banner (from the daily GitHub check).
    ui = update_info or {}
    _repo_url = "https://github.com/volneydouglas/zasder-weather-backend"
    # NO origin line here. This page is PUBLIC, and `describe_origin` answers
    # with operator configuration advice ("Fly hostname (set PUBLIC_BASE_URL
    # for a custom domain)") plus, behind a custom domain with PUBLIC_BASE_URL
    # unset, the fly.dev hostname a visitor had not otherwise been given —
    # the one thing the map's link modes exist to keep off a public surface.
    # The boot log says all of it, once, where the operator looks (SEC-G2).
    version_html = f'<span class="ver">v{__version__}</span>'
    update_banner = ""
    if ui.get("update_available") and ui.get("latest"):
        update_banner = (
            f'<div class="update-banner">⬆ Update available: '
            f'<strong>v{_html.escape(str(ui["latest"]))}</strong> '
            f'(you have v{__version__}) — '
            f'<a href="{_repo_url}/releases" target="_blank" rel="noopener">'
            f'what\'s new →</a></div>'
        )
    # Escape every operator/source-supplied value before interpolating.
    # device.name and device.location flow in through /ingest/custom from
    # whoever is running the relay; the page is public so we can't trust them.
    # last_seen_class is internally-controlled (whitelisted strings) so it
    # doesn't need escaping.
    def esc(s: object) -> str: return _html.escape(str(s), quote=True)
    rows_html = "\n".join(
        f'<tr><td>{esc(r["name"])}</td>'
        f'<td class="mono">{esc(r["mac"])}</td><td class="num">{r["count"]:,}</td>'
        f'<td class="age {r["last_seen_class"]}">{esc(r["last_seen"])}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="4" class="muted">No devices yet — waiting for first poll.</td></tr>'
    days = int(uptime_s // 86400)
    hours = int((uptime_s % 86400) // 3600)
    mins = int((uptime_s % 3600) // 60)
    uptime_label = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
    # Latest-temp tile contents. Renders "—" if no device has reported a
    # tempf yet (fresh deploy, AcuRite-only with hub silent, etc.).
    if latest_temp and now_ms:
        temp_val_html = f'{latest_temp["tempf"]:.1f}°F'
        age_s = max(0, (now_ms - latest_temp["ts_ms"]) / 1000)
        temp_sub_html = (f'<div class="stat-sub">{esc(latest_temp["device"])} · '
                        f'{esc(_humanize_age(age_s))}</div>')
    else:
        temp_val_html = "—"
        temp_sub_html = '<div class="stat-sub muted">no readings yet</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Zasder Weather — Status</title>
  <style>
{theme_css}
    body {{ font-family: system-ui, -apple-system, sans-serif;
            margin: 0; padding: 32px 16px; line-height: 1.4; }}
    .wrap {{ max-width: 720px; margin: 0 auto; }}
    h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.2px; }}
    .sub {{ font-size: 12px; color: var(--ink-55); margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 24px; }}
    .ver {{ font-size: 12px; font-weight: 600; color: var(--ink-40);
            vertical-align: middle; margin-left: 6px; }}
    .update-banner {{ margin: 14px 0 0; padding: 10px 14px; border-radius: 8px;
            background: rgba(212,168,83,0.14); border: 1px solid rgba(212,168,83,0.4);
            color: #e6c56a; font-size: 13px; }}
    .update-banner a {{ color: #e6c56a; font-weight: 700; }}
    .stat {{ background: var(--card-bg); border: 1px solid var(--card-edge);
              border-radius: 10px; padding: 12px; }}
    .stat .k {{ font-size: 9px; font-weight: 800; letter-spacing: 1.2px;
                 color: var(--ink-55); text-transform: uppercase; }}
    .stat .v {{ font-size: 22px; font-weight: 300; margin-top: 4px;
                 font-variant-numeric: tabular-nums; }}
    .stat-sub {{ font-size: 9px; color: var(--ink-40); margin-top: 4px;
                  letter-spacing: 0.3px; }}
    @media (max-width: 540px) {{
      .grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    table {{ width: 100%; border-collapse: collapse; background: var(--card-bg);
              border: 1px solid var(--card-edge); border-radius: 10px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 10px 12px; font-size: 12px;
               border-bottom: 1px solid var(--card-edge); }}
    th {{ font-size: 9px; font-weight: 800; letter-spacing: 1px; color: var(--ink-55);
           text-transform: uppercase; background: var(--card-bg); }}
    tr:last-child td {{ border-bottom: none; }}
    .num, .age {{ font-variant-numeric: tabular-nums; }}
    .muted {{ color: var(--ink-50); }}
    .mono {{ font-family: ui-monospace, SF Mono, monospace; font-size: 10px; color: var(--ink-55); }}
    .fresh {{ color: oklch(78% 0.14 145); }}
    .warm  {{ color: oklch(78% 0.14 70); }}
    .stale {{ color: oklch(70% 0.20 28); }}
    .hero {{ margin-bottom: 24px; }}
    .hero-shots {{ display: flex; gap: 16px; justify-content: center; margin-bottom: 16px; }}
    .hero-shot {{ flex: 0 0 220px; }}
    .hero-shot img {{ width: 100%; height: auto; display: block;
                       border-radius: 28px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
    .hero-shot .cap {{ font-size: 10px; color: var(--ink-40); margin-top: 8px;
                        text-align: center; letter-spacing: 0.3px; }}
    .hero-copy p {{ font-size: 13px; color: var(--ink-70); margin: 0 0 10px;
                     max-width: 560px; margin-left: auto; margin-right: auto; text-align: center; }}
    @media (max-width: 540px) {{
      .hero-shots {{ flex-wrap: wrap; }}
      .hero-shot {{ flex: 0 0 calc(50% - 8px); max-width: calc(50% - 8px); }}
    }}
    footer {{ margin-top: 24px; font-size: 10px; color: var(--ink-35); }}
    a {{ color: oklch(70% 0.14 245); text-decoration: none; }}
    {dashboard_css}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Zasder Weather {version_html}</h1>
    {update_banner}
    {hero_html}
    <div class="grid">
      <div class="stat"><div class="k">Status</div><div class="v">Up</div></div>
      <div class="stat"><div class="k">Devices</div><div class="v">{len(rows)}</div></div>
      <div class="stat"><div class="k">Observations</div><div class="v">{total_obs:,}</div></div>
      <div class="stat"><div class="k">Latest temp</div><div class="v">{temp_val_html}</div>{temp_sub_html}</div>
    </div>
    <table>
      <thead><tr><th>Device</th><th>MAC</th><th>Rows</th><th>Last seen</th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <footer>
      Read-only status — no auth required; the iOS app reads protected
      endpoints under <code>/api</code>.
      · Uptime {uptime_label} · Generated {started}
      · <a href="https://github.com/volneydouglas/zasder-weather-backend">source</a>
    </footer>
  </div>
  {_spinner}
</body>
</html>"""


@app.get("/api/config/backup", dependencies=[Depends(require_write_token)])
async def api_config_backup() -> dict[str, Any]:
    """Everything the operator configured by hand, so it survives losing the
    server. No tokens and no SMTP password — see app/config_backup.py.

    Write-gated despite being a GET: the export carries alert recipient email
    addresses, SMTP host/username/from and the operator's device coordinates —
    exactly what GET /api/alerts hides from the read-only reviewer token. A
    read-gated backup made that redaction a one-request bypass."""
    return await config_backup.export_config()


# ── Database backup (1.7) ────────────────────────────────────────────────
# Job pattern, learned the hard way on day one: the inline version VACUUMed
# BEFORE sending any bytes, so a real-sized history sat silent past the
# client's 60s timeout (and Fly's idle timeout) — Volney's own instance
# timed out on the first try. Now: POST starts the snapshot in the
# background, GET /status polls it, GET the file streams bytes immediately
# because the file already exists. Process-global job state, one at a time
# (reset per test in conftest, like main's other module caches).
_DB_BACKUP_JOB: dict[str, Any] = {"state": "idle"}
_DB_BACKUP_TASK: asyncio.Task | None = None
_DB_BACKUP_FRESH_MS = 10 * 60_000     # a ready snapshot is reusable this long

# /api/storage (2.0, 2026-09-01): the measurement is a background job with
# a cached result, same one-at-a-time process-global shape as the backup
# job above and reset per test the same way. See api_storage for why a
# request-time scan cannot be bounded on a 2 GB database.
_STORAGE_JOB: dict[str, Any] = {"state": "idle"}
_STORAGE_TASK: asyncio.Task | None = None
_STORAGE_FRESH_MS = 6 * 3_600_000     # a cached report is served this long


def _db_backup_job_expired(job: dict[str, Any], now_ms: int) -> bool:
    """A ready snapshot nobody downloaded inside the fresh window. The
    status endpoint reports it as 'expired' and the sweep may delete it."""
    return (job.get("state") == "ready"
            and now_ms - int(job.get("finished_ms") or 0) >= _DB_BACKUP_FRESH_MS)


def _db_backup_keep_path(now_ms: int) -> Path | None:
    """The one snapshot file the sweep must leave alone: a running job's
    (while its task is alive — a VACUUM of a multi-GB database outlives the
    fresh window) or a ready-and-fresh one. Expired, errored, and dead-task
    jobs keep nothing."""
    job = _DB_BACKUP_JOB
    path = job.get("path")
    if not path:
        return None
    if job.get("state") == "running":
        task = _DB_BACKUP_TASK
        return Path(path) if task is not None and not task.done() else None
    if job.get("state") == "ready" and not _db_backup_job_expired(job, now_ms):
        return Path(path)
    return None


def _sweep_orphan_snapshots(*, at_boot: bool = False) -> dict[str, Any]:
    """Delete leftover `.dbbackup-*` files (sync — call via to_thread).
    At boot nothing can be in flight in this process, so any age goes.
    Otherwise only files past the fresh window: the inline GET path
    VACUUMs into an UNTRACKED file, and its mtime stays fresh while the
    write runs."""
    from . import maintenance
    now_ms = int(time.time() * 1000)
    return maintenance.sweep_stale_snapshots(
        now_ms, None if at_boot else _db_backup_keep_path(now_ms),
        max_age_ms=None if at_boot else _DB_BACKUP_FRESH_MS)


def _db_backup_dest() -> Path:
    """Pick a directory with room for a full copy of the database.

    Data volumes are often sized just above the database itself (Fly volumes
    especially), so VACUUM INTO next to the DB dies with SQLite's 'database
    or disk is full' once history grows past half the volume — after minutes
    of work, with no hint WHOSE disk (2026-08-22, Volney's own instance).
    The root filesystem is a separate disk with its own free space; use it
    when the volume can't hold a second copy. When nothing can, refuse
    up front with a sized, human message instead of running the long VACUUM
    into a wall."""
    src = Path(settings.database_path)
    need = src.stat().st_size
    for side in ("-wal", "-shm"):
        p = Path(str(src) + side)
        # No exists() pre-check (R6): SQLite deletes the sidecars when the
        # last connection closes, and this app opens/closes aiosqlite
        # connections constantly — exists() then stat() raced exactly that
        # window and 500'd the backup endpoints intermittently (also the
        # suite's ~1-in-3 backup-test flake). A vanished sidecar simply
        # doesn't need space.
        try:
            need += p.stat().st_size
        except OSError:
            pass
    need = int(need * 1.02) + 32 * 1024 * 1024   # slack for mid-VACUUM growth
    from . import disk_watch
    candidates = (src.parent, Path(tempfile.gettempdir()))
    usage: dict[Path, Any] = {}
    for d in candidates:
        try:
            usage[d] = shutil.disk_usage(d)
        except OSError:
            continue
    # Two passes (2.4, D2). First one that can take the copy AND stay
    # under the disk watchdog's warn line: a backup is not allowed to be
    # the reason the operator's phone says the server is running out of
    # room. Only if nowhere is comfortable does bare room decide — a
    # snapshot that fits is worth a minute of a loud gauge, and
    # disk_watch skips its ticks while the job is registered anyway.
    for comfortable in (True, False):
        for d in candidates:
            u = usage.get(d)
            if u is None or u.free < need:
                continue
            writable = u.used + u.free
            projected = ((u.used + need) / writable * 100) if writable else 100.0
            if comfortable and projected >= disk_watch.WARN_PCT:
                continue
            return d / f".dbbackup-{secrets.token_hex(8)}.db"
    detail = ", ".join(f"{d} has {u.free // 2**20} MB free"
                       for d, u in usage.items())
    raise HTTPException(
        status_code=507,
        detail=(f"Not enough free disk on the server for a database "
                f"snapshot — it needs about {need // 2**20} MB ({detail})."))


async def _run_db_backup(job: dict[str, Any], dest: Path) -> None:
    """VACUUM INTO on a FRESH connection (the pooled one always has
    statements in progress and VACUUM refuses; a raw file copy of a live
    WAL database can capture a torn state). Consistent, compacted, and on
    aiosqlite's worker thread so the event loop never blocks."""
    import aiosqlite
    from . import copy_jobs
    try:
        await db.wait_open()
        # Registered for the length of the copy so the disk watchdog does
        # not report our own second copy of the database as the volume
        # filling up (D2, Doren 2026-09-20).
        with copy_jobs.running("backup", dest):
            conn = await db.open_connection(settings.database_path)
            try:
                await conn.execute("VACUUM INTO ?", (str(dest),))
            finally:
                await conn.close()
        job.update(state="ready", size=dest.stat().st_size,
                   finished_ms=int(time.time() * 1000))
    except Exception as e:
        # The job dict carries the failure — this task's exception must
        # never vanish into a fire-and-forget void.
        dest.unlink(missing_ok=True)
        job.update(state="error", error=str(e))


@app.post("/api/backup/database", dependencies=[Depends(require_write_token)])
async def api_backup_database_start() -> dict[str, Any]:
    """Start (or reuse) a snapshot job. Idempotent: a running job answers
    'running', a fresh ready snapshot answers 'ready' — double-taps and
    HTTP retries never stack VACUUMs."""
    global _DB_BACKUP_JOB, _DB_BACKUP_TASK
    job = _DB_BACKUP_JOB
    now_ms = int(time.time() * 1000)
    # Trust "running" only while the task is actually alive (CodeRabbit,
    # PR #27): _run_db_backup catches Exception but a CANCELLED task (app
    # shutdown mid-backup) left state="running" forever, and this early
    # return then refused every later backup until a process restart.
    if job.get("state") == "running":
        task = _DB_BACKUP_TASK
        if task is not None and not task.done():
            return {"state": "running"}
        job["state"] = "error"
        job.setdefault("error", "previous backup was interrupted")
    if (job.get("state") == "ready"
            and Path(job.get("path", "")).exists()
            and now_ms - int(job.get("finished_ms") or 0) < _DB_BACKUP_FRESH_MS):
        return {"state": "ready", "size": job.get("size")}
    # Sweep a stale/error leftover before starting fresh — this job's own
    # file, then every orphan past the fresh window in both candidate
    # directories, BEFORE the free-space check counts what is left.
    if job.get("path"):
        Path(job["path"]).unlink(missing_ok=True)
    _DB_BACKUP_JOB = {"state": "idle"}
    # Inline, not to_thread: an await here would open a window between
    # the idempotency checks above and the claim below for a second POST
    # to stack a VACUUM. Two directory globs and a few unlinks.
    _sweep_orphan_snapshots()
    dest = _db_backup_dest()
    _DB_BACKUP_JOB = {"state": "running", "path": str(dest), "started_ms": now_ms}
    _DB_BACKUP_TASK = asyncio.create_task(_run_db_backup(_DB_BACKUP_JOB, dest))
    return {"state": "running"}


@app.get("/api/backup/database/status", dependencies=[Depends(require_write_token)])
async def api_backup_database_status() -> dict[str, Any]:
    job = _DB_BACKUP_JOB
    out: dict[str, Any] = {"state": job.get("state", "idle")}
    if _db_backup_job_expired(job, int(time.time() * 1000)):
        # Past the fresh window: the next POST starts over and the sweep
        # may already have removed the file. Never advertised as ready.
        out["state"] = "expired"
    elif job.get("state") == "ready":
        out["size"] = job.get("size")
    if job.get("state") == "error":
        out["error"] = job.get("error")
    return out


@app.get("/api/backup/database", dependencies=[Depends(require_write_token)])
async def api_backup_database() -> FileResponse:
    """Download the snapshot. With a ready job: streams it (bytes flow
    immediately — no silent VACUUM window for timeouts to kill) and the
    file+job are cleared after the send. Without one: VACUUMs inline, which
    stays fine for small databases and curl one-liners.

    Write-gated despite being a GET, harder than config/backup even: the
    file is everything — alert prefs including the app-managed SMTP
    password, guest tokens, the works."""
    global _DB_BACKUP_JOB
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    job = _DB_BACKUP_JOB
    if _db_backup_job_expired(job, int(time.time() * 1000)):
        # The status route already calls this snapshot expired and the
        # next POST refuses to reuse it; a direct GET used to hand it out
        # anyway (R18 finding 8). Gone, and say so.
        Path(job.get("path", "")).unlink(missing_ok=True)
        _DB_BACKUP_JOB = {"state": "idle"}
        raise HTTPException(status_code=410,
                            detail="that snapshot expired; start a new one "
                                   "with POST /api/backup/database")
    if job.get("state") == "ready" and Path(job.get("path", "")).exists():
        dest = Path(job["path"])
        _DB_BACKUP_JOB = {"state": "idle"}

        def _cleanup(p: Path = dest) -> None:
            p.unlink(missing_ok=True)

        return FileResponse(
            dest, media_type="application/vnd.sqlite3",
            filename=f"zasder-weather-{stamp}.db",
            background=BackgroundTask(_cleanup))
    import aiosqlite
    from . import copy_jobs
    dest = _db_backup_dest()
    await db.wait_open()
    with copy_jobs.running("backup", dest):
        conn = await db.open_connection(settings.database_path)
        try:
            await conn.execute("VACUUM INTO ?", (str(dest),))
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        finally:
            await conn.close()
    return FileResponse(
        dest, media_type="application/vnd.sqlite3",
        filename=f"zasder-weather-{stamp}.db",
        background=BackgroundTask(lambda: dest.unlink(missing_ok=True)))


# ── Database restore (2.1) — see app/restore.py for the gate and the swap.

def _restore_other_busy() -> str | None:
    """What main.py knows and restore.py cannot see: the backup job."""
    job = _DB_BACKUP_JOB
    if job.get("state") == "running":
        task = _DB_BACKUP_TASK
        if task is not None and not task.done():
            return "a database backup is being prepared; try again when it is done"
    return None


@app.post("/api/backup/database/restore/challenge",
          dependencies=[Depends(require_write_token)])
async def api_restore_challenge() -> dict[str, Any]:
    """Step one of the two-step: a one-shot nonce plus what is about to be
    replaced, so the app can put a number in its confirmation."""
    out = _restore.issue_challenge()
    try:
        out["current_rows"] = await _restore.current_rows()
        out["rows_known"] = True
    except Exception:          # a missing table on a fresh box is not a refusal
        # Unknown is not zero (R21-07): the app must not say "empty".
        out["current_rows"] = None
        out["rows_known"] = False
    return out


@app.post("/api/backup/database/restore",
          dependencies=[Depends(require_write_token)])
async def api_restore_database(
        request: Request,
        x_restore_challenge: Annotated[str | None, Header()] = None,
        x_restore_sha256: Annotated[str | None, Header()] = None,
        content_length: Annotated[int | None, Header()] = None,
) -> dict[str, Any]:
    """Step two: the snapshot itself as the raw request body. Answers as
    soon as the bytes are on disk and their digest checks out; validation
    and the swap run behind `.../restore/status`."""
    return await _restore.start_restore(
        request.stream(), challenge=x_restore_challenge,
        sha256_hex=x_restore_sha256, content_length=content_length,
        other_busy=_restore_other_busy)


@app.get("/api/backup/database/restore/status",
         dependencies=[Depends(require_write_token)])
async def api_restore_status() -> dict[str, Any]:
    return _restore.status()


@app.post("/api/config/restore", dependencies=[Depends(require_write_token)])
async def api_config_restore(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Apply a backup. Write-gated: this replaces alert rules for everyone
    using this backend, so the read-only reviewer token must not reach it."""
    try:
        summary = await config_backup.import_config(payload)
    except config_backup.RestoreError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "restored": summary,
            "note": ("SMTP password is never included in a backup — re-enter "
                     "it in Alerts if you use email alerts.")}


# ── App-minted read-only share tokens ("Share read-only access") ─────────
# The env-secret GUEST_API_TOKENS path required the fly CLI, which family
# sharing can't ask of anyone. These are the same read-only contract, minted
# from the app: valid on GETs (require_token unions db.guest_token_cache),
# never in write_tokens. ALL THREE routes are write-gated — a guest must not
# be able to mint further guests, enumerate other people's tokens, or revoke
# the operator's shares.

class GuestTokenBody(BaseModel):
    label: str | None = Field(default=None, max_length=64)
    # 1.9: mint the WRITE tier instead. Never changeable after mint — an
    # upgrade path would turn every leaked read link into a latent write
    # link; the owner mints a new zww_ link deliberately instead.
    write: bool = False


@app.post("/api/guest-tokens", dependencies=[Depends(require_write_token)])
async def api_create_guest_token(body: GuestTokenBody | None = None) -> dict[str, Any]:
    """Mint a read-only share token. The list never carries token values —
    list/revoke/rename work with the short id. The full value surfaces in
    exactly two write-gated places: this response, and the deliberate
    re-share reveal below (added 1.7 so one person's several devices can
    reuse one token)."""
    label = (body.label or "").strip() if body and body.label else None
    can_write = bool(body and body.write)
    if can_write and not label:
        # The tier's contract is per-person attribution: an unnamed write
        # link makes the audit trail say "somebody". Refuse up front.
        raise HTTPException(status_code=400,
                            detail="write links need a name — the change "
                                   "log records who did what")
    token = ("zww_" if can_write else "zwg_") + secrets.token_hex(16)
    now_ms = int(time.time() * 1000)
    await db.add_guest_token(token, label or None, now_ms,
                             can_write=can_write)
    return {"token": token, "id": token[:db.GUEST_TOKEN_ID_LEN],
            "label": label or None, "created_ms": now_ms,
            "can_write": can_write}


@app.get("/api/guest-tokens", dependencies=[Depends(require_write_token)])
async def api_list_guest_tokens() -> dict[str, Any]:
    # Flush the in-memory auth stamps first, so "last used" is current the
    # moment the owner looks. last_used_ms is best-effort by design: stamps
    # between the last flush and a process restart are lost, which can only
    # UNDER-report recency — fine for "is anyone still using this share".
    await db.flush_guest_last_used()
    rows = await db.list_guest_tokens()
    # Deliberately app-minted tokens ONLY: the env-configured reviewer/guest
    # tokens stay invisible here (Volney's call, 2026-08-23 — "okay if it
    # doesn't show in the list"). If a row you can't place appears, it is
    # NOT the reviewer; it's an app-minted share.
    return {"tokens": [{"id": r["token"][:db.GUEST_TOKEN_ID_LEN],
                        "label": r["label"], "created_ms": r["created_ms"],
                        "last_used_ms": r["last_used_ms"],
                        "can_write": bool(r.get("can_write"))}
                       for r in rows]}


@app.get("/api/write-audit", dependencies=[Depends(require_write_token)])
async def api_write_audit(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """The write-share change log (1.9), newest first: who (label + token
    tail), did what (method + path), when. Owner-only — the audit exists
    FOR the owner, and its rows name the other holders."""
    return {"entries": await db.list_write_audit(limit)}


@app.patch("/api/guest-tokens/{token_id}", dependencies=[Depends(require_write_token)])
async def api_rename_guest_token(token_id: str,
                                 body: GuestTokenBody) -> dict[str, Any]:
    """Relabel a share (labels used to be set-at-mint only). A null/empty
    label clears it back to unnamed."""
    label = (body.label or "").strip() or None
    n = await db.rename_guest_token(token_id, label)
    if n == 0:
        raise HTTPException(status_code=404, detail="no such share token")
    return {"ok": True, "id": token_id, "label": label}


@app.get("/api/guest-tokens/{token_id}/token",
         dependencies=[Depends(require_write_token)])
async def api_reveal_guest_token(token_id: str) -> dict[str, Any]:
    """Full token value by id — the re-share path (one person, several
    devices, ONE token to manage). This deliberately amends the original
    mint-only surfacing contract: the list still never carries token
    values, and this route is write-gated, so revealing adds no capability
    an api_token holder doesn't already have by minting. Write-gated
    despite being a GET, same reasoning as the config export."""
    row = await db.get_guest_token(token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such share token")
    return {"token": row["token"], "id": token_id, "label": row["label"],
            "created_ms": row["created_ms"]}


@app.delete("/api/guest-tokens/{token_id}", dependencies=[Depends(require_write_token)])
async def api_revoke_guest_token(token_id: str) -> dict[str, Any]:
    n = await db.delete_guest_token(token_id)
    if n == 0:
        raise HTTPException(status_code=404, detail="no such share token")
    return {"ok": True, "revoked": n}


# ── App-minted per-device ingest tokens ──────────────────────────────────
# The write-side siblings of the share tokens above, with the same surface
# and the same reasons: the single INGEST_TOKEN env secret is shared by
# every board and poller, so rotating it unpairs ALL of them at once
# (security review 2026-08-19, finding 4 — recovery needs physical access
# to each board's setup key). Mint one per device, revoke one at a time.
# Valid ONLY where the env INGEST_TOKEN is valid (/ingest/custom and
# discovery) — never a read or write API credential, which the tests pin.

class IngestTokenBody(BaseModel):
    label: str | None = Field(default=None, max_length=64)


@app.post("/api/ingest-tokens", dependencies=[Depends(require_write_token)])
async def api_create_ingest_token(body: IngestTokenBody | None = None) -> dict[str, Any]:
    """Mint a per-device ingest token. Same surfacing contract as the share
    tokens: the list never carries values; the full value appears here and
    in the deliberate re-provisioning reveal below."""
    token = "zwi_" + secrets.token_hex(16)
    label = (body.label or "").strip() if body and body.label else None
    now_ms = int(time.time() * 1000)
    await db.add_ingest_token(token, label or None, now_ms)
    return {"token": token, "id": token[:db.INGEST_TOKEN_ID_LEN],
            "label": label or None, "created_ms": now_ms}


@app.get("/api/ingest-tokens", dependencies=[Depends(require_write_token)])
async def api_list_ingest_tokens() -> dict[str, Any]:
    # Flush first so "last used" is current the moment the owner looks —
    # for a board posting every few seconds this is the "is it alive and
    # using ITS token?" answer.
    await db.flush_ingest_last_used()
    rows = await db.list_ingest_tokens()
    return {"tokens": [{"id": r["token"][:db.INGEST_TOKEN_ID_LEN],
                        "label": r["label"], "created_ms": r["created_ms"],
                        "last_used_ms": r["last_used_ms"]}
                       for r in rows]}


@app.patch("/api/ingest-tokens/{token_id}", dependencies=[Depends(require_write_token)])
async def api_rename_ingest_token(token_id: str,
                                  body: IngestTokenBody) -> dict[str, Any]:
    label = (body.label or "").strip() or None
    n = await db.rename_ingest_token(token_id, label)
    if n == 0:
        raise HTTPException(status_code=404, detail="no such ingest token")
    return {"ok": True, "id": token_id, "label": label}


@app.get("/api/ingest-tokens/{token_id}/token",
         dependencies=[Depends(require_write_token)])
async def api_reveal_ingest_token(token_id: str) -> dict[str, Any]:
    """Full token value by id — the re-provisioning path: a board that
    wiped its credential needs the same value pasted back, not a fresh mint
    that orphans the old row. Write-gated despite being a GET, same
    reasoning as the share-token reveal."""
    row = await db.get_ingest_token(token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such ingest token")
    return {"token": row["token"], "id": token_id, "label": row["label"],
            "created_ms": row["created_ms"]}


@app.delete("/api/ingest-tokens/{token_id}", dependencies=[Depends(require_write_token)])
async def api_revoke_ingest_token(token_id: str) -> dict[str, Any]:
    n = await db.delete_ingest_token(token_id)
    if n == 0:
        raise HTTPException(status_code=404, detail="no such ingest token")
    return {"ok": True, "revoked": n}


# ── Operator-triggered backend upgrade (Settings → "Update now") ─────────
# The push-button sibling of AUTO_UPDATE for operators who keep it off: the
# app checks /api/version (open, already carries update_available) and this
# write-gated endpoint applies the release ON DEMAND through the same
# machinery — image verified before the machine config is touched, same
# major only, never a downgrade. Operator intent replaces the maturity
# delay. The machine restarts on success, so the caller should treat a
# dropped connection as "probably applied" and re-poll /api/version.

@app.post("/api/update/apply", dependencies=[Depends(require_write_token)])
async def api_update_apply() -> dict[str, Any]:
    from . import self_update
    from .updates import is_newer, parse_version
    # One update at a time: a double-tap or HTTP retry otherwise POSTs the
    # machine update twice — a second restart for nothing (CODE_REVIEW_R5
    # R5-21). Non-blocking on purpose: the second caller learns instantly.
    lock: asyncio.Lock | None = getattr(app.state, "update_apply_lock", None)
    if lock is None:      # lazily built — binds to the running loop
        lock = app.state.update_apply_lock = asyncio.Lock()
    if lock.locked():
        raise HTTPException(status_code=409,
                            detail="an update is already being applied")
    async with lock:
        return await _update_apply_locked(self_update, is_newer, parse_version)


async def _update_apply_locked(self_update, is_newer, parse_version) -> dict[str, Any]:
    info = getattr(app.state, "update_info", None) or {}
    latest = info.get("latest")
    if not latest or not is_newer(latest, __version__):
        raise HTTPException(status_code=409,
                            detail=f"already up to date (v{__version__})")
    # Token check BEFORE the vouch fetch (R12): the manifest lookup is a
    # network round-trip (up to 10s), and a token-less instance — the
    # default for one-tap users mid-repair — was paying it just to receive
    # the 409 this cheap local check produces instantly.
    # Off Fly first (2.4.2, mirror issue #5): a Docker or bare install has
    # no machine to rewrite, so the token recipe below cannot help it.
    if not self_update._on_fly():
        raise HTTPException(status_code=409,
                            detail=self_update.OFF_FLY_DETAIL)
    if not self_update._fly_token():
        raise HTTPException(
            status_code=409,
            detail="no deploy token on this instance — create one with "
                   "`fly tokens create deploy` and set it as the "
                   "FLY_API_TOKEN secret, then try again")
    if parse_version(latest)[0] != parse_version(__version__)[0]:
        # 1.9: a major may still one-tap when the TARGET release vouches
        # (upgrade.json's seamless_from covers this server's version) —
        # otherwise the fleet-stranding 409 the gate has always been.
        # Fails closed on any fetch/parse trouble.
        if not await self_update.upgrade_manifest_allows(latest, __version__):
            raise HTTPException(
                status_code=409,
                detail=f"v{latest} is a major upgrade and may carry manual "
                       "steps — follow the release notes to upgrade")
    repo = self_update._image_repo()
    if not await self_update.image_exists(repo, latest):
        raise HTTPException(
            status_code=409,
            detail=f"release v{latest} has no published image yet — "
                   "try again in a few minutes")
    ok = await self_update.apply_update(latest)
    if not ok:
        raise HTTPException(status_code=502,
                            detail="the platform rejected the update — "
                                   "see the server logs")
    return {"ok": True, "applying": latest,
            "note": "the server restarts into the new release; "
                    "re-check /api/version shortly"}


class RetentionIn(BaseModel):
    """Merge-per-field like the sharing PUT: omitted = unchanged, -1 =
    forget the app override (env value takes over), 0 = off. Floors keep
    fat-finger values from eating history: row thinning can't be set
    tighter than 90 days, JSON trimming than 7."""
    detail_days: int | None = Field(default=None, ge=-1, le=3650)
    json_days: int | None = Field(default=None, ge=-1, le=3650)
    # 2.0 quiet-hour window. Start is "HH:MM" station-local ("" forgets
    # the app override); minutes is the nightly budget (0 = paused,
    # -1 = forget); batch rows floor at 200 (-1 = forget).
    thin_window_start: str | None = Field(default=None, max_length=5)
    thin_window_minutes: int | None = Field(default=None, ge=-1, le=1440)
    thin_batch_rows: int | None = Field(default=None, ge=-1, le=20000)


@app.get("/api/history-retention",
         dependencies=[Depends(require_write_token)])
async def get_history_retention() -> JSONResponse:
    from . import maintenance
    eff = await maintenance.effective_retention()
    eff["colfill_pending"] = await db.colfill_pending()
    for kv_key, out_key in (("history_thin_before_ms", "thin_watermark_ms"),
                            ("history_json_before_ms", "json_watermark_ms")):
        raw = await db.get_kv(kv_key)
        eff[out_key] = int(raw) if raw and str(raw).isdigit() else None
    # 2.0: the nightly pass's progress document (None before the first
    # night) — `nights_remaining` is the number the app shows.
    eff["thin_progress"] = await maintenance.thin_progress()
    return JSONResponse(eff)


@app.put("/api/history-retention",
         dependencies=[Depends(require_write_token)])
async def put_history_retention(body: RetentionIn) -> JSONResponse:
    from . import maintenance
    for name, floor in (("detail_days", 90), ("json_days", 7)):
        v = getattr(body, name)
        if v is not None and 0 < v < floor:
            raise HTTPException(
                status_code=400,
                detail=f"{name} must be 0 (off) or at least {floor}")
    if body.thin_batch_rows is not None and \
            0 <= body.thin_batch_rows < maintenance._THIN_BATCH_FLOOR:
        raise HTTPException(
            status_code=400,
            detail=f"thin_batch_rows must be at least "
                   f"{maintenance._THIN_BATCH_FLOOR}")
    if body.thin_window_start:
        try:
            maintenance.parse_thin_window_start(body.thin_window_start)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="thin_window_start must be HH:MM (24-hour, "
                       "station-local)")
    raw = await db.get_kv(maintenance._RETENTION_KV_KEY)
    stored: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = parsed
        except ValueError:
            pass
    for name in ("detail_days", "json_days", "thin_window_minutes",
                 "thin_batch_rows"):
        v = getattr(body, name)
        if v is None:
            continue
        if v == -1:
            stored.pop(name, None)
        else:
            stored[name] = v
    if body.thin_window_start is not None:
        if body.thin_window_start == "":
            stored.pop("thin_window_start", None)
        else:
            stored["thin_window_start"] = body.thin_window_start.strip()
    # Cross-check the RESULT, not just each field (R11): thinning blanks
    # data_json on every row it keeps, so an effective json_days above
    # detail_days claims to keep JSON longer than thinning actually
    # allows — the payload is silently gone at the thinning boundary.
    def _eff(name: str, env_v: int) -> int:
        v = stored.get(name)
        return v if isinstance(v, int) else env_v
    eff_detail = _eff("detail_days", settings.history_detail_days)
    eff_json = _eff("json_days", settings.history_json_detail_days)
    if eff_detail > 0 and eff_json > eff_detail:
        raise HTTPException(
            status_code=400,
            detail=f"json_days ({eff_json}) cannot exceed detail_days "
                   f"({eff_detail}) — row thinning already blanks JSON "
                   "for everything past its window")
    await db.set_kv(maintenance._RETENTION_KV_KEY,
                    json.dumps(stored) if stored else None)
    eff = await maintenance.effective_retention()
    eff["colfill_pending"] = await db.colfill_pending()
    return JSONResponse(eff)


async def _run_storage_job(job: dict[str, Any], detail_days: int) -> None:
    """Full measurement on a worker thread, then into the kv cache. The
    job dict carries the outcome — like _run_db_backup, this task's
    exception must never vanish into a fire-and-forget void."""
    from . import maintenance
    try:
        report = await asyncio.to_thread(
            maintenance.storage_breakdown, None, detail_days)
    except Exception as e:
        job.update(state="error", error=str(e))
        return
    # The scan is minutes of work on a big archive; do not let a ten-second
    # lock wait on the one-row cache write throw it away (2026-09-02: nine
    # minutes measured, then "database is locked" on the put). Retry the
    # write with backoff, and keep the report in the job either way so the
    # next GET can serve it from memory even if the cache never lands.
    report["measured_ms"] = int(time.time() * 1000)
    job["report"] = report
    for attempt, delay in enumerate((0, 3, 6, 12, 24, 48)):
        if delay:
            await asyncio.sleep(delay)
        try:
            await asyncio.to_thread(maintenance.storage_cache_put, report,
                                    None, report["measured_ms"])
            break
        except Exception as e:
            if attempt == 5 or not db.is_lock_error(e):
                log.warning("storage report not cached (%s); serving from "
                            "memory until the next scan", e)
                break
    job.update(state="ready", finished_ms=report["measured_ms"])


@app.get("/api/storage", dependencies=[Depends(require_write_token)])
async def api_storage(refresh: bool = False) -> JSONResponse:
    """Where the disk actually goes (1.9): file sizes, per-table bytes
    (when this sqlite exposes dbstat) and row counts, the observations
    data_json split, and the thinning state. Companion to /api/version's
    free-space number — that says HOW full, this says WITH WHAT.

    A background job with a cached result (2.0, 2026-09-01). The scan is
    a full read of the observations b-tree on a big database — minutes on
    Volney's 2.07 GB box, past Fly's 60 s proxy, and sqlite's progress
    handler can't interrupt dbstat's aggregate walk (one xNext call per
    b-tree, in C) — so no request can wait for it. Instead:

    - a cached report younger than _STORAGE_FRESH_MS answers at once with
      `state: "ready"` and its `measured_ms`;
    - otherwise (or with ?refresh=1) the job starts, or is already
      running, and the answer is `state: "measuring"` — carrying the
      stale report if there is one, and always the file sizes, so the
      app's decoder has its required `db_bytes`;
    - a failed job answers `state: "error"` once, then goes idle so the
      next GET retries.
    Every 1.9 key keeps its shape; `state` and `measured_ms` are new."""
    global _STORAGE_JOB, _STORAGE_TASK
    from . import maintenance
    now_ms = int(time.time() * 1000)
    job = _STORAGE_JOB
    # Trust "running" only while the task is alive (the backup job's
    # lesson): a task cancelled at shutdown must not read as measuring
    # forever.
    if job.get("state") == "running":
        task = _STORAGE_TASK
        if task is None or task.done():
            job["state"] = "error"
            job.setdefault("error", "previous measurement was interrupted")
    if job.get("state") == "error":
        _STORAGE_JOB = {"state": "idle"}
        _STORAGE_TASK = None
        return JSONResponse({**maintenance.storage_skeleton(),
                             "state": "error", "error": job.get("error")})

    cached = await asyncio.to_thread(maintenance.storage_cache_get)
    # A finished job whose cache write failed still has its report.
    if (cached is None and job.get("state") == "ready"
            and isinstance(job.get("report"), dict)):
        cached = job["report"]

    def _measuring(job: dict[str, Any]) -> JSONResponse:
        # The old report (its old measured_ms) when there is one, and the
        # skeleton always, so every key the app decodes is present.
        return JSONResponse({**maintenance.storage_skeleton(),
                             **(cached or {}), "state": "measuring",
                             "started_ms": job.get("started_ms")})

    # A live job outranks a fresh cache: after ?refresh=1 the poller (no
    # flag) must keep seeing "measuring" until the NEW report lands, or it
    # can't tell the refresh from a no-op.
    job = _STORAGE_JOB
    task = _STORAGE_TASK
    if (job.get("state") == "running"
            and task is not None and not task.done()):
        return _measuring(job)
    if (cached is not None and not refresh
            and now_ms - cached["measured_ms"] < _STORAGE_FRESH_MS):
        return JSONResponse({**cached, "state": "ready"})

    # Stale, absent, or refresh: start the job. The await here opens a
    # window for a concurrent GET to have started it first, so the claim
    # re-checks synchronously — two GETs racing through cannot both start
    # a job.
    eff = await maintenance.effective_retention()
    job = _STORAGE_JOB
    task = _STORAGE_TASK
    if not (job.get("state") == "running"
            and task is not None and not task.done()):
        job = _STORAGE_JOB = {"state": "running", "started_ms": now_ms}
        _STORAGE_TASK = asyncio.create_task(
            _run_storage_job(job, eff["detail_days"]))
    return _measuring(job)


@app.post("/api/update/check", dependencies=[Depends(require_write_token)])
async def api_update_check() -> JSONResponse:
    """On-demand release check (1.9). The background check runs at boot and
    then daily, so a release published an hour ago reads "up to date" for
    up to a day — this lets the app's "Check for updates now" ask right
    now. Same single GitHub-redirect lookup the daily check makes; the
    result lands in the same app.state, so /api/version and the status
    banner agree immediately."""
    checker = getattr(app.state, "update_checker", None)
    from . import updates as _updates
    if checker is None or not _updates._enabled():
        raise HTTPException(status_code=409,
                            detail="the update check is disabled on this "
                                   "server (UPDATE_CHECK=0)")
    if not await checker._check_once():
        raise HTTPException(status_code=502,
                            detail="couldn't reach GitHub to check — "
                                   "try again in a minute")
    from . import disk_watch
    return JSONResponse({**app.state.update_info,
                         "disk": disk_watch.snapshot()})


# ── Cloud-source integrations (Settings → Integrations) ──────────────────
# Configure the AmbientWeather / WeatherLink / Tempest / AirGradient /
# Ecowitt-cloud pollers from the app
# (server_kv, kv-over-env — the WU-key precedent) without a redeploy. ALL
# write-gated, and the GET is too: which providers an operator uses is
# operator business, and the response enumerates credential presence.

# ── Public dashboard config (Settings → Sharing, 1.7 apps) ──────────────
# The app-facing switch for the public page: on/off, which stations
# ("" = primary only, "all" = mirror every visible station, or a MAC csv),
# and the location label. kv-over-env like the WU key — env stays the
# scripted-setup path, the app value wins. Owner-only both ways: the GET
# reveals exposure posture, the PUT changes what the world sees.

class PublicDashboardBody(BaseModel):
    enabled: bool | None = None
    macs: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=80)


@app.get("/api/public-dashboard", dependencies=[Depends(require_write_token)])
async def api_public_dashboard_get() -> dict[str, Any]:
    return await _pd_effective()


@app.put("/api/public-dashboard", dependencies=[Depends(require_write_token)])
async def api_public_dashboard_put(body: PublicDashboardBody) -> dict[str, Any]:
    """Partial update; omitted fields keep their value. For macs/location an
    EMPTY STRING clears back to the env fallback (the SMTP contract)."""
    if body.enabled is not None:
        await db.set_kv("public_dashboard.enabled", "1" if body.enabled else "0")
    if body.macs is not None:
        m = body.macs.strip()
        await db.set_kv("public_dashboard.macs", m if m else None)
    if body.location is not None:
        loc = body.location.strip()
        await db.set_kv("public_dashboard.location", loc if loc else None)
    # The page caches its HTML for ~100s — a config change must show on the
    # next visit, not two minutes later. Cleared under the build lock: a
    # bare clear raced an in-flight build, which could assign the OLD html
    # back into the cache after this PUT returned (CodeRabbit, 2026-08-21).
    await _invalidate_public_dashboard_cache()
    return await _pd_effective()


@app.get("/api/integrations", dependencies=[Depends(require_write_token)])
async def api_integrations_status() -> dict[str, Any]:
    from . import integrations
    return {"providers": await integrations.status()}


@app.put("/api/integrations/{provider}", dependencies=[Depends(require_write_token)])
async def api_integrations_put(provider: str,
                               body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Store fields (omitted = unchanged, empty = clear back to env — the
    SMTP-password partial-update contract) and apply immediately: the
    provider's poller restarts with the effective credentials."""
    from . import integrations
    if provider not in integrations.PROVIDERS:
        raise HTTPException(status_code=404,
                            detail=f"unknown provider {provider!r}; "
                                   f"known: {sorted(integrations.PROVIDERS)}")
    allowed = {f for f, _, _ in integrations.PROVIDERS[provider]}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"unknown fields {sorted(unknown)}; "
                                   f"allowed: {sorted(allowed)}")
    try:
        await integrations.store(provider, body)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"bad value: {e}")
    running = await app.state.integration_manager.apply(provider)
    # One cheap authenticated call so wrong keys never save as a silent
    # success (CODE_REVIEW_R5 R5-07; the R3-21 serverNote precedent). The
    # values persist either way — an upstream outage must not block saving —
    # but the UI gets the failure to show next to the "On" pill.
    check = await integrations.probe(provider)
    return {"ok": True, "running": running, "check": check,
            "providers": await integrations.status()}


@app.delete("/api/integrations/{provider}", dependencies=[Depends(require_write_token)])
async def api_integrations_clear(provider: str) -> dict[str, Any]:
    """Clear every app-stored field for the provider. Env-configured
    credentials (if any) take back over; otherwise the poller stops."""
    from . import integrations
    if provider not in integrations.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    await integrations.clear(provider)
    running = await app.state.integration_manager.apply(provider)
    return {"ok": True, "running": running,
            "providers": await integrations.status()}


@app.get("/api/sources", dependencies=[Depends(require_token)])
async def api_sources() -> dict[str, Any]:
    """Health of each ingest source.

    Exists because a cloud poller that quietly stops — expired API keys, a
    revoked token, an upstream outage — is indistinguishable from dead
    hardware at the station end. This says which leg last worked and what the
    last failure said.

    `wu_upload` mirrors that for the OUTBOUND leg (1.5 WU forwarding): per
    enabled mac, when the last accepted upload happened and what the last
    failure was (status/type only — never the key or URL, see wu_upload.py).
    """
    from . import wu_upload
    uploads: dict[str, Any] = {}
    for assoc in await db.list_wu_stations():
        if not assoc["upload_enabled"]:
            continue
        uploads[assoc["mac"]] = {
            "enabled": True,
            "station_id": assoc["station_id"],
            "configured": bool(assoc["station_id"] and assoc["upload_key"]),
            **wu_upload.stats(assoc["mac"]),
        }
    # 2.4 item 3: the rolling 24 hour record beside the live state, so a
    # gap in somebody's charts can be explained after the fact instead of
    # only while it is happening. Read per source and never written here.
    from . import source_history
    now_ms = int(time.time() * 1000)
    sources = source_status.snapshot()
    for row in sources:
        if not row.get("configured") or row.get("label") is None:
            continue
        try:
            runs = await source_history.history(row["name"], now_ms)
        except Exception:
            log.exception("source health read failed for %s", row["name"])
            continue
        row["health_24h"] = {
            "runs": runs,
            **source_history.summarise(runs, now_ms),
        }
    return {"sources": sources, "wu_upload": uploads}


def _strip_device_pii(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coarsen the operator's home location for read-only tokens.

    `location` is a free-text label that routinely names a house — dropped
    entirely, same as always. Coordinates used to be dropped too, which
    silently broke every coords-driven feature on shared installs: the sun
    dial and the NWS weather alerts are computed client-side from station
    coords, so guests got neither (Volney, 2026-08-23 — read-only installs
    should match the owner wherever the difference isn't a secret). Since
    1.7 coords are ROUNDED to one decimal (~11 km, town scale — the same
    granularity the public dashboard's location label already publishes),
    which keeps sunrise within a minute and the NWS zone correct while
    still hiding the address."""
    out = []
    for d in devices:
        info = {k: v for k, v in (d.get("info") or {}).items()
                if k not in ("coords", "location")}
        coords = ((d.get("info") or {}).get("coords") or {}).get("coords") or {}
        try:
            lat, lon = float(coords["lat"]), float(coords["lon"])
            info["coords"] = {"coords": {"lat": round(lat, 1),
                                         "lon": round(lon, 1)}}
        except (KeyError, TypeError, ValueError):
            pass
        out.append({**d, "location": None, "info": info})
    return out


@app.get("/api/devices", dependencies=[Depends(require_token)])
async def get_devices(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    devices = await db.list_devices()
    if _is_limited_read(authorization):
        devices = _strip_device_pii(devices)
    # 2.2: the health of the poller behind each device, so a quiet station
    # can say "AirGradient's service is not answering" instead of looking
    # like dead hardware. Process state, so it rides on here and not in
    # db.list_devices; None for devices nobody polls for. Credentials never
    # appear (source_status.redact), so guests get it too.
    for d in devices:
        d["source_health"] = source_status.health_for_device(
            (d.get("info") or {}).get("source"))
    # 2.4 item 3: the last 24 hours beside the live verdict, as one
    # character per hour plus a summary. Read ONCE per source rather than
    # once per device — several stations commonly share one poller, and
    # this is on the app's most-polled route.
    from . import source_history
    now_ms = int(time.time() * 1000)
    cache: dict[str, dict[str, Any]] = {}
    for d in devices:
        health = d.get("source_health")
        if not health or not health.get("configured"):
            continue
        name = health["name"]
        if name not in cache:
            try:
                runs = await source_history.history(name, now_ms)
                cache[name] = {"hours": source_history.buckets(runs, now_ms),
                               **source_history.summarise(runs, now_ms)}
            except Exception:
                log.exception("source health read failed for %s", name)
                cache[name] = {}
        if cache[name]:
            health["health_24h"] = cache[name]
    return JSONResponse(devices)


# ───────────────────────── alert preferences (app-managed) ─────────────────────────
# The iOS app reads/writes these to control device-down email alerts. The
# SMTP transport itself stays a server secret (env); only PREFERENCES live
# here. DB prefs override env defaults; the monitor re-reads each tick.

class AlertPrefsIn(BaseModel):
    enabled: bool | None = None
    default_threshold_minutes: float | None = Field(default=None, ge=1, le=1440)
    repeat_hours: float | None = Field(default=None, ge=0, le=168)
    recipients: list[str] | None = None
    # App-managed SMTP transport. Password is write-only (never returned).
    # Send "" to clear a field back to the env default; omit to leave as-is.
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: bool | None = None
    smtp_ssl: bool | None = None
    # 'all' | 'device_down' — which alert kinds may email (push is unscoped).
    email_scope: str | None = None
    # Storm summary. Bounded rather than free: a quiet window under 5 minutes
    # would split one storm into several summaries. min_total's ge=0 is
    # DELIBERATE: 0 opts into a summary for any measurable rain at all
    # (desert stations count single tips); the default keeps the 0.05 floor
    # so nobody gets tip-spam without asking for it (R5-28).
    storm_summary: bool | None = None
    storm_quiet_minutes: float | None = Field(default=None, ge=5, le=360)
    storm_min_total_in: float | None = Field(default=None, ge=0, le=10)
    # 1.7 rain-start nowcast toggle (Open-Meteo minutely_15, opt-in).
    rain_start: bool | None = None
    # 1.7 storm-summary channels: 'push' | 'email' | 'both'; "" clears back
    # to legacy delivery (push always, email by email_scope).
    storm_channels: str | None = None
    # 2.4: the smart-alert master switch (lightning proximity and its
    # all clear, frost, heat, rapid pressure drop, first frost, battery
    # and sensor-quiet). Omit to leave it alone, which for a box that
    # never set it means the env SMART_ALERTS keeps deciding.
    smart_alerts: bool | None = None
    # 1.8 heat-day Live Activity: opt-in toggle + threshold (°F).
    heat_day: bool | None = None
    heat_day_threshold_f: float | None = Field(default=None, ge=70, le=130)
    # 1.8 quiet hours (minutes of local day, wrap-around allowed) and the
    # daily digest hour. -1 clears.
    quiet_start_min: int | None = Field(default=None, ge=-1, le=1439)
    quiet_end_min: int | None = Field(default=None, ge=-1, le=1439)
    digest_hour: int | None = Field(default=None, ge=-1, le=23)
    # 2.0: minute past the hour; -1 clears back to :00.
    digest_minute: int | None = Field(default=None, ge=-1, le=59)
    # 2.2 outlook report: hour/minute (-1 clears) and the source.
    outlook_hour: int | None = Field(default=None, ge=-1, le=23)
    outlook_minute: int | None = Field(default=None, ge=-1, le=59)
    outlook_source: str | None = Field(default=None, pattern="^(open-meteo|twc)$")
    # 2.2 sky notes.
    sky_notes: bool | None = None
    sky_good_only: bool | None = None
    # 2.3 NWS relay: the push switch and the warnings-only filter.
    nws_push: bool | None = None
    nws_warnings_only: bool | None = None
    # 2.4 item 1: the muted alert families. The MUTED set, not the
    # allowed one, so a family added in a later release arrives on. A
    # key nobody knows is dropped rather than stored (nws_families
    # .normalise_muted); the list is short and bounded by the catalogue.
    nws_families: list[str] | None = Field(default=None, max_length=32)
    # 2.4 item 11: the report emails' palette.
    report_theme: str | None = Field(
        default=None,
        pattern="^(" + "|".join(re.escape(t) for t in _email_card.THEMES) + ")$")
    # 2.3 storm watch on the lock screen: its own switch, but it needs
    # storm_summary on (the card rides the summary's tracker).
    storm_live_activity: bool | None = None
    # 2.3 item 5: the lightning, wind-ramp and freeze-night Live
    # Activities (default on) and their trigger thresholds, API-native
    # like heat_day_threshold_f: miles for the nearest strike that opens
    # the lightning card, mph for the gust that opens the wind card.
    lightning_live_activity: bool | None = None
    wind_live_activity: bool | None = None
    freeze_live_activity: bool | None = None
    lightning_live_mi: float | None = Field(default=None, ge=1, le=40)
    wind_live_mph: float | None = Field(default=None, ge=10, le=120)


class DeviceAlertIn(BaseModel):
    # monitor None = leave the device-down setting alone. (It used to default
    # True, but a 1.7 storm-only PUT must not silently re-enable monitoring;
    # every shipped client sends monitor explicitly, so nothing regresses.)
    monitor: bool | None = None
    threshold_minutes: float | None = Field(default=None, ge=1, le=1440)
    # 1.7 per-station storm-summary switch. None = leave unchanged.
    storm_summary: bool | None = None


async def _alerts_state() -> dict[str, Any]:
    """Full alert config + per-device status — the shape the iOS app renders."""
    from .alerts import effective_config, _device_threshold
    cfg = await effective_config()
    prefs = await db.get_alert_prefs()
    dev_prefs = await db.get_device_alert_prefs()
    states = await db.get_alert_states()
    devices = await db.list_devices()
    dev_list = []
    for d in devices:
        mac = d["mac"]
        dp = dev_prefs.get(mac, {})
        thr = _device_threshold(mac, dev_prefs, cfg.default_threshold_min)
        dev_list.append({
            "mac": mac,
            "name": d.get("name") or mac,
            "monitor": thr is not None,
            "threshold_minutes": thr,                       # effective; None if unmonitored
            "threshold_override": dp.get("threshold_min"),  # raw per-device value or None
            "last_seen_ms": d.get("lastSeen"),
            "state": (states.get(mac) or {}).get("state"),  # 'ok'|'stale'|None
            # 1.7 per-station storm-summary switch, effective (never-set = on).
            "storm_summary": dp.get("storm_summary") is not False,
        })
    return {
        "transport_configured": cfg.transport_configured,
        "enabled": cfg.enabled,
        "enabled_override": prefs["enabled"],               # raw 0/1/None
        "default_threshold_minutes": cfg.default_threshold_min,
        "repeat_hours": cfg.repeat_hours,
        "recipients": cfg.recipients,
        "recipients_source": "app" if prefs["recipients"] else "env",
        # SMTP transport — everything EXCEPT the password (write-only).
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_username": cfg.smtp_username,
        "smtp_from": cfg.smtp_from,
        "smtp_tls": cfg.smtp_tls,
        "smtp_ssl": cfg.smtp_ssl,
        "smtp_password_set": bool(cfg.smtp_password),
        "smtp_source": "app" if prefs["smtp_host"] else ("env" if cfg.smtp_host else "none"),
        "email_scope": cfg.email_scope,
        # Storm summary — the effective values, plus whether they came from
        # the app or the server env, so the UI can say which is in charge.
        "storm_summary": cfg.storm_summary,
        "storm_quiet_minutes": cfg.storm_quiet_minutes,
        "storm_min_total_in": cfg.storm_min_total_in,
        "storm_source": "app" if prefs["storm_summary"] is not None else "env",
        # 1.7: 'push'|'email'|'both', or None = legacy (push always, email
        # by email_scope). A pre-1.7 backend omits the key entirely, which
        # is how the app knows to hide the picker.
        "storm_channels": prefs.get("storm_channels"),
        # Rain-start nowcast (1.7) — same effective-value + source shape.
        "rain_start": cfg.rain_start,
        "rain_start_source": "app" if prefs.get("rain_start") is not None else "env",
        # Heat-day Live Activity (1.8).
        "heat_day": cfg.heat_day,
        "heat_day_threshold_f": cfg.heat_day_threshold_f,
        "quiet_start_min": cfg.quiet_start_min,
        "quiet_end_min": cfg.quiet_end_min,
        "digest_hour": cfg.digest_hour,
        "digest_minute": cfg.digest_minute,
        "outlook_hour": cfg.outlook_hour,
        "outlook_minute": cfg.outlook_minute,
        # Effective, never null: the app shows the Outlook controls when a
        # server answers this field at all (CodeRabbit, PR #37).
        "outlook_source": cfg.outlook_source or "open-meteo",
        "sky_notes": cfg.sky_notes,
        "sky_good_only": cfg.sky_good_only,
        # 2.3: effective, never null, so the app shows the switch when a
        # server answers the field at all.
        "nws_push": cfg.nws_push,
        "nws_warnings_only": cfg.nws_warnings_only,
        # 2.4 item 1: what is muted, plus the catalogue itself — the apps
        # draw the list from the server rather than each keeping a copy
        # of the keys and their spelling, which is how a family ends up
        # mutable on the phone and not on the Mac.
        "nws_families": cfg.nws_muted_families,
        # Effective, never null, so the app shows the control whenever a
        # server answers the field at all.
        "report_theme": cfg.report_theme,
        "nws_family_catalogue": [
            {"key": k, "label": _nws_families.LABELS[k]}
            for k in _nws_families.FAMILIES],
        "storm_live_activity": cfg.storm_live_activity,
        # 2.3 item 5: effective, never null, same rule as above.
        "lightning_live_activity": cfg.lightning_live_activity,
        "wind_live_activity": cfg.wind_live_activity,
        "freeze_live_activity": cfg.freeze_live_activity,
        "lightning_live_mi": cfg.lightning_live_mi,
        "wind_live_mph": cfg.wind_live_mph,
        # Smart-alert firing state, so a client with no push channel of its
        # own (the macOS app) can edge-detect these the way it now does
        # threshold rules. Rides on this response rather than a new endpoint
        # because the Mac already fetches it every minute.
        # 2.4: the EFFECTIVE value (row over env), because it is a
        # setting now rather than a read-only fact about the server.
        "smart_alerts_enabled": cfg.smart_alerts,
        "smart_alerts": [
            {"mac": mac, "kind": kind, "triggered": bool(trig)}
            for (mac, kind), trig in sorted((await db.get_smart_alert_states()).items())
        ],
        "devices": dev_list,
    }


@app.get("/api/alerts", dependencies=[Depends(require_token)])
async def get_alerts(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    state = await _alerts_state()
    if _is_limited_read(authorization):
        # Any non-operator read token gets the alerts UI state but not the
        # SMTP transport identifiers (host/username/from reveal the
        # maintainer's mail infrastructure; password was already write-only).
        for k in ("smtp_host", "smtp_username", "smtp_from"):
            if state.get(k):
                state[k] = "(hidden)"
        # Recipient addresses are the same data class as smtp_from (often
        # literally the same mailbox) — share-link guests don't get the
        # operator's personal emails (CODE_REVIEW_R5 R5-04 / R3-07).
        state["recipients"] = []
    return JSONResponse(state)


@app.get("/api/session", dependencies=[Depends(require_token)])
async def get_session(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """What can THIS token do? (1.7) One startup probe instead of each app
    section discovering read-only-ness through its own 403 — a shared
    install can then hide whole settings pages (sharing, integrations,
    server management) rather than rendering doors onto near-empty rooms."""
    can_write = tokens_match(_extract_bearer(authorization),
                             settings.write_tokens)
    # Read-only installs can't see (or set) the forecast-source picker, so
    # they follow the server: a configured WU key means the owner set up
    # TWC, and guests should see the same forecast (Volney, 2026-08-23).
    src = "twc" if await effective_wu_key() else "open-meteo"
    # 2.1: the server names itself and says what THIS token is to it, so
    # an app holding several connections (2.2) can label each one.
    name = await effective_server_name()
    return JSONResponse({"can_write": can_write, "forecast_source": src,
                         "server_name": name["name"],
                         "role": "owner" if can_write else "guest",
                         # 2.2: stable identity + where this server runs, so
                         # the app can say "Hosted on Fly.io, iad" and key
                         # its per-server state by something a URL change
                         # does not break. Token-gated: an install id is
                         # a fingerprint and does not belong on /api/version.
                         "server_id": await ensure_server_id(),
                         "hosted": hosting_info(),
                         # 2.3: the IANA zone the rollups (daily/monthly
                         # rain, records, reports) are cut in, so the app
                         # can label a server's day as that server's day.
                         "timezone": settings.timezone,
                         # F07: the map directory this server publishes to
                         # (non-secret), so a read-token client browses
                         # the directory its server is actually on.
                         "map_directory_url": str(settings.map_directory_url).rstrip("/")})


SERVER_ID_KEY = "server_id"


async def ensure_server_id() -> str:
    """The server's stable identity: a UUID minted on first boot and kept in
    the database beside the data it identifies. Idempotent."""
    existing = await db.get_kv(SERVER_ID_KEY)
    if existing and len(existing) >= 8:
        return existing
    new = str(uuid.uuid4())
    await db.set_kv(SERVER_ID_KEY, new)
    # A concurrent first caller may have won; the stored value is the truth.
    return (await db.get_kv(SERVER_ID_KEY)) or new


def hosting_info() -> dict[str, Any]:
    """Where this server runs, from the platform's own environment. Fly
    injects FLY_APP_NAME and FLY_REGION into every machine; anything else
    is reported as 'other' rather than guessed at."""
    app_name = os.environ.get("FLY_APP_NAME")
    if app_name:
        return {"platform": "fly", "region": os.environ.get("FLY_REGION"),
                "app": app_name}
    return {"platform": "other", "region": None, "app": None}


SERVER_NAME_MAX = 60


def _clean_server_name(raw: str | None) -> str | None:
    """One line, trimmed, bounded; None when empty or unprintable."""
    if raw is None:
        return None
    name = " ".join(str(raw).split())
    if not name or _has_ctl(name):
        return None
    return name[:SERVER_NAME_MAX]


async def effective_server_name() -> dict[str, Any]:
    """{'name': str|None, 'source': 'app'|'env'|'location'|'none'}: the
    app-set name, else SERVER_NAME in the env, else the public dashboard's
    location, else nothing."""
    stored = _clean_server_name(await db.get_kv("server_name"))
    if stored:
        return {"name": stored, "source": "app"}
    env = _clean_server_name(settings.server_name)
    if env:
        return {"name": env, "source": "env"}
    loc = _clean_server_name(settings.public_dashboard_location)
    if loc:
        return {"name": loc, "source": "location"}
    return {"name": None, "source": "none"}


class ServerNameIn(BaseModel):
    """Empty forgets the app value (env, then the public location, take
    over)."""
    name: str = Field(max_length=200)


@app.get("/api/config/server-name", dependencies=[Depends(require_token)])
async def get_server_name() -> JSONResponse:
    return JSONResponse(await effective_server_name())


@app.put("/api/config/server-name", dependencies=[Depends(require_write_token)])
async def put_server_name(body: ServerNameIn) -> JSONResponse:
    name = _clean_server_name(body.name)
    if body.name.strip() and name is None:
        raise HTTPException(status_code=400, detail="name must be printable text")
    await db.set_kv("server_name", name)
    return JSONResponse(await effective_server_name())


def require_mcp_enabled() -> None:
    """MCP_ENABLED=0 removes the MCP and OAuth routers at import; these
    three owner routes are declared on the app itself, so they take the
    switch as a dependency and 404 like the rest (round-two review,
    SEC-F4). Read at request time, so a reloaded config is honoured."""
    if not settings.mcp_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/oauth/clients",
         dependencies=[Depends(require_mcp_enabled), Depends(require_write_token)])
async def list_oauth_clients() -> JSONResponse:
    """The apps that connected through OAuth (2.1): claude.ai, ChatGPT,
    whatever registered. Name, when it last read, how many logins it
    holds, and which role it got (owner or guest). Never the tokens."""
    from . import oauth
    return JSONResponse({"clients": await oauth.list_clients()})


@app.delete("/api/oauth/clients/{client_id}",
            dependencies=[Depends(require_mcp_enabled), Depends(require_write_token)])
async def revoke_oauth_client(client_id: str) -> JSONResponse:
    """Cut a connected app off: its registration, its codes, every access
    and refresh token it holds. The kill switch rotating API_TOKEN is not."""
    from . import oauth
    if not await oauth.revoke_client(client_id):
        raise HTTPException(status_code=404, detail="no such client")
    return JSONResponse({"ok": True})


@app.post("/api/oauth/clients/{client_id}/approve",
          dependencies=[Depends(require_mcp_enabled), Depends(require_write_token)])
async def approve_oauth_client(client_id: str) -> JSONResponse:
    """The owner's approval of a registered app (2.1 review, SEC-1). Until
    this, the app's consent page shows no token field and its malformed
    requests get an error page rather than a redirect. Typing a connect
    code on the consent page approves in the same step."""
    from . import oauth
    if not await oauth.approve_client(client_id):
        raise HTTPException(status_code=404, detail="no such client")
    return JSONResponse({"ok": True})


class ConnectCodeIn(BaseModel):
    """Optional: the pending client this code is for (2.2, SEC-G5)."""
    client_id: str | None = Field(default=None, max_length=128)


@app.post("/api/oauth/connect-code",
          dependencies=[Depends(require_mcp_enabled), Depends(require_write_token)])
async def mint_oauth_connect_code(body: ConnectCodeIn | None = None) -> JSONResponse:
    """A one-shot, ten-minute code the owner types on the consent page in
    place of the API token (SEC-1). Minting it is the proof: only the
    write token can. Never logged; the response is the only copy. With a
    client_id the code approves only that registration (SEC-G5)."""
    from . import oauth
    cid = (body.client_id or "").strip() if body else ""
    return JSONResponse(await oauth.mint_connect_code(cid or None),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/alerts/recent", dependencies=[Depends(require_token)])
async def get_recent_alerts(limit: int = 50) -> JSONResponse:
    """Alert history, newest first (1.7). The server-side answer to a swiped-
    away notification: every handled alert — device-down, threshold rule,
    smart, storm summary, rain-start — lands in alert_log at the delivery
    funnel, so this list is identical on iPhone, iPad, and Mac. Readable by
    any token: titles carry no more than the pushes themselves did."""
    limit = max(1, min(limit, 200))
    return JSONResponse({"alerts": await db.recent_alerts(limit)})


def _has_ctl(s: str) -> bool:
    """True if the string contains an ASCII control character (incl. \\n)."""
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


@app.put("/api/alerts", dependencies=[Depends(require_write_token)])
async def put_alerts(body: AlertPrefsIn) -> JSONResponse:
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.storm_summary is not None:
        fields["storm_summary"] = 1 if body.storm_summary else 0
    if body.rain_start is not None:
        fields["rain_start"] = 1 if body.rain_start else 0
    if body.heat_day is not None:
        fields["heat_day"] = 1 if body.heat_day else 0
    if body.heat_day_threshold_f is not None:
        fields["heat_day_threshold_f"] = body.heat_day_threshold_f
    # Quiet hours are a PAIR: in_quiet_hours engages only when both ends
    # are set, so a one-sided write got a 200 and a feature that never
    # fires (R7 finding 5). Require both in one request (either real
    # values or the -1 clear).
    if (body.quiet_start_min is None) != (body.quiet_end_min is None):
        raise HTTPException(
            status_code=400,
            detail="quiet_start_min and quiet_end_min must be set together "
                   "(use -1 for both to clear)")
    for f in ("quiet_start_min", "quiet_end_min", "digest_hour",
              "digest_minute", "outlook_hour", "outlook_minute"):
        v = getattr(body, f)
        if v is not None:
            fields[f] = None if v < 0 else v
    if body.outlook_source is not None:
        fields["outlook_source"] = body.outlook_source
    if body.report_theme is not None:
        fields["report_theme"] = body.report_theme
    if body.nws_families is not None:
        # Cleaned, then stored as JSON. An empty list is a real value
        # (nothing muted) and clears the column's contents rather than
        # the column.
        fields["nws_families"] = json.dumps(
            _nws_families.normalise_muted(body.nws_families))
    for f in ("sky_notes", "sky_good_only", "nws_push", "nws_warnings_only",
              "storm_live_activity", "lightning_live_activity",
              "wind_live_activity", "freeze_live_activity",
              "smart_alerts"):
        v = getattr(body, f)
        if v is not None:
            fields[f] = 1 if v else 0
    for f in ("lightning_live_mi", "wind_live_mph"):
        v = getattr(body, f)
        if v is not None:
            fields[f] = v
    if body.storm_quiet_minutes is not None:
        fields["storm_quiet_minutes"] = body.storm_quiet_minutes
    if body.storm_min_total_in is not None:
        fields["storm_min_total_in"] = body.storm_min_total_in
    if body.default_threshold_minutes is not None:
        fields["default_threshold_min"] = body.default_threshold_minutes
    if body.repeat_hours is not None:
        fields["repeat_hours"] = body.repeat_hours
    if body.email_scope is not None:
        if body.email_scope not in ("all", "device_down"):
            raise HTTPException(status_code=400,
                                detail="email_scope must be 'all' or 'device_down'")
        fields["email_scope"] = body.email_scope
    if body.storm_channels is not None:
        # "" clears back to legacy delivery (push always, email by scope).
        if body.storm_channels == "":
            fields["storm_channels"] = None
        elif body.storm_channels not in ("push", "email", "both"):
            raise HTTPException(
                status_code=400,
                detail="storm_channels must be 'push', 'email' or 'both'")
        else:
            fields["storm_channels"] = body.storm_channels
    if body.recipients is not None:
        clean = [r.strip() for r in body.recipients if r.strip()]
        for r in clean:
            # One address, no whitespace/control chars, and NO comma: the
            # stored form is comma-joined and re-split on ",", so a recipient
            # containing one would silently become two; a control char (\n)
            # would corrupt every alert send's headers.
            if not re.fullmatch(r"[^@\s,]+@[^@\s,]+", r) or _has_ctl(r):
                raise HTTPException(status_code=400, detail=f"invalid recipient: {r!r}")
        # Empty list clears the override → falls back to env recipients.
        fields["recipients"] = ",".join(clean) if clean else None
    # SMTP transport (DB over env). Empty string clears → env fallback.
    # Control characters are rejected on every header-bound value — a \n in
    # smtp_from breaks EmailMessage for every subsequent alert.
    for attr in ("smtp_host", "smtp_username", "smtp_from"):
        val = getattr(body, attr)
        if val is not None:
            if _has_ctl(val):
                raise HTTPException(status_code=400,
                                    detail=f"{attr} must not contain control characters")
            fields[attr] = val.strip() or None
    if body.smtp_port is not None:     fields["smtp_port"] = body.smtp_port
    if body.smtp_password is not None: fields["smtp_password"] = body.smtp_password or None
    if body.smtp_tls is not None:      fields["smtp_tls"] = 1 if body.smtp_tls else 0
    if body.smtp_ssl is not None:      fields["smtp_ssl"] = 1 if body.smtp_ssl else 0
    await db.set_alert_prefs(**fields)
    return JSONResponse(await _alerts_state())


@app.put("/api/devices/{mac}/alert", dependencies=[Depends(require_shared_write)])
async def put_device_alert(mac: str, body: DeviceAlertIn) -> JSONResponse:
    from .ingest import _format_mac
    if body.monitor is not None:
        await db.upsert_device_alert_pref(_format_mac(mac), body.monitor,
                                          body.threshold_minutes)
    elif body.threshold_minutes is not None:
        # R6: a threshold sent WITHOUT monitor used to 200 with no effect.
        # Preserve the device's current monitor state (absent row = the
        # monitored-by-default the alert monitor assumes).
        prefs = await db.get_device_alert_prefs()
        current = prefs.get(_format_mac(mac)) or {}
        await db.upsert_device_alert_pref(
            _format_mac(mac), bool(current.get("monitor", True)),
            body.threshold_minutes)
    if body.storm_summary is not None:
        await db.set_device_storm_summary(_format_mac(mac), body.storm_summary)
    return JSONResponse(await _alerts_state())


class DeviceLocationIn(BaseModel):
    lat: float
    lon: float
    label: str | None = None


@app.put("/api/devices/{mac}/location", dependencies=[Depends(require_shared_write)])
async def put_device_location(mac: str, body: DeviceLocationIn) -> JSONResponse:
    """Set a device's location (iOS per-device Location setting). Overrides the
    ingest-time default; the top-ordered device drives the forecast + sun dial."""
    from .ingest import _format_mac
    if not (-90.0 <= body.lat <= 90.0) or not (-180.0 <= body.lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    norm = _format_mac(mac)
    await db.set_device_location(norm, body.lat, body.lon, body.label,
                                 int(time.time() * 1000))
    return JSONResponse({"ok": True, "mac": norm, "lat": body.lat,
                         "lon": body.lon, "label": body.label})


class DeviceNameIn(BaseModel):
    # None or "" clears the override back to the name the station posts.
    # Bounded here as well as in db.clean_display_name so a 10 MB body is
    # refused by validation before the handler ever trims it.
    name: str | None = Field(default=None, max_length=1024)


@app.put("/api/devices/{mac}/name", dependencies=[Depends(require_shared_write)])
async def put_device_name(mac: str, body: DeviceNameIn) -> JSONResponse:
    """Rename a station (2.0). The source keeps posting its own name (an
    Ecowitt gateway only knows its model); this stores the operator's
    override, which every surface then shows through
    db.effective_device_name. Send "" or null to go back to the station's
    own name. Write-share tier, like the location editor: a guest with a
    write link may tidy the station list. 404 until the station has posted
    once — the override lives on the device row."""
    from .ingest import _format_mac
    norm = _format_mac(mac)
    try:
        clean = db.clean_display_name(body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not await db.set_device_display_name(norm, clean):
        raise HTTPException(status_code=404, detail="device not found")
    dev = next((d for d in await db.list_devices() if d["mac"] == norm), {})
    return JSONResponse({"ok": True, "mac": norm,
                         "name": dev.get("name") or norm,
                         "display_name": dev.get("display_name"),
                         "source_name": dev.get("source_name")})


class WUStationIn(BaseModel):
    # All fields optional = "leave unchanged", so the app can flip the upload
    # toggle without re-sending the station ID (and 1.4 clients that only
    # ever send wu_station_id keep their exact semantics). Empty string
    # clears. WU IDs are short uppercase alphanumerics (KAZCHAND802);
    # bound + shape-check so junk can't land.
    wu_station_id: str | None = Field(default=None, max_length=32,
                                      pattern=r"^[A-Za-z0-9]*$")
    # WU *station* key for live upload — write-only, like /api/config/wu-key:
    # "" clears, and no endpoint ever returns it (GET reports upload_key_set).
    upload_key: str | None = Field(default=None, max_length=64,
                                   pattern=r"^[A-Za-z0-9]*$")
    # Turn live forwarding on/off (app/wu_upload.py).
    upload_enabled: bool | None = None


def _wu_station_view(norm: str, row: dict[str, Any] | None) -> dict[str, Any]:
    """API shape for a WU association: the key itself NEVER leaves the
    server — only whether one is set."""
    from . import wu_upload
    stats = wu_upload.stats(norm)
    return {"mac": norm,
            "wu_station_id": row["station_id"] if row else None,
            "upload_enabled": bool(row and row["upload_enabled"]),
            "upload_key_set": bool(row and row["upload_key"]),
            # The forwarding page shows "sent 3 minutes ago" like the
            # networks list does (Doren, 2026-09-06); status only, never
            # the key.
            "upload_last_ok_ms": stats.get("last_ok_ms"),
            "upload_last_error": stats.get("last_error")}


@app.get("/api/devices/{mac}/wu-station", dependencies=[Depends(require_token)])
async def get_wu_station(mac: str) -> JSONResponse:
    from .ingest import _format_mac
    norm = _format_mac(mac)
    return JSONResponse(_wu_station_view(norm, await db.get_wu_station(norm)))


@app.put("/api/devices/{mac}/wu-station", dependencies=[Depends(require_shared_write)])
async def put_wu_station(mac: str, body: WUStationIn,
                         authorization: Annotated[str | None,
                                                  Header()] = None) -> JSONResponse:
    """Associate a Weather Underground station ID with a device — the WU
    importer's target mapping and (1.5) the live-upload config. Omitted
    fields are left unchanged; "" clears. Clearing the station ID drops the
    upload key + toggle with it (see db.set_wu_station).

    TIER SPLIT (R12 W1): the route is on the write-share tier for the
    station-ID mapping only. upload_key / upload_enabled are CREDENTIALS —
    a zww_ holder who could set them would redirect the owner's live feed
    to their own WU station, and the redirect would survive revoking the
    share (revocation drops the token, not wu_station_map). Owner only."""
    from .ingest import _format_mac
    is_owner = tokens_match(_extract_bearer(authorization),
                            settings.write_tokens)
    if ((body.upload_key is not None or body.upload_enabled is not None)
            and not is_owner):
        raise HTTPException(
            status_code=403,
            detail="the WU upload key and forwarding toggle are owner-only "
                   "— a write share can only set the station-ID mapping")
    norm = _format_mac(mac)
    # Known devices only (same check as start_wu_import): a typo'd MAC would
    # otherwise create a wu_station_map row for a nonexistent device that
    # silently attaches to whatever registers under that MAC later.
    if not any(d["mac"] == norm for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {norm}")
    # R13 X1: clearing the station ID cascades to deleting the whole row —
    # upload key included (set_wu_station's documented semantics). That's
    # fine for a pure mapping, but when the owner has configured the
    # upload credential, a shared writer's clear would DESTROY it (the key
    # is write-only, so only the owner can re-enter it). Refuse; the
    # mapping stays shared-writable whenever no credential is at stake.
    if not is_owner and body.wu_station_id is not None \
            and not body.wu_station_id.strip():
        prior = await db.get_wu_station(norm)
        if prior and (prior["upload_key"] or prior["upload_enabled"]):
            raise HTTPException(
                status_code=403,
                detail="clearing this station mapping would delete the "
                       "owner's WU upload credential — ask the owner to "
                       "clear it")
    kwargs: dict[str, Any] = {}
    if body.wu_station_id is not None:
        kwargs["station_id"] = body.wu_station_id.strip().upper() or None
    if body.upload_key is not None:
        kwargs["upload_key"] = body.upload_key.strip() or None
    if body.upload_enabled is not None:
        kwargs["upload_enabled"] = body.upload_enabled
    # Upload config without a station to upload to would be silently dropped
    # by the row-deletion semantics — refuse it loudly instead.
    existing = await db.get_wu_station(norm)
    effective_sid = kwargs.get("station_id",
                               existing["station_id"] if existing else None)
    if effective_sid is None and (kwargs.get("upload_key")
                                  or kwargs.get("upload_enabled")):
        raise HTTPException(status_code=400,
                            detail="set a wu_station_id before configuring "
                                   "the WU upload key or enabling forwarding")
    if kwargs:
        await db.set_wu_station(norm, now_ms=int(time.time() * 1000), **kwargs)
    row = await db.get_wu_station(norm)
    return JSONResponse({"ok": True, **_wu_station_view(norm, row)})


class WUKeyIn(BaseModel):
    # Write-only, like the SMTP password: never returned by any endpoint.
    # "" clears back to the WU_API_KEY env fallback.
    api_key: str = Field(max_length=128, pattern=r"^[A-Za-z0-9]*$")


async def effective_wu_key() -> str | None:
    """App-managed key over env secret — the SMTP resolution pattern."""
    return await db.get_kv("wu_api_key") or settings.wu_api_key


@app.get("/api/config/wu-key", dependencies=[Depends(require_token)])
async def get_wu_key_status() -> JSONResponse:
    stored = await db.get_kv("wu_api_key")
    return JSONResponse({
        "configured": bool(stored or settings.wu_api_key),
        "source": "app" if stored else ("env" if settings.wu_api_key else "none"),
    })


@app.put("/api/config/wu-key", dependencies=[Depends(require_write_token)])
async def put_wu_key(body: WUKeyIn) -> JSONResponse:
    """Store the Weather Underground API key server-side (powers the TWC
    forecast source; the app's Import History screen syncs it here)."""
    key = body.api_key.strip() or None
    if key is not None and len(key) < 8:
        raise HTTPException(status_code=400, detail="api_key too short")
    await db.set_kv("wu_api_key", key)
    stored = await db.get_kv("wu_api_key")
    return JSONResponse({
        "ok": True,
        "configured": bool(stored or settings.wu_api_key),
        "source": "app" if stored else ("env" if settings.wu_api_key else "none"),
    })


@app.get("/api/insights", dependencies=[Depends(require_token)])
async def get_insights(mac: str = Query(...)) -> JSONResponse:
    """Station statistics over the rollup tables (heat ledger, rain seasons,
    normals/anomalies, diurnal grid, calendar). Opt-in: INSIGHTS=1."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    norm = _format_mac(mac)
    payload = await insights.assemble(norm)
    if payload["day_count"] == 0:
        # Distinguish "no data" from "flag enabled after data existed".
        payload["hint"] = ("no rollups yet — if this station has history, "
                           "POST /api/insights/rebuild once")
    return JSONResponse(payload)


@app.get("/api/insights/daily", dependencies=[Depends(require_token)])
async def get_insights_daily(mac: str = Query(...),
                             days: int = Query(60, ge=7, le=366)) -> JSONResponse:
    """Per-day temperature series for one station (rollups only) — the
    sensor-drift card fetches this once per visible station and diffs the
    daily means client-side. Opt-in with the rest of Insights."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await insights.daily_series(_format_mac(mac), days))


@app.get("/api/devices/{mac}/daily-series",
         dependencies=[Depends(require_token)])
async def get_daily_series(
    mac: str,
    days: int = Query(366, ge=7, le=3700),
    end_day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> JSONResponse:
    """Year-span chart data (1.9): one row per LOCAL DAY from the rollups
    — min/max/mean temp, rain, peak gust — so a year (or a decade) of
    chart never touches raw rows. /history keeps its 745-hour cap for
    raw detail; this is the coarser series above it. Days beyond the
    station's history simply aren't in the result. Opt-in with Insights
    (the rollups ARE the data source)."""
    from . import climate
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from datetime import date as _date, timedelta as _td
    from .ingest import _format_mac
    norm = _format_mac(mac)
    try:
        # Default end = today in the STATION timezone (the clock that
        # names rollup days) — a UTC host is a day ahead of Phoenix for
        # part of every evening (CodeRabbit, PR #33).
        end = _date.fromisoformat(end_day) if end_day else climate.local_today()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad end_day: {e}")
    first = end - _td(days=days - 1)
    rows = await climate._rollup_rows(norm, first.isoformat(), end.isoformat())
    series = []
    for r in rows:
        d = climate._day_stats(r)
        series.append([d["day"], d["tmin"], d["tmax"],
                       round(d["mean"], 1) if d["mean"] is not None else None,
                       d["rain"], d["gust"]])
    return JSONResponse({"mac": norm, "days": days,
                         "end_day": end.isoformat(),
                         # [day, tmin, tmax, mean, rain_in, gust_mph]
                         "series": series})


@app.get("/api/devices/{mac}/climate", dependencies=[Depends(require_token)])
async def get_climate(mac: str,
                      year: int = Query(..., ge=1970, le=2100)) -> JSONResponse:
    """Climate summary (1.9, WeeWX parity): twelve month rows (mean/
    extremes with dates, rain, HDD/CDD/GDD) + annual totals + the running
    water year — all from rollups. Degree days use the NOAA (max+min)/2
    convention, base 65; GDD base 50 cap 86 (computed now, surfaced by
    the agriculture pack later)."""
    from . import climate
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await climate.year_summary(_format_mac(mac), year))


@app.get("/api/devices/{mac}/forecast-accuracy",
         dependencies=[Depends(require_token)])
async def get_forecast_accuracy(
    mac: str,
    days: int = Query(forecast_skill.DEFAULT_DAYS, ge=7,
                      le=forecast_skill.MAX_DAYS),
    provider: str = Query("open-meteo", pattern="^[a-z0-9-]{1,32}$"),
) -> JSONResponse:
    """How wrong the forecast tends to be for THIS backyard (2.3).

    `forecast_snapshots` has archived every forecast AS ISSUED since 1.8,
    with its lead time; this scores those calls against the station's own
    daily rollups at lead 1 through 6. Bias is signed forecast minus
    measured in °F, so positive means the model ran warm. `available`
    false means the archive has nothing to grade yet, which on a fresh
    server is simply the truth — verification cannot be done
    retroactively, which is why the collector shipped first.
    """
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await forecast_skill.scorecard(
        _format_mac(mac), provider=provider, days=days))


@app.get("/api/devices/{mac}/storms", dependencies=[Depends(require_token)])
async def get_storms(mac: str,
                     limit: int = Query(10, ge=1, le=50)) -> JSONResponse:
    """Recent closed storm episodes (1.9) — the structured stats behind
    each delivered storm summary, newest first, for the Storm Report
    share card. Only storms recorded since 1.9 appear; older summaries
    live in the alert history as text."""
    from .ingest import _format_mac
    return JSONResponse({"storms": await db.list_storms(_format_mac(mac),
                                                        limit)})


@app.get("/api/devices/{mac}/changes", dependencies=[Depends(require_token)])
async def get_changes(mac: str,
                      hours: int = Query(changes.WINDOW_HOURS_DEFAULT,
                                         ge=1,
                                         le=changes.WINDOW_HOURS_MAX)
                      ) -> JSONResponse:
    """The weather-change timeline (2.4): when the weather TURNED over
    the last day, from this station's own readings. Highlights say how
    unusual today is; this says when it changed."""
    from .ingest import _format_mac
    return JSONResponse(await changes.assemble(_format_mac(mac), hours))


# The most stories one request may ask for. Comfortably above the whole
# registry (21 producers, some of which emit several) so "show me
# everything" is answerable, and low enough that a payload stays sane.
STORY_LIMIT_MAX = 50


# ── Reports (2.1) ───────────────────────────────────────────────────────
#
# The morning report and every storm summary, kept as rows instead of
# notifications that scroll away. `/api/reports` is the Reports pane's
# list; `/api/reports/{id}` is the detail page a push or a Live Activity
# tap deep-links into; `/api/reports/morning/preview` renders today's
# report on demand, so a fresh install has something to open before its
# first 7am and a "run it now" button has something to call.


@app.get("/api/reports", dependencies=[Depends(require_token)])
async def get_reports(
    kind: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    before_ms: int | None = Query(None, ge=0),
    before_id: int | None = Query(None, ge=0),
    temp_unit: str | None = Query(None),
    wind_unit: str | None = Query(None),
    rain_unit: str | None = Query(None),
    pressure_unit: str | None = Query(None),
) -> JSONResponse:
    """Stored reports, newest first. `kind` filters to one family; omit it
    for the mixed list the Reports pane shows. `before_ms` and `before_id`
    page: pass the oldest row's `ts_ms` AND `id` (the keyset, so rows
    written in the same millisecond are never skipped); `before_ms` alone
    still works for older clients. Payloads are NOT included — read one report
    for its numbers. The unit query parameters are the ones
    /api/devices/{mac}/stories takes: with any of them set, each row's
    `summary` is re-rendered in those units (2.1 pre-release review
    BE-3); omit them for the stored API-native text."""
    from . import reports as rp, stories as _st
    if kind is not None and kind not in rp.KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of {', '.join(rp.KINDS)}")
    try:
        units = _st.parse_units(temp_unit, wind_unit, rain_unit, pressure_unit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    convert = units != _st.UNITS_NATIVE
    rows = await db.list_reports(kind=kind, limit=limit, before_ms=before_ms,
                                 before_id=before_id, with_payload=convert)
    if convert:
        for row in rows:
            payload = row.pop("payload", None) or {}
            rendered = rp.summary_line(row["kind"], payload, units)
            if rendered:
                row["summary"] = rendered
    return JSONResponse({"reports": rows, "kinds": list(rp.KINDS)})


class ReportRetentionIn(BaseModel):
    """`max_rows` -1 forgets the app override (env, then the default, take
    over). Anything else is clamped to the documented floor/ceiling."""
    max_rows: int = Field(ge=-1, le=100_000)


@app.get("/api/reports/retention", dependencies=[Depends(require_write_token)])
async def get_report_retention() -> JSONResponse:
    """How many reports the server keeps and where that number came from
    (2.1): app, env, or the default. `count` is what it holds now."""
    from . import reports as rp
    eff = await db.effective_report_retention()
    eff.update(count=await db.count_reports(),
               floor=rp.MIN_ROWS, ceiling=rp.MAX_ROWS_CEILING,
               default=rp.MAX_ROWS)
    return JSONResponse(eff)


@app.put("/api/reports/retention", dependencies=[Depends(require_write_token)])
async def put_report_retention(body: ReportRetentionIn) -> JSONResponse:
    """Set the bound. Prunes at once so a smaller number shows at once."""
    from . import reports as rp
    try:
        eff = await db.set_report_retention(
            None if body.max_rows == -1 else body.max_rows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    eff.update(count=await db.count_reports(),
               floor=rp.MIN_ROWS, ceiling=rp.MAX_ROWS_CEILING,
               default=rp.MAX_ROWS)
    return JSONResponse(eff)


class StormRetentionIn(BaseModel):
    """`max_per_station` -1 forgets the app override (env, then the
    default, take over). Anything else is clamped to the floor/ceiling."""
    max_per_station: int = Field(ge=-1, le=100_000)


def _storm_retention_body(eff: dict[str, Any], count: int) -> dict[str, Any]:
    eff.update(count=count, floor=db.STORM_HISTORY_FLOOR,
               ceiling=db.STORM_HISTORY_CEILING,
               default=db.STORM_HISTORY_DEFAULT)
    return eff


@app.get("/api/storms/retention", dependencies=[Depends(require_write_token)])
async def get_storm_retention() -> JSONResponse:
    """How many closed storms the server keeps per station (2.1) and where
    that number came from: app, env, or the default. `count` is the
    whole ledger's row count now."""
    return JSONResponse(_storm_retention_body(
        await db.effective_storm_retention(), await db.count_storm_history()))


@app.put("/api/storms/retention", dependencies=[Depends(require_write_token)])
async def put_storm_retention(body: StormRetentionIn) -> JSONResponse:
    """Set the bound. Prunes every station at once."""
    try:
        eff = await db.set_storm_retention(
            None if body.max_per_station == -1 else body.max_per_station)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(_storm_retention_body(eff, await db.count_storm_history()))


class ReportRunIn(BaseModel):
    """A report to build and store now. `month` is required for the
    monthly kind and ignored by the yearly one."""
    kind: str = Field(max_length=32)
    mac: str = Field(min_length=1, max_length=64)
    year: int = Field(ge=1970, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    # 2.2: the daily kind names its day; year/month are ignored for it.
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/reports/run", dependencies=[Depends(require_write_token)])
async def run_report(body: ReportRunIn) -> JSONResponse:
    """Build one of the runnable report kinds for a station and period,
    store it (one row per station and period: a re-run updates), and
    return it as the detail page would (2.1). The NOAA-style climate
    tables come from the same renderer `/reports/noaa` has served since
    1.9; this is what puts them in the Reports pane."""
    from . import climate
    from . import reports as rp
    if body.kind not in rp.RUNNABLE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {', '.join(rp.RUNNABLE_KINDS)}")
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    if body.kind == rp.KIND_NOAA_MONTH and body.month is None:
        raise HTTPException(status_code=400,
                            detail="month is required for noaa_month")
    day_obj = None
    if body.kind == rp.KIND_NOAA_DAY:
        if body.day is None:
            raise HTTPException(status_code=400,
                                detail="day (YYYY-MM-DD) is required for noaa_day")
        from datetime import date as _date
        try:
            day_obj = _date.fromisoformat(body.day)
        except ValueError:
            raise HTTPException(status_code=400, detail="day is not a calendar date")
    from .ingest import _format_mac
    norm = _format_mac(body.mac)
    dev = next((d for d in await db.list_devices() if d["mac"] == norm), None)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such device")
    name = dev.get("name") or norm
    month = body.month if body.kind == rp.KIND_NOAA_MONTH else None
    year = day_obj.year if day_obj else body.year
    if day_obj is not None:
        text = await climate.noaa_day_report(norm, name, day_obj)
        numbers = await climate.noaa_day_numbers(norm, day_obj)
    elif month is not None:
        text = await climate.noaa_month_report(norm, name, body.year, month)
        numbers = await climate.noaa_month_numbers(norm, body.year, month)
    else:
        text = await climate.noaa_year_report(norm, name, body.year)
        numbers = await climate.noaa_year_numbers(norm, body.year)
    day_str = day_obj.isoformat() if day_obj else None
    payload = rp.noaa_payload(body.kind, norm, name, year, month,
                              text, numbers, day=day_str)
    now_ms = int(time.time() * 1000)
    for_date = (day_str if day_str
                else f"{body.year}-{month:02d}-01" if month
                else f"{body.year}-01-01")
    rid = await db.insert_report(
        kind=body.kind, mac=norm, ts_ms=now_ms, for_date=for_date,
        title=rp.noaa_title(body.kind, name, year, month, day=day_str),
        summary=rp.noaa_summary_line(payload), payload=payload,
        dedupe=rp.noaa_key(body.kind, norm, year, month, day=day_str))
    row = await db.get_report(rid) if rid is not None else None
    if row is None:
        raise HTTPException(status_code=500,
                            detail="the report was built but not stored")
    return JSONResponse({"report": row})


@app.get("/api/reports/morning/preview", dependencies=[Depends(require_token)])
async def get_morning_preview() -> JSONResponse:
    """Today's morning report, built NOW and not stored or sent. The same
    builder the 7am job uses, so the preview is the report — this is the
    "run it now" path, and the only thing a brand-new server can show in
    the Reports pane before its first morning."""
    from zoneinfo import ZoneInfo

    from . import alerts as al
    from . import reports as rp
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now_ms = int(time.time() * 1000)
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    report, _stations, _rows = await al.build_morning_report(
        await db.list_devices(), now_ms, now_ms - 86_400_000, tz, local)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="no rollups yet for yesterday; Insights must be on and "
                   "the station must have reported")
    payload = rp.morning_payload(report)
    return JSONResponse({
        "report": {"id": None, "kind": rp.KIND_MORNING, "mac": None,
                   "ts_ms": int(time.time() * 1000), "for_date": None,
                   "title": "Morning report · " + report.date_label,
                   "summary": rp.morning_summary(payload),
                   "payload": payload, "preview": True}})


@app.get("/api/reports/{report_id}", dependencies=[Depends(require_token)])
async def get_report_detail(report_id: int) -> JSONResponse:
    """One stored report with its payload — the detail page."""
    row = await db.get_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such report")
    return JSONResponse({"report": row})


@app.get("/api/devices/{mac}/stories", dependencies=[Depends(require_token)])
async def get_stories(
    mac: str,
    # The ceiling is "every card this station can produce", not a page
    # size. 21 producers and several emit more than one story, so 12 was
    # silently hiding cards from a rich archive: Volney's oldest station
    # had 16 candidates and the Reports pane's "every shareable you can
    # run" list could only ever show 12 of them (2.1). Still bounded —
    # each story carries its rendered strings and viz rows.
    limit: int = Query(4, ge=1, le=STORY_LIMIT_MAX),
    family: str | None = Query(None),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    temp_unit: str | None = Query(None),
    wind_unit: str | None = Query(None),
    rain_unit: str | None = Query(None),
    pressure_unit: str | None = Query(None),
) -> JSONResponse:
    """Ranked stories about this station (2.0) — the Worth Sharing section
    and the share cards behind it. The server decides what is interesting;
    the app renders the template each story names.

    Rollups only, like the rest of the Insights family, so it costs the
    same as one /api/insights call no matter how many producers run. A
    producer with nothing honest to say declines and is named in
    `declined` — an empty `stories` list is a valid answer, not an error.

    THE UNIT PARAMETERS ARE NOT COSMETIC. Every string a card shows is
    written here, and those strings bake the unit into the words ("108 DAYS
    ≥100°F"). A client cannot convert them afterwards: converting the
    numbers alone would put "44°C" next to "≥100°F" in one picture. So the
    caller sends what its user is set to and the whole story comes back in
    that scale. Values are the app's own enum spellings — temp_unit
    fahrenheit|celsius, wind_unit mph|kph|ms|knots|beaufort, rain_unit
    inches|mm, pressure_unit inHg|hPa — and omitting them keeps the
    API-native rendering unchanged."""
    from . import stories
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    if family is not None and family not in stories.FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown family {family!r} — one of {list(stories.FAMILIES)}")
    try:
        units = stories.parse_units(temp_unit, wind_unit, rain_unit,
                                    pressure_unit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from .ingest import _format_mac
    return JSONResponse(await stories.top_stories(
        _format_mac(mac), limit=limit,
        families=[family] if family else None, min_score=min_score,
        units=units))


@app.get("/api/devices/{mac}/reports/noaa",
         dependencies=[Depends(require_token)])
async def get_noaa_report(
    mac: str,
    year: int = Query(..., ge=1970, le=2100),
    month: int | None = Query(None, ge=1, le=12),
) -> PlainTextResponse:
    """The classic NOAA-style fixed-width climate report (1.9, the WeeWX
    flagship): month given → day rows; omitted → month rows for the
    year. text/plain by design — it's the report every long-time station
    owner already knows by shape."""
    from . import climate
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    norm = _format_mac(mac)
    dev = next((d for d in await db.list_devices() if d["mac"] == norm), None)
    name = (dev or {}).get("name") or norm
    if month is not None:
        text = await climate.noaa_month_report(norm, name, year, month)
    else:
        text = await climate.noaa_year_report(norm, name, year)
    return PlainTextResponse(text)


@app.post("/api/insights/rebuild", dependencies=[Depends(require_write_token)])
async def rebuild_insights(mac: str | None = Query(None)) -> JSONResponse:
    """Recompute rollups from raw history — run once after enabling INSIGHTS
    on existing data, or after importing history while it was disabled."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await insights.rebuild(_format_mac(mac) if mac else None))


class WUImportIn(BaseModel):
    mac: str
    # Falls back to the device's stored wu_station_map association.
    wu_station_id: str | None = Field(default=None, max_length=32,
                                      pattern=r"^[A-Za-z0-9]+$")
    # Never persisted or logged; lives only in the import task's closure.
    # Optional: when omitted, the server-stored key (PUT /api/config/wu-key,
    # or the WU_API_KEY env var) is used — a LAN user who followed the app's
    # own cleartext-safety advice and configured the key server-side must
    # not be forced to POST it in the body to start an import.
    api_key: str | None = Field(default=None, min_length=8, max_length=128,
                                pattern=r"^[A-Za-z0-9]+$")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    dry_run: bool = False
    # 1.9: re-import days the ledger says are already done (the default
    # skips them — no quota spent, and thinned history stays thinned).
    force: bool = False


@app.post("/api/import/wu", dependencies=[Depends(require_write_token)])
async def start_wu_import(body: WUImportIn) -> JSONResponse:
    """Begin a day-by-day WU history import into an existing device. One at a
    time; poll GET /api/import/wu/status. dry_run counts without inserting."""
    from datetime import date as _date
    from . import wu_import
    from .ingest import _format_mac
    mac = _format_mac(body.mac)
    if not any(d["mac"] == mac for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {mac}")
    assoc = await db.get_wu_station(mac)
    station = ((body.wu_station_id or "").strip().upper()
               or (assoc["station_id"] if assoc else None))
    if not station:
        raise HTTPException(status_code=400,
                            detail="no wu_station_id given and none associated "
                                   "with this device (PUT .../wu-station first)")
    try:
        start = _date.fromisoformat(body.start_date)
        end = _date.fromisoformat(body.end_date) if body.end_date else _date.today()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad date: {e}")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date is after end_date")
    api_key = body.api_key or await effective_wu_key()
    if not api_key:
        raise HTTPException(status_code=400,
                            detail="no api_key given and no server-stored WU "
                                   "key (PUT /api/config/wu-key first)")
    if not wu_import.start_import(mac, station, api_key, start, end,
                                  body.dry_run, force=body.force):
        raise HTTPException(status_code=409, detail="an import is already running")
    return JSONResponse({"ok": True, "mac": mac, "wu_station_id": station,
                         "days": (end - start).days + 1, "dry_run": body.dry_run})


# ── importing somebody else's archive (2.4, item 4) ───────────────────

class ArchiveImportIn(BaseModel):
    """A CSV import. The file rides in the body as text; the mapping says
    what its columns mean, because there is no standard and every export
    names them differently."""
    mac: str
    # The same number as limits.CSV_IMPORT_MAX: the ASGI cap is what
    # bounds an anonymous body, this is what bounds a parsed one.
    csv: str = Field(max_length=16 * 1024 * 1024)
    # column name → our field name. A column we have no home for is
    # refused at the model rather than quietly dropped, so a typo in a
    # mapping is a 400 and not a silently thinner import.
    mapping: dict[str, str] = Field(default_factory=dict, max_length=64)
    time_column: str = Field(default="", max_length=128)
    # strptime pattern, or empty for an epoch in seconds or milliseconds.
    time_format: str = Field(default="", max_length=64)
    units: str = Field(default="us", pattern="^(us|metric|metricwx)$")
    dry_run: bool = False


@app.post("/api/import/csv", dependencies=[Depends(require_write_token)])
async def start_csv_import(body: ArchiveImportIn) -> JSONResponse:
    """Import a CSV export into an existing device.

    Everything exports one: a GW3000's microSD card, Cumulus, Weather
    Display, a spreadsheet somebody keeps by hand. Runs in the
    background, paced; poll GET /api/import/archive/status.

    Up to 16 MiB of CSV in the body (limits.CSV_IMPORT_MAX); a bigger
    history comes through the WeeWX door, which streams to disk.
    """
    from . import archive_import
    from .ingest import _format_mac
    global _ARCHIVE_IMPORT_TASK
    mac = _format_mac(body.mac)
    if not any(d["mac"] == mac for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {mac}")
    if archive_import.status().get("state") == "running":
        raise HTTPException(status_code=409, detail="an import is already running")
    if not body.time_column:
        raise HTTPException(status_code=400,
                            detail="time_column names the column holding the "
                                   "timestamp")
    unknown = sorted(f for f in body.mapping.values()
                     if f not in archive_import.FIELD_KINDS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=("these fields are not readings this server stores: "
                    + ", ".join(unknown)))
    rows = archive_import.csv_rows(
        body.csv, body.mapping,
        system=archive_import.UNIT_SYSTEMS[body.units],
        time_column=body.time_column, time_format=body.time_format)
    _ARCHIVE_IMPORT_TASK = asyncio.create_task(
        archive_import.run_import(mac, rows, kind="csv",
                                  dry_run=body.dry_run, ordered=False))
    return JSONResponse({"ok": True, "mac": mac, "kind": "csv",
                         "dry_run": body.dry_run})


@app.get("/api/import/csv/fields", dependencies=[Depends(require_token)])
async def csv_import_fields() -> JSONResponse:
    """The readings a CSV column can be mapped to (2.4 item 4, the app
    half). Served rather than duplicated: the calibration route hands
    the apps its catalogue for the same reason, so a field added here
    is on the mapping screen without a release."""
    from . import archive_import
    return JSONResponse({
        "fields": [{"field": f, "kind": k}
                   for f, k in sorted(archive_import.FIELD_KINDS.items())],
        "units": sorted(archive_import.UNIT_SYSTEMS),
        "note": ("Map each column to one reading, or leave it out. The time "
                 "column is separate; an epoch needs no format, anything "
                 "else needs a strptime pattern and is read in the "
                 "server's zone."),
    })


@app.post("/api/import/weewx", dependencies=[Depends(require_write_token)])
async def start_weewx_import(
    mac: str,
    request: Request,
    dry_run: bool = False,
) -> JSONResponse:
    """Import a `weewx.sdb` uploaded as the raw request body.

    Streamed to a temp file rather than held in memory: people arrive
    with years of archive, and a decade of five minute records is a file
    of hundreds of megabytes. Bounded at limits.WEEWX_IMPORT_MAX by the
    ASGI layer and by free disk here (R24-02).
    """
    import tempfile
    from pathlib import Path as _Path
    from . import archive_import
    from .ingest import _format_mac
    global _ARCHIVE_IMPORT_TASK
    formatted = _format_mac(mac)
    if not any(d["mac"] == formatted for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {formatted}")
    if archive_import.status().get("state") == "running":
        raise HTTPException(status_code=409, detail="an import is already running")
    tmp = _Path(tempfile.gettempdir()) / f".weewx-{secrets.token_hex(8)}.sdb"
    # Room for the upload before a byte of it lands (R24-02): the restore
    # door asks the same question for the same reason.
    import shutil as _shutil
    from .limits import WEEWX_IMPORT_MAX as _WEEWX_MAX
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    # No usable length (chunked, or a malformed header) reserves the whole
    # limit: the middleware still lets that much through, and a route
    # that reserved 64 MiB for it could fill the temporary filesystem
    # (CodeRabbit, PR #40).
    need = (min(declared, _WEEWX_MAX) if declared > 0 else _WEEWX_MAX) \
        + 64 * 1024 * 1024
    try:
        free = _shutil.disk_usage(tmp.parent).free
    except OSError as e:
        raise HTTPException(status_code=507, detail=f"cannot measure free disk: {e}")
    if free < need:
        raise HTTPException(
            status_code=507,
            detail=f"not enough free disk for the upload: it needs about "
                   f"{need // 2**20} MB and {tmp.parent} has {free // 2**20} MB free")
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="no file was uploaded")
        try:
            summary = archive_import.weewx_summary(str(tmp))
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"that does not look like a weewx.sdb ({e})")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    async def _run() -> None:
        try:
            await archive_import.run_import(
                formatted, archive_import.read_weewx(str(tmp)),
                kind="weewx", dry_run=dry_run)
        finally:
            tmp.unlink(missing_ok=True)

    _ARCHIVE_IMPORT_TASK = asyncio.create_task(_run())
    return JSONResponse({"ok": True, "mac": formatted, "kind": "weewx",
                         "dry_run": dry_run, **summary})


@app.get("/api/import/archive/status", dependencies=[Depends(require_token)])
async def archive_import_status() -> JSONResponse:
    from . import archive_import
    return JSONResponse(archive_import.status())


@app.post("/api/import/archive/cancel",
          dependencies=[Depends(require_write_token)])
async def archive_import_cancel() -> JSONResponse:
    from . import archive_import
    return JSONResponse({"ok": archive_import.cancel()})


@app.get("/api/import/wu/status", dependencies=[Depends(require_token)])
async def wu_import_status() -> JSONResponse:
    from . import wu_import
    return JSONResponse(wu_import.status())


@app.post("/api/import/wu/cancel", dependencies=[Depends(require_write_token)])
async def wu_import_cancel() -> JSONResponse:
    from . import wu_import
    return JSONResponse({"ok": wu_import.cancel()})


# Test-email throttle (R2-111 second half): /api/alerts/test is a real SMTP
# send to every configured recipient, so an unthrottled write-token holder
# could pump unlimited email through the operator's SMTP account (and trip
# provider abuse lockouts with failed logins). One send attempt per process
# per minute is plenty for the app's setup screen. Process-global like
# _AUTH_FAIL_LOG_TS.
# The archive import's task handle (2.4 item 4), so a fire and forget
# task is not garbage collected mid-import.
_ARCHIVE_IMPORT_TASK: "asyncio.Task | None" = None

_TEST_ALERT_TS: float | None = None
_TEST_ALERT_MIN_INTERVAL_S = 60.0


@app.post("/api/alerts/test", dependencies=[Depends(require_shared_write)])
async def test_alert() -> JSONResponse:
    """Send a one-off test email to the current recipients — lets the app's
    setup screen verify delivery end to end. Throttled to one attempt per
    minute (429 on repeats)."""
    import asyncio as _asyncio
    from .alerts import effective_config, _send_sync
    global _TEST_ALERT_TS
    cfg = await effective_config()
    if not cfg.transport_configured:
        raise HTTPException(status_code=400,
                            detail="SMTP transport not configured (set SMTP_HOST + creds as secrets)")
    if not cfg.recipients:
        raise HTTPException(status_code=400, detail="no recipients configured")
    now = time.monotonic()
    if _TEST_ALERT_TS is not None and now - _TEST_ALERT_TS < _TEST_ALERT_MIN_INTERVAL_S:
        raise HTTPException(status_code=429,
                            detail="a test email was just sent — wait a "
                                   "minute before sending another")
    # Marked before the attempt: a FAILED send still hit the SMTP server
    # (repeated bad logins can lock the operator's account), so it counts.
    _TEST_ALERT_TS = now
    try:
        await _asyncio.to_thread(
            _send_sync, "[Zasder Weather] Test alert",
            "This is a test from your Zasder Weather backend — device-down "
            "alerts are wired up correctly.", cfg.recipients, cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"send failed: {e}")
    return JSONResponse({"ok": True, "sent_to": cfg.recipients})


# ───────────────────────── push notifications (APNs) ─────────────────────────

# Test push (2.4, D6). Doren spent 2026-09-19 unable to tell a broken
# push channel from quiet weather — the only way to prove delivery was to
# invent a temporary alert rule with a threshold the weather would cross,
# wait, and delete it afterwards. Email has had `/api/alerts/test` since
# 1.4; push had nothing.
#
# Owner-gated, not shared-write: push follows the DEFAULT site and guests
# never receive it, so a guest token asking for a test push is asking to
# ring somebody else's phone. Throttled exactly like the test email, and
# for the same reason — one attempt per process per minute, marked BEFORE
# the attempt so a failed send still counts against the window.
_TEST_PUSH_TS: float | None = None
_TEST_PUSH_MIN_INTERVAL_S = 60.0
# The wire name of the Live Activity the push-to-start test starts. Its
# own type on purpose: reusing a real card would mean a test that says
# rain is coming. Like every attributes type, this string IS the Swift
# type name and is never renamed.
PUSH_TEST_ACTIVITY = "PushTestActivityAttributes"


class PushTestIn(BaseModel):
    # "alert" proves the plain push path (token, key/relay, the phone's
    # notification settings); "live_activity" proves the separate
    # push-to-start token path, which fails on its own and silently.
    kind: str = "alert"
    # 2.4 item 1: which of the app's two tones to play, so the button
    # that exists to PROVE push also lets someone hear what a warning
    # will sound like against a watch without waiting for weather.
    # Absent means the ordinary notification sound, which is what a test
    # should default to.
    tier: str | None = None


@app.post("/api/push/test", dependencies=[Depends(require_write_token)])
async def push_test(body: PushTestIn | None = None) -> JSONResponse:
    """Send a one-off test notification to this server's registered
    devices — the push half of `/api/alerts/test`."""
    global _TEST_PUSH_TS
    from . import apns
    kind = (body.kind if body else "alert")
    if kind not in ("alert", "live_activity"):
        raise HTTPException(status_code=400,
                            detail="kind must be 'alert' or 'live_activity'")
    if not await apns.push_configured():
        raise HTTPException(
            status_code=400,
            detail="no push channel is configured on this server — add an "
                   "APNs key or a push relay first")
    tier = body.tier if body else None
    if tier is not None and tier not in apns.SOUND_FOR_TIER:
        raise HTTPException(
            status_code=400,
            detail="tier must be one of " + "|".join(sorted(apns.SOUND_FOR_TIER)))
    if kind == "alert":
        targets = len(await db.list_push_tokens())
        what = "device"
    else:
        targets = len(await db.list_live_activity_tokens("start"))
        what = "device with Live Activities"
    if not targets:
        raise HTTPException(
            status_code=400,
            detail=f"no {what} is registered with this server yet — open the "
                   f"app, allow notifications, and try again")
    now = time.monotonic()
    if (_TEST_PUSH_TS is not None
            and now - _TEST_PUSH_TS < _TEST_PUSH_MIN_INTERVAL_S):
        raise HTTPException(status_code=429,
                            detail="a test push was just sent — wait a "
                                   "minute before sending another")
    _TEST_PUSH_TS = now
    now_ms = int(time.time() * 1000)
    title = "Test notification"
    body_text = ("Push from your Zasder Weather server is working. "
                 "Nothing is wrong — you asked for this one.")
    if tier:
        title = f"Test notification ({tier} tone)"
    try:
        if kind == "alert":
            res = await apns.send_to_all(title, body_text, tier=tier)
        else:
            station = "Zasder Weather"
            try:
                devices = await db.list_devices()
                if devices:
                    station = (db.effective_device_name(devices[0])
                               or station)
            except Exception:
                log.warning("test push: device name lookup failed",
                            exc_info=True)
            # Short-lived by construction: the card greys out in two
            # minutes and dismisses itself in five, so a test never needs
            # the operator to clear it. (A start event ignores the
            # dismissal date on some iOS builds — five minutes of a card
            # that says TEST is the worst case, 2.0.1.)
            payload = apns.build_live_activity_start(
                PUSH_TEST_ACTIVITY, {"stationName": station},
                {"sentMs": now_ms}, title, body_text,
                now_s=now_ms // 1000,
                stale_s=(now_ms + 2 * 60_000) // 1000,
                dismiss_s=(now_ms + 5 * 60_000) // 1000)
            res = await apns.send_live_activity_start(payload, title,
                                                      body_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"push failed: {e}")
    sent = int(res.get("sent") or 0)
    out = {"ok": sent > 0, "kind": kind, "sent": sent, "registered": targets,
           "sound": apns.sound_for(tier),
           "failed": int(res.get("failed") or 0),
           "pruned": len(res.get("dead") or [])}
    if not sent:
        # Never a 5xx: the channel answered, it just had nothing it could
        # deliver to. The app shows the reason rather than a red error.
        out["detail"] = ("nothing was delivered — every registered token "
                         "was refused by the push service. Reconnect the "
                         "app's notifications to register a fresh one.")
    return JSONResponse(out)



class PushRegisterIn(BaseModel):
    # Real APNs tokens are 64 hex chars and FCM registration tokens are
    # printable ASCII (base64url + ':') — but neither is multi-KB or carries
    # whitespace/control chars. Bound + shape-check so junk can't accumulate
    # as fake "device tokens" up to the body cap.
    token: str = Field(min_length=8, max_length=512, pattern=r"^[\x21-\x7e]+$")
    env: str | None = None            # "sandbox" (dev build) | "production"
    platform: str = "ios"


@app.post("/api/push/register", dependencies=[Depends(require_shared_write)])
async def push_register(body: PushRegisterIn) -> JSONResponse:
    """The iOS app posts its APNs device token here after the user grants
    notification permission. Idempotent (upsert)."""
    env = body.env if body.env in ("sandbox", "production") else None
    await db.register_push_token(body.token, body.platform, env)
    return JSONResponse({"ok": True})


class PushUnregisterIn(BaseModel):
    token: str | None = Field(default=None, min_length=8, max_length=512,
                              pattern=r"^[\x21-\x7e]+$")
    # R10 U2: the Live Activity tokens this device remembers minting —
    # unregistering only the APNs token left the old server able to start
    # storm/heat Activities on a device that moved on.
    live_activity_tokens: list[str] = Field(default_factory=list,
                                            max_length=64)


@app.post("/api/push/unregister",
          dependencies=[Depends(require_shared_write)])
async def push_unregister(body: PushUnregisterIn) -> JSONResponse:
    """Best-effort mirror of the register endpoints: the app's Disconnect
    & reset calls this before forgetting the server, so the old server
    stops pushing at a device that moved on. Idempotent — unknown tokens
    still answer ok (nothing to stop)."""
    removed = False
    if body.token:
        removed = await db.delete_push_token(body.token)
    la_removed = 0
    for t in body.live_activity_tokens[:64]:
        if isinstance(t, str) and 8 <= len(t) <= 512:
            if await db.delete_live_activity_token(t):
                la_removed += 1
    return JSONResponse({"ok": True, "removed": removed,
                         "live_activity_removed": la_removed})


# The Activities the server drives with per-activity update tokens: the
# 1.7 rain-start countdown, the 1.8 storm and heat cards, and the 2.3
# lightning / wind-ramp / freeze-night cards (item 5).
_LIVE_ACTIVITIES = ("rain", "storm", "heat", "lightning", "wind", "freeze")


class LiveActivityTokenIn(BaseModel):
    # Same bound + shape rationale as PushRegisterIn: ActivityKit tokens
    # are hex, but Apple documents no fixed length — printable ASCII with
    # sane bounds keeps junk out without betting on today's format.
    token: str = Field(min_length=8, max_length=512, pattern=r"^[\x21-\x7e]+$")
    env: str | None = None
    kind: str = "start"
    # 1.8: which Activity an 'update' token belongs to.
    activity: str | None = None


@app.post("/api/push/live-activity-token",
          dependencies=[Depends(require_shared_write)])
async def live_activity_token_register(body: LiveActivityTokenIn) -> JSONResponse:
    """Push-to-start token for the rain-start Live Activity (1.7, nowcast
    phase 2). The app re-posts whenever iOS rotates it; idempotent upsert,
    same lifecycle as /api/push/register."""
    if body.kind not in ("start", "update", "widgets"):
        raise HTTPException(status_code=400,
                            detail="kind must be 'start', 'update' or 'widgets'")
    activity = None
    if body.kind == "update":
        # 1.8: per-activity update tokens for the live-tracking Activities.
        if body.activity not in _LIVE_ACTIVITIES:
            raise HTTPException(
                status_code=400,
                detail="update tokens need activity "
                       + "|".join(_LIVE_ACTIVITIES))
        activity = body.activity
    elif body.kind == "widgets":
        # iOS 26 push-updated widgets: an extension-wide reload token, no
        # activity dimension.
        activity = None
    else:
        # Push-to-start tokens are APP-WIDE — one per device, every
        # attributes type hands the app the same token, so the activity
        # label here is observability only (the lookup ignores it for
        # starts; see db.list_live_activity_tokens, proven live
        # 2026-08-27). "morning" joined in 1.9.
        if body.activity is not None and body.activity not in (
                _LIVE_ACTIVITIES + ("morning",)):
            raise HTTPException(status_code=400, detail="unknown activity")
        activity = body.activity or "rain"
    env = body.env if body.env in ("sandbox", "production") else None
    await db.register_live_activity_token(body.token, body.kind, env,
                                          activity=activity)
    return JSONResponse({"ok": True})


# Shown back by an ALLOW-list (round-three review, SEC info): a field that
# is not named here is treated as a secret whatever it is called, so a
# future `token` or `secret` field cannot leak by default.
_SHARE_SHOWN = {"station_id", "wid", "station"}
_SHARE_FIELDS = {
    "pwsweather": ("station_id", "api_key"),
    # Windy's 2026 API wants the station's own id and password; api_key and
    # station index are the legacy account key, still merged and honoured
    # until Windy retires that path.
    "windy": ("station_id", "password", "api_key", "station"),
    "weathercloud": ("wid", "key"),
    "cwop": ("station_id",),
    # 2.4 item 8. Each one's own two names, and only `station_id` is ever
    # shown back: WOW's key is a six digit PIN and AWEKAS's is an account
    # password, which are exactly the kind of thing the allow-list above
    # exists to keep out of a GET.
    "wow": ("station_id", "api_key"),
    "awekas": ("station_id", "api_key"),
    "openweathermap": ("station_id", "api_key"),
}


@app.get("/api/sharing", dependencies=[Depends(require_write_token)])
async def sharing_status() -> JSONResponse:
    """1.8 upload fan-out: per-target enabled + health. Credentials are
    write-only — the response says whether each is SET, never its value
    (the integrations-sheet rule)."""
    from . import share_targets as st
    out = {}
    for t in st.TARGETS:
        cfg = await st.get_config(t)
        status = await st.get_status(t)
        out[t] = {
            "enabled": bool(cfg.get("enabled")),
            "fields": {f: bool((str(cfg.get(f) or "")).strip())
                       for f in _SHARE_FIELDS[t]},
            "last_ok_ms": status.get("last_ok_ms"),
            "last_error": status.get("last_error"),
            "last_error_ms": status.get("last_error_ms"),
            "interval_min": st.interval_min(t, cfg),
            "min_interval_min": st.MIN_INTERVAL_MIN[t],
            "mac": cfg.get("mac"),
            # The non-secret settings, shown back so the sheet reads as
            # filled in; secrets stay write-only (Volney, 2026-09-06).
            "values": {f: str(cfg.get(f)) for f in _SHARE_FIELDS[t]
                       if f in _SHARE_SHOWN and cfg.get(f) not in (None, "")},
        }
    # Weather Underground is forwarded per station (its own page), but the
    # networks list shows it beside the four, and "sent 3 minutes ago"
    # belongs there too (Doren, 2026-09-06: "is there a notation of a
    # successful send for WU?"). One summary row: on if any station
    # forwards, the newest acceptance, the newest error.
    from . import wu_upload
    wu = {"enabled": False, "fields": {}, "last_ok_ms": None,
          "last_error": None, "last_error_ms": None}
    for assoc in await db.list_wu_stations():
        if not assoc["upload_enabled"]:
            continue
        wu["enabled"] = True
        stats = wu_upload.stats(assoc["mac"])
        ok = stats.get("last_ok_ms")
        if ok is not None and (wu["last_ok_ms"] is None or ok > wu["last_ok_ms"]):
            wu["last_ok_ms"] = ok
        if stats.get("last_error") and wu["last_error"] is None:
            wu["last_error"] = stats["last_error"]
    out["wu"] = wu
    return JSONResponse(out)


class SharePut(BaseModel):
    enabled: bool | None = None
    station_id: str | None = Field(default=None, max_length=64)
    api_key: str | None = Field(default=None, max_length=128)
    # -1 = clear (R8 S2): an int field can never arrive as the "" the
    # merge loop treats as clear, so a set station index was un-removable.
    station: int | None = Field(default=None, ge=-1, le=255)
    wid: str | None = Field(default=None, max_length=64)
    key: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=128)
    # Minutes between sends; clamped to the network's floor server-side.
    # -1 clears the setting (back to the network's default), like `station`.
    interval_min: int | None = Field(default=None, ge=-1, le=60)
    # Which station this network publishes; "" = the first weather station.
    mac: str | None = Field(default=None, max_length=32)


@app.put("/api/sharing/{target}", dependencies=[Depends(require_write_token)])
async def sharing_put(target: str, body: SharePut) -> JSONResponse:
    """Merge-per-field like the integrations PUT: omitted = unchanged,
    "" clears. Enabling with missing credentials is refused loudly —
    a target that can only fail is worse than one that is off."""
    from . import share_targets as st
    if target not in st.TARGETS:
        raise HTTPException(status_code=404, detail="unknown target")
    cfg = await st.get_config(target)
    for f in _SHARE_FIELDS[target]:
        v = getattr(body, f, None)
        if v is not None:
            if f == "station" and v == -1:
                cfg.pop(f, None)
                continue
            sv = str(v).strip()
            if sv:
                cfg[f] = v if f == "station" else sv
            else:
                cfg.pop(f, None)
    if body.mac is not None:
        from .ingest import _format_mac
        if body.mac.strip():
            norm = _format_mac(body.mac)
            device = next((d for d in await db.list_devices()
                           if str(d.get("mac") or "").upper() == norm), None)
            if device is None:
                raise HTTPException(status_code=400,
                                    detail=f"{norm} is not a station this server knows")
            if db.is_air_monitor_device(device):
                raise HTTPException(status_code=400,
                                    detail="an air monitor's readings are not outdoor "
                                           "weather; pick a weather station")
            cfg["mac"] = norm
        else:
            cfg.pop("mac", None)
    if body.interval_min is not None:
        if body.interval_min == -1:
            cfg.pop("interval_min", None)
        elif body.interval_min == 0:
            raise HTTPException(status_code=422, detail="interval_min must be 1..60, or -1 to clear")
        else:
            cfg["interval_min"] = max(st.MIN_INTERVAL_MIN[target],
                                      min(st.MAX_INTERVAL_MIN, int(body.interval_min)))
    if body.enabled is not None:
        if body.enabled:
            if target == "windy":
                # Either credential shape enables: id + password (2026
                # API) or the legacy account key.
                missing = ([] if st.windy_is_v2(cfg) or str(cfg.get("api_key") or "").strip()
                           else ["station_id", "password"])
            else:
                missing = [f for f in _SHARE_FIELDS[target]
                           if f != "station" and not str(cfg.get(f) or "").strip()]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"missing credentials: {', '.join(missing)}")
        cfg["enabled"] = body.enabled
    await st.set_config(target, cfg)
    return JSONResponse({"ok": True, "enabled": bool(cfg.get("enabled"))})


# Save-and-verify sends per target: one every SHARE_TEST_EVERY_S, so a
# held-down button cannot hammer a network that feeds NOAA (round-three
# review SEC-G6). Reset per test in conftest.
_SHARE_TEST_LAST: dict[str, float] = {}
SHARE_TEST_EVERY_S = 30.0


@app.post("/api/sharing/{target}/test", dependencies=[Depends(require_write_token)])
async def sharing_test(target: str) -> JSONResponse:
    """Send one report now and say what the network answered (the Save
    and verify button). Cadence is ignored for this one send; the
    staleness gate is not (a dead station's last reading is never
    "verified" onto a network); the status row is stamped so the list
    agrees with the sheet."""
    from . import share_targets as st
    if target not in st.TARGETS:
        raise HTTPException(status_code=404, detail="unknown target")
    cfg = await st.get_config(target)
    if not cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="turn the upload on first")
    now_mono = time.monotonic()
    if now_mono - _SHARE_TEST_LAST.get(target, -1e9) < SHARE_TEST_EVERY_S:
        raise HTTPException(status_code=429,
                            detail=f"one verify per {int(SHARE_TEST_EVERY_S)} s per network")
    _SHARE_TEST_LAST[target] = now_mono
    devices = await db.list_devices()
    result = await st.send_once(target, devices, int(time.time() * 1000))
    return JSONResponse(result)


class WebhookIn(BaseModel):
    url: str = Field(min_length=12, max_length=500)


@app.post("/api/webhooks", dependencies=[Depends(require_write_token)])
async def webhook_create(body: WebhookIn) -> JSONResponse:
    """1.8 outbound webhooks. The secret is returned ONCE, at creation —
    the row never reveals it again (same one-shot rule as tokens)."""
    from . import webhooks as wh
    try:
        # Threadpool: the validator resolves DNS (socket.getaddrinfo, a
        # blocking call with no timeout) — on the event loop it would stall
        # every request behind a slow resolver (CodeRabbit, PR #32).
        # Starlette's pool, NOT asyncio.to_thread: the default executor's
        # worker outlives the TestClient loop and interpreter shutdown then
        # joins it forever — the suite hung at exit ~half the time.
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(wh.validate_webhook_url, body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    row = await db.create_webhook(body.url)
    return JSONResponse(row)


class WebhookPatch(BaseModel):
    enabled: bool


@app.patch("/api/webhooks/{hook_id}",
           dependencies=[Depends(require_write_token)])
async def webhook_patch(hook_id: str, body: WebhookPatch) -> JSONResponse:
    if not await db.set_webhook_enabled(hook_id, body.enabled):
        raise HTTPException(status_code=404, detail="webhook not found")
    return JSONResponse({"ok": True, "id": hook_id, "enabled": body.enabled})


@app.get("/api/webhooks", dependencies=[Depends(require_write_token)])
async def webhook_list() -> JSONResponse:
    rows = await db.list_webhooks()
    return JSONResponse({"webhooks": [
        {"id": r["id"], "url": r["url"], "enabled": bool(r["enabled"]),
         "created_ms": r["created_ms"], "last_ok_ms": r["last_ok_ms"],
         "last_error": r["last_error"]} for r in rows]})


@app.delete("/api/webhooks/{wid}", dependencies=[Depends(require_write_token)])
async def webhook_delete(wid: str) -> JSONResponse:
    if not await db.delete_webhook(wid):
        raise HTTPException(status_code=404, detail="unknown webhook")
    return JSONResponse({"ok": True})


class StormWatchStartIn(BaseModel):
    mac: str = Field(min_length=1, max_length=64)


@app.post("/api/storm/watch/start",
          dependencies=[Depends(require_shared_write)])
async def storm_watch_start(body: StormWatchStartIn) -> JSONResponse:
    """1.8 Storm Watch manual trigger (Volney: light onset sits below the
    rain threshold exactly when you're watching the sky). Opens a real
    storm episode at NOW — the summary covers from this mark, the Live
    Activity starts on the next monitor tick, and the normal quiet-window
    machinery closes it (a false alarm self-cleans as a silent close)."""
    devices = await db.list_devices()
    device = next((d for d in devices if d["mac"] == body.mac), None)
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    # Global toggle first: with storm summaries off, on_open_tick and the
    # summary checker both no-op — the endpoint would 200, open an episode,
    # and nothing would ever start or close it (R7 watch finding 6).
    from .alerts import effective_config
    if not (await effective_config()).storm_summary:
        raise HTTPException(
            status_code=409,
            detail="storm summaries are turned off — enable them under "
                   "Alerts & Notifications first")
    # Honor the per-station opt-out: a station whose storm summaries are
    # switched off must not be startable by hand either — the summary and
    # Live Activity machinery would run for a station the user silenced
    # (CodeRabbit, PR #32).
    prefs = await db.get_device_alert_prefs()
    if (prefs.get(body.mac) or {}).get("storm_summary") is False:
        raise HTTPException(
            status_code=409,
            detail="storm summaries are turned off for this station")
    from . import storm_watch
    res = await storm_watch.manual_start(body.mac, device)
    return JSONResponse(res)


class PushRelayIn(BaseModel):
    # Both optional: omit a field to leave it unchanged, send "" to clear it.
    relay_url: str | None = None
    relay_token: str | None = None


def _validate_relay_url(url: str) -> None:
    """Reject relay URLs that could be used to exfiltrate APNs device tokens
    via SSRF (reviewer P3). https only; refuse loopback/private/link-local IP
    literals. Hostnames pass through — DNS-rebinding mitigation belongs at the
    egress layer, not here."""
    import ipaddress
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="relay_url is not a valid URL")
    if u.scheme != "https":
        raise HTTPException(status_code=400, detail="relay_url must be https://")
    host = (u.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="relay_url is missing a host")
    if host in ("localhost", "ip6-localhost", "broadcasthost"):
        raise HTTPException(status_code=400,
                            detail="relay_url cannot point at a local address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return                                    # hostname (not an IP) → OK
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise HTTPException(status_code=400,
                            detail="relay_url cannot point at a private/local address")


@app.get("/api/push/relay", dependencies=[Depends(require_token)])
async def get_push_relay() -> JSONResponse:
    """Report the app-managed relay config. The token is WRITE-ONLY — never
    returned; only whether one is set + the effective enabled state."""
    from .apns import effective_relay
    cfg = await db.get_push_relay() or {}
    url, token = await effective_relay()
    return JSONResponse({"relay_url": cfg.get("url"),
                         "relay_token_set": bool(cfg.get("token")),
                         "relay_configured": bool(url and token)})


@app.put("/api/push/relay", dependencies=[Depends(require_write_token)])
async def put_push_relay(body: PushRelayIn) -> JSONResponse:
    """The iOS app stores the relay token it obtained (via App Attest against
    the relay) here so this backend can push through the relay. Write-only
    token, same pattern as SMTP creds."""
    cur = await db.get_push_relay() or {}
    url = cur.get("url")
    if body.relay_url is not None:
        if body.relay_url:
            _validate_relay_url(body.relay_url)
        url = body.relay_url or None
    token = cur.get("token")
    if body.relay_token is not None:
        token = body.relay_token or None
    await db.set_push_relay(url, token)
    return JSONResponse({"ok": True, "relay_url": url,
                         "relay_configured": bool(url and token)})


# ───────────────────────── threshold alert rules ─────────────────────────

class AlertRuleIn(BaseModel):
    field: str
    comparator: str
    threshold: float
    target_mac: str | None = None     # None = any device
    severity: str = "minor"           # minor | standard | major | urgent (major 1.9)
    # 2.3: a line the owner types, appended to the push ("Park the 115H").
    note: str | None = Field(default=None, max_length=400)


@app.get("/api/alerts/rules", dependencies=[Depends(require_token)])
async def list_rules() -> JSONResponse:
    return JSONResponse(await db.list_alert_rules())


@app.post("/api/alerts/rules", dependencies=[Depends(require_shared_write)])
async def create_rule(body: AlertRuleIn) -> JSONResponse:
    from .alerts import THRESHOLD_FIELDS, THRESHOLD_COMPARATORS
    from .ingest import _format_mac
    if body.field not in THRESHOLD_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"unknown field {body.field!r}; allowed: {sorted(THRESHOLD_FIELDS)}")
    if body.comparator not in THRESHOLD_COMPARATORS:
        raise HTTPException(status_code=400,
                            detail=f"comparator must be one of {sorted(THRESHOLD_COMPARATORS)}")
    # R6: same guard PATCH has. Starlette's JSON parser accepts the
    # non-standard Infinity/NaN literals and pydantic's float admits them;
    # an Infinity threshold STORED (sqlite keeps inf as REAL) and then
    # 500'd every GET /api/alerts/rules forever — JSONResponse serializes
    # with allow_nan=False, and the list is the only way to learn the id
    # to delete. One bad authenticated request bricked the alerts screen.
    if not math.isfinite(body.threshold):
        raise HTTPException(status_code=400,
                            detail="threshold must be a finite number")
    from .alerts import RULE_SEVERITIES
    if body.severity not in RULE_SEVERITIES:
        raise HTTPException(status_code=400,
                            detail=f"severity must be one of {list(RULE_SEVERITIES)}")
    from .alerts import clean_rule_note
    mac = _format_mac(body.target_mac) if body.target_mac else None
    rule = await db.create_alert_rule(mac, body.field, body.comparator,
                                      body.threshold, severity=body.severity,
                                      note=clean_rule_note(body.note))
    return JSONResponse(rule)


class AlertRulePatch(BaseModel):
    # All optional (1.7): omit = leave unchanged. Pre-1.7 clients that send
    # only {"enabled": …} keep working; a 1.7 app editing a rule sends
    # threshold and/or target_mac ("" = back to all devices).
    enabled: bool | None = None
    threshold: float | None = None
    target_mac: str | None = None
    severity: str | None = None       # minor | standard | major | urgent (major 1.9)
    note: str | None = Field(default=None, max_length=400)   # "" clears (2.3)


@app.patch("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_shared_write)])
async def patch_rule(rule_id: int, body: AlertRulePatch) -> JSONResponse:
    import math
    from .ingest import _format_mac
    if body.threshold is not None and not math.isfinite(body.threshold):
        raise HTTPException(status_code=400, detail="threshold must be finite")
    if body.severity is not None:
        from .alerts import RULE_SEVERITIES
        if body.severity not in RULE_SEVERITIES:
            raise HTTPException(status_code=400,
                                detail=f"severity must be one of {list(RULE_SEVERITIES)}")
    set_target = body.target_mac is not None
    tgt = (_format_mac(body.target_mac)
           if set_target and body.target_mac != "" else None)
    from .alerts import clean_rule_note
    rule = await db.update_alert_rule(rule_id, enabled=body.enabled,
                                      threshold=body.threshold,
                                      target_mac=tgt, set_target=set_target,
                                      severity=body.severity,
                                      note=clean_rule_note(body.note),
                                      set_note=body.note is not None)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return JSONResponse(rule)


@app.delete("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_shared_write)])
async def delete_rule(rule_id: int) -> JSONResponse:
    if not await db.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail="rule not found")
    return JSONResponse({"ok": True, "deleted": rule_id})


@app.delete("/api/devices/{mac}", dependencies=[Depends(require_write_token)])
async def delete_device(mac: str) -> JSONResponse:
    """Remove a device + all its observations + alert state. Useful after
    retiring a source (e.g. you stopped polling a cloud feed) so a stale
    device doesn't sit on the dashboard. Returns a count summary."""
    from .ingest import _format_mac
    counts = await db.delete_device(_format_mac(mac))
    if counts["devices"] == 0:
        raise HTTPException(status_code=404, detail="device not found")
    return JSONResponse({"ok": True, "deleted_mac": _format_mac(mac), **counts})


async def _fill_rain_periods(mac: str, obs: dict[str, Any]) -> None:
    """Rain rollup enrichment: fill period totals the source doesn't post.
    SDR posts only yearlyrainin (2.3: the day, week, month and year are
    read from daily_rollups when the ledger can answer them, the counter
    differenced at the boundary otherwise — see db.rain_rollups), the
    Tempest posts only hourly+daily (summed per-day, rain_rollups tier 3 —
    before that tier, the old yearlyrainin-only gate here meant a Tempest
    dashboard simply had no week/month/year). AWN-sourced rows ship every
    bucket pre-computed and the fill-only-None leaves them untouched. Gated
    on SOME rain counter being present: a station with no rain sensor at
    all must stay absent everywhere, not gain zeros. Shared by /current and
    the public dashboard's rain-periods board."""
    has_rain_counter = any(obs.get(k) is not None for k in
                           ("yearlyrainin", "monthlyrainin", "dailyrainin"))
    if not has_rain_counter or not any(
        obs.get(k) is None for k in
        ("dailyrainin", "hourlyrainin", "weeklyrainin", "monthlyrainin",
         "yearlyrainin")
    ):
        return
    # A source that posts ONLY a yearly counter — no daily, no monthly — is
    # an SDR/LilyGO sensor, and that counter is the sensor's LIFETIME total
    # (a WH24 read 18.47" in a 1.6" year). Decided before the fill below
    # adds the buckets the source lacks. Every other shape (AWN, Davis,
    # Ecowitt, Tempest) posts a yearly that resets on Jan 1 or none at all.
    lifetime_counter = (obs.get("yearlyrainin") is not None
                        and obs.get("monthlyrainin") is None
                        and obs.get("dailyrainin") is None)
    try:
        rollups = await db.rain_rollups(mac, settings.timezone)
    except Exception as e:
        log.warning("rain_rollups failed for %s: %s", mac, e)
        rollups = {}
    for k, v in (("dailyrainin",   rollups.get("daily_in")),
                  ("hourlyrainin",  rollups.get("hourly_in")),
                  ("weeklyrainin",  rollups.get("weekly_in")),
                  ("monthlyrainin", rollups.get("monthly_in")),
                  ("yearlyrainin",  rollups.get("yearly_in"))):
        if obs.get(k) is None and v is not None:
            obs[k] = v
    # Lifetime counter: the YEAR bucket becomes the counter differenced
    # against Jan 1 (or the first reading on file), and the raw counter
    # moves to `totalrainin` — Ambient's own name for a lifetime total —
    # so nothing is lost and a reader can tell the two apart. Read-side
    # only: the 2026-08-11 ingest offset rewrote stored history and broke
    # rollups and records; this touches the served dict and nothing else.
    if lifetime_counter and rollups.get("yearly_in") is not None:
        obs["totalrainin"] = obs["yearlyrainin"]
        obs["yearlyrainin"] = rollups["yearly_in"]


def _strip_observation_pii(obs: dict[str, Any]) -> dict[str, Any]:
    """The reading for a read-only token. `_source` is the poster's own
    payload kept verbatim, and for a cloud-polled station (Tempest,
    AirGradient) it carries the station's name and exact coordinates —
    the fields _strip_device_pii hides on /api/devices. The apps never
    read it; it exists for the owner's debugging (CodeRabbit, PR #36)."""
    return {k: v for k, v in obs.items() if k != "_source"}


@app.get("/api/devices/{mac}/current", dependencies=[Depends(require_token)])
async def get_current(
    mac: str,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    # Read-side MAC normalization: storage keys are the uppercase colonized
    # form. Write endpoints already normalize; without the same here a
    # lowercase/compact MAC from a script 404s while the uppercase works.
    from .ingest import _format_mac
    mac = _format_mac(mac)
    obs = await db.latest_observation(mac)
    if not obs:
        raise HTTPException(status_code=404, detail="no data for device")
    await _fill_rain_periods(mac, obs)
    if _is_limited_read(authorization):
        obs = _strip_observation_pii(obs)
    return JSONResponse(obs)


class CalibrationIn(BaseModel):
    """A station's corrections, replaced wholesale. An empty object
    clears them, which is the only way to undo one and has to be
    possible."""
    calibration: dict[str, float] = Field(default_factory=dict, max_length=64)


@app.get("/api/devices/{mac}/calibration",
         dependencies=[Depends(require_token)])
async def get_calibration(mac: str) -> JSONResponse:
    """What this station's readings are being corrected by (2.4 item 7).

    The catalogue rides along so an app never keeps its own copy of which
    fields can be corrected or by how much.
    """
    from . import calibration as _cal
    from .ingest import _format_mac
    formatted = _format_mac(mac)
    table = await db.get_calibration(formatted)
    return JSONResponse({
        "mac": formatted,
        "calibration": table,
        "applied": [_cal.describe(f, v) for f, v in sorted(table.items())],
        "fields": [{"field": f, "kind": kind} for f, kind
                   in sorted(_cal.CALIBRATABLE.items())],
        "offset_limit": _cal.OFFSET_LIMIT,
        "scale_range": [_cal.SCALE_MIN, _cal.SCALE_MAX],
        # Said out loud on the route itself, because it is the one thing
        # about this feature somebody can be surprised by.
        "note": ("Corrections apply to readings from now on. History "
                 "already stored keeps the numbers it was stored with."),
    })


@app.put("/api/devices/{mac}/calibration",
         dependencies=[Depends(require_write_token)])
async def put_calibration(mac: str, body: CalibrationIn) -> JSONResponse:
    from . import calibration as _cal
    from .ingest import _format_mac
    formatted = _format_mac(mac)
    # A field nobody can correct, or a correction out of bounds, is a 400
    # rather than a silent drop: a typo that quietly does nothing is
    # worse than one that says so.
    bad = []
    for field, value in body.calibration.items():
        kind = _cal.CALIBRATABLE.get(field)
        if kind is None:
            bad.append(f"{field} is not a correctable reading")
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            bad.append(f"{field} is not a number")
            continue
        if kind == "offset" and abs(v) > _cal.OFFSET_LIMIT:
            bad.append(f"{field} offset must be within "
                       f"±{_cal.OFFSET_LIMIT:g}")
        elif kind == "scale" and not (_cal.SCALE_MIN <= v <= _cal.SCALE_MAX):
            bad.append(f"{field} scale must be between {_cal.SCALE_MIN:g} "
                       f"and {_cal.SCALE_MAX:g}")
    if bad:
        raise HTTPException(status_code=400, detail="; ".join(bad))
    if not await db.set_calibration(formatted, body.calibration):
        raise HTTPException(status_code=404, detail=f"unknown device {formatted}")
    table = await db.get_calibration(formatted)
    return JSONResponse({"ok": True, "mac": formatted, "calibration": table,
                         "applied": [_cal.describe(f, v)
                                     for f, v in sorted(table.items())]})


@app.get("/api/devices/{mac}/derived", dependencies=[Depends(require_token)])
async def get_derived(mac: str) -> JSONResponse:
    """Derived metrics from the latest observation (1.8, Pillar C): wet
    bulb, Delta-T, frost point, fire-weather indices, density altitude,
    the WMO pressure tendency, and what the barometer thinks (Zambretti).
    Fields appear only when their inputs exist — absent is not zero."""
    from . import derived
    from .ingest import _format_mac
    mac = _format_mac(mac)
    obs = await db.latest_observation(mac)
    if not obs:
        raise HTTPException(status_code=404, detail="no data for device")

    def n(key):
        v = obs.get(key)
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    t, rh = n("tempf"), n("humidity")
    out: dict[str, Any] = {}

    def put(key, v, digits=1):
        if v is not None:
            out[key] = round(v, digits)

    # ── 2.4 item 6 ────────────────────────────────────────────────
    # Instantaneous first, from the reading already in hand.
    put("vapourPressureDeficitKpa",
        derived.vapour_pressure_deficit_kpa(t, rh), 2)
    put("humidex", derived.humidex(t, rh))
    put("apparentTemperatureF",
        derived.apparent_temperature_f(t, rh, n("windspeedmph")))
    aqi = derived.aqi_pm25(n("pm25"))
    if aqi is not None:
        # The EPA's breakpoints are defined on a 24 HOUR average and this
        # is one reading, which is what every consumer monitor shows and
        # is NOT what the EPA publishes. The window is named so whatever
        # draws it cannot quietly imply otherwise.
        out["aqiPm25"] = {"aqi": aqi[0], "category": aqi[1],
                          "window": "instant"}
    put("wetBulbF", derived.wet_bulb_f(t, rh))
    put("deltaTC", derived.delta_t_c(t, rh))
    put("frostPointF", derived.frost_point_f(t, rh))
    put("fosbergFwi", derived.fosberg_fwi(t, rh, n("windspeedmph")), 0)
    put("chandlerBurningIndex", derived.chandler_burning_index(t, rh), 0)
    dew = n("dewPoint") if obs.get("dewPoint") is not None \
        else derived.dew_point_f(t, rh)
    put("densityAltitudeFt",
        derived.density_altitude_ft(t, dew, n("baromabsin")), 0)
    # Fair weather cumulus only, which is why the key says what it is
    # rather than calling itself "cloud height".
    put("cumulusBaseFt", derived.cloud_base_ft(t, dew), 0)

    # Barometer story: 3h tendency + Zambretti from sea-level pressure.
    slp = n("baromrelin")
    obs_ms = obs.get("dateutc")
    if slp is not None and isinstance(obs_ms, (int, float)):
        # Freshness floor (R7 R4): a "3h" tendency whose anchor predates a
        # multi-hour outage is mislabeled and feeds Zambretti garbage.
        past = await db.value_at_or_before(mac, "baromrelin",
                                           int(obs_ms) - 3 * 3_600_000,
                                           max_age_ms=3 * 3_600_000)
        if past is not None:
            delta = slp - past
            tend = derived.pressure_tendency_code(delta)
            if tend is not None:
                code, word = tend
                out["pressureTendency"] = {
                    "code": code, "word": word,
                    "delta3hInHg": round(delta, 3),
                }
                says = derived.zambretti(slp * 33.8639, word)
                if says is not None:
                    out["barometerSays"] = says

    # ── the ones that accumulate (2.4 item 6) ─────────────────────────
    #
    # Wind run, sunshine, chill hours and evapotranspiration are not
    # readings, they are the day so far added up. Computed from TODAY in
    # the station's own zone, from the raw window so the numbers are the
    # station's own samples rather than an average of averages.
    #
    # Every one of them is omitted rather than reported as zero when the
    # sensor it needs is absent: a station with no solar head has no
    # sunshine hours, which is not the same as a day with no sun.
    try:
        await _accumulated_today(mac, obs, out)
    except Exception:
        log.exception("accumulated derived metrics failed for %s", mac)
    return JSONResponse(out)


async def _accumulated_today(mac: str, obs: dict[str, Any],
                             out: dict[str, Any]) -> None:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    # Bound HERE, like get_derived binds it: this function is module
    # level and never had it, so the first reference raised NameError,
    # the caller's except swallowed it, and windRunMiToday, chillHours-
    # Today, sunshineHoursToday and evapotranspirationInToday never
    # appeared for any station while the route answered 200. The test
    # only checked that keys were ABSENT for a station without the
    # sensor, which a broken function also satisfies (CodeRabbit, PR #40).
    from . import derived
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    now_ms = int(time.time() * 1000)
    local = _dt.fromtimestamp(now_ms / 1000, tz)
    midnight = int(local.replace(hour=0, minute=0, second=0,
                                 microsecond=0).timestamp() * 1000)
    # RAW rows, paged, the way changes.assemble reads its window:
    # db.history buckets any window over six hours into 1-minute
    # averages keyed `dateutc` rather than `dateutc_ms`, so from 06:00 on
    # every accumulation below found nothing to sum (CodeRabbit, PR #40,
    # and the 2.4 review).
    # Paged to the end of the day (R24-04): a 20 000-row stop summed
    # twelve hours of two-second posts to 111 miles of a 120-mile run.
    rows = await db.observation_window(mac, midnight, now_ms)
    if not rows:
        return

    def num(row, key):
        v = row.get(key)
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    # Raw rows carry `dateutc_ms`; the bucketed history this read before
    # carried `dateutc`, and nothing here ever matched.
    winds = [(int(r["dateutc_ms"]), num(r, "windspeedmph")) for r in rows
             if r.get("dateutc_ms") is not None
             and num(r, "windspeedmph") is not None]
    run = derived.wind_run_mi(winds)
    if run is not None:
        out["windRunMiToday"] = round(run, 1)

    # Sunshine and ET both want the sun's elevation for the sample's own
    # moment, so they share one pass.
    lat, lon = None, None
    device = next((d for d in await db.list_devices() if d["mac"] == mac), None)
    coords = (((device or {}).get("info") or {}).get("coords") or {}).get("coords") or {}
    try:
        lat, lon = float(coords.get("lat")), float(coords.get("lon"))
    except (TypeError, ValueError):
        lat = lon = None

    sunshine_ms = 0
    et_in = 0.0
    saw_solar = saw_et = False
    chill = 0.0
    saw_chill = False
    previous_ms = None
    for row in rows:
        ts = row.get("dateutc_ms")
        if not isinstance(ts, (int, float)):
            continue
        ts = int(ts)
        gap_h = 0.0 if previous_ms is None else (ts - previous_ms) / 3_600_000
        previous_ms = ts
        if gap_h <= 0 or gap_h > 1.0:
            gap_h = 0.0

        temp = num(row, "tempf")
        if temp is not None and gap_h:
            hour = derived.chill_hours_utah(temp)
            if hour is not None:
                chill += hour * gap_h
                saw_chill = True

        solar = num(row, "solarradiation")
        if solar is not None and lat is not None and lon is not None:
            clear = derived.clear_sky_wm2(ts, lat, lon)
            lit = derived.is_sunshine(solar, clear)
            if lit is not None:
                saw_solar = True
                if lit and gap_h:
                    sunshine_ms += int(gap_h * 3_600_000)
            if gap_h:
                et = derived.evapotranspiration_in(
                    temp, num(row, "humidity"), num(row, "windspeedmph"),
                    solar, num(row, "baromabsin") or num(row, "baromrelin"),
                    hours=gap_h)
                if et is not None:
                    et_in += et
                    saw_et = True

    if saw_chill:
        out["chillHoursToday"] = round(chill, 1)
    if saw_solar:
        out["sunshineHoursToday"] = round(sunshine_ms / 3_600_000, 1)
    if saw_et:
        # A Davis console computes its own ET and that one wins where it
        # exists; this is the estimate for everybody else, and it says so.
        out["evapotranspirationInToday"] = round(et_in, 3)
        out["evapotranspirationSource"] = "penman-monteith"
@app.get("/api/devices/{mac}/export.csv",
         dependencies=[Depends(require_token)])
async def export_csv(mac: str, start_ms: int | None = None,
                     end_ms: int | None = None) -> StreamingResponse:
    """CSV export (1.8, Pillar B) — the non-negotiable data-ownership
    baseline: every stored column for a station over a date range,
    streamed so a decade of rows never has to fit in memory. Defaults to
    the last 30 days; empty cells are empty, never zero."""
    from .ingest import _format_mac
    mac_n = _format_mac(mac)
    now_ms = int(time.time() * 1000)
    end = int(end_ms) if end_ms is not None else now_ms
    start = int(start_ms) if start_ms is not None else end - 30 * 86_400_000
    if start >= end:
        raise HTTPException(status_code=400, detail="start_ms must precede end_ms")

    cols = await db.observation_columns()

    async def rows():
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["timestamp_utc"] + cols)
        yield buf.getvalue()
        cursor = start
        while True:
            batch = await db.observation_rows(mac_n, cursor, end, limit=5000)
            if not batch:
                break
            buf = _io.StringIO()
            w = _csv.writer(buf)
            for r in batch:
                ts = datetime.fromtimestamp(
                    r["dateutc_ms"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                w.writerow([ts] + [
                    "" if r.get(c) is None else r[c] for c in cols])
            yield buf.getvalue()
            cursor = batch[-1]["dateutc_ms"] + 1

    # The filename lands in a quoted Content-Disposition header, and
    # _format_mac passes non-MAC input through unchanged — strip anything
    # that could escape the quotes (CodeRabbit, PR #32).
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", mac_n.replace(":", ""))[:32]
    fname = f"zasder-{safe_id}-{start}-{end}.csv"
    return StreamingResponse(rows(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/devices/{mac}/history", dependencies=[Depends(require_token)])
async def get_history(
    mac: str,
    # 31 days + 1 h, not 30: Explore requests whole calendar months and July
    # is 744 hours — the 720 cap 422'd every 31-day month. The extra hour is
    # for a 31-day month spanning a DST fall-back transition (US November,
    # EU October), which is 745 ABSOLUTE hours; at exactly 744 the app's
    # whole-month request 422'd every year in those zones.
    hours: int = Query(24, ge=1, le=24 * 31 + 1),
    limit: int = Query(2000, ge=1, le=10_000),
    # Optional window END (epoch ms). Default = now, preserving the original
    # trailing-window behavior; the History/Explore browser passes a past
    # month's end to page through imported archives.
    end_ms: int | None = Query(None, ge=0),
) -> JSONResponse:
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    rows = await db.history(mac, start, end, limit=limit)
    return JSONResponse({"start": start, "end": end, "count": len(rows), "rows": rows})


# ───────────────────────── the shared station map (2.3) ─────────────────

class MapSharePut(BaseModel):
    enabled: bool | None = None
    mac: str | None = None                 # "" = the first weather station
    name_visible: bool | None = None
    # exact | area | city (09-14). No typed link: the only visit link is
    # the server's own public page, and only while that page is on.
    location_precision: str | None = None
    visit_public_page: bool | None = None
    # direct | id | none — how the map may reach this server from its pin.
    link_mode: str | None = None


@app.get("/api/map", dependencies=[Depends(require_write_token)])
async def get_map_share() -> JSONResponse:
    """The owner's map switch, its status, and a preview of exactly what a
    stranger would see, so the choice is made with the beacon in view."""
    from . import map_beacon as mb, share_targets as st
    cfg = await mb.get_config()
    status = await mb.get_status()
    devices = await db.list_devices()
    station = st.station_for(cfg, devices)
    preview = None
    page_on = await mb.public_page_enabled()
    page_url = mb.public_page_url() if page_on else None
    if station is not None:
        preview = mb.build(server_id=await ensure_server_id(), station=station,
                           cfg=cfg, now_ms=int(time.time() * 1000),
                           visit_url=await mb.resolved_visit_url(cfg),
                           region=await mb.region_hint())
    return JSONResponse({
        "enabled": bool(cfg.get("enabled")),
        "mac": cfg.get("mac"),
        "name_visible": bool(cfg.get("name_visible")),
        "location_precision": mb.precision_of(cfg),
        "precisions": list(mb.PRECISIONS),
        # The public page: whether it is on, its address if the server
        # can know it, and whether the owner chose to link it. The app
        # shows the link switch only when the page is on AND addressable.
        "public_page_enabled": page_on,
        "public_page_url": page_url,
        "visit_public_page": bool(cfg.get("visit_public_page")),
        # How the pin may reach this server, the id the directory assigned
        # for `id` mode, and the link it actually shows. The id arrives on
        # the directory's reply to a beacon, so it is None until the first
        # one lands.
        "link_mode": mb.link_mode_of(cfg),
        "link_modes": list(mb.LINK_MODES),
        "public_id": status.get("public_id"),
        "public_visit_url": status.get("visit"),
        "region_hint": await mb.region_hint(),
        "directory_url": mb.directory_url(),
        "fuzz_km": mb.FUZZ_KM,
        "city_km": mb.CITY_KM,
        "last_ok_ms": status.get("last_ok_ms"),
        "last_error": status.get("last_error"),
        "last_error_ms": status.get("last_error_ms"),
        "preview": preview,
        # The server's map signing key (public half) and whether a
        # rotation is still waiting for the directory's acknowledgement.
        "pubkey": mb.public_key_b64(await mb.ensure_key()),
        "rotation_pending": await mb.rotation_pending(),
        # F06: withdrawals the directory has not taken yet, retried from
        # the tick. PUT says so once in a sentence; this is the durable
        # state, so reopening the app still sees the old pin may stand.
        "pending_withdraw": [p["mac"] for p in await mb.pending_withdrawals()
                             if isinstance(p.get("mac"), str)],
    })


# Serializes PUT /api/map (R23): the handler reads the config, awaits
# the device list, then writes the whole dict back, so two overlapping
# partial updates lost whichever field the earlier one set. Lazy, and
# reset per test, like _PUBLIC_DASH_LOCK.
_MAP_CFG_LOCK: asyncio.Lock | None = None


@app.put("/api/map", dependencies=[Depends(require_write_token)])
async def put_map_share(body: MapSharePut) -> JSONResponse:
    global _MAP_CFG_LOCK
    if _MAP_CFG_LOCK is None:            # no await between test and assignment
        _MAP_CFG_LOCK = asyncio.Lock()
    async with _MAP_CFG_LOCK:
        return await _put_map_share_locked(body)


async def _put_map_share_locked(body: MapSharePut) -> JSONResponse:
    from . import map_beacon as mb
    from .ingest import _format_mac
    cfg = await mb.get_config()
    was_on = bool(cfg.get("enabled"))
    devices = await db.list_devices()
    # The mac the directory currently lists us under, resolved BEFORE the
    # edit. Beacons are keyed by station, so switching station A -> B only
    # ever ADDED B: A sat on the map under its own pin until its three-hour
    # TTL ran out, and the owner saw the station they had just switched away
    # from (2026-09-14, two live pins for one server). The switch has to
    # carry a tombstone for the station it leaves behind.
    prev_mac = mb.effective_mac(cfg, devices) if was_on else None
    if body.enabled is not None:
        cfg["enabled"] = bool(body.enabled)
    if body.mac is not None:
        cfg["mac"] = _format_mac(body.mac) if body.mac.strip() else None
    if body.name_visible is not None:
        cfg["name_visible"] = bool(body.name_visible)
    if body.location_precision is not None:
        if body.location_precision not in mb.PRECISIONS:
            raise HTTPException(status_code=400,
                                detail="location_precision must be exact, area or city")
        cfg["location_precision"] = body.location_precision
        cfg.pop("exact_location", None)
    if body.visit_public_page is not None:
        cfg["visit_public_page"] = bool(body.visit_public_page)
    if body.link_mode is not None:
        if body.link_mode not in mb.LINK_MODES:
            raise HTTPException(status_code=400,
                                detail="link_mode must be direct, id or none")
        cfg["link_mode"] = body.link_mode
    cfg.pop("visit_url", None)             # typed links are gone (09-14)
    now_ms = int(time.time() * 1000)
    # A withdrawal the directory did not take is queued and retried from
    # the tick (R23: the old pin stayed public for up to three hours and
    # nothing retried, since the switch was already off). The save still
    # succeeds; the response says so in a sentence.
    withdraw_notice: str | None = None
    if was_on and not cfg.get("enabled"):
        # Off: the config write and the tombstone go out together under
        # the publication lock (S1, 2026-09-16 review): a Save and verify
        # that had passed its switch check and was awaiting the device
        # list used to resume after this withdrawal and put the pin
        # straight back. Now nothing signs once the switch is off.
        res = await mb.turn_off(cfg, prev_mac, now_ms)
        if prev_mac and not res.get("ok"):
            withdraw_notice = mb.WITHDRAW_PENDING
    else:
        await mb.set_config(cfg)
    if cfg.get("enabled"):
        new_mac = mb.effective_mac(cfg, devices)
        if prev_mac and prev_mac != new_mac:
            # Best-effort: a directory that is down must not fail the save.
            try:
                res = await mb.withdraw_mac(prev_mac, now_ms)
            except Exception:                # noqa: BLE001
                log.exception("map: could not withdraw the previous station")
                res = {"ok": False}
            if not res.get("ok"):
                withdraw_notice = mb.WITHDRAW_PENDING
        if new_mac:
            # Back on (or back to this station) while its withdrawal was
            # still queued from an earlier off: that tombstone is obsolete
            # and would take down the pin the next beacon renews (S3).
            await mb.drop_pending_withdrawal(new_mac)
        mb._last_publish_ms.clear()          # the next tick sends fresh
    out = await get_map_share()
    if withdraw_notice is None:
        return out
    payload = json.loads(out.body)
    payload["withdraw_notice"] = withdraw_notice
    return JSONResponse(payload)


async def _require_map_share_on() -> None:
    """R23: Save and verify and Rotate published while the switch was
    off (API only; the app hides both), putting a pin on the map the
    owner had said no to."""
    from . import map_beacon as mb
    if not (await mb.get_config()).get("enabled"):
        raise HTTPException(status_code=409,
                            detail="Map sharing is off. Turn it on before "
                                   "sending a beacon or rotating the key.")


@app.post("/api/map/test", dependencies=[Depends(require_write_token)])
async def test_map_share() -> JSONResponse:
    """One beacon right now: the Save and verify button."""
    from . import map_beacon as mb
    await _require_map_share_on()
    result = await mb.publish_once(await db.list_devices(), int(time.time() * 1000))
    return JSONResponse(result)


@app.post("/api/map/rotate", dependencies=[Depends(require_write_token)])
async def rotate_map_key() -> JSONResponse:
    """Mint a new map signing key and send the old key's blessing of it
    to the directory now. Until the directory acknowledges, every beacon
    carries the proof. A key that is LOST cannot bless anything; that is
    the directory operator's forget action."""
    from . import map_beacon as mb
    await _require_map_share_on()
    try:
        out = await mb.rotate(await db.list_devices(), int(time.time() * 1000))
    except mb.RotationRefused as e:
        # A rotation the directory has not acknowledged is resolved with a
        # beacon first; when that cannot land, nothing rotates (S2).
        raise HTTPException(status_code=409, detail=str(e))
    return JSONResponse(out)


@app.get("/api/devices/{mac}/highlights", dependencies=[Depends(require_token)])
async def get_highlights(mac: str) -> JSONResponse:
    """Today's highlights (2.3): the readings that make today unusual for
    THIS station, ranked against its own daily rollups for the same time
    of year. Empty (never invented) under two years of record."""
    from . import highlights
    from .ingest import _format_mac
    return JSONResponse(await highlights.assemble(_format_mac(mac)))


@app.get("/api/devices/{mac}/normals", dependencies=[Depends(require_token)])
async def get_normals(mac: str) -> JSONResponse:
    """Today's NOAA 1991-2020 climate normals for this station's location
    (1.7 "today vs normal"). {"available": false} when the station has no
    coordinates or no U.S. normals coverage — the app renders nothing then,
    never a made-up comparison. Exposes only the NCEI station NAME (coarse,
    city-level — same exposure class as the public location label), never
    the device's coordinates."""
    from . import normals
    from .ingest import _format_mac
    mac = _format_mac(mac)
    for d in await db.list_devices():
        if d["mac"] != mac:
            continue
        coords = ((d.get("info") or {}).get("coords") or {}).get("coords") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if lat is None or lon is None:
            break
        result = await normals.today(float(lat), float(lon))
        if result:
            return JSONResponse({"available": True, **result})
        break
    return JSONResponse({"available": False})


@app.get("/api/devices/{mac}/summary", dependencies=[Depends(require_token)])
async def get_summary(
    mac: str,
    field: str = Query("tempf"),
    hours: int = Query(24, ge=1, le=24 * 30),
    # Optional bucket-averaged series over the same window, sized for
    # sparklines (the widgets' 2x2 face). Bounded small on purpose: a
    # client that wants real resolution should use /history's buckets.
    points: int | None = Query(None, ge=2, le=200),
) -> JSONResponse:
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    try:
        agg = await db.aggregate(mac, field, start, end)
        if points is not None:
            agg["series"] = await db.field_series(mac, field, start, end, points)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(agg)


# Records are expensive (all-time window scans the full per-mac history) and
# barely change minute-to-minute, so cache per-mac for a while. Shared by the
# API endpoint and the public dashboard.
_RECORDS_CACHE: dict[str, tuple[float, dict]] = {}
_RECORDS_TTL_S = 900  # 15 min
_RECORDS_MAX_ENTRIES = 64          # far above any real device count
_RECORDS_LOCKS: dict[str, "asyncio.Lock"] = {}
# Strong refs to in-flight background warms (see _records_cached_or_warm).
_WARM_TASKS: set = set()


async def _warm_records(mac: str) -> dict:
    """Compute records for a device + refresh the cache.

    Serialized per MAC: the all-time window scans the full per-mac history, so
    a second caller must WAIT for the in-flight compute, not race it. The old
    dedupe short-circuited to `{}` when a warm was already running, which
    handed concurrent clients a 200 with an empty body (blank Records screen)
    whenever the status page had kicked off a background warm.
    """
    lock = _RECORDS_LOCKS.setdefault(mac, asyncio.Lock())
    async with lock:
        # A caller that queued behind the compute gets its fresh result.
        hit = _RECORDS_CACHE.get(mac)
        if hit and time.time() - hit[0] < _RECORDS_TTL_S:
            return hit[1]
        data = await db.records(mac, settings.timezone)
        _RECORDS_CACHE[mac] = (time.time(), data)
        _prune_records_cache()
        return data


def _prune_records_cache() -> None:
    """Drop expired entries and bound the cache.

    `mac` is an unvalidated path param, so without this a token holder could
    request unlimited distinct MACs and each would leave a permanent entry
    (db.records happily returns an empty skeleton for an unknown device).
    """
    def _drop_lock(key: str) -> None:
        # Never evict a HELD lock: dropping it lets the next caller build a fresh
        # one and run a second concurrent compute for the same MAC, defeating the
        # serialization the lock exists for.
        lk = _RECORDS_LOCKS.get(key)
        if lk is not None and not lk.locked():
            del _RECORDS_LOCKS[key]

    now = time.time()
    for k, (ts, _) in list(_RECORDS_CACHE.items()):
        if now - ts >= _RECORDS_TTL_S:
            del _RECORDS_CACHE[k]
            _drop_lock(k)
    while len(_RECORDS_CACHE) > _RECORDS_MAX_ENTRIES:      # oldest-first
        oldest = min(_RECORDS_CACHE, key=lambda k: _RECORDS_CACHE[k][0])
        del _RECORDS_CACHE[oldest]
        _drop_lock(oldest)


async def _cached_records(mac: str) -> dict:
    """Fresh cached records, else compute synchronously. Used by the API
    endpoint (authenticated, infrequent — OK to wait for a cold compute)."""
    hit = _RECORDS_CACHE.get(mac)
    if hit and time.time() - hit[0] < _RECORDS_TTL_S:
        return hit[1]
    return await _warm_records(mac)


def _records_cached_or_warm(mac: str) -> dict | None:
    """NON-blocking: fresh cached records, else spawn a background warm and
    return None. Keeps the status-page render off the full-history scan."""
    hit = _RECORDS_CACHE.get(mac)
    if hit and time.time() - hit[0] < _RECORDS_TTL_S:
        return hit[1]
    lock = _RECORDS_LOCKS.get(mac)
    if lock is None or not lock.locked():
        # Hold a strong reference: a bare create_task can be garbage-collected
        # mid-flight, and without a done-callback any failure (e.g. "database
        # is locked" during maintenance) surfaces only as a GC-time warning and
        # the records strip silently never appears.
        t = asyncio.create_task(_warm_records(mac))
        _WARM_TASKS.add(t)
        t.add_done_callback(_warm_task_done)
    return None


def _warm_task_done(t: "asyncio.Task") -> None:
    _WARM_TASKS.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.warning("records warm failed: %s", t.exception())


@app.get("/api/devices/{mac}/records", dependencies=[Depends(require_token)])
async def get_records(mac: str) -> JSONResponse:
    """All-time / yearly / monthly / today highs & lows per metric, with the
    local time each record was set. Cached 15 min per device."""
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    # 404 unknown MACs: db.records() returns an empty skeleton for any string,
    # so without this each bogus MAC burned 40 aggregate queries and left a
    # permanent cache entry.
    known = {d["mac"] for d in await db.list_devices()}
    if mac not in known:
        raise HTTPException(status_code=404, detail="unknown device")
    return JSONResponse(await _cached_records(mac))


@app.get("/api/captures/{slug}", dependencies=[Depends(require_write_token)])
async def get_captures(slug: str, tail: int = Query(50, ge=1, le=10_000)) -> JSONResponse:
    """Read recent capture-endpoint hits for a slug. Gated on the PRIMARY
    api_token only (require_write_token) — the read-only reviewer/demo token
    must NOT be able to read raw captured request bodies/headers, which can
    contain other sources' secrets. Random folks on the internet can't
    enumerate someone else's traffic either."""
    from .capture import _log_path
    path = _log_path(slug)
    if not path.exists():
        return JSONResponse({"slug": slug, "rows": []})
    import json as _json
    from collections import deque

    # Read only the requested tail into memory (bounded by `tail`, not the
    # whole file) so a large append-only capture log can't be turned into a
    # memory-exhaustion read. Off the event loop: the deque still SCANS the
    # whole file, and an ever-growing capture log would otherwise block every
    # other request for the duration.
    def _read_tail() -> deque[str]:
        # The exists() check above ran on the event loop; log rotation or
        # cleanup can remove the file before this thread opens it. Missing
        # then == missing now: same empty result, not a 500.
        try:
            with path.open("r", encoding="utf-8") as f:
                return deque(f, maxlen=tail)
        except FileNotFoundError:
            return deque()

    last_lines = await asyncio.to_thread(_read_tail)
    # Tolerate corrupt/partial JSONL — older log lines from a crashed
    # write can have a truncated trailing line. Skip rather than 500.
    rows: list[dict] = []
    skipped = 0
    for line in last_lines:
        try: rows.append(_json.loads(line))
        except _json.JSONDecodeError: skipped += 1
    out: dict[str, Any] = {"slug": slug, "count": len(rows), "rows": rows}
    if skipped:
        out["skipped_malformed"] = skipped
    return JSONResponse(out)


@app.get("/api/forecast", dependencies=[Depends(require_token)])
async def get_forecast(
    lat: float | None = None, lon: float | None = None,
    source: str | None = Query(None, pattern="^(open-meteo|twc)$"),
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Forecast. Default: 7-day Open-Meteo (free, no key). source=twc asks
    for The Weather Company's 5-day (needs the WU_API_KEY secret — free for
    PWS owners); ANY TWC failure falls back to Open-Meteo for this response
    only, marked with fallback_from so the app can label the strip. The
    preference itself lives in the app and is never flipped here."""
    flat = lat if lat is not None else settings.forecast_lat
    flon = lon if lon is not None else settings.forecast_lon
    if flat is None or flon is None:
        # Fallback: use the first device's known lat/lon if available
        devs = await db.list_devices()
        for d in devs:
            info = d.get("info") or {}
            coords = (info.get("coords") or {}).get("coords") or {}
            if "lat" in coords and "lon" in coords:
                flat, flon = coords["lat"], coords["lon"]
                break
    if flat is None or flon is None:
        raise HTTPException(status_code=400, detail="no lat/lon available; pass ?lat=&lon=")
    # The device-info fallback pulls from a JSON blob a custom ingest source
    # controls — coords stored as STRINGS would make the range comparison
    # below raise TypeError, i.e. a bare 500 for a bad-data condition.
    try:
        flat, flon = float(flat), float(flon)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="device coordinates are not numeric")
    # Same range check as put_device_location. Open-Meteo answers an
    # out-of-range coordinate with a 400, which used to surface here as a bare
    # 500 — the app then showed "server error" for what is a bad request.
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    params = {
        "latitude": flat,
        "longitude": flon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,wind_direction_10m_dominant",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": 7,
    }
    fallback_from: str | None = None
    if source == "twc":
        from . import forecast_twc
        wu_key = await effective_wu_key()
        if wu_key:
            try:
                return JSONResponse(await forecast_twc.fetch(
                    flat, flon, wu_key))
            except Exception as e:
                # Dead key (WU deactivates them when a station stops
                # uploading), WU outage, transform surprise — all routine.
                # Never log the exception repr: the key rides the URL.
                log.warning("TWC forecast failed (%s); falling back to "
                            "Open-Meteo", type(e).__name__)
                fallback_from = "twc"
        else:
            fallback_from = "twc"          # asked for TWC, no key configured
    # A third-party API that times out, 500s or returns an HTML error page is
    # routine, not a bug in this server — report it as an upstream failure so
    # the app can say "forecast unavailable" instead of "server error".
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
        body = r.json()
        if _is_limited_read(authorization):
            # Guests get the forecast (the family-sharing strip needs it)
            # but not the operator's home location: Open-Meteo echoes back
            # the grid-snapped lat/lon it was asked for, which on the
            # no-args call is the first device's stored coordinates — the
            # exact fields _strip_device_pii hides on /api/devices
            # (CODE_REVIEW_R5 R5-03 / R3-06).
            for k in ("latitude", "longitude", "elevation"):
                body.pop(k, None)
        body["source"] = "open-meteo"
        # Always present so the client decodes one shape from both sources.
        # Empty because Open-Meteo has no written forecast to give — only TWC
        # ships prose, which is why the app hides the card rather than
        # inventing one (see forecast_twc.transform).
        body["narrative"] = []
        if fallback_from:
            body["fallback_from"] = fallback_from
        return JSONResponse(body)
    except httpx.HTTPError as e:
        log.warning("forecast upstream failed: %s", e)
        raise HTTPException(status_code=502, detail="forecast upstream unavailable")
    except ValueError:
        log.warning("forecast upstream returned non-JSON")
        raise HTTPException(status_code=502, detail="forecast upstream unavailable")
