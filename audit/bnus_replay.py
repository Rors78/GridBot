#!/usr/bin/env python3
"""Binance.US replays of GridBot's grid, as pre-registered in
audit/PREREG_BINANCEUS_REPLAY_2026-09-27.md. Read that first: this file
implements it and nothing else, and every constant below is a registered one.

    python -X utf8 audit/bnus_replay.py fetch     # 1m klines -> cache (paced, resumable)
    python -X utf8 audit/bnus_replay.py screen    # SCREEN: trade-price replay
    python -X utf8 audit/bnus_replay.py report    # per-pair table from the last screen run

The engine is gridbot.Grid, unmodified: the harness only decides which price
Grid sees and when. It never computes a fill, a fee or a P&L. The scanner is
oracle.evaluate, unmodified: the venue enters as Cfg overrides and tick inputs.

The kline cache lives outside the repo (GRIDBOT_REPLAY_CACHE, default
~/.cache/gridbot_replay/binanceus): about 50 MB per pair.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import oracle   # noqa: E402  -- read-only scanner math
import gridbot  # noqa: E402  -- Grid, snap_levels, feasibility

API = "https://api.binance.us/api/v3"
CACHE = Path(os.environ.get("GRIDBOT_REPLAY_CACHE")
             or Path.home() / ".cache" / "gridbot_replay" / "binanceus")


def utc(s: str) -> int:
    """'YYYY-MM-DD' -> epoch seconds at 00:00 UTC."""
    return calendar.timegm(datetime.strptime(s, "%Y-%m-%d").timetuple())


# ------------------------------------------------ registered (prereg section 3)
PAIRS = {   # spread = median recorder spread_bps_median, 2026-09-27 09:38-16:12 UTC
    "SOLUSDT": {"spread_bps": 0.812, "tick": "0.01", "step": "0.001", "min_notional": "1"},
    "XRPUSDT": {"spread_bps": 0.655, "tick": "0.0001", "step": "0.1", "min_notional": "1"},
    "ETHUSDT": {"spread_bps": 0.222, "tick": "0.01", "step": "0.0001", "min_notional": "1"},
    "DOGEUSDT": {"spread_bps": 5.065, "tick": "0.00001", "step": "1", "min_notional": "1"},
    "LTCUSDT": {"spread_bps": 4.195, "tick": "0.01", "step": "0.001", "min_notional": "1"},
    "LINKUSDT": {"spread_bps": 6.377, "tick": "0.001", "step": "0.01", "min_notional": "1"},
    "AVAXUSDT": {"spread_bps": 6.821, "tick": "0.001", "step": "0.01", "min_notional": "1"},
    "SUIUSDT": {"spread_bps": 7.987, "tick": "0.0001", "step": "0.1", "min_notional": "1"},
    "ADAUSDT": {"spread_bps": 6.072, "tick": "0.00001", "step": "0.1", "min_notional": "1"},
}
ALLOC = 500.0 / 3.0
MAKER_PCT, TAKER_PCT = 0.0, 0.02
EXIT_AFTER_S = 300.0
HORIZON_S = 240 * 3600
STEP_S = 2 * 86400
LOOKBACK_BARS, BAR_S = 480, 1800
MIN_S = 60
MIN_TURNOVER = 10_000.0
PRINT_OFFSETS = (0.0, 20.0, 40.0, 59.0)          # 4 prints per traded minute
SCREEN_DATA_START = utc("2024-12-22")
SCREEN_DATA_END = utc("2026-09-27")               # exclusive
SCREEN_FIRST_T = utc("2025-01-01")
DOWNSIDE_TAIL_BAR = -10.0                         # % of the grid's capital (ruling 4)
FLOORS = {"SCREEN": (200, 20), "DECIDER": (60, 5)}  # (clean grids, clean downside exits)


def scanner_cfg() -> "oracle.Cfg":
    """The only scanner settings that differ from oracle's defaults."""
    return oracle.Cfg(fee=MAKER_PCT, min_turnover=MIN_TURNOVER)


def pair_meta(sym: str) -> dict:
    """The pair's Binance.US rules in the shape the bot's own deploy checks read."""
    p = PAIRS[sym]
    return {"tick_size": p["tick"], "ordermin": p["step"], "costmin": p["min_notional"]}


def half_spread(sym: str) -> float:
    return PAIRS[sym]["spread_bps"] / 10_000.0 / 2.0


def screen_times() -> list[int]:
    last = SCREEN_DATA_END - HORIZON_S
    return list(range(SCREEN_FIRST_T, last + 1, STEP_S))


