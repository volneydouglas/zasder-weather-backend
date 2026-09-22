"""The two bundled alert tones (2.4, item 1).

Doren asked for a warning and a watch to sound different. An APNs
payload can name "default" or a file that ships INSIDE the app, and iOS
will not lend an app one of its own UISounds, so the app carries two of
its own (bin/make-alert-sounds.py generates them).

What is pinned here is the CHOICE, which is the part that can silently
regress: a tone named for a tier nobody sends, or a filename that stops
matching what the app bundles, is a push that arrives with the wrong
character and no error anywhere.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import apns  # noqa: E402

# That the app actually SHIPS the files these names point at is checked
# by bin/tests/test_alert_sound_files.py, which is outside this package
# on purpose: the public mirror carries backend/ and no ios/ at all.


def test_the_ladder_is_loud_then_quiet_then_default():
    assert apns.sound_for("warning") == "zw-warning.caf"
    # A watch wakes you; it does not get the urgent tone.
    assert apns.sound_for("major") == "zw-watch.caf"
    assert apns.sound_for("watch") == "zw-watch.caf"
    # The quietest tier keeps the system sound: a note that waits for
    # morning has no business having a voice of its own.
    assert apns.sound_for("info") == "default"
    assert apns.sound_for(None) == "default"
    assert apns.sound_for("nonsense") == "default"


def test_the_payload_carries_the_tone_and_still_defaults():
    aps = apns.build_payload("t", "b", sound="zw-warning.caf")["aps"]
    assert aps["sound"] == "zw-warning.caf"
    # Unasked, every existing caller keeps the sound it has always had.
    assert apns.build_payload("t", "b")["aps"]["sound"] == "default"
