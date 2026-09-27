"""The opt-in update request file (mirror issue #5, adam8833's proposal).

Off Fly, one-tap update has no machine to rewrite. With UPDATE_REQUEST_FILE
set, the button instead writes the release tag the server itself vetted to
that one file and answers 202; whatever the operator runs on the host (a
systemd path unit, a launchd WatchPaths job, nothing) decides what to do
with it. The web process never runs a command and never writes anything
it did not check: the same gates as the Fly path (newer, same major or a
vouched major, image published) and a strict X.Y.Z shape, because a
watcher may well interpolate the file's contents into a shell line.
"""
from __future__ import annotations

import pytest

from app.version import __version__ as _CURRENT

_MAJOR = _CURRENT.split(".")[0]
NEWER = f"{_MAJOR}.999.0"
NEXT_MAJOR = f"{int(_MAJOR) + 1}.0.0"
H = {"Authorization": "Bearer test-api-token"}


async def _true(*_a):
    return True


async def _false(*_a):
    return False


@pytest.fixture
def req(client, monkeypatch, tmp_path):
    """Off Fly (conftest blanks the Fly trio), request file configured, a
    newer release known and its image published."""
    from app import main, self_update
    path = tmp_path / "update-request"
    monkeypatch.setenv("UPDATE_REQUEST_FILE", str(path))
    monkeypatch.setattr(self_update, "image_exists", _true)
    main.app.state.update_info = {"version": _CURRENT, "latest": NEWER,
                                  "update_available": True,
                                  "checked_ms": 1, "enabled": True}
    return path


def test_the_button_writes_the_vetted_tag_and_runs_nothing(client, req, monkeypatch):
    from app import self_update
    machine_calls: list[str] = []

    async def fly(tag):
        machine_calls.append(tag)
        return True
    monkeypatch.setattr(self_update, "apply_update", fly)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 202, r.text
    assert r.json()["requested"] == NEWER
    assert req.read_text() == f"v{NEWER}\n"
    assert machine_calls == []
    # Written whole through a temp file and a rename: nothing left beside it,
    # and readable by a watcher running as another user.
    assert [p.name for p in req.parent.iterdir()] == ["update-request"]
    assert req.stat().st_mode & 0o044 == 0o044


def test_the_snapshot_is_laid_before_the_request_is_written(client, req, monkeypatch):
    """Review on PR #47: the Fly path snapshots the database before the
    swap and this path acknowledged without one. The host's updater runs
    migrations as soon as it sees the file, so the net goes down first."""
    from app import self_update
    order: list[str] = []

    async def snap(tag):
        order.append(f"snapshot {tag}")
        assert not req.exists(), "request written before the snapshot"
        return None
    real_write = self_update.write_update_request

    def write(tag):
        order.append(f"write {tag}")
        return real_write(tag)
    monkeypatch.setattr(self_update, "snapshot_before_upgrade", snap)
    monkeypatch.setattr(self_update, "write_update_request", write)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 202
    assert order == [f"snapshot {NEWER}", f"write {NEWER}"]


def test_a_real_snapshot_lands_beside_the_database(client, req):
    """End to end, no mocks: the pre-upgrade copy exists after the request."""
    from pathlib import Path
    from app.config import settings
    client.post("/api/update/apply", headers=H)
    db = Path(settings.database_path)
    snaps = list(db.parent.glob(f"{db.name}.pre-upgrade-{NEWER}.db"))
    assert len(snaps) == 1 and snaps[0].stat().st_size > 0


def test_version_reports_the_mode_and_the_pending_request(client, req):
    body = client.get("/api/version").json()
    assert body["one_tap"] == "request_file"
    assert body["update_request"] is None
    client.post("/api/update/apply", headers=H)
    pending = client.get("/api/version").json()["update_request"]
    assert pending["tag"] == NEWER and pending["requested_ms"] > 0