# ------------------------------------------------------------------ fetch --
class Banned(RuntimeError):
    """HTTP 418: an escalating IP ban that would refuse every bot on this IP."""


def _get(session, path: str, params: dict, log=print):
    """One public GET, paced by the caller. 429 honours Retry-After (never
    under 60 s); 418 raises Banned at once; the IP's used weight is watched
    and the fetch backs off above 40% of the 6000/min budget, because the
    budget is shared with every other process on this IP."""
    while True:
        r = session.get(f"{API}/{path}", params=params, timeout=(5, 20))
        used = int(r.headers.get("X-MBX-USED-WEIGHT-1m") or 0)
        if r.status_code == 418:
            raise Banned(f"HTTP 418 on {path}: IP ban -- stopping now, no retry")
        if r.status_code == 429:
            wait = max(60.0, float(r.headers.get("Retry-After") or 60))
            log(f"HTTP 429, sleeping {wait:.0f}s (Retry-After honoured)")
            time.sleep(wait)
            continue
        r.raise_for_status()
        if used > 2400:
            log(f"IP weight {used}/6000 -- pausing 30s to leave the budget to others")
            time.sleep(30)
        return r.json(), used


def _months(start: int, end: int):
    d = datetime.fromtimestamp(start, timezone.utc).replace(day=1, hour=0, minute=0, second=0)
    while True:
        m0 = calendar.timegm(d.timetuple())
        nxt = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
        m1 = calendar.timegm(nxt.timetuple())
        if m0 >= end:
            return
        yield d.strftime("%Y-%m"), max(m0, start), min(m1, end)
        d = nxt


KLINE_FIELDS = ("open_ms", "o", "h", "l", "c", "vb", "vq", "n")


def fetch_pair(sym: str, start: int = SCREEN_DATA_START, end: int = SCREEN_DATA_END,
               pace_s: float = 0.3, session=None, log=print) -> dict:
    """1m klines for [start, end) into CACHE/klines_1m/<sym>/<YYYY-MM>.npz.
    A month file is written only once complete (atomically), so a stopped
    fetch resumes at the first missing month. Minutes the exchange never
    returned stay absent: absent is unmeasured, never filled."""
    import requests
    session = session or requests.Session()
    session.headers["User-Agent"] = "gridbot-replay"
    out = CACHE / "klines_1m" / sym
    out.mkdir(parents=True, exist_ok=True)
    stats = {"calls": 0, "rows": 0, "months": 0, "max_ip_weight": 0}
    for label, m0, m1 in _months(start, end):
        f = out / f"{label}.npz"
        if f.exists():
            continue
        cols = {k: [] for k in KLINE_FIELDS}
        cursor = m0 * 1000
        while cursor < m1 * 1000:
            page, used = _get(session, "klines", {"symbol": sym, "interval": "1m", "startTime": cursor,
                                                  "endTime": m1 * 1000 - 1, "limit": 1000}, log)
            stats["calls"] += 1
            stats["max_ip_weight"] = max(stats["max_ip_weight"], used)
            if not page:
                break
            for k in page:
                cols["open_ms"].append(int(k[0]))
                for i, name in ((1, "o"), (2, "h"), (3, "l"), (4, "c"), (5, "vb"), (7, "vq")):
                    cols[name].append(float(k[i]))
                cols["n"].append(int(k[8]))
            cursor = int(page[-1][0]) + MIN_S * 1000
            time.sleep(pace_s)
        tmp = f.with_name(f.name + ".tmp.npz")
        np.savez(tmp, **{k: np.asarray(v, dtype=np.int64 if k in ("open_ms", "n") else np.float64)
                         for k, v in cols.items()})
        os.replace(tmp, f)
        stats["rows"] += len(cols["open_ms"])
        stats["months"] += 1
        log(f"{sym} {label}: {len(cols['open_ms'])} minutes, IP weight {stats['max_ip_weight']}")
    return stats


def load_klines(sym: str) -> dict:
    files = sorted((CACHE / "klines_1m" / sym).glob("*.npz"))
    if not files:
        raise FileNotFoundError(f"no cached klines for {sym}: run `fetch` first")
    parts = [np.load(f) for f in files]
    k = {name: np.concatenate([p[name] for p in parts]) for name in KLINE_FIELDS}
    order = np.argsort(k["open_ms"], kind="stable")
    k = {name: v[order] for name, v in k.items()}
    keep = np.concatenate([[True], np.diff(k["open_ms"]) > 0])      # drop duplicates
    return {name: v[keep] for name, v in k.items()}


