"""Probation for a device that looks like a corrupted twin of a known one.

On 2026-08-15 a single 433 MHz packet from AcuRite Atlas #711 decoded with
the high byte of its station ID lost — 0x0002C7 read as 0x0000C7. The relay
synthesizes a device MAC from that ID, so one bad packet minted
`5D:5D:01:00:00:C7`, a device row that never existed, alongside the real
`5D:5D:01:00:02:C7`. It then went stale and **emailed a device-down alert**
about a station that was never real.

The fix is deliberately narrow. Quarantining every unknown MAC would delay
legitimate setup for everyone, and the failure mode is specific: the bogus
MAC is always a near neighbour of a real one, because a bit flip is a small
edit. So only near neighbours serve probation; anything genuinely new
registers immediately, exactly as before.

Probation is not rejection. A real station transmits continuously and clears
the bar in minutes. A corrupt packet is a one-off and never does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("device-probation")

_POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def _octets(mac: str) -> list[int] | None:
    """A MAC's six bytes, or None if it is not six hex octets."""
    parts = mac.replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        vals = [int(p, 16) for p in parts]
    except ValueError:
        return None
    return vals if all(0 <= v <= 255 for v in vals) else None


def bit_distance(a: str, b: str) -> int | None:
    """Bits that differ between two MACs, or None if either is unparseable."""
    oa, ob = _octets(a), _octets(b)
    if oa is None or ob is None:
        return None
    return sum(_POPCOUNT[x ^ y] for x, y in zip(oa, ob))


def suspect_of(mac: str, known: list[str], max_bits: int) -> str | None:
    """The known MAC this one looks like a corruption of, if any.

    Requires an identical first three octets — the vendor OUI on a real MAC,
    and `5D:5D:<type>` on the synthetic ones the relays mint — so a device of
    a different make or a different sensor type is never a suspect. Within
    that family, a small bit distance is the signature of a damaged frame.

    Ties go to the CLOSEST known MAC so the log names the right neighbour.
    """
    if max_bits <= 0:
        return None
    mine = _octets(mac)
    if mine is None:
        return None
    best: tuple[int, str] | None = None
    for k in known:
        if k.upper() == mac.upper():
            continue
        theirs = _octets(k)
        if theirs is None or theirs[:3] != mine[:3]:
            continue
        d = sum(_POPCOUNT[x ^ y] for x, y in zip(mine[3:], theirs[3:]))
        if d <= max_bits and (best is None or d < best[0]):
            best = (d, k)
    return best[1] if best else None


@dataclass(frozen=True)
class Verdict:
    """What to do with a reading from an unknown MAC."""
    admit: bool
    suspect_of: str | None
    hits: int
    needed: int
    counted: bool  # whether this sighting advanced the counter

    @property
    def quarantined(self) -> bool:
        return not self.admit


def decide(*, prior_hits: int, prior_ms: int | None, now_ms: int,
           suspect: str | None, needed: int, min_gap_ms: int) -> Verdict:
    """Whether an unknown MAC has proven itself yet.

    A sighting only advances the counter if it is `min_gap_ms` after the last
    counted one. Stations retransmit the same reading and receivers emit
    duplicates within seconds, so counting raw sightings would let one burst
    clear the bar — which is exactly the thing being guarded against.
    """
    if suspect is None or needed <= 0:
        return Verdict(admit=True, suspect_of=suspect, hits=prior_hits,
                       needed=needed, counted=False)
    counted = prior_ms is None or (now_ms - prior_ms) >= min_gap_ms
    hits = prior_hits + 1 if counted else prior_hits
    return Verdict(admit=hits >= needed, suspect_of=suspect, hits=hits,
                   needed=needed, counted=counted)
