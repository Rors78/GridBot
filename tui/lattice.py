"""GRIDPICK · LATTICE -- the all-in-one console for GridBot and its v3.1 scanner.

    python tui/lattice.py [--status PATH] [--oracle-json PATH] [--log PATH]
                          [--journal PATH] [--scanner-log PATH] [--refresh S]

One process, one screen. Rich Live on the alternate screen buffer: it never
scrolls, and it re-lays itself out from console.size on every frame.

Sources (see feeds.py), and nothing else:
  status.json    the bot's snapshot: book, capital, guard, fee tier, trade
                 feed, scanner child, every grid with its resting ladder
  scan.json      the scanner's ranked pairs with every metric it computed
  journal.jsonl  the tape: DEPLOY, FILL, FIRST_RT, EXIT, HALT, START, STOP
  gridbot.log    the bot's own narrative
  scanner.log    the scanner's gates and rejection reasons

It never opens state.json, never imports gridbot.py, never calls Kraken, and
never changes anything -- it only reads. Fields the bot does not serve are
drawn as a dim em dash, never derived.

Colour law: amber = a scanner (predicted) number, cyan = a bot (realised)
number, white = identity, green/amber/red = health.

Screens: main · enter = oracle detail for the selected pair · l = log ·
t = tape · ? = help · esc back · q quit.
"""
from __future__ import annotations

import os

# The user's invariant for anything near the bot. This module does not
# import it -- but if anything ever does, it must find this set first.
os.environ.setdefault("GRIDPICK_TEST", "1")

import argparse                      # noqa: E402
import contextlib                    # noqa: E402
import io                            # noqa: E402
import re                            # noqa: E402
import sys                           # noqa: E402
import threading                     # noqa: E402
import time                          # noqa: E402
from dataclasses import dataclass    # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace    # noqa: E402
from typing import NamedTuple        # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from rich import box                 # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.layout import Layout       # noqa: E402
from rich.live import Live           # noqa: E402
from rich.panel import Panel         # noqa: E402
from rich.table import Table         # noqa: E402
from rich.text import Text           # noqa: E402

import feeds                         # noqa: E402
import glyphs                        # noqa: E402
from glyphs import G                 # noqa: E402
from theme import THEME              # noqa: E402

DASH = "—"
# GridBot writes status.json about every 5.3 s in whole-second ts: a 5 s stale
# limit flickered "stale" ~14% of the time on a healthy bot (audit 2026-09-23).
STALE_S, DEAD_S = 12.0, 30.0


# ---------------------------------------------------------------- modes --
class Mode(NamedTuple):
    w: str      # wide | ref | narrow | stacked | single
    h: str      # tall | full | short | tiny


def mode_for(width, height):
    """Breakpoints. Pure, so a test can assert each size lands in a distinct
    layout rather than one layout that happens to fit all."""
    if width >= 176:
        w = "wide"
    elif width >= 140:
        w = "ref"
    elif width >= 110:
        w = "narrow"
    elif width >= 90:
        w = "stacked"
    else:
        w = "single"
    h = "tall" if height >= 44 else ("full" if height >= 36 else ("short" if height >= 28 else "tiny"))
    return Mode(w, h)


@dataclass
class UI:
    sel: int = 0
    view: str = "main"          # main | help | detail | log | tape
    paused: bool = False


# ------------------------------------------------------------ formatting --
def T(*parts):
    """Text from (string, style) pairs; never wraps, crops at the edge."""
    t = Text(no_wrap=True, overflow="crop")
    for p in parts:
        if isinstance(p, Text):
            t.append_text(p)
        elif isinstance(p, tuple):
            t.append(p[0], style=p[1])
        else:
            t.append(p)
    return t


def glyph_text(s, empty="dim"):
    """Style a glyph string (gauge/strip/bar) char by char from the theme."""
    style = {G["held"]: "held", G["empty"]: empty, G["cursor"]: "cursor",
             G["lcap"]: "frame", G["rcap"]: "frame"}
    t = Text(no_wrap=True, overflow="crop")
    for ch in s:
        t.append(ch, style=style.get(ch, "dim"))
    return t


def short_px(p):
    """Price in at most 7 cells, so ladders and band labels stay aligned."""
    if p is None:
        return DASH
    a = abs(p)
    if a == 0:
        return "0"
    if a < 0.01:
        return f"{p:.2e}".replace("e-0", "e-")         # 5.87e-6, 6.97e-3
    if a < 1:
        return f"{p:.4f}"                               # 0.3432
    if a < 10000:
        return f"{p:.4g}"                               # 1.046, 236.8, 4417
    return f"{p:,.0f}"[:7]


def fmt_clock(s):
    if s is None or s == float("inf"):
        return DASH
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}"


def fmt_secs(s):
    if s is None or s == float("inf"):
        return DASH
    return f"{s:.1f}s" if s < 10 else f"{s:.0f}s"


