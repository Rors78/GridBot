"""GRIDPICK · LATTICE -- all-in-one console for GridBot and its v3.1 scanner.

    python tui/lattice.py [--status PATH] [--oracle-json PATH] [--log PATH] [--refresh S]

PORTED 2026-09-23 from D:/GridPick/tui to D:/GridBot: the executor this was
written against (HTTP on :8089) is retired. GridBot serves no API, so the
feeds read the files the bot writes -- status.json, scan.json, gridbot.log --
see feeds.py. The notes below describe the original; the invariants hold.

One process, one screen. Rich Live on the alternate screen buffer: it never
scrolls, and it re-lays itself out from console.size on every frame.

Sources, and nothing else:
  executor  GET /api/snapshot and /api/grids on the running executor
  oracle    the running oracle's last scans.jsonl row (or its --json file)
  log       tail of executor/gridpick_executor.log

It never opens gridpick_executor_state.json, never imports the executor,
never calls Kraken, and never changes the execution mode -- it only reads.
Fields the executor does not serve are drawn as a dim em dash, not derived.
"""
from __future__ import annotations

import os

# The user's invariant for anything near the executor. This module does not
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
    w: str      # ref | narrow | stacked | single
    h: str      # full | short | tiny


def mode_for(width, height):
    """Spec §3 breakpoints. Pure, so the test can assert each size lands
    in a distinct layout rather than one layout that happens to fit all."""
    if width >= 140:
        w = "ref"
    elif width >= 110:
        w = "narrow"
    elif width >= 90:
        w = "stacked"
    else:
        w = "single"
    h = "full" if height >= 36 else ("short" if height >= 28 else "tiny")
    return Mode(w, h)


@dataclass
class UI:
    sel: int = 0
    view: str = "main"          # main | help | detail | log
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
    """Style a glyph string (gauge/strip/bar) char by char from the theme.
    `empty` is the style for empty cells: a gauge with nothing to mark on it
    takes the quieter frame tone, so a run of them does not read as a slab."""
    style = {G["held"]: "held", G["empty"]: empty, G["cursor"]: "cursor",
             G["lcap"]: "frame", G["rcap"]: "frame"}
    t = Text(no_wrap=True, overflow="crop")
    for ch in s:
        t.append(ch, style=style.get(ch, "dim"))
    return t


def short_px(p):
    """Price in at most 7 cells, so band labels keep the gauges aligned.
    Measured on the live board: SHIB/PEPE sit near 5e-6, PENGU near 0.007."""
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


def fmt_span_min(m):
    if m is None:
        return DASH
    m = int(m)
    return f"{m // 1440}d{m % 1440 // 60:02d}h"


def fmt_uptime(hours):
    if hours is None:
        return DASH
    m = int(hours * 60)
    return f"{m // 1440}d {m % 1440 // 60:02d}:{m % 60:02d}"


def signed_usd(v):
    if v is None:
        return DASH
    return f"{'+' if v >= 0 else '−'}${abs(v):.2f}"


def fresh(age):
    """(glyph, style) for a data age, per spec stale rule."""
    if age is None or age > DEAD_S:
        return G["dead"], "danger"
    if age > STALE_S:
        return G["stale"], "warn"
    return G["fresh"], "ok"


def base(sym):
    return (sym or "?").split("/")[0]


# --------------------------------------------------------------- derive --
_CAP_RE = re.compile(r"at max exposure: \$([\d,.]+) of \$([\d,.]+)")


