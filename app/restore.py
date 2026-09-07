"""Restore the database from an uploaded snapshot (2.1).

The other half of `GET /api/backup/database`: a self-hoster who saved the
snapshot the app hands them (iCloud Drive, a USB stick, wherever) can put
it back on a fresh box from the app, without flyctl, sqlite3 or a shell.
Before this the only way back was Fly's own volume snapshots, which need
the CLI, or hand-copying the file over `fly ssh sftp`.

Shape, in order:

  1. `POST /api/backup/database/restore/challenge` (write token) hands out
     a one-shot nonce, five minutes, one at a time, plus the current
     database's row count so the app can say what is about to be replaced.
  2. `POST /api/backup/database/restore` (write token) streams the upload
     to a temp file BESIDE the database — never into memory, the row-cap
     middleware exempts exactly this path — checking the nonce first, the
     free space up front, and a SHA-256 the caller computed over the file
     it meant to send against the bytes that actually arrived. Then a
     background job validates and swaps; the route answers at once so a
     multi-minute integrity check cannot trip a proxy idle timeout.
  3. Validation (`PRAGMA integrity_check` = ok, the `observations` and
     `server_kv` tables present, no table or column the running code does
     not know — a backup from a NEWER release is refused rather than fed
     to migrations that would silently drop what they cannot read).
  4. The swap: checkpoint the live WAL, rename the live file to
     `<db>.pre-restore-<stamp>.db` (the old database IS the safety net;
     a rename is atomic and needs no third copy of a multi-GB file where
     a VACUUM INTO would), rename the upload into place, drop the stale
     WAL/SHM sidecars, run `init_db` so migrations and the in-memory auth
     caches see the restored file. Ingest parks behind the same flag the
     chart-index build uses for the milliseconds the swap takes, then the
     queue drains. Only the newest pre-restore file is kept.
  5. `GET /api/backup/database/restore/status` reports the job.

THE STEP-UP GATE, honestly described. The hosted-tier plan settles that
destructive routes take the write token PLUS a per-request App Attest
assertion from a device key registered at pairing. Today (2.1) no such
key is registered anywhere: the relay verifies an attestation ONCE when
it issues a relay key and deliberately does not persist the attested
public key, and that whole module is stripped from the public mirror, so
a self-hosted backend has no device keys to assert against at all. The
gate this route enforces now is therefore write token + a fresh one-shot
challenge + the upload digest: the write token proves ownership, the
challenge proves the caller performed the two-step in the last five
minutes (a replayed or blind upload has no valid nonce), and the digest
proves the bytes that landed are the bytes the person chose. Guest and
reviewer tokens are refused by `require_write_token` with 403. When the
3.0 pairing flow registers per-device keys, the challenge issued here is
what the device signs; `verify_step_up` is the single seam to extend.

Refused while anything else holds the database's future: a rollup
rebuild in flight, a backup job running, the chart index building.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import HTTPException

from . import config as _config

log = logging.getLogger("api")


def _db_path() -> Path:
    # Read through the module, not a bound name: the test suite reloads
    # app.config per test and this module is not on its reload list, so a
    # bound `settings` would point at the first test's deleted tempdir.
    return Path(_config.settings.database_path)

RESTORE_TMP_PREFIX = ".dbrestore-"
PRE_RESTORE_SUFFIX = ".pre-restore"
# The durable transition marker: written beside the database before the
# first rename, removed after the restore is committed. A process killed
# between the renames leaves it behind, and the next boot reads it to put
# the pre-restore copy back (R21-02: a boot must never create an empty
# database because the live filename vanished mid-swap).
RESTORING_SUFFIX = ".restoring"
CHALLENGE_TTL_MS = 5 * 60 * 1000
# Headroom over the upload for the pre-restore rename (none: same
# filesystem, no copy) plus the WAL the restored file will grow.
UPLOAD_SLACK_BYTES = 64 * 1024 * 1024
# Read chunks this large from the request stream.
_CHUNK = 1024 * 1024

# One challenge at a time. A second request replaces the first: the app
# asks right before it shows the confirmation, so the newest is the one
# the person is looking at.
_CHALLENGE: dict[str, Any] | None = None

# The job. idle | receiving | validating | swapping | done | error.
JOB: dict[str, Any] = {"state": "idle"}
_TASK: "asyncio.Task | None" = None

# Every table initialiser the boot runs BESIDE db.init_db, re-run after the
# swap so a restored file that predates one of them gets its tables. Modules
# register themselves at import (the private relay does), which keeps this
# file free of names the public mirror's residue sweep forbids.
POST_SWAP_HOOKS: list[Callable[[], Any]] = []


def _now_ms() -> int:
    return int(time.time() * 1000)


# ───────────────────────── challenge ─────────────────────────

def issue_challenge(now_ms: int | None = None) -> dict[str, Any]:
    global _CHALLENGE
    now = now_ms if now_ms is not None else _now_ms()
    nonce = secrets.token_urlsafe(32)
    _CHALLENGE = {"nonce": nonce, "issued_ms": now}
    return {"challenge": nonce, "expires_ms": now + CHALLENGE_TTL_MS}


def consume_challenge(presented: str | None, now_ms: int | None = None) -> bool:
    """True exactly once for the live nonce inside its TTL. Constant-time
    compare; the nonce is cleared whether it matched or not, so a guess
    costs the person a fresh round trip, never a second guess."""
    global _CHALLENGE
    now = now_ms if now_ms is not None else _now_ms()
    live, _CHALLENGE = _CHALLENGE, None
    if not live or not presented:
        return False
    if now - int(live["issued_ms"]) > CHALLENGE_TTL_MS:
        return False
    return secrets.compare_digest(str(presented), str(live["nonce"]))


def verify_step_up(challenge: str | None) -> None:
    """The seam described in the module docstring: today the fresh
    challenge; later the challenge plus a device assertion over it."""
    if not consume_challenge(challenge):
        raise HTTPException(
            status_code=403,
            detail="restoring the database needs a fresh confirmation: ask "
                   "for a challenge first (POST .../restore/challenge) and "
                   "send it back within five minutes")


# ───────────────────────── validation ─────────────────────────

def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _live_schema(live: Path) -> dict[str, set[str]]:
    con = _open_ro(live)
    try:
        return _schema_of(con)
    finally:
        con.close()


def _schema_of(con: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"):
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{name}")')}
        out[name] = cols
    return out


def validate_snapshot(path: Path, live: Path | None = None) -> dict[str, Any]:
    """Sync (call via to_thread). Raises HTTPException with a plain-language
    reason; returns {"rows": n, "tables": k} on success."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"could not read the upload: {e}")
    if head != b"SQLite format 3\x00":
        raise HTTPException(
            status_code=400,
            detail="that file is not a SQLite database — the weather "
                   "database backup is the .db file the app saved")
    try:
        con = _open_ro(path)
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"could not open the upload: {e}")
    try:
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as e:
            raise HTTPException(status_code=400,
                                detail=f"the upload is damaged: {e}")
        if not check or str(check[0]).lower() != "ok":
            raise HTTPException(
                status_code=400,
                detail="the upload failed SQLite's integrity check: "
                       f"{(check[0] if check else 'no answer')!s:.200}")
        schema = _schema_of(con)
        for required in ("observations", "server_kv"):
            if required not in schema:
                raise HTTPException(
                    status_code=400,
                    detail=f"the upload has no '{required}' table — it is "
                           "not a Zasder Weather database")
        if live is not None and live.exists():
            known = _live_schema(live)
            unknown_tables = sorted(t for t in schema if t not in known)
            unknown_cols = sorted(
                f"{t}.{c}" for t, cols in schema.items() if t in known
                for c in cols if c not in known[t])
            if unknown_tables or unknown_cols:
                what = ", ".join((unknown_tables + unknown_cols)[:6])
                raise HTTPException(
                    status_code=409,
                    detail="the upload was made by a NEWER release than this "
                           f"server runs (it has {what} this server does not "
                           "know). Update the server first, then restore.")
        rows = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return {"rows": int(rows), "tables": len(schema)}
    finally:
        con.close()