def fmt_dur(s):
    """A duration for humans: 45s, 5m12s, 22h30m, 1d02h."""
    if s is None or s == float("inf"):
        return DASH
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def fmt_uptime(seconds):
    if seconds is None:
        return DASH
    m = int(seconds // 60)
    return f"{m // 1440}d {m % 1440 // 60:02d}:{m % 60:02d}"


def signed_usd(v):
    if v is None:
        return DASH
    return f"{'+' if v >= 0 else '−'}${abs(v):.2f}"


def usd(v, nd=2):
    return DASH if v is None else f"${v:,.{nd}f}"


def pct(v, nd=1, signed=False):
    if v is None:
        return DASH
    return f"{v:+.{nd}f}%" if signed else f"{v:.{nd}f}%"


def num(v, f="{:.2f}"):
    return DASH if v is None else f.format(v)


def pnl_style(v):
    if v is None:
        return "dim"
    return "ok" if v > 0 else ("danger" if v < 0 else "realised")


def fresh(age):
    """(glyph, style) for a data age."""
    if age is None or age > DEAD_S:
        return G["dead"], "danger"
    if age > STALE_S:
        return G["stale"], "warn"
    return G["fresh"], "ok"


def base(sym):
    return (sym or "?").split("/")[0]


def bar(frac, width, fill="held", empty="dim", hot=None):
    """A horizontal gauge of `width` cells for frac in [0, 1]."""
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return T((G["held"] * n, hot or fill), (G["empty"] * (width - n), empty))


# --------------------------------------------------------------- derive --
def derive(st):
    """Everything the panels draw, picked out once. Nothing here is a
    recomputation of a trading quantity: it is the bot's and scanner's own
    values, named and formatted."""
    S = st.get("snapshot") or {}
    V = SimpleNamespace()
    V.S = S
    V.grids = st.get("grids") or []
    V.snap_age = st.get("snapshot_age")
    V.api_dead = V.snap_age is None or V.snap_age > DEAD_S
    V.mode = (S.get("mode") or DASH).upper()
    orc = st.get("oracle") or {}
    V.orc = orc
    V.orc_rows = orc.get("rows") or []
    V.scan_age = st.get("oracle_scan_age")
    V.refresh = st.get("refresh_s") or 120
    V.scan_stale = V.scan_age is None or V.scan_age > 2 * V.refresh
    sl = st.get("scanner_log") or {}
    V.gates = sl.get("gates")
    V.rejects = sl.get("rejects") or []
    V.n_rejected = sl.get("n_rejected")
    V.scanner_log_age = st.get("scanner_log_age")
    V.journal = st.get("journal") or []
    V.log = st.get("log") or []
    V.pnl_hist = st.get("pnl_hist") or []
    V.now = st.get("now") or datetime.now(timezone.utc)
    V.today = st.get("today") or ""
    return V


# ---------------------------------------------------------------- header --
LOGO_MARK = "#010203"
LOGO_ROWS, LOGO_COLS = 3, 6
HEAD_LINES = 4
HEAD_H = HEAD_LINES + 2
ESC = chr(27)
_LOGO_SIXEL = None


def header_lines(V, width):
    S = V.S
    g, gs = fresh(V.snap_age)
    mode_style = "danger" if V.mode == "LIVE" else "title"
    # 1. identity and the three things that must be alive: bot, feed, scanner
    fc = S.get("feed_connected")
    feed_g, feed_s = ((G["fresh"], "ok") if fc else ((G["dead"], "danger") if fc is False else (DASH, "dim")))
    ws = S.get("feed_msg_age_s")
    sr = S.get("scanner_running")
    sc_g, sc_s = ((G["fresh"], "ok") if sr else ((G["dead"], "danger") if sr is False else (DASH, "dim")))
    nxt = DASH if V.scan_age is None else fmt_clock(max(0, V.refresh - V.scan_age % V.refresh))
    l1 = T(("GRIDBOT ", "title"), (f"v{S.get('version') or DASH} ", "dim"), (g + " ", gs), (V.mode, mode_style),
           ("  up ", "dim"), (fmt_uptime(S.get("uptime_s")), "realised"),
           ("  status ", "dim"), (fmt_secs(V.snap_age), gs),
           ("   FEED ", "title"), (feed_g + " ", feed_s), ("ws ", "dim"),
           (fmt_secs(ws), "warn" if (ws or 0) > DEAD_S else "realised"),
           ("  conn ", "dim"), (str(S.get("feed_connects") if S.get("feed_connects") is not None else DASH), "realised"),
           ("  err ", "dim"), (str(S.get("feed_errors") if S.get("feed_errors") is not None else DASH),
                              "warn" if S.get("feed_errors") else "realised"),
           ("  gaps ", "dim"), (str(S.get("catch_up_gaps") or 0), "danger" if S.get("catch_up_gaps") else "realised"),
           ("   SCANNER ", "title"), (sc_g + " ", sc_s),
           ("pid ", "dim"), (str(S.get("scanner_pid") or DASH), "realised"),
           ("  starts ", "dim"), (str(S.get("scanner_starts") if S.get("scanner_starts") is not None else DASH), "realised"),
           ("  scan ", "dim"), (f"{fmt_clock(V.scan_age)} ago" if V.scan_age is not None else DASH,
                               "warn" if V.scan_stale else "realised"),
           (f" · next ~{nxt}", "dim"))
    # 2. capital: what is committed, of what, and what the next grid would get
    cap, dep = S.get("capital"), S.get("deployed")
    held = S.get("held_usd") or 0.0
    frac = ((dep or 0.0) + held) / cap if cap else None
    bw = 16 if width >= 140 else 8
    ea = S.get("exit_after_s")
    l2 = T(("capital ", "dim"), (usd(cap), "realised"),
           ("  deployed ", "dim"), (usd((dep or 0.0) + held) if dep is not None else DASH, "realised"), (" ", ""),
           bar(frac, bw, hot="warn" if (frac or 0) >= 0.999 else None),
           (f" {frac * 100:.0f}%" if frac is not None else f" {DASH}", "realised"),
           ("   grids ", "dim"), (f"{S.get('grids_active') if S.get('grids_active') is not None else DASH}/"
                                 f"{S.get('max_grids') or DASH}", "realised"),
           (f"  held {S.get('grids_held')}" if S.get("grids_held") else "", "danger"),
           ("  · ", "dim"), (usd(S.get("alloc_per_grid")), "realised"), (" each · next ", "dim"),
           (usd(S.get("alloc_next")), "realised"),
           ("  · capital now ", "dim"), (usd(S.get("capital_now")), "realised"),
           ("  · exit after ", "dim"),
           (f"{ea:.0f}s" if isinstance(ea, (int, float)) and ea > 0 else ("never" if ea == 0 else DASH), "realised"))
    # 3. the book
    l3 = T(("equity ", "dim"), (usd(S.get("equity")), "realised"),
           ("  pnl ", "dim"), (signed_usd(S.get("total_pnl")), pnl_style(S.get("total_pnl"))),
           ("  = real ", "dim"), (signed_usd(S.get("realised")), pnl_style(S.get("realised"))),
           (" · unreal ", "dim"), (signed_usd(S.get("unrealised")), pnl_style(S.get("unrealised"))),
           (" · fees ", "dim"), (usd(S.get("fees")), "realised"),
           ("   fills ", "dim"), (str(S.get("fills")), "realised"),
           (" · round trips ", "dim"), (str(S.get("round_trips")), "realised"),
           (" · closed grids ", "dim"), (str(S.get("closed_count") if S.get("closed_count") is not None else DASH), "realised"),
           ("   fees maker ", "dim"), (pct(S.get("fee_maker"), 2), "realised"),
           (" taker ", "dim"), (pct(S.get("fee_taker"), 2), "realised"),
           (f"  · {S.get('fee_source')}" if S.get("fee_source") else "", "dim"))
    # 4. the guard, in its own units
    dl, dlm = S.get("guard_day_loss_pct"), S.get("guard_max_day_loss_pct")
    dd, ddm = S.get("guard_drawdown_pct"), S.get("guard_max_drawdown_pct")
    gw = 10 if width >= 140 else 6

    def guard_bit(label, v, lim):
        if v is None:
            return T((f"{label} ", "dim"), (DASH, "dim"))
        used = max(0.0, v)
        frac = (used / lim) if lim else 0.0
        style = "danger" if frac >= 1 else ("warn" if frac >= 0.5 else "ok")
        up = f" (up {-v:.1f}%)" if v < 0 else ""
        return T((f"{label} ", "dim"), (f"{used:.1f}%", style), (" of ", "dim"),
                 (f"{lim:g}%" if lim else "off", "realised"), (" ", ""), bar(frac, gw, hot=style), (up, "ok"))

    halt = S.get("halt")
    l4 = T(("GUARD ", "title"), guard_bit("day loss", dl, dlm), ("   ", ""), guard_bit("drawdown", dd, ddm),
           ("   peak ", "dim"), (usd(S.get("guard_peak")), "realised"),
           ("  day start ", "dim"), (usd(S.get("guard_day_start")), "realised"),
           ("   halt ", "dim"), ((halt if halt else DASH), "danger" if halt else "ok"))
    return [l1, l2, l3, l4]


def header_panel(V, width):
    clock = V.now.strftime("%Y-%m-%d %H:%M:%S UTC")
    title = T(("GRIDPICK ", "title"), (G["cursor"], "cursor"), (" LATTICE", "title"))
    fill = width - 6 - title.cell_len - len(clock) - 2
    if fill >= 1:
        title.append(" " + "─" * fill + " ", style="frame")
        title.append(clock, style="dim")
    lines = header_lines(V, width)
    g = Table.grid(padding=(0, 2))
    g.add_column(width=LOGO_COLS, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(logo_block(), Group(*lines))
    return Panel(g, title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 1), height=HEAD_H)


# THE HEADER LOGO IS THE REAL ICON. The header reserves a block of cells with a
# background colour nothing else uses; after each frame draw_logo() finds it and
# paints assets/gridpick_logo.png over it as a Sixel image (Windows Terminal:
# 10x20 image pixels per cell, 3 rows = 60 px). A terminal without Sixel
# ignores the sequence and shows the blank block.
def logo_block():
    t = Text()
    for i in range(LOGO_ROWS):
        t.append(" " * LOGO_COLS, style="on " + LOGO_MARK)
        if i < LOGO_ROWS - 1:
            t.append(chr(10))
    return t


def draw_logo(console, renderable):
    """Paint the logo over the reserved block. Cosmetic: never raises."""
    global _LOGO_SIXEL
    try:
        if _LOGO_SIXEL is None:
            from sixelimg import sixel
            _LOGO_SIXEL = sixel(os.path.join(_HERE, "assets", "gridpick_logo.png"), height_px=LOGO_ROWS * 20)
        from rich.cells import cell_len
        rows = console.render_lines(renderable, console.options.update(height=console.height), pad=False)
        for y, line in enumerate(rows):
            x = 0
            for seg in line:
                st = seg.style
                if st is not None and st.bgcolor is not None and st.bgcolor.name == LOGO_MARK:
                    console.file.write(ESC + "7" + ESC + "[%d;%dH" % (y + 1, x + 1) + _LOGO_SIXEL + ESC + "8")
                    console.file.flush()
                    return
                x += cell_len(seg.text)
    except Exception:
        pass


# ---------------------------------------------------------------- oracle --
# (header, width, justify). band is the flexible column.
_COLS = {
    "rank": ("  #", 3, "right"),
    "pair": ("PAIR", 6, "left"),
    "price": ("PRICE", 7, "right"),
    "band": ("BAND  lo ─◆─ hi", 0, "left"),
    "kind": ("KIND", 5, "left"),
    "lv": ("LV", 3, "right"),
    "k": ("k", 3, "right"),
    "step": ("STEP%", 5, "right"),
    "net": ("NET%", 5, "right"),
    "fd": ("F/d", 5, "right"),
    "rtd": ("RT/d", 5, "right"),
    "yd": ("Y/D", 5, "right"),
    "adj": ("ADJ", 5, "right"),
    "cont": ("CONT", 4, "right"),
    "mae": ("MAE", 4, "right"),
    "vr": ("VR", 4, "right"),
    "h": ("H", 4, "right"),
    "atr": ("ATR%", 4, "right"),
    "liq": ("LIQ", 5, "right"),
    "spr": ("SPR%", 4, "right"),
    "d5": ("D5%", 4, "right"),
    "flag": ("FLAG", 6, "left"),
    "q": ("Q", 1, "right"),
}
_ORDER = ["rank", "pair", "price", "band", "kind", "lv", "step", "net", "fd", "rtd", "yd", "adj",
          "cont", "mae", "vr", "h", "atr", "liq", "spr", "d5", "k", "flag", "q"]
# Dropped in this order as the panel narrows. Y/D, CONT, Q and the band go last.
_DROP = ["k", "d5", "spr", "liq", "atr", "h", "vr", "mae", "rtd", "adj", "net", "kind", "price",
         "step", "fd", "flag", "lv", "band"]


def oracle_columns(inner_w):
    cols = list(_ORDER)

    def fixed(cs):
        return sum(_COLS[c][1] for c in cs if c != "band") + len(cs) - 1

    for c in _DROP:
        if "band" in cols and inner_w - fixed(cols) >= 12:
            break
        if "band" not in cols and fixed(cols) <= inner_w:
            break
        cols.remove(c)
    band_w = max(0, inner_w - fixed(cols)) if "band" in cols else 0
    return cols, band_w


def band_cell(r, band_w):
    lo, hi = r.get("lo"), r.get("hi")
    los, his = short_px(lo).rjust(7), short_px(hi).ljust(7)
    g_w = band_w - len(los) - len(his) - 2
    px = r.get("price")
    tone = "dim" if px is not None else "frame"
    if g_w >= 4:
        g_w = min(g_w, 24)
        return T((los + " ", "dim"), glyph_text(glyphs.band_gauge(px, lo, hi, g_w), tone),
                 (" " + his, "dim"))
    return glyph_text(glyphs.band_gauge(px, lo, hi, max(1, min(band_w, 24))), tone)


def oracle_cell(c, i, r, band_w, selected=False, live=False):
    def P(v, f="{:.2f}"):
        return (f.format(v), "predicted") if v is not None else (DASH, "dim")

    if c == "rank":
        n = str(i + 1)
        if selected:
            return T((G["cursor"], "cursor"), (n.rjust(2), "cursor"))
        return T((n.rjust(3), "dim"))
    if c == "pair":
        return T((base(r.get("symbol")), "realised" if live else "title"))
    if c == "price":
        return T((short_px(r.get("price")), "predicted" if r.get("price") else "dim"))
    if c == "band":
        return band_cell(r, band_w)
    if c == "kind":
        return T(((r.get("kind") or DASH)[:5], "dim"))
    if c == "lv":
        return T(P(r.get("levels"), "{}"))
    if c == "k":
        return T(P(r.get("k"), "{:g}"))
    if c == "step":
        return T(P(r.get("step_pct"), "{:.1f}"))
    if c == "net":
        return T(P(r.get("net_pct"), "{:.1f}"))
    if c == "fd":
        return T(P(r.get("fills_day"), "{:.1f}"))
    if c == "rtd":
        return T(P(r.get("rt_day"), "{:.1f}"))
    if c == "yd":
        return T(P(r.get("yield_day")))
    if c == "adj":
        return T(P(r.get("yield_adj")))
    if c == "cont":
        return T(P(r.get("cont")))
    if c == "mae":
        return T(P(r.get("mae_pct"), "{:.0f}"))
    if c == "vr":
        return T(P(r.get("vr")))
    if c == "h":
        return T(P(r.get("hurst")))
    if c == "atr":
        return T(P(r.get("atr_pct"), "{:.1f}"))
    if c == "liq":
        v = r.get("turnover24")
        return T((f"{v / 1e6:.1f}M" if v is not None else DASH, "predicted" if v is not None else "dim"))
    if c == "spr":
        return T(P(r.get("spread_pct")))
    if c == "d5":
        return T(P(r.get("days_to_5pct"), "{:.1f}"))
    if c == "flag":
        return T(("basket" if r.get("basket") else "", "warn"))
    if c == "q":
        return T((G["fresh"], "ok")) if r.get("qualified") else T(" ")
    return T("")


def rejected_lines(V, inner_w):
    """The scanner's own reasons, from scanner.log. Two lines at most."""
    out = []
    if V.gates:
        out.append(T(("gates ", "dim"), (V.gates.replace("│", "·"), "dim")))
    if not V.rejects:
        why = "(scanner.log unread)" if V.n_rejected is None else "none this scan"
        out.append(T(("rejected ", "dim"), (why, "dim")))
        return out
    parts = [("rejected ", "dim"), (str(V.n_rejected), "warn"), ("  ", "")]
    for rj in V.rejects:
        parts += [(f"{rj['why']}: ", "dim"), (" ".join(base(p) for p in rj["pairs"]), "predicted"), ("  ·  ", "frame")]
    out.append(T(*parts[:-1]))
    return out


def oracle_panel(V, mode, width, height, ui, top_n=None):
    inner_w, inner_h = max(0, width - 2), max(0, height - 2)
    cols, band_w = oracle_columns(inner_w)
    rows = V.orc_rows[:top_n] if top_n else V.orc_rows
    foot = rejected_lines(V, inner_w) if inner_h >= 8 else []
    # The gates line is the first thing to give way to a ranked row.
    if len(foot) == 2 and inner_h - 2 - len(foot) < len(rows):
        foot = foot[1:]
    n_fit = max(0, inner_h - 2 - len(foot))          # header + rule
    sel = min(max(0, ui.sel), max(0, len(rows) - 1))
    start = 0 if sel < n_fit else sel - n_fit + 1
    live = {g["symbol"] for g in V.grids}
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True, pad_edge=False,
                padding=(0, 0), header_style="dim", border_style="frame")
    for c in cols:
        h, w, j = _COLS[c]
        if c == "band":
            tbl.add_column(h, ratio=1, min_width=band_w, no_wrap=True, overflow="crop")
        else:
            tbl.add_column(h, width=w, justify=j, no_wrap=True, overflow="crop")
    for i in range(start, min(len(rows), start + n_fit)):
        r = rows[i]
        is_sel = i == sel and ui.view == "main"
        tbl.add_row(*[oracle_cell(c, i, r, band_w, is_sel, r.get("symbol") in live) for c in cols])
    body = [tbl] if inner_h >= 3 else []
    pad = n_fit - min(n_fit, max(0, len(rows) - start))
    body += [Text("")] * pad
    if not rows:
        body = [T(("  no scan yet", "dim"))] + [Text("")] * max(0, inner_h - 1 - len(foot))
    body += foot
    o = V.orc
    title = T(("ORACLE ", "title"), (f"v{o.get('version') or DASH}", "predicted"),
              (f" · {o.get('interval') or DASH} · ", "dim"),
              (str(o.get("n_scanned") if o.get("n_scanned") is not None else DASH), "predicted"), (" ranked · ", "dim"),
              (str(o.get("n_qualified") if o.get("n_qualified") is not None else DASH), "predicted"), (" qualified · ", "dim"),
              (str(o.get("n_basket") if o.get("n_basket") is not None else DASH), "predicted"), (" basket", "dim"),
              ("  · stale" if V.scan_stale else "", "warn"),
              ("  · cyan pair = live grid", "dim") if width >= 120 else ("", ""))
    return Panel(Group(*body[-inner_h:] if inner_h else []), title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 0), height=height)


