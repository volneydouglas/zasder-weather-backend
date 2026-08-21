"""The stripped-build boot guard.

Regression for 2026-08-16: the generated public mirror was deployed onto the
hosted instance, deleting /api/relay/* for every user of the app for 8h17m.
`/healthz` and `/api/version` both stayed green throughout, which is exactly
why the guard has to be a startup failure rather than another status field.
"""

import pytest

from app import build_guard


def test_inert_when_flag_unset(monkeypatch):
    """Self-hosters never set REQUIRE_RELAY — the guard must not fire for them
    even though their build genuinely has no relay module."""
    monkeypatch.delenv("REQUIRE_RELAY", raising=False)
    monkeypatch.setattr(build_guard, "_FULL_BUILD_MODULES", ("app.does_not_exist",))
    build_guard.assert_build_variant()   # must not raise


@pytest.mark.skipif(
    build_guard.missing_full_build_modules() != [],
    reason="stripped public build — the private modules are absent by design, "
           "so the guard is SUPPOSED to fire here",
)
def test_passes_on_the_full_build(monkeypatch):
    monkeypatch.setenv("REQUIRE_RELAY", "1")
    build_guard.assert_build_variant()   # the private modules are all present here
    assert build_guard.missing_full_build_modules() == []


def test_raises_when_relay_is_missing(monkeypatch):
    """The failure that matters: flagged as the relay host, but stripped."""
    monkeypatch.setenv("REQUIRE_RELAY", "1")
    monkeypatch.setattr(build_guard, "_FULL_BUILD_MODULES",
                        ("app.relay", "app.stripped_away"))
    with pytest.raises(RuntimeError) as e:
        build_guard.assert_build_variant()
    msg = str(e.value)
    assert "STRIPPED public build" in msg
    assert "app.stripped_away" in msg
    # The message must say what to do, not just what broke — it will be read
    # off a failed deploy log by someone who is already stressed.
    assert "monorepo backend/" in msg


@pytest.mark.parametrize("val,should_fire", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("  ", False),
])
def test_flag_parsing(monkeypatch, val, should_fire):
    monkeypatch.setenv("REQUIRE_RELAY", val)
    monkeypatch.setattr(build_guard, "_FULL_BUILD_MODULES", ("app.not_here",))
    if should_fire:
        with pytest.raises(RuntimeError):
            build_guard.assert_build_variant()
    else:
        build_guard.assert_build_variant()