def derive(st):
    """Every number shown in more than one place is computed here, once.
    Nothing here is a recomputation of a trading quantity: it is the
    executor's and oracle's own values, picked out and formatted."""
    snap = st.get("snapshot") or {}
    grids = st.get("grids") or {}
    orc = st.get("oracle") or {}
    V = SimpleNamespace()
    V.snap_age = st.get("snapshot_age")
    V.grids_age = st.get("grids_age")
    V.api_dead = V.snap_age is None or V.snap_age > DEAD_S
    V.mode = (snap.get("execution_mode") or DASH).upper()
    V.uptime = fmt_uptime(snap.get("uptime_hours"))
    ws = snap.get("ws") or {}
    V.ws_tick = ws.get("oldest_tick_age_s")
    V.n_grids = snap.get("n_active_grids", len(grids) if grids else None)
    V.cycles = snap.get("total_cycles")
    V.pnl = snap.get("total_pnl")            # GROSS, per the executor
    V.fees = snap.get("total_fees")
    V.realised = snap.get("realised")
    V.unrealised = snap.get("unrealised")
    V.closed_n = snap.get("closed_count")
    V.halt = snap.get("halt")
    V.unrestored = snap.get("unrestored") or []
    V.exit_after = snap.get("exit_after_s")
    V.feed_connected = snap.get("feed_connected")
    V.feed_last_error = snap.get("feed_last_error") or ""
    V.gaps = snap.get("catch_up_gaps") or 0
    V.scanner_running = snap.get("scanner_running")
    V.scanner_note = snap.get("scanner_note") or ""
    V.history_n = len(snap.get("trade_history") or [])
    refusals = snap.get("refusals") or {}
    V.n_refused = len(refusals)
    # The executor's own ledger (reserved_usd / pool_total_usd, since
    # 91fc438). Older executors only say it inside a refusal string; that
    # parse stays as a labelled fallback, never silently.
    V.reserved, V.cap, V.reserved_src = snap.get("reserved_usd"), snap.get("pool_total_usd"), ""
    if V.reserved is None or not V.cap:
        V.reserved = V.cap = None
        for why in refusals.values():
            m = _CAP_RE.search(str(why))
            if m:
                V.reserved = float(m.group(1).replace(",", ""))
                V.cap = float(m.group(2).replace(",", ""))
                V.reserved_src = " (from refusal)"
                break
    V.grids = [dict(g, pair=g.get("pair") or k) for k, g in grids.items()]
    V.orc_rows = orc.get("rows") or []
    V.orc_version = orc.get("version")
    V.orc_interval = orc.get("interval")
    V.orc_scanned = orc.get("n_scanned")
    V.orc_qual = orc.get("n_qualified")
    V.orc_rejects = orc.get("rejects")
    V.scan_age = st.get("oracle_scan_age")
    V.refresh = st.get("refresh_s") or 120
    V.scan_stale = V.scan_age is None or V.scan_age > 2 * V.refresh
    V.log = st.get("log") or []
    V.pnl_hist = st.get("pnl_hist") or []
    V.now = st.get("now") or datetime.now(timezone.utc)
    return V