@pytest.mark.parametrize("content", [f"v{_CURRENT}\n", "v0.0.1\n", "garbage", ""])
def test_a_request_that_is_installed_or_unreadable_is_not_pending(client, req, content):
    req.write_text(content)
    assert client.get("/api/version").json()["update_request"] is None


@pytest.mark.parametrize("latest", [f"{NEWER}; rm -rf ~", f"{NEWER}\nv9.9.9",
                                    f"{NEWER}-rc1", "../../etc/passwd"])
def test_the_file_never_carries_what_the_check_did_not_vet(client, req, latest):
    from app import main
    main.app.state.update_info["latest"] = latest
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409
    assert not req.exists()


def test_the_fly_gates_hold_before_anything_is_written(client, req, monkeypatch):
    from app import main, self_update
    monkeypatch.setattr(self_update, "image_exists", _false)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "no published image" in r.json()["detail"]
    assert not req.exists()

    monkeypatch.setattr(self_update, "image_exists", _true)
    monkeypatch.setattr(self_update, "upgrade_manifest_allows", _false)
    main.app.state.update_info["latest"] = NEXT_MAJOR
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "major" in r.json()["detail"]
    assert not req.exists()

    monkeypatch.setattr(self_update, "upgrade_manifest_allows", _true)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 202 and req.read_text() == f"v{NEXT_MAJOR}\n"


def test_an_unwritable_path_says_which_setting(client, req, monkeypatch, tmp_path):
    missing = tmp_path / "no-such-dir" / "update-request"
    monkeypatch.setenv("UPDATE_REQUEST_FILE", str(missing))
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 500
    assert "UPDATE_REQUEST_FILE" in r.json()["detail"]
    assert not missing.parent.exists()


def test_a_relative_path_is_not_a_request_file(client, req, monkeypatch):
    """The server's cwd is not something an operator reasons about; a
    relative path would land wherever uvicorn happened to start."""
    monkeypatch.setenv("UPDATE_REQUEST_FILE", "update-request")
    assert client.get("/api/version").json()["one_tap"] is None
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "UPDATE_REQUEST_FILE" in r.json()["detail"]


def test_on_fly_the_machine_path_wins(client, req, monkeypatch):
    """A Fly box with the variable set still updates its own machine; the
    request file is for installs that have no machine to rewrite."""
    monkeypatch.setenv("FLY_APP_NAME", "zw-test")
    monkeypatch.setenv("FLY_MACHINE_ID", "d891234")
    assert client.get("/api/version").json()["one_tap"] == "fly"
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "deploy token" in r.json()["detail"]
    assert not req.exists()
    req.write_text(f"v{NEWER}\n")
    assert client.get("/api/version").json()["update_request"] is None


def test_without_the_setting_nothing_changes(client, monkeypatch):
    from app import main
    main.app.state.update_info = {"version": _CURRENT, "latest": NEWER,
                                  "update_available": True,
                                  "checked_ms": 1, "enabled": True}
    body = client.get("/api/version").json()
    assert body["one_tap"] is None and body["update_request"] is None
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "upgrade.sh" in detail and "UPDATE_REQUEST_FILE" in detail
    assert "deploy token" not in detail.lower()