async def _required_schema(beside: Path) -> dict[str, set[str]]:
    """What a database this build can serve must contain: the schema
    init_db creates in an EMPTY file, read back. Derived, not hand-listed,
    so a table or column added to db.SCHEMA is required here the same day."""
    from . import db
    probe = beside.parent / f"{RESTORE_TMP_PREFIX}schema-{secrets.token_hex(6)}.db"
    try:
        await db.init_db(path=str(probe))
        con = _open_ro(probe)
        try:
            return _schema_of(con)
        finally:
            con.close()
    finally:
        for side in ("", "-wal", "-shm"):
            Path(str(probe) + side).unlink(missing_ok=True)


async def prepare_candidate(upload: Path) -> None:
    """Make the upload the database this build expects BEFORE anything
    live moves (R21-02). Runs init_db's migrations (SCHEMA plus every late
    ALTER) against the upload in place, then checks that every table and
    column a fresh database would have is present. An upload that cannot
    be migrated, or that lacks a mandatory column (a two-column
    `observations` passed the old subset check), is refused here and the
    live database is never touched."""
    from . import db
    try:
        await db.init_db(path=str(upload))
        await asyncio.to_thread(_fold_journal, upload)
    except Exception as e:                            # noqa: BLE001
        _sidecars(upload)
        raise HTTPException(
            status_code=400,
            detail=f"the upload could not be brought up to this server's "
                   f"schema: {e!s:.200}")
    required = await _required_schema(upload)
    con = _open_ro(upload)
    try:
        have = _schema_of(con)
    finally:
        con.close()
    missing: list[str] = []
    for table, cols in required.items():
        if table not in have:
            missing.append(table)
            continue
        missing.extend(f"{table}.{c}" for c in sorted(cols - have[table]))
    if missing:
        raise HTTPException(
            status_code=400,
            detail="the upload is missing parts of a Zasder Weather database "
                   f"({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}); "
                   "it was not restored")


