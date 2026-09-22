"""The email card: the dark single-column dress the morning report has
worn since 1.9, factored out in 2.3 so the outlook report and the storm
summary can wear it too (Doren, 09-11: "shouldn't this be sent in your
infographic style?").

Every style is inline and nothing is fetched, so the card renders the
same in Mail, Gmail and Outlook and never phones home. The palette is
the share cards' vocabulary; digest.py keeps its underscore aliases so
its tests and callers do not move.
"""
from __future__ import annotations

import html

BG = "#0b0d12"
CARD = "#151922"
EDGE = "#262c38"
ACCENT = "#4fa6f2"
TEXT = "#e8ecf2"
DIM = "#8a93a3"
WARM = "#ff9a4d"
SEV = {"warning": "#ff5c47", "major": "#ff9a4d",
       "watch": "#4fa6f2", "info": "#8a93a3"}

# ── 2.4 item 11: the card in a theme ──────────────────────────────────
#
# Doren, 2026-09-20: his 10 PM outlook arrived light over a card drawn
# for dark, that morning's report arrived dark, same inbox, same day.
# D7 stopped the client inventing a scheme by declaring one. This is the
# setting on top of it.
#
# The colours above stay the dark palette and every builder in this
# module, in digest.py, outlook.py and storm.py goes on using them, which
# is deliberate: the alternative was threading a palette object through
# about seventy call sites to change some hex codes. The theme is applied
# to the FINISHED html instead, as an exact substitution of one palette
# for the other. Exact because every colour in these emails comes from
# the constants above, so the mapping is total and a test can prove no
# dark value survives.
#
# LIGHT is not the dark palette inverted. Inverting gives you grey text
# on off-white and a blue that vibrates; these are chosen for contrast on
# white, which is the only thing a light email has to get right.
LIGHT = {
    BG: "#ffffff",
    CARD: "#f4f6f9",
    EDGE: "#d8dee7",
    TEXT: "#12161c",
    DIM: "#5b6675",
    ACCENT: "#1668c4",
    WARM: "#b4560f",
    SEV["warning"]: "#c0281a",
    SEV["major"]: "#b4560f",
    SEV["watch"]: "#1668c4",
    SEV["info"]: "#5b6675",
}

THEMES = ("dark", "light", "sky", "device")


def resolve_theme(theme: str | None, *, after_dark: bool | None) -> str:
    """Which palette a "sky" setting means right now.

    `after_dark` is True between sunset and sunrise AT THE STATION,
    which is the point of the option: the email matches the sky the
    reader's own weather station is under. Unknown (no coordinates) is
    dark, because dark is what every one of these emails has always
    been and a setting should not change behaviour the day the location
    goes missing.
    """
    if theme not in THEMES:
        return "dark"
    if theme != "sky":
        return theme
    return "dark" if after_dark is None or after_dark else "light"


def _to_light(html: str) -> str:
    """Swap the dark palette for the light one. Longest first, so a
    colour that is a prefix of another cannot be half replaced."""
    for dark in sorted(LIGHT, key=len, reverse=True):
        html = html.replace(dark, LIGHT[dark])
    return html


# Apple Mail honours a <style> block and a media query; Gmail's web
# client strips them. So "follow your device" sends the LIGHT card and
# asks the client to darken it, which means it darkens in Apple Mail and
# stays light elsewhere. That is a real limitation and it is what the
# setting's own description says, rather than a promise the wire cannot
# keep.
def _device_style() -> str:
    rules = []
    for dark, light in sorted(LIGHT.items(), key=lambda kv: kv[1]):
        rules.append(f'  [style*="background:{light}"]'
                     f'{{background-color:{dark} !important;}}')
        rules.append(f'  [style*="color:{light}"]'
                     f'{{color:{dark} !important;}}')
        rules.append(f'  [style*="solid {light}"]'
                     f'{{border-color:{dark} !important;}}')
    return ("<style>@media (prefers-color-scheme: dark){\n"
            + "\n".join(rules) + "\n}</style>")