# ---------------------------------------------------------------- header --
def header_panel(V, width):
    clock = V.now.strftime("%Y-%m-%d %H:%M:%S UTC")
    g, gs = fresh(V.snap_age)
    ages = f" {fmt_clock(V.scan_age)} ago" if V.scan_age is not None else f" {DASH}"
    nxt = DASH if V.scan_age is None else fmt_clock(max(0, V.refresh - V.scan_age % V.refresh))
    orc = T(("ORACLE ", "title"), (f"v{V.orc_version or DASH}", "predicted"),
            (f"  {V.orc_interval or DASH} · ", "dim"),
            (f"{V.orc_scanned if V.orc_scanned is not None else DASH}", "predicted"),
            (" pairs · ", "dim"),
            (f"{V.orc_qual if V.orc_qual is not None else DASH}", "predicted"),
            (" qual · scan", "dim"),
            (ages, "warn" if V.scan_stale else "dim"),
            (f" · next ~{nxt}", "dim"))
    mode_style = "danger" if V.mode == "LIVE" else "title"
    ws_style = "warn" if (V.ws_tick or 0) > DEAD_S else "realised"
    exe = T(("   GRIDBOT ", "title"), (g + " ", gs), (V.mode, mode_style),
            ("  up ", "dim"), (V.uptime, "realised"),
            ("  ws ", "dim"),
            (fmt_secs(V.ws_tick), ws_style),
            ("  status ", "dim"),
            (fmt_secs(V.snap_age), gs))
    # Below the reference width the line crops; executor liveness goes
    # first there, since it is the one thing that must never be cut off.
    top = T(orc, exe) if width >= 140 else T(exe.copy()[3:], ("   ", ""), orc)

    if V.reserved is not None and V.cap:
        frac = V.reserved / V.cap
        bw = 20 if width >= 110 else 10
        n = min(bw, int(round(frac * bw)))
        full = frac >= 0.999
        res = T(("reserved ", "dim"), (f"${V.reserved:.2f} / ${V.cap:.2f} ", "realised"),
                (G["held"] * n, "danger" if full else "held"), (G["empty"] * (bw - n), "dim"),
                (f" {frac * 100:.0f}%", "danger" if full else "realised"),
                (V.reserved_src, "dim"))
    else:
        res = T(("reserved ", "dim"), (DASH, "dim"))
    line2 = T(res, ("   live grids ", "dim"),
              (str(V.n_grids) if V.n_grids is not None else DASH, "realised"),
              ("   cycles ", "dim"), (str(V.cycles) if V.cycles is not None else DASH, "realised"),
              ("   pnl ", "dim"), (signed_usd(V.pnl), "realised"),
              ("   skipped ", "dim"), (str(V.n_refused), "warn" if V.n_refused else "dim"),
              ("   HALTED" if V.halt else "", "danger"))
    title = T(("GRIDPICK ", "title"), (G["cursor"], "cursor"), (" LATTICE", "title"))
    # Clock on the top border, right-aligned, as in the spec frame. Panel
    # draws "┌─ " + title + " ─…┐", so the title may use width - 6 cells.
    fill = width - 6 - title.cell_len - len(clock) - 2
    if fill >= 1:
        title.append(" " + "─" * fill + " ", style="frame")
        title.append(clock, style="dim")
    name = T(("G R I D P I C K", "title"), ("   lattice console, read-only", "dim"))
    g = Table.grid(padding=(0, 2))
    g.add_column(width=LOGO_COLS, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(logo_block(), Group(name, top, line2))
    return Panel(g, title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 1), height=HEAD_H)


# THE HEADER LOGO IS THE REAL ICON (operator, 2026-09-23: "everyone else will need a
# rewrite"). The header reserves a block of cells with a background colour nothing
# else uses; after each frame draw_logo() finds it and paints assets/gridpick_logo.png
# over it as a Sixel image (Windows Terminal: 10x20 image pixels per cell, 3 rows =
# 60 px). A terminal without Sixel ignores the sequence and shows the blank block.
LOGO_MARK = "#010203"
LOGO_ROWS, LOGO_COLS = 3, 6
HEAD_H = LOGO_ROWS + 2
ESC = chr(27)
_LOGO_SIXEL = None


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
            _LOGO_SIXEL = sixel(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "gridpick_logo.png"),
                                height_px=LOGO_ROWS * 20)
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
# (key, header, width, justify). Band is the flexible column.
_COLS = {
    "rank": ("  #", 3, "right"),
    "pair": ("PAIR", 6, "left"),
    "price": ("PRICE", 7, "right"),
    "band": ("BAND  lo ─◆─ hi", 0, "left"),
    "k": ("k", 4, "right"),
    "lv": ("LV", 3, "right"),
    "yd": ("Y/D IS", 6, "right"),
    "ydo": ("Y/D OOS", 7, "right"),
    "cont": ("CONT", 4, "right"),
    "conto": ("CONT IS/OOS", 11, "right"),
    "dd": ("DD%", 5, "right"),
    "flag": ("FLAG", 6, "left"),
    "q": ("Q", 1, "right"),
}
_ORDER = ["rank", "pair", "price", "band", "k", "lv", "yd", "ydo", "conto", "cont",
          "dd", "flag", "q"]
# Spec order for 110-139: FLAG, DD%, CONT OOS, Y/D OOS. Then, only if a
# panel is narrower than any spec size, further silent drops.
_DROP = ["flag", "dd", "conto", "ydo", "price", "k", "lv", "band"]