# ───────────────────────── the swap ─────────────────────────

def _fold_journal(path: Path) -> None:
    """init_db switched the candidate to WAL; fold that WAL into the file
    and go back to a rollback journal so the upload is ONE file again (the
    live database re-enables WAL when init_db runs on it after the swap)."""
    con = sqlite3.connect(str(path), timeout=30)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    _sidecars(path)


def _marker(live: Path) -> Path:
    return live.with_name(live.name + RESTORING_SUFFIX)


def _sidecars(path: Path) -> None:
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)


def _usable(path: Path) -> bool:
    """Opens read-only and reads the catalogue: a torn or missing file is
    not usable, a database is."""
    if not path.exists():
        return False
    try:
        con = _open_ro(path)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def roll_back(live: Path, pre: Path) -> None:
    """Put the pre-restore copy back as the live database. Called after any
    failure past the first rename: the failed candidate is discarded (the
    person still holds their file), the old database returns under its own
    name, the sidecars and the marker go."""
    if live.exists():
        live.unlink()
    _sidecars(live)
    if pre.exists():
        os.replace(pre, live)
    _marker(live).unlink(missing_ok=True)


def commit_restore(live: Path, pre: Path) -> None:
    """The point of no return, AFTER the restored file proved usable: drop
    the marker, keep only this pre-restore copy."""
    _marker(live).unlink(missing_ok=True)
    _prune_pre_restore(live, keep=pre)