def test_the_on_demand_check_answers_in_the_same_shape(client, req, monkeypatch):
    """The apps REPLACE their version info with /api/update/check's answer
    ("Check for updates now"). Without one_tap there the mode read as
    unknown after a check and the dead Update button came back on a box
    that cannot update itself (found building the C16 app half)."""
    from app import main
    checker = main.app.state.update_checker

    async def fake_check():
        return True
    monkeypatch.setattr(checker, "_check_once", fake_check)
    monkeypatch.setenv("UPDATE_CHECK", "1")
    r = client.post("/api/update/check", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["one_tap"] == "request_file" and "update_request" in body
    assert set(body) == set(client.get("/api/version").json())


def _dotenv_only(monkeypatch, tmp_path, lines: str) -> None:
    """A bare install: the option lives in `.env` in the server's cwd and
    nowhere in the process environment. delenv is safe here, unlike the
    conftest rule's credentials: cwd is a tmp dir holding only this file,
    so nothing can fall back to the developer's real `.env`."""
    from app import envfile
    (tmp_path / ".env").write_text(lines)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(envfile, "_CACHE", None)
    monkeypatch.setattr(envfile, "_UNDER_TEST", lambda: False)


def test_a_request_file_set_only_in_dotenv_is_honoured(monkeypatch, tmp_path):
    """Mirror issue #5 round two (adam8833): pydantic-settings reads `.env`
    into Settings but never into os.environ, so outside Docker the button
    said it could not update while `.env` named the file."""
    from app import self_update
    target = tmp_path / "update-request"
    _dotenv_only(monkeypatch, tmp_path, f"UPDATE_REQUEST_FILE={target}\n")
    monkeypatch.delenv("UPDATE_REQUEST_FILE", raising=False)
    assert self_update.one_tap_mode() == "request_file"
    assert self_update.write_update_request("2.5.0") == target
    assert target.read_text().strip() == "v2.5.0"


def test_the_process_environment_still_wins_over_dotenv(monkeypatch, tmp_path):
    """Docker's env_file and `export` keep their precedence, and an empty
    value counts as set (pydantic's rule), so conftest's blanking holds."""
    from app import envfile
    _dotenv_only(monkeypatch, tmp_path, "UPDATE_REQUEST_FILE=/from/dotenv\n")
    monkeypatch.setenv("UPDATE_REQUEST_FILE", "/from/env")
    assert envfile.env_value("UPDATE_REQUEST_FILE") == "/from/env"
    monkeypatch.setenv("UPDATE_REQUEST_FILE", "")
    assert envfile.env_value("UPDATE_REQUEST_FILE") == ""


def test_the_suite_never_reads_the_developers_dotenv(monkeypatch, tmp_path):
    from app import envfile
    (tmp_path / ".env").write_text("ZW_PROBE_ONLY_IN_DOTENV=leaked\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(envfile, "_CACHE", None)
    assert envfile.env_value("ZW_PROBE_ONLY_IN_DOTENV", "absent") == "absent"


@pytest.mark.parametrize("module, attr, name, value, expect", [
    ("app.self_update", "_enabled", "AUTO_UPDATE", "1", True),
    ("app.self_update", "_image_repo", "AUTO_UPDATE_IMAGE_REPO", "ghcr.io/x/y", "ghcr.io/x/y"),
    ("app.limits", "_max_bytes", "MAX_REQUEST_BYTES", "12345", 12345),
    ("app.oauth", "_allowed_hosts", "ALLOWED_HOSTS", "a.example,B.example", ["a.example", "b.example"]),
])
def test_every_direct_read_takes_dotenv(monkeypatch, tmp_path, module, attr, name, value, expect):
    """The same bug in each module that read os.environ for a documented
    option; the helper is the one door."""
    import importlib
    mod = importlib.import_module(module)
    _dotenv_only(monkeypatch, tmp_path, f"{name}={value}\n")
    monkeypatch.delenv(name, raising=False)
    assert getattr(mod, attr)() == expect


def test_no_documented_option_is_read_from_os_environ():
    """The guard for the family: every option `.env.example` documents is
    read through Settings or envfile.env_value, so a bare install's `.env`
    reaches it. Platform-injected names (FLY_*) are not documented there
    and stay direct reads."""
    import glob
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    documented: set[str] = set()
    for example in (root / ".env.example", root.parent / "public-template" / ".env.example"):
        if example.exists():
            documented |= set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{2,})=",
                                         example.read_text(), re.M))
    assert "UPDATE_REQUEST_FILE" in documented
    direct = re.compile(r"""os\.(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z0-9_]+)""")
    offenders = [f"{Path(f).name}:{i}: {m.group(1)}"
                 for f in glob.glob(str(root / "app" / "*.py"))
                 for i, line in enumerate(open(f), 1)
                 for m in direct.finditer(line) if m.group(1) in documented]
    assert not offenders, offenders