# ------------------------------------------------------------ live grids --
def ladder_text(g, inner_w):
    """The resting ladder, low to high, with the price cursor in place:
    0.2240B 0.2412B 0.2597○ ◆0.3010 0.3696S. Returns None if it cannot fit."""
    lad = g.get("ladder")
    if not lad:
        return None
    px = g.get("price")
    parts = []
    placed = px is None
    for lv, side in lad:
        if not placed and px is not None and px < lv:
            parts.append((G["cursor"] + short_px(px), "cursor"))
            placed = True
        mark, style = {"BUY": ("B", "ok"), "SELL": ("S", "held")}.get(side, ("○", "dim"))
        parts.append((short_px(lv) + mark, style))
    if not placed:
        parts.append((G["cursor"] + short_px(px), "cursor"))
    total = sum(len(p[0]) for p in parts) + len(parts) - 1
    if total > inner_w:
        return None
    t = T()
    for i, p in enumerate(parts):
        if i:
            t.append(" ")
        t.append(p[0], style=p[1])
    return t


def grid_flags(g):
    pa = g.get("last_print_age_s")
    out = []
    if g.get("catching_up"):
        out.append(("SYNC", "warn"))
    if g.get("outside_for_s") is not None:
        out.append((f"OUTSIDE {fmt_dur(g['outside_for_s'])}", "danger"))
    if pa is not None and pa > 600:
        out.append((f"QUIET {fmt_dur(pa)}", "warn"))
    return out


