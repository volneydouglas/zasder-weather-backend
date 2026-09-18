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


def shell(date_label: str, headline: str, inner: str, footer: str) -> str:
    """The whole email body around `inner` (trusted markup). `date_label`,
    `headline` and `footer` are plain text, escaped here."""
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:{BG};">
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
</body></html>"""