def oracle_columns(mode, inner_w):
    cols = [c for c in _ORDER if c != "cont"]
    n_spec = 0 if mode.w == "ref" else 4
    for c in _DROP[:n_spec]:
        cols.remove(c)
    if "conto" not in cols and "cont" not in cols:
        cols.insert(cols.index("yd") + 1, "cont")

    def fixed(cs):
        return sum(_COLS[c][1] for c in cs if c != "band") + len(cs) - 1

    for c in _DROP[n_spec:]:
        if "band" in cols and inner_w - fixed(cols) >= 6:
            break
        if "band" not in cols and fixed(cols) <= inner_w:
            break
        if c in cols:
            cols.remove(c)
    band_w = max(0, inner_w - fixed(cols)) if "band" in cols else 0
    return cols, band_w


def band_cell(r, band_w):
    lo, hi = r.get("lo"), r.get("hi")
    los, his = short_px(lo).rjust(7), short_px(hi).ljust(7)
    g_w = band_w - len(los) - len(his) - 2
    px = r.get("price")
    # Capped at 24 (the spec frame draws 15): seen live on a 180-column
    # window, a 40-cell gauge with no price cursor is a grey slab in Consolas.
    tone = "dim" if px is not None else "frame"
    if g_w >= 4:
        g_w = min(g_w, 24)
        return T((los + " ", "dim"), glyph_text(glyphs.band_gauge(px, lo, hi, g_w), tone),
                 (" " + his, "dim"))
    return glyph_text(glyphs.band_gauge(px, lo, hi, max(1, min(band_w, 24))), tone)


def oracle_cell(c, i, r, band_w, selected=False):
    num = lambda v, f: (f.format(v), "predicted") if v is not None else (DASH, "dim")  # noqa: E731
    if c == "rank":
        n = str(i + 1)
        if selected:       # the spec's price-cursor glyph doubles as row cursor
            return T((G["cursor"], "cursor"), (n.rjust(2), "cursor"))
        return T((n.rjust(3), "dim"))
    if c == "pair":
        return T((base(r.get("symbol")), "title"))
    if c == "price":
        return T((short_px(r.get("price")), "predicted" if r.get("price") else "dim"))
    if c == "band":
        return band_cell(r, band_w)
    if c == "k":
        return T(num(r.get("k"), "{:g}"))
    if c == "lv":
        return T(num(r.get("levels"), "{}"))
    if c == "yd":
        return T(num(r.get("yield_day"), "{:.2f}"))
    if c == "ydo":
        return T(num(r.get("oos_yield_day"), "{:.2f}"))
    if c == "cont":
        return T(num(r.get("cont"), "{:.2f}"))
    if c == "conto":
        a, b = r.get("cont"), r.get("oos_cont")
        return T(num(a, "{:.2f}"), ("/", "dim"), num(b, "{:.2f}"))
    if c == "dd":
        return T(num(r.get("inv_dd_pct"), "{:.1f}"))
    if c == "flag":
        return T((r.get("boundary") or "", "warn"))
    if c == "q":
        return T((G["fresh"], "ok")) if r.get("qualified") else T(" ")
    return T("")


def rejected_line(V):
    rej = V.orc_rejects
    if rej is None:
        return T(("rejected ", "dim"), (DASH, "dim"), ("  (not in scan.json)", "dim"))
    total = sum(rej.values())
    parts = [("rejected ", "dim"), (f"{total}", "predicted"), (":", "dim")]
    for why, n in sorted(rej.items(), key=lambda kv: -kv[1]):
        parts += [(f" {why} ", "dim"), (str(n), "predicted"), (" ·", "frame")]
    return T(*parts[:-1] if len(parts) > 3 else parts)