def head_for(theme: str) -> str:
    """The <head> that tells the client what it has been sent.

    Both spellings on purpose: `supported-color-schemes` is what Apple
    Mail reads and `color-scheme` is the standard.
    """
    scheme = "light dark" if theme == "device" else theme
    style = _device_style() if theme == "device" else ""
    return ('<head><meta charset="utf-8">'
            f'<meta name="color-scheme" content="{scheme}">'
            f'<meta name="supported-color-schemes" content="{scheme}">'
            f'{style}</head>')


def themed(body: str, theme: str) -> str:
    """Apply a RESOLVED theme to a finished dark card BODY.

    The body only, never the head: "follow your device" puts dark hex
    values inside a media query, and running the light substitution over
    those would rewrite the rule into one that does nothing. Callers
    build the body, theme it, and then prepend `head_for`.
    """
    if theme == "dark":
        return body
    # light and device both send light pixels; device adds the media
    # query that asks a capable client to put the dark ones back.
    return _to_light(body)


def document(body: str, theme: str) -> str:
    """A finished dark body → the whole themed email."""
    return (f"<!DOCTYPE html>\n<html>{head_for(theme)}\n"
            + themed(body, theme) + "\n</html>")


# Kept for the 2.4 D7 callers that predate the setting; dark is what the
# card was before there was one.
DARK_SCHEME_HEAD = head_for("dark")

FONT = "-apple-system,'Segoe UI',Arial,sans-serif"
LABEL = (f"font:800 11px {FONT};letter-spacing:1.2px;color:{DIM};")
TILE = (f"background:{CARD};border:1px solid {EDGE};"
        "border-radius:10px;padding:10px 12px;")
SPACER = '<td style="width:6px;"></td>'


def tile(label: str, value: str, tint: str = TEXT, width: str = "25%") -> str:
    """One stat tile. `value` is trusted markup (callers escape their own
    strings and pass entities like &deg;); `label` is escaped here."""
    return (f'<td style="{TILE}width:{width};">'
            f'<div style="{LABEL}">{html.escape(label)}</div>'
            f'<div style="font:800 20px {FONT};color:{tint};'
            f'padding-top:2px;">{value}</div></td>')


def tile_row(tiles: list[str], margin_top: int = 8) -> str:
    if not tiles:
        return ""
    return ('<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="margin-top:{margin_top}px;"><tr>'
            + SPACER.join(tiles) + '</tr></table>')


def section_label(text: str, padding: str = "18px 0 6px") -> str:
    return f'<div style="{LABEL}padding:{padding};">{html.escape(text)}</div>'


def prose(text: str, size: int = 14) -> str:
    """A paragraph card. `text` is plain; escaped here, newlines kept."""
    body = html.escape(text).replace("\n", "<br>")
    return (f'<div style="{TILE}font:400 {size}px {FONT};'
            f'color:{TEXT};line-height:1.45;">{body}</div>')


def shell(date_label: str, headline: str, inner: str, footer: str,
          theme: str = "dark") -> str:
    """The whole email body around `inner` (trusted markup). `date_label`,
    `headline` and `footer` are plain text, escaped here."""
    return document(f"""<body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{BG};"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="480" cellpadding="0" cellspacing="0"
       style="max-width:480px;width:100%;">
<tr><td>
  <div style="font:300 13px {FONT};color:{DIM};">
    <i>zasder</i><b style="color:{TEXT};letter-spacing:1px;">WEATHER</b>
    &nbsp;&#183;&nbsp; {html.escape(date_label)}
  </div>
  <div style="font:800 22px {FONT};
              color:{TEXT};padding:10px 0 2px;">{html.escape(headline)}</div>
  {inner}
  <div style="border-top:1px solid {EDGE};margin-top:20px;padding-top:10px;
              font:400 11px {FONT};color:{DIM};">
    {html.escape(footer)}
  </div>
</td></tr></table></td></tr></table>
</body>""", theme)