def grid_card(g, inner_w, lines):
    """1, 2 or 3 lines per grid, most important first."""
    n, held = g.get("levels") or 0, g.get("holding") or 0
    ttf, note = g.get("time_to_first_rt_s"), g.get("first_rt_note")
    first = fmt_dur(ttf) if ttf is not None else {"predates logging": "pre-log"}.get(note, note or DASH)
    l1 = T((f"{g['symbol']:<10}", "title"))
    for f, s in grid_flags(g):
        l1.append_text(T((f + " ", s)))
    l1.append_text(T((f"{(g.get('kind') or DASH)[:5]} {n}", "dim"), ("  ", ""),
                     (usd(g.get("alloc"), 0), "realised"), ("  ", ""), (fmt_dur(g.get("age_s")), "realised"),
                     ("  step ", "dim"), (pct(g.get("step_pct")), "predicted"),
                     ("  pred ", "dim"), (num(g.get("predicted_yield_day")), "predicted"),
                     (" real ", "dim"), (num(g.get("realised_yield_day")),
                                         "realised" if g.get("realised_yield_day") is not None else "dim"),
                     ("  RT ", "dim"), (str(g.get("round_trips") if g.get("round_trips") is not None else DASH), "realised"),
                     ("  1st ", "dim"), (first, "realised" if ttf is not None else "dim")))
    if lines == 1:
        return [l1]
    lad = ladder_text(g, inner_w - 12)
    if lad is not None:
        l2 = T(lad, (f"  {held}/{max(0, n - 1)} held", "realised"))
    else:
        sw = max(8, min(30, inner_w - 30))
        l2 = T(glyph_text(glyphs.rung_strip(g.get("held_mask", held), n, g.get("px_idx"), sw)),
               (f" {held}/{max(0, n - 1)} held  ", "realised"),
               (f"{short_px(g.get('lo'))}–{short_px(g.get('hi'))}", "dim"),
               ("  px ", "dim"), (short_px(g.get("price")), "realised"))
    if lines == 2:
        return [l1, l2]
    # Floor/ceiling distance and the pierce loss live in the RISK cell.
    l3 = T(("  pos ", "dim"), (pct(g["pos"] * 100, 0) if g.get("pos") is not None else DASH, "realised"),
           ("  real ", "dim"), (signed_usd(g.get("realised")), pnl_style(g.get("realised"))),
           ("  unreal ", "dim"), (signed_usd(g.get("unrealised")), pnl_style(g.get("unrealised"))),
           ("  fees ", "dim"), (usd(g.get("fees")), "realised"),
           ("  fills ", "dim"), (str(g.get("fills") if g.get("fills") is not None else DASH), "realised"),
           ("  cash ", "dim"), (usd(g.get("cash"), 0), "realised"),
           ("  oracle ", "dim"),
           ({"qualified": "qualified", "ranked, not qualified": "ranked, unqual.", "not ranked": "not ranked"}
            .get(g.get("scanner_now"), g.get("scanner_now") or DASH),
            "ok" if g.get("scanner_now") == "qualified" else "warn"))
    return [l1, l2, l3]