# ---------------------------------------------------------------- scanner --
def lookback_rows(k: dict, i_t: int, t: int) -> list | None:
    """The 480 closed 30m bars before t, aggregated from 1m klines as Binance
    aggregates. None if any of the 14,400 minutes is absent."""
    need = LOOKBACK_BARS * BAR_S // MIN_S
    i0 = i_t - need
    if i0 < 0 or int(k["open_ms"][i0]) != (t - LOOKBACK_BARS * BAR_S) * 1000 \
            or int(k["open_ms"][i_t - 1]) != (t - MIN_S) * 1000:
        return None
    per = BAR_S // MIN_S
    sl = slice(i0, i_t)
    o = k["o"][sl].reshape(LOOKBACK_BARS, per)
    h = k["h"][sl].reshape(LOOKBACK_BARS, per)
    lo = k["l"][sl].reshape(LOOKBACK_BARS, per)
    c = k["c"][sl].reshape(LOOKBACK_BARS, per)
    vb = k["vb"][sl].reshape(LOOKBACK_BARS, per)
    n = k["n"][sl].reshape(LOOKBACK_BARS, per)
    ts0 = t - LOOKBACK_BARS * BAR_S
    return [(ts0 + b * BAR_S, float(o[b, 0]), float(h[b].max()), float(lo[b].min()), float(c[b, -1]),
             0.0, float(vb[b].sum()), int(n[b].sum())) for b in range(LOOKBACK_BARS)]


def evaluate_at(sym: str, k: dict, t: int) -> dict:
    """One registered evaluation. Returns {'skip'|'reject'|'refused': why} or a
    deployable {'rec', 'levels', 'i_t'}."""
    i_t = int(np.searchsorted(k["open_ms"], t * 1000))
    rows = lookback_rows(k, i_t, t)
    if rows is None:
        return {"skip": "lookback incomplete"}
    last = rows[-1][4]
    turnover = float(k["vq"][i_t - 1440:i_t].sum())
    tick = {"last": last, "turnover24": turnover,
            "spread_pct": PAIRS[sym]["spread_bps"] / 100.0, "vwap24": last}
    rec, why = oracle.evaluate(sym, sym, rows, tick, scanner_cfg())
    if not rec:
        return {"reject": why}
    levels = oracle.make_levels(rec["lo"], rec["hi"], rec["levels"], rec["kind"])
    try:
        levels = gridbot.snap_levels(levels, pair_meta(sym))
    except ValueError as e:
        return {"refused": str(e), "rec": rec}
    why = gridbot.feasibility(levels, ALLOC, pair_meta(sym))
    if why:
        return {"refused": why, "rec": rec}
    return {"rec": rec, "levels": levels, "i_t": i_t, "turnover": turnover}


# ----------------------------------------------------------------- SCREEN --
def minute_prints(o: float, h: float, l: float, c: float, n: int, t_open: float) -> list:
    """A traded minute's 4 prints in the 2026-09-23 order; none if no trade."""
    if n <= 0:
        return []
    pts = (o, l, h, c) if c >= o else (o, h, l, c)
    return [(t_open + off, p) for off, p in zip(PRINT_OFFSETS, pts)]


def screen_forward(sym: str, k: dict, i_t: int, t: int, levels: list, kind: str,
                   meta: dict | None = None) -> dict:
    """Deploy at the last print before t plus the half-spread, drive every
    later print through Grid.on_print, exit by GridBot's 300 s rule, mark at
    240 h. Returns the grid's result."""
    h = half_spread(sym)
    j = i_t - 1
    while j >= 0 and k["n"][j] <= 0:
        j -= 1
    if j < 0:
        return {"skip": "no print before t"}
    px = float(k["c"][j]) * (1.0 + h)
    if not (levels[0] < px < levels[-1]):
        return {"skip": "deploy price outside the band"}
    g = gridbot.Grid(sym, sym, levels, kind, ALLOC, MAKER_PCT, TAKER_PCT, meta)
    g.deploy(px, float(t))
    end = t + HORIZON_S
    i_end = int(np.searchsorted(k["open_ms"], end * 1000))
    expected = (end - t) // MIN_S
    unmeasured = expected - (i_end - i_t)
    exit_at = None

    def do_exit():
        side = "down" if g.last_px < g.lo else "up"
        when = g.outside_since + EXIT_AFTER_S
        g.close(g.last_px * (1.0 - h), when, f"price {side} band for 300s")
        return {"side": side, "at": when}

    for i in range(i_t, i_end):
        t_open = int(k["open_ms"][i]) // 1000
        for ts, p in minute_prints(float(k["o"][i]), float(k["h"][i]), float(k["l"][i]),
                                   float(k["c"][i]), int(k["n"][i]), t_open):
            if g.outside_since is not None and ts >= g.outside_since + EXIT_AFTER_S:
                exit_at = do_exit()
                break
            g.on_print(p, ts)
        if exit_at:
            break
        if g.outside_since is not None and t_open + MIN_S >= g.outside_since + EXIT_AFTER_S:
            exit_at = do_exit()
            break
    if exit_at:
        total = g.realised
    else:
        total = g.mark(g.last_px * (1.0 - h))["total"]
    return {"net_pct": total / ALLOC * 100.0, "exit": exit_at["side"] if exit_at else None,
            "exit_h": ((exit_at["at"] - t) / 3600.0) if exit_at else None,
            "fills": g.fills, "round_trips": g.round_trips, "unmeasured_min": int(unmeasured),
            "clean": unmeasured == 0, "deploy_px": px}


