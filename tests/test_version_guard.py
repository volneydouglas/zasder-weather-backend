"""T14 (2.1 pre-release review): the changelog's newest heading and
`__version__` cannot drift apart at release time.

While a version is in development the changelog carries a section dated
"unreleased" ahead of the running version (2.1.0 over 2.0.1) and that is
fine. The moment that heading gets a date, it is a release, and the code
must say the same number: tagging with the two apart makes every
instance banner "update available" forever (updates.py compares the
running version against the newest release).
"""
import re
from pathlib import Path

from app.version import __version__

# The monorepo keeps the changelog in public-template/; the generated
# mirror flattens it to its root. This test ships to both. The LOCAL copy
# wins: a mirror checkout that happens to sit inside a directory holding
# a public-template/ must read its own files, not that one's (round three
# I3-3). The monorepo layout is recognised by its own shape -- this file
# under backend/tests/ with public-template/ beside backend/.
_HERE = Path(__file__).resolve()
_MONOREPO = (_HERE.parents[1].name == "backend"
             and (_HERE.parents[2] / "public-template").is_dir())
CHANGELOG = next(p for p in (_HERE.parents[1] / "CHANGELOG.md",
                             _HERE.parents[2] / "public-template" / "CHANGELOG.md")
                 if p.exists())
HEADING = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]\s*[—-]\s*(.+?)\s*$", re.M)


def _newest():
    text = CHANGELOG.read_text(encoding="utf-8")
    m = HEADING.search(text)
    assert m, "no '## [x.y.z] — date' heading in the changelog"
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4)


def _running():
    return tuple(int(p) for p in __version__.split("."))


# ios/project.yml exists only in the monorepo; the mirror ships the
# backend alone, so the app-side check skips there -- and a mirror
# checkout nested under some other tree must not pick that tree's copy.
PROJECT_YML = (_HERE.parents[2] / "ios" / "project.yml" if _MONOREPO
               else _HERE.parents[1] / "ios" / "project.yml")


def _app_versions():
    """(MARKETING_VERSION, CURRENT_PROJECT_VERSION) from project.yml, read
    as text so the guard needs no YAML parser."""
    text = PROJECT_YML.read_text(encoding="utf-8")
    mv = re.search(r'^\s*MARKETING_VERSION:\s*"?([\d.]+)"?', text, re.M)
    bn = re.search(r'^\s*CURRENT_PROJECT_VERSION:\s*"?(\d+)"?', text, re.M)
    assert mv and bn, "project.yml lost MARKETING_VERSION / CURRENT_PROJECT_VERSION"
    return tuple(int(x) for x in mv.group(1).split(".")), int(bn.group(1))


def test_the_changelog_is_never_behind_the_code():
    newest, _ = _newest()
    assert newest >= _running(), (
        f"changelog newest section {newest} is behind __version__ {__version__}")


def test_a_dated_heading_is_a_release_and_must_match_the_code():
    newest, date = _newest()
    if date.strip().lower() == "unreleased":
        return   # in development: the heading may run ahead of the code
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.strip()), f"odd heading date {date!r}"
    assert newest == _running(), (
        f"the changelog dates {newest} as released but the code says "
        f"{__version__}; bump app/version.py (and ios/project.yml) before tagging")


def test_a_dated_heading_means_the_apps_carry_the_same_number():
    """Round two (I1): the guard read only app/version.py, and its message
    told you to bump project.yml without ever looking at it. The apps and
    the backend share one release number (project.yml's own comment), so
    a dated heading pins MARKETING_VERSION too. The build number is only
    checked for shape: App Store Connect refuses a re-upload at the same
    version+build, and nothing here can know the last upload."""
    if not PROJECT_YML.exists():
        return   # the generated mirror carries no app
    newest, date = _newest()
    marketing, build = _app_versions()
    assert build > 0
    if date.strip().lower() == "unreleased":
        assert marketing <= newest, (
            f"ios/project.yml says {marketing} but the changelog's newest "
            f"section is {newest}")
        return
    assert marketing == newest, (
        f"the changelog dates {newest} as released but ios/project.yml says "
        f"MARKETING_VERSION {marketing}; bump it (and CURRENT_PROJECT_VERSION) "
        "before tagging")