def grids_panel(V, mode, width, height):
    inner_w, inner_h = max(0, width - 2), max(0, height - 2)
    S = V.S
    n = len(V.grids)
    lines = []
    if S.get("unrestored"):
        lines.append(T(("UNRESTORED ", "danger"), (", ".join(map(str, S["unrestored"])), "danger"),
                       ("  slot, coins and capital held -- see gridbot.log", "dim")))
    if not V.grids:
        lines.append(T(("no running grids: waiting for a qualified pick that fits the capital", "dim")))
    else:
        avail = inner_h - len(lines)
        per = 3 if n * 4 - 1 <= avail else (2 if n * 3 - 1 <= avail else (1 if n <= avail else 0))
        if per == 0:
            show = V.grids[:max(0, avail - 1)]
            for g in show:
                lines += grid_card(g, inner_w, 1)
            lines.append(T((f"+{n - len(show)} more", "dim")))
        else:
            for i, g in enumerate(V.grids):
                if i and per > 1:
                    lines.append(Text(""))
                lines += grid_card(g, inner_w, per)
    lines = lines[:inner_h] + [Text("")] * max(0, inner_h - len(lines))
    g, gs = fresh(V.snap_age)
    title = T(("LIVE GRIDS ", "title"), (g, gs),
              (f"  {n}/{S.get('max_grids') or DASH}", "realised"),
              ("  · yields %/d · ladder ", "dim"), ("B", "ok"), (" buy ", "dim"), ("S", "held"),
              (" sell ", "dim"), ("○", "dim"), (" empty ", "dim"), (G["cursor"], "cursor"), (" price", "dim"))
    return Panel(Group(*lines) if lines else Text(""), title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 1), height=height,
                 style="dim" if V.api_dead else "")


# ----------------------------------------------------------- bottom band --
def risk_lines(V):
    """Alerts first (the states where the bot stops trading but looks fine),
    then the bot's own edge figures, then why picks were skipped."""
    S = V.S
    out = []
    if S.get("halt"):
        out.append(T(("HALT ", "danger"), (S["halt"], "danger")))
    if S.get("scanner_running") is False:
        out.append(T(("SCANNER DOWN ", "danger"), (S.get("scanner_note"), "dim")))
    elif (S.get("scanner_note") or "").startswith("scan is"):
        out.append(T(("SCAN STALE ", "warn"), (S.get("scanner_note"), "dim")))
    if S.get("feed_connected") is False:
        out.append(T(("FEED DOWN ", "danger"), (S.get("feed_last_error"), "dim")))
    if S.get("catch_up_gaps"):
        out.append(T(("catch-up gaps ", "warn"), (str(S["catch_up_gaps"]), "warn"),
                     ("  prints lost: fills may be missing", "dim")))
    ea = S.get("exit_after_s")
    ea_s = f"{ea:.0f}s" if isinstance(ea, (int, float)) and ea > 0 else "never"
    for g in V.grids:
        f, c, fl = g.get("to_floor_pct"), g.get("to_ceiling_pct"), g.get("floor_pnl")
        near = f is not None and f < 3.0
        out.append(T((f"{base(g['symbol']):<6}", "title"),
                     (" floor ", "dim"), (pct(f), "danger" if near else "realised"),
                     (" ceil ", "dim"), (pct(c), "realised"),
                     (" @floor ", "dim"), (signed_usd(fl), "danger" if (fl or 0) < 0 else "realised")))
        if g.get("outside_for_s") is not None:
            out.append(T(("  OUTSIDE band ", "danger"), (f"{fmt_dur(g['outside_for_s'])} -> exit at {ea_s}", "danger")))
    sk = S.get("skipped") or {}
    if sk:
        out.append(T(("skipped picks ", "dim"), (str(len(sk)), "warn")))
        for sym, why in list(sk.items())[:4]:
            out.append(T((f"  {base(sym):<6} ", "title"), (str(why), "dim")))
    else:
        out.append(T(("skipped picks ", "dim"), ("0", "realised")))
    if S.get("feed_last_error") and S.get("feed_connected"):
        out.append(T(("last feed error ", "dim"), (S["feed_last_error"], "dim")))
    return out


def pnl_lines(V, width):
    S = V.S
    sw = max(4, min(24, width - 14))
    per = T()
    for i, g in enumerate(V.grids):
        if i:
            per.append(" · ", style="dim")
        per.append(base(g["symbol"]) + " ", style="title")
        per.append(signed_usd(g.get("total")), style=pnl_style(g.get("total")))
    def n_(v):
        return str(v) if v is not None else DASH

    out = [
        T((glyphs.spark(V.pnl_hist, sw), "realised"), ("  ", ""),
          (signed_usd(S.get("total_pnl")), pnl_style(S.get("total_pnl"))), ("  session", "dim")),
        T(("        real     unreal    fees   fills  RT", "dim")),
        T(("open   ", "dim"), (f"{signed_usd(S.get('open_realised')):>7}", pnl_style(S.get("open_realised"))),
          (f"  {signed_usd(S.get('unrealised')):>8}", pnl_style(S.get("unrealised"))),
          (f"  {usd(S.get('open_fees')):>6}", "realised"),
          (f"  {n_(S.get('open_fills')):>5}", "realised"), (f"  {n_(S.get('open_round_trips')):>3}", "realised")),
        T(("closed ", "dim"), (f"{signed_usd(S.get('closed_realised')):>7}", pnl_style(S.get("closed_realised"))),
          (f"  {DASH:>8}", "dim"),
          (f"  {usd(S.get('closed_fees')):>6}", "realised"),
          (f"  {n_(S.get('closed_fills')):>5}", "realised"), (f"  {n_(S.get('closed_round_trips')):>3}", "realised")),
        T(("grids  ", "dim"), per if V.grids else T((DASH, "dim"))),
    ]
    for c in (S.get("recent_closed") or [])[-3:][::-1]:
        out.append(T(("  ", ""), (f"{base(c.get('symbol')):<6}", "title"), (" closed ", "dim"),
                     (signed_usd(c.get("realised")), pnl_style(c.get("realised"))),
                     (f"  {c.get('round_trips', DASH)} RT · ", "dim"), (str(c.get("close_reason") or DASH), "dim")))
    return out


_TAPE_STYLE = {"FILL": "realised", "DEPLOY": "title", "EXIT": "warn", "FIRST_RT": "ok",
               "HALT": "danger", "START": "dim", "STOP": "dim"}


