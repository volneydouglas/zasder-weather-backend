"""A deterministic, deliberately untidy forecast archive for the 2.7
per-source scorecard tests.

Built once from a seeded RNG so the golden file
(`fixtures/forecast_skill_golden.json`) can pin what the scorecard said
on this exact data BEFORE the 2.7 refactor made the grader take a source.
Every awkward case the grader has a rule for is in here: several issues a
day (only the last word counts), a missing low (no_forecast), a missing
measurement, a pre-2.3 row with no span, a partial day, a gauge-less day,
a missing probability, runs filed for another sky, a lead-0 nowcast, and
future valid dates.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

MAC = "AA:BB:CC:00:27:F5"
TODAY = date(2026, 8, 30)
FAR = (40.0, -105.0)          # another sky, ~900 km from the station
HERE = (33.3, -111.9)

# provider -> (warm bias °F, spread °F, the last lead it files)
PROVIDERS = {
    "open-meteo": (1.5, 2.0, 6),
    "twc": (-0.5, 3.0, 4),
    "open-meteo-gfs": (3.0, 4.0, 6),
}


def _ms(on: date, hour: int) -> int:
    return int(datetime.combine(on, time(hour), tzinfo=timezone.utc)
               .timestamp() * 1000)


async def build(db, *, providers=PROVIDERS, days: int = 130,
                seed: int = 27) -> None:
    rng = random.Random(seed)
    first = TODAY - timedelta(days=days)
    async with db.connect() as conn:
        for i in range(days + 1):
            d = first + timedelta(days=i)
            if d >= TODAY:
                break
            hi = 95.0 + rng.uniform(-8, 8)
            lo = 70.0 + rng.uniform(-6, 6)
            rain = rng.choice([0.0, 0.0, 0.0, 0.0, 0.05, 0.3, None])
            span: float | None = 23.9
            r = rng.random()
            if r < 0.04:
                span = None             # folded before 2.3
            elif r < 0.08:
                span = 5.0              # the station was up for a morning
            m_hi: float | None = hi
            if rng.random() < 0.03:
                m_hi = None             # no measured high
            if rng.random() < 0.05:
                continue                # no row for the day at all
            f = l_ = None
            if span is not None:
                f = _ms(d, 0)
                l_ = f + int(span * 3_600_000)
            await conn.execute(
                "INSERT OR REPLACE INTO daily_rollups "
                "(mac, day, tempf_min, tempf_max, rain_total, obs_first_ms, obs_last_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (MAC, d.isoformat(), lo, m_hi, rain, f, l_))
        await conn.commit()

    # One bulk insert rather than a db.insert_forecast_snapshots per run:
    # the same rows (its prune cannot bite inside a 130-day span), in a
    # fraction of the time.
    filed: list[tuple] = []
    for provider, (bias, spread, max_lead) in providers.items():
        prng = random.Random(f"{seed}-{provider}")
        for i in range(days + 8):
            issue_day = first + timedelta(days=i)
            if issue_day > TODAY:
                break
            # Four runs a day; the later run is the one that counts.
            for hour in (0, 6, 12, 18):
                rows = []
                for lead in range(0, max_lead + 1):
                    valid = issue_day + timedelta(days=lead)
                    hi = 95.0 + bias + prng.uniform(-spread, spread)
                    lo = 70.0 + bias + prng.uniform(-spread, spread)
                    pop: float | None = prng.choice([0, 10, 30, 60, 80])
                    if prng.random() < 0.05:
                        pop = None
                    tmin: float | None = lo
                    if prng.random() < 0.03:
                        tmin = None
                    rows.append({"valid_date": valid.isoformat(),
                                 "lead_days": lead, "tmax_f": hi,
                                 "tmin_f": tmin, "pop": pop,
                                 "precip_in": 0.0})
                sky = FAR if prng.random() < 0.04 else (
                    None if prng.random() < 0.3 else HERE)
                for r in rows:
                    filed.append((provider, _ms(issue_day, hour), r["valid_date"],
                                  r["lead_days"], r["tmax_f"], r["tmin_f"], r["pop"],
                                  r["precip_in"],
                                  None if sky is None else sky[0],
                                  None if sky is None else sky[1]))
    async with db.connect() as conn:
        await conn.executemany(
            "INSERT INTO forecast_snapshots (provider, issued_ms, valid_date, "
            "lead_days, tmax_f, tmin_f, pop, precip_in, lat, lon) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", filed)
        await conn.commit()
