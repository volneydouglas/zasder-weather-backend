"""The report emails' palette (2.4, item 11).

Doren's idea, from the night his 10 PM outlook arrived light over a card
drawn for dark. D7 stopped the client inventing a scheme; this is the
setting that says which one to send.

The substitution is the risky part and is what these check: a light
email with one dark colour left in it is a hole in the middle of
somebody's card, and it would look exactly like a rendering bug.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import email_card as ec  # noqa: E402

H = {"Authorization": "Bearer test-api-token"}


def _card(theme: str) -> str:
    return ec.shell("Sunday, September 21", "Morning report",
                    ec.tile_row([ec.tile("HIGH", "101&deg;")])
                    + ec.prose("It rained."),
                    "Measured in your backyard.", theme=theme)


def _body(html: str) -> str:
    return html.split("</head>", 1)[1]


def test_the_light_card_keeps_no_dark_colour_anywhere():
    body = _body(_card("light"))
    left = sorted(c for c in ec.LIGHT if c in body)
    assert left == [], f"dark colours survived into the light card: {left}"
    # And it is actually light, not merely not-dark.
    assert ec.LIGHT[ec.BG] in body and ec.LIGHT[ec.TEXT] in body


def test_the_dark_card_is_exactly_what_it_always_was():
    body = _body(_card("dark"))
    assert ec.BG in body and ec.TEXT in body
    assert ec.LIGHT[ec.BG] not in body


def test_follow_your_device_sends_light_pixels_and_asks_for_dark():
    html = _card("device")
    head, body = html.split("</head>", 1)
    # Light inline, because a client that strips the style block has to
    # be left with something readable rather than something broken.
    assert ec.LIGHT[ec.BG] in body and ec.BG not in body
    # And the dark values live in the media query, where the
    # substitution must not have touched them.
    assert "prefers-color-scheme: dark" in head
    assert ec.BG in head and ec.TEXT in head
    assert "!important" in head


def test_the_head_says_what_was_sent():
    assert 'content="dark"' in _card("dark")
    assert 'content="light"' in _card("light")
    assert 'content="light dark"' in _card("device")


def test_follow_the_sky_is_resolved_at_the_station_not_the_server():
    assert ec.resolve_theme("sky", after_dark=True) == "dark"
    assert ec.resolve_theme("sky", after_dark=False) == "light"
    # No coordinates is dark, because dark is what these emails have
    # always been and a missing location should not change the look of
    # somebody's mail.
    assert ec.resolve_theme("sky", after_dark=None) == "dark"
    # The fixed themes ignore the sky entirely.
    assert ec.resolve_theme("light", after_dark=True) == "light"
    assert ec.resolve_theme("dark", after_dark=False) == "dark"
    assert ec.resolve_theme("nonsense", after_dark=False) == "dark"
    assert ec.resolve_theme(None, after_dark=False) == "dark"


def test_every_email_the_server_sends_can_wear_it():
    """All three cards, not just the shared shell — the morning report
    builds its own document and would have been missed."""
    from app import digest as dg, outlook as ol, storm as sm
    import inspect
    for fn in (dg.build_html, ol.build_html, sm.build_storm_html):
        assert "theme" in inspect.signature(fn).parameters, fn.__name__


def test_the_setting_round_trips_and_defaults_to_dark(client):
    g = client.get("/api/alerts", headers=H).json()
    assert g["report_theme"] == "dark"
    r = client.put("/api/alerts", headers=H, json={"report_theme": "sky"})
    assert r.status_code == 200, r.text
    assert client.get("/api/alerts", headers=H).json()["report_theme"] == "sky"
    # A theme nobody ships is refused at the model rather than stored.
    assert client.put("/api/alerts", headers=H,
                      json={"report_theme": "neon"}).status_code == 422


def test_the_theme_survives_a_config_restore(client):
    client.put("/api/alerts", headers=H, json={"report_theme": "light"})
    export = client.get("/api/config/backup", headers=H).json()
    client.put("/api/alerts", headers=H, json={"report_theme": "dark"})
    client.post("/api/config/restore", headers=H, json=export)
    assert client.get("/api/alerts", headers=H).json()["report_theme"] == "light"