def tape_line(r, today=""):
    ev = r.get("event", "?")
    full = str(r.get("ts", ""))
    # Rows from another day carry their date: a tape spanning two days with
    # bare clock times reads as out of order.
    ts = full[11:19] if full[:10] == today else full[5:16]
    st = _TAPE_STYLE.get(ev, "dim")
    sym = base(r.get("symbol")) if r.get("symbol") else ""
    if ev == "FILL":
        side = r.get("side", "")
        pnl = r.get("pnl")
        return T((ts + " ", "dim"), (f"{sym:<6}", "title"), (f"{side:<4} ", "ok" if side == "BUY" else "held"),
                 (f"L{r.get('level', '?')} @", "dim"), (short_px(r.get("price")), "realised"),
                 (f"  {signed_usd(pnl)}" if pnl is not None else "", pnl_style(pnl)))
    if ev == "DEPLOY":
        return T((ts + " ", "dim"), (f"{sym:<6}", "title"), ("DEPLOY ", st),
                 (f"{(r.get('kind') or '')[:5]} {r.get('levels', '?')} ", "dim"),
                 (f"{short_px(r.get('lo'))}–{short_px(r.get('hi'))}", "predicted"),
                 (" @", "dim"), (short_px(r.get("price")), "realised"),
                 (f"  {r.get('buys', '?')}B/{r.get('sells', '?')}S", "dim"))
    if ev == "FIRST_RT":
        return T((ts + " ", "dim"), (f"{sym:<6}", "title"), ("1st RT ", st),
                 ("after ", "dim"), (fmt_dur(r.get("since_deploy_s")), "realised"),
                 (f"  {r.get('fills', '?')} fills", "dim"),
                 (f"  pred {r['predicted_fills_day']:.1f}/d" if isinstance(r.get("predicted_fills_day"), (int, float)) else "", "predicted"))
    if ev == "EXIT":
        return T((ts + " ", "dim"), (f"{sym:<6}", "title"), ("EXIT ", st),
                 (signed_usd(r.get("realised")), pnl_style(r.get("realised"))),
                 (f"  {r.get('round_trips', '?')} RT · ", "dim"), (str(r.get("close_reason") or ""), "dim"))
    if ev == "HALT":
        return T((ts + " ", "dim"), ("HALT ", st), (str(r.get("reason") or ""), "dim"))
    if ev in ("START", "STOP"):
        gl = r.get("grids") or []
        return T((ts + " ", "dim"), (ev + " ", st), (" ".join(base(s) for s in gl), "dim"))
    return T((ts + " ", "dim"), (f"{sym:<6}", "title"), (ev, st))


def tape_lines(V, n, trading_only=False):
    rows = V.journal
    if trading_only:                 # the small cell: START/STOP restarts are noise there
        rows = [r for r in rows if r.get("event") not in ("START", "STOP")]
    rows = rows[-n:] if n > 0 else []
    lines = [tape_line(r, V.today) for r in rows]
    return [Text("")] * max(0, n - len(lines)) + lines          # newest at bottom


_FILL_RE = re.compile(r" (BUY|SELL) L\d+ @|first round trip|^EXIT |restored|started:|HALT")


def log_line(s):
    m = re.match(r"(\d\d:\d\d:\d\d) \[(\w+)\] (.*)", s)
    if not m:
        return T((s, "dim"))
    lvl, msg = m.group(2), m.group(3)
    style = {"ERROR": "danger", "CRITICAL": "danger", "WARNING": "warn"}.get(lvl)
    if style is None:
        style = "title" if _FILL_RE.search(msg) else ("dim" if "trade feed" in msg or "caught up" in msg else "realised")
    return T((m.group(1) + "  ", "dim"), (msg, style))


def log_lines(V, n):
    lines = [log_line(s) for s in V.log[-n:]] if n > 0 else []
    return [Text("")] * max(0, n - len(lines)) + lines          # newest at bottom


def cell(title, lines, height, age=None, sub=None):
    inner = max(0, height - 2)
    lines = lines[:inner] + [Text("")] * max(0, inner - len(lines))
    t = T((title, "title"))
    if age is not None:
        g, gs = fresh(age)
        t.append(" " + g, style=gs)
    if sub:
        t.append("  " + sub, style="dim")
    return Panel(Group(*lines), title=t, title_align="left", border_style="frame",
                 box=box.SQUARE, padding=(0, 1), height=height)


def bottom_band(V, mode, width, height):
    inner = height - 2
    lay = Layout(size=height, name="bottom")
    if mode.w == "wide":
        w1 = max(44, int(width * 0.25))
        w2 = max(44, int(width * 0.25))
        w3 = max(34, int(width * 0.24))
        lay.split_row(Layout(cell("RISK", risk_lines(V)[:inner], height), size=w1),
                      Layout(cell("P/L", pnl_lines(V, w2 - 4)[:inner], height), size=w2),
                      Layout(cell("TAPE", tape_lines(V, inner, True), height, sub="t = all rows"), size=w3),
                      Layout(cell("GRIDBOT LOG", log_lines(V, inner), height, V.snap_age), ratio=1))
    elif mode.w == "ref":
        # Three cells: the log is mostly feed reconnect noise and has its own
        # screen (l); the tape does not.
        w1 = max(44, int(width * 0.32))
        w2 = max(44, int(width * 0.33))
        lay.split_row(Layout(cell("RISK", risk_lines(V)[:inner], height), size=w1),
                      Layout(cell("P/L", pnl_lines(V, w2 - 4)[:inner], height), size=w2),
                      Layout(cell("TAPE", tape_lines(V, inner, True), height, sub="t = all · l = log"), ratio=1))
    elif mode.w == "narrow":
        pw = width // 3
        lay.split_row(Layout(cell("RISK", risk_lines(V)[:inner], height), size=pw),
                      Layout(cell("TAPE", tape_lines(V, inner, True), height), size=pw),
                      Layout(cell("GRIDBOT LOG", log_lines(V, inner), height, V.snap_age), ratio=1))
    else:
        pw = width // 2
        lay.split_row(Layout(cell("RISK", risk_lines(V)[:inner], height), size=pw),
                      Layout(cell("GRIDBOT LOG", log_lines(V, inner), height, V.snap_age), ratio=1))
    return lay


# ----------------------------------------------------------- footer/help --
def footer(V, ui, log_in_footer, width):
    keys = T(("q", "title"), (" quit · ", "dim"), ("r", "title"), (" reread · ", "dim"),
             ("p", "title"), (" pause · ", "dim"), ("↑↓", "title"), (" select · ", "dim"),
             ("enter", "title"), (" detail · ", "dim"), ("l", "title"), (" log · ", "dim"),
             ("t", "title"), (" tape · ", "dim"), ("?", "title"), (" help", "dim"),
             ("   PAUSED" if ui.paused else "", "warn"))
    if width >= 110:
        legend = T(("  amber ", "predicted"), ("scanner · ", "dim"), ("cyan ", "realised"), ("bot · ", "dim"),
                   (G["fresh"] + "fresh ", "ok"), (G["stale"] + ">12s ", "warn"), (G["dead"] + ">30s", "danger"))
    else:
        legend = T(("  ", ""), (G["fresh"] + "ok ", "ok"), (G["stale"] + ">12s ", "warn"), (G["dead"] + ">30s", "danger"))
    g = Table.grid(expand=True)
    g.add_column(ratio=1, no_wrap=True, overflow="crop")
    g.add_column(width=legend.cell_len, no_wrap=True, justify="right")
    g.add_row(keys, legend)
    if log_in_footer:
        last = log_line(V.log[-1]) if V.log else T((DASH, "dim"))
        return Group(last, g)
    return g