def screen_pair(sym: str) -> list[dict]:
    k = load_klines(sym)
    out = []
    for t in screen_times():
        ev = evaluate_at(sym, k, t)
        row = {"pair": sym, "t": t, "date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")}
        if "rec" in ev:
            r = ev["rec"]
            row.update({"qualified": bool(r["qualified"]), "levels": r["levels"], "kind": r["kind"],
                        "step_pct": r["step_pct"], "span_pct": r["span_pct"], "yield_day": r["yield_day"]})
        if "levels" not in ev:
            row["outcome"] = ev.get("skip") and "skip" or ev.get("reject") and "reject" or "refused"
            row["why"] = ev.get("skip") or ev.get("reject") or ev.get("refused")
            out.append(row)
            continue
        res = screen_forward(sym, k, ev["i_t"], t, ev["levels"], ev["rec"]["kind"])
        if "skip" in res:
            row.update({"outcome": "skip", "why": res["skip"]})
        else:
            row.update({"outcome": "deployed", **res})
        out.append(row)
    return out


# ---------------------------------------------------------------- metrics --
def nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)]


def summarize(rows: list[dict], role: str) -> dict:
    """The registered statistics over CLEAN deployed grids, with the pass-rule
    conditions checked. For the SCREEN this is a number check, not a verdict."""
    dep = [r for r in rows if r.get("outcome") == "deployed"]
    clean = [r for r in dep if r.get("clean")]
    nets = [r["net_pct"] for r in clean]
    down = [r["net_pct"] for r in clean if r.get("exit") == "down"]
    floor_grids, floor_down = FLOORS[role]
    mean = statistics.fmean(nets) if nets else None
    median = statistics.median(nets) if nets else None
    tail = nearest_rank(down, 0.10)
    cond = {"mean>0": mean is not None and mean > 0,
            "median>0": median is not None and median > 0,
            f"worst tenth of downside exits >= {DOWNSIDE_TAIL_BAR:g}%": tail is not None and tail >= DOWNSIDE_TAIL_BAR,
            f"sample >= {floor_grids} clean grids, >= {floor_down} downside exits":
                len(clean) >= floor_grids and len(down) >= floor_down}
    return {"evaluations": len(rows), "deployed": len(dep), "clean": len(clean),
            "unclean": len(dep) - len(clean), "mean": mean, "median": median,
            "tail_p10_down": tail, "down": len(down),
            "up": sum(1 for r in clean if r.get("exit") == "up"),
            "held": sum(1 for r in clean if r.get("exit") is None),
            "conditions": cond, "all_met": all(cond.values())}


# ------------------------------------------------------------------- CLI --
def _run_screen(workers: int) -> Path:
    from multiprocessing import Pool
    with Pool(min(workers, len(PAIRS))) as pool:
        per = pool.map(screen_pair, list(PAIRS))
    rows = [r for rs in per for r in rs]
    out = CACHE / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"screen_{stamp}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=("fetch", "screen", "report"))
    p.add_argument("--pairs", default=",".join(PAIRS))
    p.add_argument("--workers", type=int, default=9)
    p.add_argument("--pace", type=float, default=0.3)
    p.add_argument("--results", default=None, help="a screen_*.jsonl (default: newest)")
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        import requests
        s = requests.Session()
        for sym in a.pairs.split(","):
            st = fetch_pair(sym, pace_s=a.pace, session=s)
            print(f"{sym}: {st}", flush=True)
        return 0
    if a.cmd == "screen":
        path = _run_screen(a.workers)
        print(f"wrote {path}")
        return 0
    path = Path(a.results) if a.results else max((CACHE / "results").glob("screen_*.jsonl"))
    rows = [json.loads(x) for x in open(path, encoding="utf-8")]
    print(json.dumps({"file": str(path), "pooled": summarize(rows, "SCREEN")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