def oracle_panel(V, mode, width, height, ui, top_n=None):
    inner_w, inner_h = max(0, width - 2), max(0, height - 2)
    cols, band_w = oracle_columns(mode, inner_w)
    rows = V.orc_rows[:top_n] if top_n else V.orc_rows
    n_fit = max(0, inner_h - 3)                    # header + rule + rejected
    sel = min(max(0, ui.sel), max(0, len(rows) - 1))
    start = 0 if sel < n_fit else sel - n_fit + 1
    tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True, pad_edge=False,
                padding=(0, 0), header_style="dim", border_style="frame")
    for c in cols:
        h, w, j = _COLS[c]
        if c == "band":
            tbl.add_column(h, ratio=1, min_width=band_w, no_wrap=True, overflow="crop")
        else:
            tbl.add_column(h, width=w, justify=j, no_wrap=True, overflow="crop")
    for i in range(start, min(len(rows), start + n_fit)):
        is_sel = i == sel and ui.view == "main"
        tbl.add_row(*[oracle_cell(c, i, rows[i], band_w, is_sel) for c in cols])
    body = [tbl] if inner_h >= 3 else []
    pad = n_fit - min(n_fit, max(0, len(rows) - start))
    body += [Text("")] * pad
    if not rows:
        body = ([T(("  no oracle results yet", "dim"))] + [Text("")] * max(0, inner_h - 2))
    body.append(rejected_line(V))
    src = "" if any(r.get("price") for r in rows) else "  (scans.jsonl: no price)"
    title = T(("ORACLE", "title"), (" · ranked by risk-adjusted yield", "dim"),
              (src, "dim"), ("  stale" if V.scan_stale else "", "warn"))
    return Panel(Group(*body[-inner_h:] if inner_h else []), title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 0), height=height)


# ------------------------------------------------------------ live grids --
def grid_rows(g, mode, inner_w, strips_only):
    levels, filled = g.get("levels") or 0, g.get("filled") or 0
    if strips_only:
        sw = max(3, min(22, inner_w - 16))
        return [T((f"{base(g['pair']):<7}", "title"),
                  glyph_text(glyphs.rung_strip(g.get("held_mask", filled), levels,
                                               g.get("px_idx"), sw)),
                  (f" {filled}/{levels}", "realised"))]
    # Sized to the 44 cells the reference layout leaves a card: the spec's
    # own column list fixes the oracle panel's width, so the card line is
    # base symbol, basis@leverage, age, pred · real -- and the unit sits in
    # the panel title. range is appended last so cropping it is silent.
    basis, lev = g.get("yield_basis_usd"), g.get("leverage")
    # Flags first, so a narrow card never crops them: a grid that is catching
    # up, or has had no print for 10 min, cannot fill or exit on time.
    pa = g.get("last_print_age_s")
    flag = (" SYNC" if g.get("catching_up")
            else (f" QUIET {pa / 60:.0f}m" if pa is not None and pa > 600 else ""))
    row1 = T((f"{base(g['pair']):<6}", "title"), (flag, "warn"),
             (f" ${basis:,.0f}" if basis is not None else f" {DASH}", "realised"),
             (f"@{lev:g}x" if lev is not None else "", "realised"),
             ("  ", ""), (fmt_span_min(g.get("uptime_min")), "realised"))
    if mode.w in ("ref", "stacked"):
        # Both printed from /api/grids as served -- predicted_yield_day and
        # realised_yield_day are on one basis there (yield_basis_usd); the
        # spec forbids recomputing either here. None is drawn as a dash: the
        # executor sends None when the basis or deploy time is unknown, and
        # that is not a measured zero.
        py, ry = g.get("predicted_yield_day"), g.get("realised_yield_day")
        row1.append_text(T(("  pred ", "dim"),
                           (f"{py:.2f}" if py is not None else DASH, "predicted"),
                           (" · real ", "dim"),
                           (f"{ry:.2f}" if ry is not None else DASH,
                            "realised" if ry is not None else "dim")))
    if mode.w != "ref":     # 44 cells at the reference width: no room for it
        row1.append_text(T(("  range ", "dim"), (g.get("range") or DASH, "realised")))
    tail = T((f" {filled}/{levels} held", "realised"),
             ("  cyc ", "dim"), (str(g.get("cycles", DASH)), "realised"),
             ("  pnl ", "dim"), (signed_usd(g.get("pnl")), "realised"))
    sw = max(8, min(22, inner_w - tail.cell_len))    # strip yields to the numbers
    row2 = T(glyph_text(glyphs.rung_strip(g.get("held_mask", filled), levels,
                                          g.get("px_idx"), sw)), tail)
    return [row1, row2]


