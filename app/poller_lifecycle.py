"""The lifecycle every cloud poller shares (2.1 pre-release review BE-4).

An optional source must never be on the boot path: start() registers the
task and returns; discovery, metadata lookups and bootstraps run INSIDE
the task; stop() cancels the task whatever it is doing and waits a bounded
time for it to go. Before this, one provider (Ecowitt) had the shape and
five did vendor I/O in start() with a 15 s timeout per call, so a dead
AmbientWeather held the lifespan about a minute and every settings PUT
that restarts a poller waited on the old credentials' timeout.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("poller")

# How long stop() waits for a cancelled task to unwind. A vendor call that
# ignores cancellation (a blocking resolver inside httpx) is abandoned
# after this; the task finalizes on its own later.
STOP_TIMEOUT_S = 5.0


async def reap(task: "asyncio.Task | None", name: str) -> None:
    """Cancel `task` and wait up to STOP_TIMEOUT_S for it. Never raises:
    the settings PUT and the lifespan shutdown must not wait on, or fail
    for, a poller that will not die."""
    if task is None:
        return
    task.cancel()
    # asyncio.wait, not wait_for: on its timeout wait_for cancels the task
    # AGAIN and then awaits it, so a task that swallows cancellation held
    # the caller forever and the "abandoned" branch below could never run
    # (round-three review §5 row 6: the test written for it hung the
    # suite). wait() just stops waiting; the task is left to finish on
    # its own, which is what "abandoned" means.
    try:
        done, _pending = await asyncio.wait({task}, timeout=STOP_TIMEOUT_S)
    except asyncio.CancelledError:
        # The CALLER being cancelled while it waits here must keep
        # propagating, or a lifespan shutdown that is itself cancelled
        # carries on as if nothing happened (CodeRabbit, PR #36).
        raise
    if not done:
        log.warning("%s did not stop within %.0fs; abandoned", name, STOP_TIMEOUT_S)
        return
    try:
        task.result()
    except asyncio.CancelledError:
        pass                                           # the expected end
    except Exception:                                  # noqa: BLE001
        log.exception("%s raised while stopping", name)
