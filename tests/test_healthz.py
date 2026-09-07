"""/healthz (round-two review, SEC-F2): the probe opens the database
READ-ONLY and reads sqlite_master, so a corrupt file and a missing file
both answer 503, and a liveness check can never create an empty
schemaless weather.db on a volume that remounted bare. One probe answers
every caller for a few seconds: the route is unauthenticated."""
import os


def _reset():
    from app import main
    main._HEALTHZ_CACHE = None


def test_a_real_database_answers_ok(client, temp_env):
    _reset()
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is True and body["status"] == "ok"
    assert isinstance(body["uid"], int)


def test_a_corrupt_database_is_degraded(client, temp_env):
    _reset()
    with open(temp_env, "wb") as f:
        f.write(b"this is not a database, it is sixty-four bytes of nothing at all!!")
    for side in ("-wal", "-shm"):
        try:
            os.remove(temp_env + side)
        except FileNotFoundError:
            pass
    r = client.get("/healthz")
    assert r.status_code == 503, r.text
    assert r.json()["db"] is False and r.json()["status"] == "degraded"


def test_a_missing_database_is_degraded_and_nothing_is_created(client, temp_env):
    _reset()
    for side in ("", "-wal", "-shm"):
        try:
            os.remove(temp_env + side)
        except FileNotFoundError:
            pass
    r = client.get("/healthz")
    assert r.status_code == 503, r.text
    assert r.json()["db"] is False
    # The old probe's sqlite3.connect(path) created this file; mode=ro
    # refuses to, so a bare volume stays visibly bare.
    assert not os.path.exists(temp_env), "the liveness probe created a database"


def test_the_probe_is_cached_for_a_few_seconds(client, temp_env, monkeypatch):
    from app import main
    _reset()
    calls = {"n": 0}
    real = main._probe_database

    def counted(path):
        calls["n"] += 1
        return real(path)
    monkeypatch.setattr(main, "_probe_database", counted)
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert calls["n"] == 1, "the second call inside the window re-probed"
    monkeypatch.setattr(main, "HEALTHZ_CACHE_S", 0.0)
    _reset()
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert calls["n"] == 3


def test_concurrent_probes_are_single_flight(client, temp_env, monkeypatch):
    """Round-three review SEC-G4: the cache was written after the probe, so
    anonymous requests arriving during a slow probe each took a thread on
    the shared executor. One probe answers everyone."""
    import asyncio
    import time as _t
    from app import main
    _reset()
    main._HEALTHZ_LOCK = None
    probes = []

    def slow_probe(path):
        probes.append(path)
        _t.sleep(0.05)
        return True
    monkeypatch.setattr(main, "_probe_database", slow_probe)

    async def burst():
        return await asyncio.gather(*(main._database_healthy() for _ in range(6)))
    assert asyncio.run(burst()) == [True] * 6
    assert len(probes) == 1, probes
