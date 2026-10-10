"""What the public pages may show is a cache dependency (2.6, follow-up
review F26-01).

The public page's HTML is cached, served stale for up to a day while it
rebuilds, and since 2.6 it carries location-bearing output: the lightning
rings' map tiles and the station-map link, both derived from the map
sharing settings. Turning sharing off, coarsening its precision or moving
it to another station must stop that output reaching the NEXT visitor at
once, not when the cache happens to expire.

So every change to what may be published bumps a process-wide generation.
A cached page remembers the generation it was built under and is served
only while that generation is current (fresh or stale alike), and a build
that started under an older generation is thrown away when it finishes
instead of being stored. Process-wide is enough: the server runs one
process, and a restart starts with an empty cache.
"""
from __future__ import annotations

_GENERATION = 0


def generation() -> int:
    return _GENERATION


def bump() -> int:
    """Something that decides what may be published changed."""
    global _GENERATION
    _GENERATION += 1
    return _GENERATION