HELP = [
    ("q / Ctrl-C", "quit (alternate screen: your shell is left as it was)"),
    ("r", "re-read scan.json and scanner.log now. The console never scans Kraken itself."),
    ("p", "pause / resume polling"),
    ("↑ ↓", "select an oracle row"),
    ("enter", "the scanner's own detail() for the selected pair"),
    ("l", "gridbot.log, full width"),
    ("t", "the tape (journal.jsonl), full width"),
    ("esc", "back"),
    ("", ""),
    ("HEADER", "bot · feed · scanner liveness; capital deployed; the book; the guard in its own units"),
    ("LIVE GRIDS", "one card per grid: identity · yield pred/real · RT · time to 1st RT / ladder / edge and P/L"),
    ("ladder", "B resting buy (cash)  S resting sell (coins)  ○ the one empty rung  ◆ last print"),
    ("ORACLE", "scan.json as ranked. STEP/NET % per rung, F/d fills, RT/d round trips, Y/D yield, ADJ risk-adj,"),
    ("", "CONT containment, MAE inventory %, VR variance ratio, H Hurst, ATR%, LIQ $M/24h, SPR spread %, D5 days to 5%"),
    ("gates/rejected", "from scanner.log: the gates in force and why pairs were dropped"),
    ("RISK", "floor/ceiling distance and the loss a floor pierce would lock in; skipped picks with reasons"),
    ("TAPE", "journal rows: DEPLOY, FILL, 1st RT (time to first round trip), EXIT, HALT, START, STOP"),
    ("amber / cyan", "scanner (predicted) numbers / bot (realised) numbers. — = not served, never derived"),
    ("pred vs real", "the scanner's yield_day is a replay count, not a forecast (audit 2026-09-23); real is measured"),
]


def help_panel(height):
    lines = [T((f"{k:<15}", "title"), (v, "dim")) for k, v in HELP]
    return cell("HELP", lines, height)


def detail_panel(V, ui, width, height, oracle_meta):
    rows = V.orc_rows
    if not rows:
        return cell("DETAIL", [T(("no oracle row selected", "dim"))], height)
    r = rows[min(ui.sel, len(rows) - 1)]
    inner_w = max(10, width - 4)
    lines = []
    if r.get("full"):
        try:
            cfg = glyphs._oracle.Cfg(fee=oracle_meta.get("fee_pct") or 0.22,
                                     interval_label=oracle_meta.get("interval") or "30m")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                glyphs._oracle.detail(r["full"], cfg, inner_w)
            lines = [Text.from_ansi(ln, no_wrap=True, overflow="crop")
                     for ln in buf.getvalue().splitlines()]
            return cell(f"DETAIL · {r['symbol']}", lines, height)
        except Exception as e:
            lines = [T((f"detail() failed: {type(e).__name__}: {e}", "danger"))]
    for k in ("symbol", "kind", "levels", "lo", "hi", "center", "step_pct", "net_pct", "fills_day", "rt_day",
              "yield_day", "yield_adj", "cont", "mae_pct", "vr", "hurst", "atr_pct", "turnover24",
              "spread_pct", "days_to_5pct", "corr_top", "qualified", "basket"):
        v = r.get(k)
        lines.append(T((f"{k:<15}", "dim"), (DASH if v is None else str(v), "predicted")))
    return cell(f"DETAIL · {r.get('symbol')}", lines, height)


