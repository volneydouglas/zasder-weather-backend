"""Every credential the app can read must be blanked by conftest (2.1
pre-release review, round two I4).

conftest.temp_env sets each credential env var to "" so a developer's
.env — which Settings reads through env_file — can never boot a real
poller, send a real push, or hit a live API from inside the suite. The
list there was extended by hand each time a credential landed, and it
was missed for the Tempest token (1.6), AirGradient (1.8), Ecowitt (2.0)
and the Fly trio (2.1): the pattern is the bug. This test derives the
set of credential names from the code and fails on any name conftest
does not blank or this file does not allow-list with a reason.
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# A name is a credential when it ends like one. Settings field names are
# upper-cased (pydantic reads the env var of the same name).
CREDENTIAL = re.compile(r"(TOKENS?|KEY|KEYS|SECRET|PASSWORD|_P8)$")

# Names that look like credentials and are deliberately NOT blanked.
# Each entry needs a reason a reviewer can check. Empty today.
ALLOWED: dict[str, str] = {}

# Modules bin/strip-*-for-public.py remove from the public mirror. Their
# credential names are forbidden words in the mirror (the strip guard
# greps for them), so neither this file nor conftest may spell them; the
# hosted relay's own test module sets what it needs explicitly.
STRIPPED_MODULES = {"relay.py", "meter.py"}


def _settings_env_names() -> set[str]:
    text = (APP / "config.py").read_text(encoding="utf-8")
    names = set()
    for m in re.finditer(r"^\s{4}([a-z][a-z0-9_]*)\s*:\s*[^=\n]+?(?:=|$)", text, re.M):
        names.add(m.group(1).upper())
    return names


def _direct_env_names() -> set[str]:
    names = set()
    pat = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
                     r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""")
    for f in APP.glob("*.py"):
        if f.name in STRIPPED_MODULES:
            continue
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            names.add(m.group(1) or m.group(2))
    return names


def _blanked_by_conftest() -> set[str]:
    text = CONFTEST.read_text(encoding="utf-8")
    # setenv("NAME", ...) calls and the quoted names inside the blanking
    # tuple both count; a name merely mentioned in a comment does not
    # (comments are stripped first).
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return set(re.findall(r'"([A-Z][A-Z0-9_]*)"', code))


def test_every_credential_the_app_reads_is_blanked_or_allow_listed():
    credentials = {n for n in _settings_env_names() | _direct_env_names()
                   if CREDENTIAL.search(n)}
    assert credentials, "the scan found no credential names at all; the regexes rotted"
    blanked = _blanked_by_conftest()
    missing = sorted(credentials - blanked - set(ALLOWED))
    assert not missing, (
        "credential env var(s) the suite does not blank — a developer .env "
        f"reaches the tests through Settings' env_file: {missing}. Add each "
        "to conftest.temp_env's blanking tuple (setenv(\"\"), never delenv) "
        "or to ALLOWED here with a reason.")
    stale = sorted(set(ALLOWED) - credentials)
    assert not stale, f"ALLOWED names the code no longer reads: {stale}"