def grids_panel(V, mode, width, height, strips=False):
    inner_w, inner_h = max(0, width - 2), max(0, height - 2)
    n = len(V.grids)
    strips = strips or n * 2 > inner_h
    lines = []
    if not V.grids:
        lines = [T(("── no grid ──", "dim"))]
    else:
        show = V.grids if n <= inner_h or not strips else V.grids[:max(0, inner_h - 1)]
        for g in show:
            lines += grid_rows(g, mode, inner_w, strips)
        if len(show) < n:
            lines.append(T((f"+{n - len(show)} more", "dim")))
    lines = lines[:inner_h] + [Text("")] * max(0, inner_h - len(lines))
    g, gs = fresh(V.grids_age)
    title = T(("LIVE GRIDS ", "title"), (g, gs))
    if mode.w in ("ref", "stacked") and not strips:
        title.append_text(T(("  pred · real in %/d", "dim")))
    return Panel(Group(*lines) if lines else Text(""), title=title, title_align="left",
                 border_style="frame", box=box.SQUARE, padding=(0, 0), height=height,
                 style="dim" if V.api_dead else "")


# ----------------------------------------------------------- bottom band --
def pierce_lines(V):
    """Alerts first (the states where the bot stops trading but looks fine),
    then GridBot's own edge figures (status.json), one line per grid."""
    out = []
    if V.halt:
        out.append(T(("HALT ", "danger"), (V.halt, "danger")))
    if V.unrestored:
        out.append(T(("UNRESTORED ", "danger"), (", ".join(map(str, V.unrestored)), "danger"),
                      ("  slot held", "dim")))
    if V.scanner_running is False:
        out.append(T(("SCANNER DOWN ", "danger"), (V.scanner_note, "dim")))
    elif V.scanner_note.startswith("scan is"):
        out.append(T(("SCAN STALE ", "warn"), (V.scanner_note, "dim")))
    if V.feed_connected is False:
        out.append(T(("FEED DOWN ", "danger"), (V.feed_last_error, "dim")))
    if V.gaps:
        out.append(T(("catch-up gaps ", "warn"), (str(V.gaps), "warn")))
    ea = f"{V.exit_after:.0f}s" if isinstance(V.exit_after, (int, float)) and V.exit_after > 0 else "never"
    for g in V.grids:
        f, c, fl = g.get("to_floor_pct"), g.get("to_ceiling_pct"), g.get("floor_pnl")
        near = f is not None and f < 3.0
        out.append(T((f"{base(g['pair']):<6}", "title"),
                     (" floor ", "dim"), (f"{f:.1f}%" if f is not None else DASH,
                                          "danger" if near else "realised"),
                     (" ceil ", "dim"), (f"{c:.1f}%" if c is not None else DASH, "realised"),
                     (" @floor ", "dim"), (signed_usd(fl), "danger" if (fl or 0) < 0 else "realised")))
        if g.get("outside_for_s") is not None:
            out.append(T(("  OUTSIDE band ", "danger"),
                         (f"{g['outside_for_s']:.0f}s -> exit at {ea}", "danger")))
    if not out:
        out = [T(("no grid", "dim"))]
    out.append(T(("skipped pairs  ", "dim"), (str(V.n_refused), "warn" if V.n_refused else "realised"),
                 ("  (correlated / cooldown)" if V.n_refused else "", "dim")))
    out.append(T(("mode  ", "dim"), (V.mode, "title")))
    return out


def pnl_lines(V, width):
    sw = max(4, min(20, width - 14))
    per = " · ".join(f"{base(g['pair'])} {signed_usd(g.get('pnl'))}" for g in V.grids) or DASH
    return [
        T((glyphs.spark(V.pnl_hist, sw), "realised"), ("  ", ""), (signed_usd(V.pnl), "realised")),
        T(("realised ", "dim"), (signed_usd(V.realised), "realised"),
          (" · unreal ", "dim"), (signed_usd(V.unrealised), "realised")),
        T(("fees ", "dim"), (signed_usd(V.fees), "realised"),
          (" · closed grids ", "dim"), (str(V.closed_n) if V.closed_n is not None else DASH, "realised")),
        T(("per grid  ", "dim"), (per, "realised")),
        T(("spark: this session, 1 sample/poll", "dim")),
    ]