def recover_at_boot(live: Path | None = None) -> str | None:
    """Called before init_db at boot. A marker means a restore was cut
    short between its renames: if the live file is missing or unusable and
    the pre-restore copy exists, reinstall it. Returns a log line or None."""
    live = live or _db_path()
    marker = _marker(live)
    if not marker.exists():
        return None
    try:
        pre = Path(json.loads(marker.read_text()).get("pre", ""))
    except (OSError, ValueError):
        pre = Path("")
    if _usable(live):
        marker.unlink(missing_ok=True)
        return "restore marker found beside a usable database; cleared"
    if pre.name and pre.exists():
        if live.exists():
            live.unlink()
        _sidecars(live)
        os.replace(pre, live)
        marker.unlink(missing_ok=True)
        return (f"a restore was interrupted between renames; the previous "
                f"database {pre.name} is back in place")
    marker.unlink(missing_ok=True)
    return ("a restore was interrupted and neither the live database nor "
            "the pre-restore copy is usable; starting from the file on disk")


def _prune_pre_restore(live: Path, keep: Path) -> None:
    for old in live.parent.glob(f"{live.name}{PRE_RESTORE_SUFFIX}-*.db"):
        if old != keep:
            old.unlink(missing_ok=True)


def _checkpoint(live: Path) -> tuple[int, int, int]:
    """PRAGMA wal_checkpoint(TRUNCATE) → (busy, log_frames, checkpointed)."""
    con = sqlite3.connect(str(live), timeout=30)
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)


class CheckpointBusy(RuntimeError):
    """A reader held the live database through every checkpoint attempt;
    nothing was swapped and the upload is kept (round-three review
    BE-F10: one transient reader used to cost the whole upload)."""


CHECKPOINT_ATTEMPTS = 5
CHECKPOINT_RETRY_S = 1.0


