"""The write-lock watchdog and the on-volume log file (2026-09-02).

A stuck writer held the database for four minutes with nothing in the
log naming it, and the boot lines that might have had already rolled
off Fly's log window. Two answers: dump every thread's stack once the
writer has been held for three probes, and keep a rotated log on the
data volume.
"""
import asyncio
import os

import pytest


def _drive(strike_pattern, extra_ticks=0):
    """Run the watchdog against a scripted probe. `strike_pattern` is the
    sequence of probe answers (True = free); the fake sleep stops the loop
    once the script runs out."""
    from app import main as M
    answers = list(strike_pattern) + [True] * extra_ticks
    dumps = []
    clock = {"t": 0.0}

    async def sleep(_s):
        clock["t"] += M._WRITE_LOCK_PROBE_S
        if not answers:
            raise asyncio.CancelledError

    async def probe():
        return answers.pop(0)

    def dump(reason):
        dumps.append(reason)

    async def run():
        try:
            await M._write_lock_watchdog(probe=probe, dump=dump, sleep=sleep)
        except asyncio.CancelledError:
            pass
    asyncio.run(run())
    return dumps


@pytest.mark.usefixtures("temp_env")
def test_watchdog_dumps_once_after_three_locked_probes():
    dumps = _drive([False, False, False, False, False])
    assert len(dumps) == 1 and "3 probes" in dumps[0]


@pytest.mark.usefixtures("temp_env")
def test_watchdog_stays_quiet_below_three_strikes():
    assert _drive([False, False, True, False, False, True]) == []


@pytest.mark.usefixtures("temp_env")
def test_watchdog_dumps_again_only_after_the_lock_frees_and_returns(monkeypatch):
    from app import main as M
    # Ten-minute rate limit: with the probe cadence at 30 s, a lock held
    # for a long time produces one dump, not one per probe. Shrink the
    # window so the test can see a second dump after it passes.
    monkeypatch.setattr(M, "_WRITE_LOCK_DUMP_EVERY_S", 0.0)
    dumps = _drive([False] * 3 + [True] + [False] * 3)
    assert len(dumps) == 2


@pytest.mark.usefixtures("client")   # the client boot creates the database file
def test_probe_reports_a_held_writer():
    import sqlite3
    from app import main as M
    from app.config import settings
    holder = sqlite3.connect(settings.database_path)
    holder.execute("CREATE TABLE IF NOT EXISTS _t (x)")
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert M._probe_write_lock() is False
    finally:
        holder.rollback()
        holder.close()
    assert M._probe_write_lock() is True


@pytest.mark.usefixtures("client")
def test_boot_attaches_a_rotating_log_file_beside_the_database():
    import logging
    from app.config import settings
    path = os.path.join(os.path.dirname(os.path.abspath(settings.database_path)),
                        "logs", "zasder.log")
    handlers = [h for h in logging.getLogger().handlers
                if getattr(h, "_zasder_file_log", False)]
    assert handlers and handlers[0].baseFilename == path
    # WARNING, not INFO: pytest's log capture can raise the effective level
    # for the duration of a test, and the point here is the handler's path
    # and that records reach the file, not the level policy.
    logging.getLogger("api").warning("watchdog test line")
    handlers[0].flush()
    assert "watchdog test line" in open(path, encoding="utf-8").read()


@pytest.mark.usefixtures("temp_env")
def test_log_file_can_be_disabled(monkeypatch, tmp_path):
    import logging
    from app import main as M
    monkeypatch.setenv("LOG_FILE", "")
    before = [h for h in logging.getLogger().handlers
              if getattr(h, "_zasder_file_log", False)]
    assert M.attach_file_log(str(tmp_path / "w.db")) is None
    after = [h for h in logging.getLogger().handlers
             if getattr(h, "_zasder_file_log", False)]
    assert before == after