def log_line(s):
    m = re.match(r"(\d\d:\d\d:\d\d) \[(\w+)\] (.*)", s)
    if not m:
        return T((s, "dim"))
    lvl = m.group(2)
    style = {"ERROR": "danger", "CRITICAL": "danger", "WARNING": "warn"}.get(lvl, "realised")
    return T((m.group(1) + "  ", "dim"), (m.group(3), style))


def log_lines(V, n):
    lines = [log_line(s) for s in V.log[-n:]] if n > 0 else []
    return [Text("")] * max(0, n - len(lines)) + lines          # newest at bottom


def cell(title, lines, height, age=None):
    inner = max(0, height - 2)
    lines = lines[:inner] + [Text("")] * max(0, inner - len(lines))
    t = T((title, "title"))
    if age is not None:
        g, gs = fresh(age)
        t.append(" " + g, style=gs)
    return Panel(Group(*lines), title=t, title_align="left", border_style="frame",
                 box=box.SQUARE, padding=(0, 1), height=height)


def bottom_band(V, mode, width, height):
    inner = height - 2
    lay = Layout(size=height, name="bottom")
    if mode.w in ("ref", "narrow"):
        pw = width // 3
        lay.split_row(Layout(cell("PIERCE / RISK", pierce_lines(V)[:inner], height), size=pw),
                      Layout(cell("P/L", pnl_lines(V, pw - 4)[:inner], height), size=pw),
                      Layout(cell("GRIDBOT LOG", log_lines(V, inner), height, V.snap_age), ratio=1))
    else:
        pw = width // 2
        lay.split_row(Layout(cell("PIERCE / RISK", pierce_lines(V)[:inner], height), size=pw),
                      Layout(cell("GRIDBOT LOG", log_lines(V, inner), height, V.snap_age), ratio=1))
    return lay


# ----------------------------------------------------------- footer/help --
def footer(V, ui, log_in_footer, width):
    keys = T(("q", "title"), (" quit · ", "dim"), ("r", "title"), (" reread · ", "dim"),
             ("p", "title"), (" pause · ", "dim"), ("↑↓", "title"), (" select · ", "dim"),
             ("enter", "title"), (" detail · ", "dim"), ("l", "title"), (" log · ", "dim"),
             ("?", "title"), (" help", "dim"),
             ("   PAUSED" if ui.paused else "", "warn"))
    if width >= 110:
        legend = T(("  data ", "dim"), (G["fresh"] + "fresh ", "ok"),
                   (G["stale"] + "stale >12s ", "warn"), (G["dead"] + "dead >30s", "danger"))
    else:
        legend = T(("  ", ""), (G["fresh"] + "ok ", "ok"), (G["stale"] + ">12s ", "warn"),
                   (G["dead"] + ">30s", "danger"))
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
    ("r", "re-read the oracle's output now. The console never scans Kraken itself."),
    ("p", "pause / resume polling"),
    ("↑ ↓", "select an oracle row"),
    ("enter", "oracle detail() for the selected pair"),
    ("l", "gridbot.log, full width"),
    ("esc", "back"),
    ("", ""),
    ("amber", "predicted: oracle numbers (Y/D, OOS, k, LV, CONT, DD%)"),
    ("cyan", "realised: GridBot numbers from status.json (pnl, cycles, held)"),
    (DASH, "not in status.json / scan.json -- shown empty, never derived"),
    ("strip", "▓ rung holding coins (a resting SELL), ░ rung holding cash, ◆ price"),
    ("card", "$alloc per grid; pred/real %/d as status.json serves them"),
    ("reserved", "deployed / capital, from status.json"),
    ("next ~", "scan age vs --refresh (default 120s, the launcher's GP_REFRESH)"),
]


def help_panel(height):
    lines = [T((f"{k:<11}", "title"), (v, "dim")) for k, v in HELP]
    return cell("HELP", lines, height)