def swap_in(upload: Path, live: Path, stamp: str | None = None) -> Path:
    """Sync (call via to_thread, with writers parked). Returns the
    pre-restore path the old database now lives at."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    pre = live.with_name(f"{live.name}{PRE_RESTORE_SUFFIX}-{stamp}.db")
    if live.exists():
        # Fold the WAL into the main file so the renamed copy is complete
        # on its own; TRUNCATE also empties the sidecar we then delete.
        # The pragma does not raise when a reader blocks it — it answers
        # (busy=1, ...) and leaves frames in the WAL, and the rename below
        # would then keep a pre-restore copy missing committed rows
        # (CodeRabbit, PR #36). Writers are parked but readers are not,
        # so check the row and refuse the swap rather than lose data —
        # after a few tries a second apart, since a widget fetch or a poll
        # is a reader for milliseconds, not minutes (BE-F10).
        busy = 1
        for attempt in range(CHECKPOINT_ATTEMPTS):
            busy, _logged, _done = _checkpoint(live)
            if not busy:
                break
            if attempt + 1 < CHECKPOINT_ATTEMPTS:
                time.sleep(CHECKPOINT_RETRY_S)
        if busy:
            raise CheckpointBusy("could not checkpoint the live database "
                                 f"(a reader held it open through {CHECKPOINT_ATTEMPTS} "
                                 "tries); nothing was swapped and the upload is kept "
                                 "for one more attempt")
        _marker(live).write_text(json.dumps({"pre": str(pre), "upload": str(upload),
                                             "stamp": stamp}))
        os.replace(live, pre)
    _sidecars(live)
    try:
        os.replace(upload, live)
    except BaseException:
        # The old database goes straight back under its own name; the
        # upload is wherever the failed rename left it.
        roll_back(live, pre)
        raise
    # The pre-restore prune waits for commit_restore(): until the restored
    # file has proven usable, every earlier copy is still a safety net.
    return pre


# ───────────────────────── the job ─────────────────────────

def status() -> dict[str, Any]:
    out = {"state": JOB.get("state", "idle")}
    for k in ("rows", "error", "snapshot", "finished_ms", "received_bytes", "warning"):
        if JOB.get(k) is not None:
            out[k] = JOB[k]
    return out


def busy_reason() -> str | None:
    if JOB.get("state") in ("receiving", "validating", "swapping"):
        task = _TASK
        if JOB["state"] == "receiving" or (task is not None and not task.done()):
            return "a restore is already in progress"
        JOB.update(state="error", error="previous restore was interrupted")
    try:
        from . import insights
        if insights.in_flight() is not None:
            return "a rollup rebuild is running; try again when it finishes"
        lock = insights._REBUILD_LOCK
        if lock is not None and lock.locked():
            return "a rollup rebuild is running; try again when it finishes"
    except Exception:       # never let the diagnostic block the route
        pass
    try:
        from . import db
        if db._CHART_INDEX_BUILDING:
            return "the chart index is being rebuilt; try again in a few minutes"
    except Exception:
        pass
    return None


def upload_dest(live: Path, expected_bytes: int | None) -> Path:
    """Where the upload lands: beside the database (the swap is a rename,
    so it must be the same filesystem), if the volume has room for it."""
    need = (expected_bytes or 0) + UPLOAD_SLACK_BYTES
    try:
        free = shutil.disk_usage(live.parent).free
    except OSError as e:
        raise HTTPException(status_code=507, detail=f"cannot measure free disk: {e}")
    if free < need:
        raise HTTPException(
            status_code=507,
            detail=f"not enough free disk on the server for the upload — it "
                   f"needs about {need // 2**20} MB and {live.parent} has "
                   f"{free // 2**20} MB free")
    return live.parent / f"{RESTORE_TMP_PREFIX}{secrets.token_hex(8)}.db"


async def receive_upload(chunks: AsyncIterator[bytes], dest: Path,
                         *, expected_sha256: str, expected_bytes: int | None,
                         free_at_start: int) -> int:
    """Stream to disk, hashing as it goes. Returns bytes received."""
    digest = hashlib.sha256()
    total = 0
    cap = free_at_start - UPLOAD_SLACK_BYTES
    try:
        with open(dest, "wb") as f:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > cap:
                    raise HTTPException(
                        status_code=507,
                        detail="the upload outgrew the server's free disk")
                f.write(chunk)
                digest.update(chunk)
                JOB["received_bytes"] = total
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if expected_bytes is not None and total != expected_bytes:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"the upload stopped short: {total} of {expected_bytes} bytes")
    if digest.hexdigest().lower() != expected_sha256.strip().lower():
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="the bytes that arrived do not match the file's SHA-256 — "
                   "the upload was corrupted in transit; try again")
    return total


async def _validate_and_swap(upload: Path, live: Path) -> None:
    """The background half. Every failure lands in JOB; nothing here is
    allowed to vanish into a fire-and-forget void."""
    from . import db, ingest
    try:
        JOB["state"] = "validating"
        info = await asyncio.to_thread(validate_snapshot, upload, live)
        # Migrate the candidate and require the full schema BEFORE the
        # live database moves (R21-02).
        await prepare_candidate(upload)
        JOB["state"] = "swapping"
        # Ingest parks in its write-behind queue behind the chart-index flag
        # (a LilyGO post must never 503 for the seconds this takes); every
        # other database user is held by the maintenance lease, which also
        # drains the connections already open (R21-01).
        db._CHART_INDEX_BUILDING = True
        try:
            async with db.maintenance_lease():
                pre = await asyncio.to_thread(swap_in, upload, live)
                try:
                    await db.init_db()
                except BaseException as e:
                    # The restored file cannot be served: put the old one
                    # back before anyone can connect (R21-02).
                    await asyncio.to_thread(roll_back, live, pre)
                    raise RuntimeError(
                        "the restored database could not be initialised, so "
                        f"the previous database is back in place: {e!s:.200}"
                    ) from e
                await asyncio.to_thread(commit_restore, live, pre)
        finally:
            db._CHART_INDEX_BUILDING = False
        # Reconcile the running process with the restored state (R21-03):
        # pollers from the restored credentials, caches, deferred jobs.
        # Registered by main.py at boot; a failure here is reported, not
        # hidden, and the database itself is already restored.
        warnings: list[str] = []
        for hook in list(POST_SWAP_HOOKS):
            try:
                await hook()
            except Exception as e:
                name = getattr(hook, "__name__", str(hook))
                log.exception("post-restore hook %s failed", name)
                warnings.append(f"{name}: {e!s:.120}")
        try:
            await ingest.drain_write_behind()
        except Exception:
            log.exception("write-behind drain after the restore failed; the "
                          "next chart-index attempt re-drains")
        JOB.update(state="done", rows=info["rows"], snapshot=pre.name,
                   finished_ms=_now_ms())
        if warnings:
            JOB["warning"] = ("restored, but part of the server did not "
                              "pick the new database up (a restart will): "
                              + "; ".join(warnings))
        log.warning("DATABASE RESTORED from an uploaded snapshot: %d rows; "
                    "the previous database is %s", info["rows"], pre.name)
    except HTTPException as e:
        upload.unlink(missing_ok=True)
        _sidecars(upload)
        JOB.update(state="error", error=str(e.detail), finished_ms=_now_ms())
        log.warning("database restore refused: %s", e.detail)
    except (CheckpointBusy, db.LeaseBusy) as e:
        # The upload stays beside the database (the boot sweep clears it
        # if nobody retries): nothing about the file was wrong.
        JOB.update(state="error", error=str(e), finished_ms=_now_ms())
        log.warning("database restore refused: %s", e)
    except Exception as e:
        upload.unlink(missing_ok=True)
        _sidecars(upload)
        JOB.update(state="error", error=str(e), finished_ms=_now_ms())
        log.exception("database restore failed")


async def start_restore(chunks: AsyncIterator[bytes], *, challenge: str | None,
                        sha256_hex: str | None, content_length: int | None,
                        other_busy: Callable[[], str | None] | None = None
                        ) -> dict[str, Any]:
    """The route body. Order matters: cheap refusals before the step-up
    (a busy server must not burn the person's one challenge), the step-up
    before a single body byte."""
    global _TASK
    reason = busy_reason() or (other_busy() if other_busy else None)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    if not sha256_hex or len(sha256_hex.strip()) != 64:
        raise HTTPException(status_code=400,
                            detail="X-Restore-SHA256 must carry the hex "
                                   "SHA-256 of the file being uploaded")
    verify_step_up(challenge)
    live = _db_path()
    dest = upload_dest(live, content_length)
    free = shutil.disk_usage(live.parent).free
    JOB.clear()
    JOB.update(state="receiving", started_ms=_now_ms(), received_bytes=0)
    try:
        received = await receive_upload(chunks, dest,
                                        expected_sha256=sha256_hex,
                                        expected_bytes=content_length,
                                        free_at_start=free)
    except BaseException as e:
        JOB.update(state="error", finished_ms=_now_ms(),
                   error=(e.detail if isinstance(e, HTTPException) else str(e)))
        raise
    JOB["state"] = "validating"
    _TASK = asyncio.create_task(_validate_and_swap(dest, live))
    return {"state": "validating", "received_bytes": received}


def sweep_leftovers(live: Path | None = None) -> int:
    """Delete `.dbrestore-*` uploads a killed process left beside the
    database. Called at boot from the lifespan's snapshot sweep."""
    live = live or _db_path()
    n = 0
    try:
        for p in live.parent.glob(f"{RESTORE_TMP_PREFIX}*.db"):
            p.unlink(missing_ok=True)
            n += 1
    except OSError:
        pass
    return n


async def current_rows() -> int:
    from . import db
    async with db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM observations")
        return int((await cur.fetchone())[0])
