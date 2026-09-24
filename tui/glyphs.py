"""Block-glyph helpers: band gauge, rung strip, sparkline.

All three return PLAIN strings of exactly `width` cells. Styling is the
caller's job (lattice.glyph_text maps each glyph to a theme token), so the
width maths lives in one place and a breakpoint only changes `width`.

Glyphs are probed against the console font before use. Measured on this
machine with GetGlyphIndicesW (2026-09-22): Consolas -- the console default
-- has no U+25C6 (diamond), U+2595/258F (the strip caps), U+2716 (heavy x)
or the U+2581..2587 spark ramp; Cascadia Mono lacks only U+2716. Each
fallback below is a glyph Consolas does have, and is also one cell wide, so
a fallback never changes a layout.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import warnings                    # noqa: E402
with warnings.catch_warnings():    # oracle.py's docstring escape warns; harmless
    warnings.simplefilter("ignore")
    import oracle as _oracle       # noqa: E402  (GridBot's v3.1 scanner: spark + detail)

# name -> (preferred, fallback)
_TABLE = {
    "cursor": ("◆", "●"),
    "held": ("▓", "▓"),
    "empty": ("░", "░"),
    "lcap": ("▕", "│"),
    "rcap": ("▏", "│"),
    "fresh": ("●", "●"),
    "stale": ("▲", "▲"),
    "dead": ("✖", "×"),
    "edge": ("◄", "◄"),
}

G = {k: v[0] for k, v in _TABLE.items()}


def _font_missing(chars):
    """Codepoints the current console font cannot draw. Empty off Windows,
    or when there is no console (tests, pipes): render the preferred set."""
    if sys.platform != "win32":
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        class _CFIX(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD),
                        ("dwFontSize", wintypes._COORD),
                        ("FontFamily", wintypes.UINT),
                        ("FontWeight", wintypes.UINT),
                        ("FaceName", wintypes.WCHAR * 32)]
        k32, g32, u32 = (ctypes.windll.kernel32, ctypes.windll.gdi32,
                         ctypes.windll.user32)
        cfi = _CFIX()
        cfi.cbSize = ctypes.sizeof(cfi)
        if not k32.GetCurrentConsoleFontEx(k32.GetStdHandle(-11), False,
                                           ctypes.byref(cfi)):
            return set()
        hdc = u32.GetDC(None)
        if not hdc:
            return set()
        hfont = g32.CreateFontW(cfi.dwFontSize.Y or 16, 0, 0, 0,
                                int(cfi.FontWeight) or 400, 0, 0, 0, 1, 0, 0,
                                0, 0, cfi.FaceName or "Consolas")
        old = g32.SelectObject(hdc, hfont)
        try:
            out = set()
            for ch in chars:
                arr = (wintypes.WORD * 1)()
                g32.GetGlyphIndicesW(hdc, ch, 1, arr, 1)
                if arr[0] == 0xFFFF:
                    out.add(ch)
            return out
        finally:
            g32.SelectObject(hdc, old)
            g32.DeleteObject(hfont)
            u32.ReleaseDC(None, hdc)
    except Exception:
        return set()


def probe():
    """Swap in fallbacks for glyphs the live console font lacks. Called once
    at startup by lattice.main(); never by the tests."""
    missing = _font_missing({v[0] for v in _TABLE.values()})
    for k, (pref, fb) in _TABLE.items():
        G[k] = fb if pref in missing else pref
    # GridBot's v3.1 oracle has no probe_glyphs(); its spark() always draws
    # the U+2581..2588 ramp, which Consolas lacks. Remap it here instead.
    if hasattr(_oracle, "probe_glyphs"):
        _oracle.probe_glyphs()
    global _SPARK_FALLBACK
    _SPARK_FALLBACK = bool(_font_missing(set(_RAMP)))
    return missing


def use_fallbacks(on=True):
    """Force one glyph set (tests render both)."""
    global _SPARK_FALLBACK
    for k, (pref, fb) in _TABLE.items():
        G[k] = fb if on else pref
    if hasattr(_oracle, "GLYPH_OK"):
        _oracle.GLYPH_OK["spark"] = not on
    _SPARK_FALLBACK = on


_RAMP = "▁▂▃▄▅▆▇█"
_RAMP_FB = "  ░░▒▒▓▓"          # shade blocks: in Consolas, one cell each
_SPARK_FALLBACK = False


def _pos(frac, width):
    return min(width - 1, max(0, int(frac * width)))


def band_gauge(px, lo, hi, width):
    """Where price sits inside [lo, hi], low to high, `width` cells.
    No price (or a degenerate band) draws the band with no cursor rather
    than a cursor at a guessed position."""
    if width <= 0:
        return ""
    cells = [G["empty"]] * width
    if px is not None and lo is not None and hi is not None and hi > lo:
        cells[_pos((px - lo) / (hi - lo), width)] = G["cursor"]
    return "".join(cells)


def rung_strip(held, n, px_idx, width):
    """A grid ladder, low to high, capped both ends, `width` cells total.

    held    int count of rungs holding inventory (drawn from the bottom),
            or a sequence of bools, one per rung.
    n       rung count.
    px_idx  rung index price sits at, or None when unknown -- then no
            cursor is drawn.
    """
    if width <= 0:
        return ""
    if width < 3 or not n:
        return (G["lcap"] + G["empty"] * max(0, width - 2) + G["rcap"])[:width]
    inner = width - 2
    if isinstance(held, int):
        mask = [i < held for i in range(n)]
    else:
        mask = [bool(x) for x in list(held)[:n]] + [False] * max(0, n - len(held))
    cells = []
    for c in range(inner):
        rung = min(n - 1, int(c * n / inner))
        cells.append(G["held"] if mask[rung] else G["empty"])
    if px_idx is not None:
        cells[min(inner - 1, max(0, int(px_idx * inner / n)))] = G["cursor"]
    return G["lcap"] + "".join(cells) + G["rcap"]


def spark(vals, width):
    """The oracle's sparkline, padded to exactly `width` cells."""
    s = _oracle.spark(list(vals), width) if vals else ""
    if _SPARK_FALLBACK:
        s = s.translate(str.maketrans(_RAMP, _RAMP_FB))
    return s[:width].ljust(width)