def detail_panel(V, ui, width, height, oracle_meta):
    rows = V.orc_rows
    if not rows:
        return cell("DETAIL", [T(("no oracle row selected", "dim"))], height)
    r = rows[min(ui.sel, len(rows) - 1)]
    inner_w = max(10, width - 4)
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
            err = f"detail() failed: {type(e).__name__}: {e}"
        lines = [T((err, "danger"))]
    else:
        lines = [T(("scans.jsonl row -- the oracle's full detail() needs --oracle-json", "dim"))]
    for k in ("symbol", "yield_day", "oos_yield_day", "cont", "oos_cont", "inv_dd_pct",
              "lo", "hi", "k", "levels", "qualified", "boundary"):
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
    # single column wants a 3-line log panel; below that the log is one
    # footer line, like every other layout under 28 rows.
    log_panel_h = 5 if single and left >= 1 + 5 + 4 + 6 else 0
    foot_h = min(left, 2 if (mode.h == "tiny" and not log_panel_h) else 1)
    left -= foot_h
    bot_h = 0
    if not single and ui.view != "log":
        bot_h = {"full": 7, "short": 3, "tiny": 0}[mode.h]
        bot_h = bot_h if left - bot_h >= 8 else 0
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
        elif mode.w in ("ref", "narrow"):
            gw = max(46, int(width * 0.33))
            ow = width - gw
            mid.split_row(Layout(oracle_panel(V, mode, ow, mid_h, ui), size=ow),
                          Layout(grids_panel(V, mode, gw, mid_h), ratio=1))
        else:
            n = len(V.grids)
            want = 2 + max(1, n if single else 2 * n)
            gh = min(want, max(3, mid_h // 3))
            oh = mid_h - gh
            oracle = oracle_panel(V, mode, width, oh, ui, top_n=5 if single else None)
            mid.split_column(Layout(grids_panel(V, mode, width, gh, strips=single), size=gh),
                             Layout(oracle, size=oh)) if single else \
                mid.split_column(Layout(oracle, size=oh),
                                 Layout(grids_panel(V, mode, width, gh), size=gh))
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
        self.refresh = args.refresh
        self.ui = UI()
        self.pnl_hist = []
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self._sig = None
        self._last_pnl_id = None

    def poll_once(self):
        for f in (self.status, self.oracle, self.log):
            f.poll()
        with self.lock:
            pnl = self.status.snapshot.get("total_pnl")
            if self.status.age_s < STALE_S and pnl is not None and id(self.status.data) != self._last_pnl_id:
                self._last_pnl_id = id(self.status.data)      # one sample per status write
                self.pnl_hist = (self.pnl_hist + [pnl])[-240:]
            sig = (id(self.status.data), id(self.oracle.data), id(self.log.data),
                   len(self.pnl_hist), int(self.status.age_s))
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
                "grids": self.status.grids, "grids_age": self.status.age_s,
                "oracle": self.oracle.data,
                "oracle_scan_age": self.oracle.scan_age_s(),
                "refresh_s": self.refresh,
                "log": (self.log.data or {}).get("lines", []),
                "pnl_hist": list(self.pnl_hist),
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
        elif k == "p":
            ui.paused = not ui.paused
        elif k == "r":
            self.oracle.reload()
            threading.Thread(target=self.oracle.poll, daemon=True).start()
        return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="GRIDPICK · LATTICE console (read-only)")
    p.add_argument("--status", default=None, help="GridBot status.json (default: next to gridbot.py)")
    p.add_argument("--oracle-json", default=None, help="the scanner's scan.json (default: next to gridbot.py)")
    p.add_argument("--log", default=None, help="GridBot log (default: gridbot.log)")
    p.add_argument("--refresh", type=int, default=int(os.environ.get("GP_REFRESH", "120")),
                   help="oracle refresh seconds, for 'next scan' and staleness")
    p.add_argument("--exit-after", type=float, default=None,
                   help="quit on their own after N seconds (smoke tests)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Same scar as the oracle's main(): a redirected stdout on Windows
    # defaults to cp1252 and the box glyphs would raise. A real console
    # goes through WriteConsoleW and is unaffected either way.
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
                time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        keys.close()
    if args.exit_after:
        print(f"lattice: {frames} frames in {time.monotonic() - t_start:.1f}s, "
              f"{console.size.width}x{console.size.height}, status err={app.status.error}, "
              f"scan err={app.oracle.error}, log err={app.log.error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
