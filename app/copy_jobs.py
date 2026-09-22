"""Which whole-database copies are in flight right now (2.4, D2).

Doren's box raised `disk_low` at 88% for two minutes at 01:51Z on
2026-09-20 — the volume was not filling, his nightly backup's own
`VACUUM INTO` copy was landing on it and the watchdog measured the copy.
The alert was true about the number and wrong about the world.

A backup, a restore and a pre-upgrade snapshot each put a second copy of
the database on a disk for the length of one job. Every one of them
registers here for its duration, and `disk_watch` skips a tick while
anything is registered rather than reporting a number it knows is about
to fall back on its own.

Deliberately dumb: a labelled counter behind a lock, no timers, no
persistence. A job that dies takes its registration with it because the
`with` block unwinds; a process that dies takes the whole table with it.
Never gate anything DESTRUCTIVE on this — `restore.py` keeps its own
refusal rules (it can see jobs this table never hears about).
"""
from __future__ import annotations

import os
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_lock = threading.Lock()
# label -> count, and the st_dev of each registration's destination.
# The device matters (CodeRabbit, PR #40): `_db_backup_dest` can put the
# copy in the TEMPDIR, which on a Fly machine is the root filesystem and
# not the data volume at all. A copy landing somewhere else is no reason
# to stop watching the volume the database lives on.
_active: Counter = Counter()
_devices: list[tuple[str, int | None]] = []

# Restore drives a multi-stage job across a route AND a background task,
# so it is read from its own state dict rather than wrapped in a `with`.
_RESTORE_BUSY = ("receiving", "validating", "swapping")


def _device_of(path) -> int | None:
    """The filesystem a path is on, by st_dev. Walks up to the nearest
    existing ancestor: the destination file does not exist yet when the
    job registers."""
    if path is None:
        return None
    p = Path(path).resolve()
    for candidate in (p, *p.parents):
        try:
            return os.stat(candidate).st_dev
        except OSError:
            continue
    return None


@contextmanager
def running(label: str, dest=None) -> Iterator[None]:
    """Register a whole-database copy for the length of the block.

    `dest` is where the copy is being written; pass it so a watcher can
    ask about ITS filesystem rather than treating every copy anywhere as
    a reason to stop measuring."""
    entry = (label, _device_of(dest))
    with _lock:
        _active[label] += 1
        _devices.append(entry)
    try:
        yield
    finally:
        with _lock:
            _active[label] -= 1
            if _active[label] <= 0:
                del _active[label]
            try:
                _devices.remove(entry)
            except ValueError:        # pragma: no cover — paired by construction
                pass


def in_flight(path=None) -> str | None:
    """A label for one job in flight, or None.

    With `path`, only jobs writing to THAT filesystem count — plus any
    job that did not say where it was writing, which is the safe reading
    of "we do not know". Without it, any job counts."""
    want = _device_of(path) if path is not None else None
    with _lock:
        if want is not None:
            for label, dev in _devices:
                if dev is None or dev == want:
                    return label
        elif _active:
            return sorted(_active)[0]
    # A restore always writes beside the database (restore.upload_dest is
    # on the live file's filesystem by design), so it counts for that
    # device as much as for the unfiltered question — the path-filtered
    # branch used to return before this was ever asked, and the watchdog
    # only ever asks with a path (2.4 review).
    # Imported here, not at module scope: restore.py is the heavier module
    # and this one is imported from the watchdog path on every tick.
    try:
        from .restore import JOB
        if JOB.get("state") in _RESTORE_BUSY:
            return "restore"
    except Exception:            # pragma: no cover — import-time only
        pass
    return None


def _reset_for_tests() -> None:
    with _lock:
        _active.clear()
        _devices.clear()