# ---------------------------------------------------------------- frame --
def build_frame(st, width, height, ui=None):
    """The whole screen as a Layout of exactly width x height. Pure: no I/O,
    no clock reads -- everything comes in through `st`."""
    ui = ui or UI()
    mode = mode_for(width, height)
    V = derive(st)
    single = mode.w == "single"

    head_h = min(HEAD_H, height)
    left = height - head_h
    log_panel_h = 5 if single and left >= 1 + 5 + 4 + 6 else 0
    foot_h = min(left, 2 if (mode.h == "tiny" and not log_panel_h) else 1)
    left -= foot_h
    bot_h = 0
    if not single and ui.view == "main":
        bot_h = {"tall": 10, "full": 8, "short": 4, "tiny": 0}[mode.h]
        bot_h = bot_h if left - bot_h >= 10 else 0
    left -= bot_h + log_panel_h
    mid_h = max(0, left)

    parts = [Layout(header_panel(V, width), name="header", size=head_h)]
    if mid_h:
        mid = Layout(name="middle", size=mid_h)
        if ui.view == "help":
            mid.update(help_panel(mid_h))
        elif ui.view == "detail":
            mid.update(detail_panel(V, ui, width, mid_h, st.get("oracle") or {}))
        elif ui.view == "log":
            mid.update(cell("GRIDBOT LOG", log_lines(V, mid_h - 2), mid_h, V.snap_age))
        elif ui.view == "tape":
            mid.update(cell("TAPE", tape_lines(V, mid_h - 2), mid_h, sub="journal.jsonl, newest at the bottom"))
        elif mode.w in ("wide", "ref", "narrow"):
            gw = max(84, int(width * 0.46)) if mode.w == "wide" else max(48, int(width * 0.36))
            ow = width - gw
            mid.split_row(Layout(oracle_panel(V, mode, ow, mid_h, ui), size=ow),
                          Layout(grids_panel(V, mode, gw, mid_h), ratio=1))
        else:
            n = len(V.grids)
            want = 2 + max(1, n if single else 3 * n)
            gh = min(want, max(3, mid_h // 2))
            oh = mid_h - gh
            oracle = oracle_panel(V, mode, width, oh, ui, top_n=5 if single else None)
            mid.split_column(Layout(grids_panel(V, mode, width, gh), size=gh),
                             Layout(oracle, size=oh))
        parts.append(mid)
    if bot_h:
        parts.append(bottom_band(V, mode, width, bot_h))
    if log_panel_h:
        parts.append(Layout(cell("GRIDBOT LOG", log_lines(V, 3), log_panel_h, V.snap_age),
                            size=log_panel_h))
    if foot_h:
        parts.append(Layout(footer(V, ui, foot_h == 2, width), name="footer", size=foot_h))
    root = Layout(name="root")
    root.split_column(*parts)
    return root


# ------------------------------------------------------------------ keys --
class Keys:
    """Non-blocking single-key reader. Returns None, a char, or one of
    'up' 'down' 'enter' 'esc'."""

    def __init__(self):
        self.win = sys.platform == "win32"
        self._old = None
        if not self.win and sys.stdin.isatty():
            import termios
            import tty
            self._old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def close(self):
        if self._old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)

    def read(self):
        if self.win:
            import msvcrt
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return {"H": "up", "P": "down"}.get(msvcrt.getwch())
        else:
            import select
            if not select.select([sys.stdin], [], [], 0)[0]:
                return None
            ch = sys.stdin.read(1)
            if ch == "\x1b" and select.select([sys.stdin], [], [], 0.02)[0]:
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(seq)
        return {"\r": "enter", "\n": "enter", "\x1b": "esc", "\x03": "q"}.get(ch, ch)


# ------------------------------------------------------------------- app --
class App:
    def __init__(self, args):
        self.status = feeds.StatusFeed(args.status)
        self.oracle = feeds.OracleFeed(json_path=args.oracle_json)
        self.log = feeds.LogFeed(args.log)
        self.journal = feeds.JournalFeed(args.journal)
        self.scanner_log = feeds.ScannerLogFeed(args.scanner_log)
        self.refresh = args.refresh
        self.ui = UI()
        self.pnl_hist = []
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self._sig = None
        self._last_pnl_id = None

    def poll_once(self):
        for f in (self.status, self.oracle, self.log, self.journal, self.scanner_log):
            f.poll()
        with self.lock:
            pnl = self.status.snapshot.get("total_pnl")
            if self.status.age_s < STALE_S and pnl is not None and id(self.status.data) != self._last_pnl_id:
                self._last_pnl_id = id(self.status.data)      # one sample per status write
                self.pnl_hist = (self.pnl_hist + [pnl])[-240:]
            sig = (id(self.status.data), id(self.oracle.data), id(self.log.data), id(self.journal.data),
                   id(self.scanner_log.data), len(self.pnl_hist), int(self.status.age_s))
            if sig != self._sig:
                self._sig = sig
                self.dirty.set()

    def poller(self):
        while not self.stop.is_set():
            t0 = time.monotonic()
            if not self.ui.paused:
                self.poll_once()
            self.stop.wait(max(0.05, 1.0 - (time.monotonic() - t0)))

    def state(self):
        with self.lock:
            return {
                "now": datetime.now(timezone.utc),
                "snapshot": self.status.snapshot, "snapshot_age": self.status.age_s,
                "grids": self.status.grids,
                "oracle": self.oracle.data,
                "oracle_scan_age": self.oracle.scan_age_s(),
                "refresh_s": self.refresh,
                "log": (self.log.data or {}).get("lines", []),
                "journal": (self.journal.data or {}).get("rows", []),
                "scanner_log": self.scanner_log.data,
                "scanner_log_age": self.scanner_log.age_s,
                "pnl_hist": list(self.pnl_hist),
                "today": datetime.now().strftime("%Y-%m-%d"),     # journal ts are local
            }

    def key(self, k):
        ui = self.ui
        n = len((self.oracle.data or {}).get("rows") or [])
        if k == "q":
            return False
        if k == "esc":
            ui.view = "main"
        elif k == "up":
            ui.sel = max(0, ui.sel - 1)
        elif k == "down":
            ui.sel = min(max(0, n - 1), ui.sel + 1)
        elif k == "enter":
            ui.view = "main" if ui.view == "detail" else "detail"
        elif k == "?":
            ui.view = "main" if ui.view == "help" else "help"
        elif k == "l":
            ui.view = "main" if ui.view == "log" else "log"
        elif k == "t":
            ui.view = "main" if ui.view == "tape" else "tape"
        elif k == "p":
            ui.paused = not ui.paused
        elif k == "r":
            self.oracle.reload()
            self.scanner_log._mtime = None
            threading.Thread(target=lambda: (self.oracle.poll(), self.scanner_log.poll()), daemon=True).start()
        return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="GRIDPICK · LATTICE console (read-only)")
    p.add_argument("--status", default=None, help="GridBot status.json (default: next to gridbot.py)")
    p.add_argument("--oracle-json", default=None, help="the scanner's scan.json (default: next to gridbot.py)")
    p.add_argument("--log", default=None, help="GridBot log (default: gridbot.log)")
    p.add_argument("--journal", default=None, help="GridBot journal (default: journal.jsonl)")
    p.add_argument("--scanner-log", default=None, help="scanner output (default: scanner.log)")
    p.add_argument("--refresh", type=int, default=int(os.environ.get("GP_REFRESH", "120")),
                   help="oracle refresh seconds, for 'next scan' and staleness")
    p.add_argument("--exit-after", type=float, default=None,
                   help="quit on their own after N seconds (smoke tests)")
    return p.parse_args(argv)


STOP_FILE = "lattice.stop"      # written by gridbot_stop.ps1; consumed here, exit 0


def stop_file_for(args) -> "Path":
    """Next to the status.json this console reads (the GridBot folder by default)."""
    from pathlib import Path
    return (Path(args.status).resolve().parent if args.status else feeds.HERE) / STOP_FILE


def main(argv=None):
    args = parse_args(argv)
    stop_file = stop_file_for(args)
    try:
        stop_file.unlink()          # a stale request must not quit a fresh console
    except OSError:
        pass
    # A redirected stdout on Windows defaults to cp1252 and the box glyphs
    # would raise. A real console goes through WriteConsoleW either way.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import colorama
        colorama.just_fix_windows_console()
    except Exception:
        pass
    glyphs.probe()
    console = Console(theme=THEME)
    app = App(args)
    app.poll_once()
    threading.Thread(target=app.poller, daemon=True).start()
    keys = Keys()
    last_t, last_size, last_sec = 0.0, None, None
    last_stop_check = None
    t_start, frames = time.monotonic(), 0
    try:
        with Live(Text(""), console=console, screen=True, auto_refresh=False,
                  redirect_stdout=False, redirect_stderr=False, transient=True) as live:
            while True:
                k = keys.read()
                if k is not None:
                    if not app.key(k):
                        break
                    app.dirty.set()
                size = console.size
                sec = int(time.time())
                changed = app.dirty.is_set() or size != last_size or sec != last_sec
                if changed and time.monotonic() - last_t >= 0.25:       # <= 4 Hz
                    app.dirty.clear()
                    frame = build_frame(app.state(), size.width, size.height, app.ui)
                    live.update(frame, refresh=True)
                    draw_logo(console, frame)              # the header logo, in the same write
                    last_t, last_size, last_sec = time.monotonic(), size, sec
                    frames += 1
                if args.exit_after and time.monotonic() - t_start >= args.exit_after:
                    break
                # A kill leaves the Windows Terminal tab open ("process exited
                # with code -1"); a clean exit 0 lets launch.bat end and the
                # tab close. gridbot_stop.ps1 asks for that with this file.
                if sec != last_stop_check and stop_file.exists():
                    try:
                        stop_file.unlink()
                    except OSError:
                        pass
                    break
                last_stop_check = sec
                time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        keys.close()
    if args.exit_after:
        print(f"lattice: {frames} frames in {time.monotonic() - t_start:.1f}s, "
              f"{console.size.width}x{console.size.height}, status err={app.status.error}, "
              f"scan err={app.oracle.error}, log err={app.log.error}, journal err={app.journal.error}, "
              f"scanner.log err={app.scanner_log.error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
