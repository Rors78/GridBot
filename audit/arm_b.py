#!/usr/bin/env python3
"""ARM-B: GridBot's grid with the deploy pre-buy removed, replayed on the
DECIDER's data, as pre-registered in audit/PREREG_ARM_B_2026-10-22.md. Read
that first: this file implements it and nothing else.

    python -X utf8 audit/arm_b.py run --cut 2026-10-28        # the registered measurement
    python -X utf8 audit/arm_b.py smoke --smoke-t 2026-09-27T12:00 --smoke-hours 3
                                                              # harness smoke: NOT a measurement

One change against the DECIDER: deploy() buys nothing. Everything else is
imported from bnus_replay.py and shared with the DECIDER: the scanner call,
the ladder, the fill driver (apply_book_minute through gridbot.Grid.on_print,
inherited unchanged), the gap rule, the band exit and the 240 h mark. The
harness never computes a fill, a fee or a P&L: ArmBGrid books them through
the inherited Grid methods, and only adds the harvest / bleed split.

ArmBGrid is replay-only. Its working ladder is truncated at deploy, so it is
not meant for state.json or the bot.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import gridbot            # noqa: E402  -- Grid, unmodified (checked below)
import bnus_replay as R   # noqa: E402  -- the DECIDER's registered harness

# git blob ids at b29f31d (the registered code): `git rev-parse b29f31d:<path>`
REGISTERED = {"gridbot.py": "4b469c8483671332cdf7e2f6bbc8c04e5acc8d3e",
              "audit/bnus_replay.py": "5cc06e53fe6fc830ba318dac9d99eda9d617273f"}
ARM_B_DEADLINE_CUT = R.utc("2026-11-27")        # the DECIDER's deadline (prereg 7)
SAMPLE_FLOOR = R.FLOORS["DECIDER"][0]           # 60 clean grids: the statement bar (prereg 5.3)
Q_SET = (5, 10, 15, 30, 60, 120, 240, 480)      # candidate rung scales, minutes (prereg 6.2)
MIN_RETURNS_PER_Q = 10
OUT = R.CACHE / "arm_b"


def blob_id(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def engine_problem() -> str | None:
    """None if the engine and the shared harness are the registered blobs."""
    for rel, want in REGISTERED.items():
        got = blob_id(ROOT / rel)
        if got != want:
            return f"{rel} is {got}, registered {want}: refusing (prereg 3.1)"
    return None


# ---------------------------------------------------------------- engine --
class ArmBGrid(gridbot.Grid):
    """gridbot.Grid whose deploy() buys nothing (prereg 3).

    The scanner's full band is kept for lo / hi. The working ladder is the
    levels 0..e: BUYs rest below the empty level e, nothing rests at or above
    it, and a SELL appears only where the inherited _fill_buy puts it, one
    step above a filled lot. on_print, _fill_buy, _fill_sell, close and mark
    are the parent's."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.band_levels = list(self.levels)       # the scanner's ladder, lo..hi
        self.realised_ladder = 0.0                 # harvest: completed buy -> sell one step up
        self.ladder_levels: int | None = None      # e + 1 once deployed

    @property
    def hi(self) -> float:
        return self.band_levels[-1]

    def deploy(self, price: float, ts: float) -> dict:
        """Rest a BUY at every level below the one nearest `price`. Buy nothing."""
        if self.status != "NEW":
            raise RuntimeError(f"{self.symbol}: deploy on a {self.status} grid")
        if not (self.lo < price < self.hi):
            raise ValueError(f"{self.symbol}: price {price} outside band {self.lo}-{self.hi}")
        full = self.band_levels
        e = min(range(len(full)), key=lambda j: abs(full[j] - price))   # as Grid.deploy picks it
        if e == 0:
            raise ValueError(f"{self.symbol}: no buy rung below the deploy price")
        self.levels = full[:e + 1]
        self.n = e + 1
        self.ladder_levels = self.n
        self.orders = [{"side": gridbot.BUY, "qty": self.rung / self.levels[j]} for j in range(e)] + [None]
        self.empty = e
        self.status = "ACTIVE"
        self.deployed_at = ts
        self.deploy_px = price
        self.last_px = price
        self.last_ts = ts
        return {"empty_level": e, "buys": e, "sells": 0, "coins_bought": 0.0, "cost": 0.0}

    def _fill_sell(self, j: int, printed: float, ts: float) -> dict:
        fill = super()._fill_sell(j, printed, ts)
        self.realised_ladder += fill["pnl"]        # every sell here is a ladder sell
        return fill

    def split(self, price: float | None = None) -> dict:
        """total = harvest + bleed (prereg 3.7). Closed: realised. Open: the mark."""
        total = self.realised if self.status == "CLOSED" else self.mark(price)["total"]
        return {"total": total, "harvest": self.realised_ladder, "bleed": total - self.realised_ladder}

    def summary(self) -> dict:
        s = super().summary()
        s.update({"levels": len(self.band_levels), "hi": self.hi, "ladder_levels": self.ladder_levels,
                  "realised_ladder": self.realised_ladder,
                  "realised_inventory": self.realised - self.realised_ladder})
        return s


# -------------------------------------------------------------- forward --
def vr_rung_scale(mids: list[float], step_pct: float | None) -> dict:
    """Variance ratio at rung scale over one clean window (prereg 6)."""
    if len(mids) < 2 or not step_pct:
        return {"vr_q": None, "vr": None, "vr60": None, "vr_note": "window too short"}
    r = np.diff(np.log(np.asarray(mids, dtype=np.float64)))
    n = len(r)
    v1 = float(np.var(r, ddof=1)) if n > 1 else 0.0
    if n < MIN_RETURNS_PER_Q * Q_SET[0] or v1 <= 0.0:
        return {"vr_q": None, "vr": None, "vr60": None, "vr_note": "window too short or flat"}

    def vr_at(q: int) -> float | None:
        if n < MIN_RETURNS_PER_Q * q:
            return None
        qs = np.convolve(r, np.ones(q), mode="valid")
        return float(np.var(qs, ddof=1)) / (q * v1)

    step = step_pct / 100.0
    q_star, note = None, "rms >= step"
    for q in Q_SET:
        if n < MIN_RETURNS_PER_Q * q:
            break
        qs = np.convolve(r, np.ones(q), mode="valid")
        if math.sqrt(float(np.mean(qs * qs))) >= step:
            q_star = q
            break
    if q_star is None:
        q_star = max(q for q in Q_SET if n >= MIN_RETURNS_PER_Q * q)
        note = "rms never reached step: coarsest measurable q"
    return {"vr_q": q_star, "vr": vr_at(q_star), "vr60": vr_at(60), "vr_note": note}


def book_forward(sym: str, book: dict, t: int, levels: list, kind: str,
                 horizon_s: int = R.HORIZON_S, meta: dict | None = None,
                 step_pct: float | None = None) -> dict:
    """The DECIDER's book_forward with ArmBGrid in place of Grid (prereg 3):
    same deploy price, same fill driver, same exit, same mark, same gap rule."""
    dep = R._measured(book, t - R.MIN_S)
    if dep is None:
        return {"skip": "no recorded book at t"}
    px = dep[1]
    if not (levels[0] < px < levels[-1]):
        return {"skip": "deploy price outside the band"}
    g = ArmBGrid(sym, sym, levels, kind, R.ALLOC, R.MAKER_PCT, R.TAKER_PCT, meta)
    try:
        g.deploy(px, float(t))
    except ValueError:
        return {"skip": "no buy rung below the deploy price"}
    unmeasured, outside_first, exit_at, last_bid = 0, None, None, dep[0]
    mids = [dep[2]]
    for m in range(t, t + horizon_s, R.MIN_S):
        q = R._measured(book, m)
        if q is None:
            unmeasured += 1
            outside_first = None                 # the 300 s must be RECORDED time
            continue
        bid, ask, mid = q
        ts = float(m + R.MIN_S)                  # the values are the minute's close
        last_bid = bid
        mids.append(mid)
        R.apply_book_minute(g, bid, ask, ts)
        if mid < g.lo or mid > g.hi:
            if outside_first is None:
                outside_first = ts
            elif ts - outside_first >= R.EXIT_AFTER_S:
                side = "down" if mid < g.lo else "up"
                g.close(bid, ts, f"price {side} band for 300s (book)")
                exit_at = {"side": side, "at": ts}
                break
        else:
            outside_first = None
    sp = g.split(last_bid)
    pct = lambda v: v / R.ALLOC * 100.0
    out = {"net_pct": pct(sp["total"]), "harvest_pct": pct(sp["harvest"]), "bleed_pct": pct(sp["bleed"]),
           "exit": exit_at["side"] if exit_at else None,
           "exit_h": ((exit_at["at"] - t) / 3600.0) if exit_at else None,
           "fills": g.fills, "round_trips": g.round_trips, "unmeasured_min": unmeasured,
           "clean": unmeasured == 0, "deploy_px": px, "ladder_levels": g.ladder_levels}
    if out["clean"]:
        out.update(vr_rung_scale(mids, step_pct))
    return out


def arm_b_times(cut: int) -> list[int]:
    return R.decider_times(cut)


def arm_b_pair(sym: str, cut: int, times=None, horizon_s: int = R.HORIZON_S,
               klines: dict | None = None, book_root=None) -> list[dict]:
    """ARM-B rows for one pair: the DECIDER's evaluations, ARM-B's forward."""
    times = arm_b_times(cut) if times is None else times
    if not times:
        return []
    if klines is None:
        klines = R.fetch_range(sym, times[0] - R.LOOKBACK_BARS * R.BAR_S, times[-1])
    book = R.load_book(sym, cut, book_root)
    out = []
    for t in times:
        row = {"pair": sym, "t": t, "date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")}
        ev = R.evaluate_at(sym, klines, t)
        if "rec" in ev:
            r = ev["rec"]
            row.update({"qualified": bool(r["qualified"]), "levels": r["levels"], "kind": r["kind"],
                        "step_pct": r["step_pct"], "span_pct": r["span_pct"], "yield_day": r["yield_day"]})
        if "levels" not in ev:
            row["outcome"] = ev.get("skip") and "skip" or ev.get("reject") and "reject" or "refused"
            row["why"] = ev.get("skip") or ev.get("reject") or ev.get("refused")
            out.append(row)
            continue
        res = book_forward(sym, book, t, ev["levels"], ev["rec"]["kind"], horizon_s,
                           step_pct=row.get("step_pct"))
        row.update({"outcome": "skip", "why": res["skip"]} if "skip" in res else {"outcome": "deployed", **res})
        out.append(row)
    return out


# ---------------------------------------------------------------- summary --
def summarize(rows: list[dict]) -> dict:
    """The registered statistics over CLEAN deployed grids, the statement
    rule (prereg 5.3) and the VR cross-check (prereg 6.5)."""
    dep = [r for r in rows if r.get("outcome") == "deployed"]
    clean = [r for r in dep if r.get("clean")]
    nets = [r["net_pct"] for r in clean]
    down = [r["net_pct"] for r in clean if r.get("exit") == "down"]
    mean = statistics.fmean(nets) if nets else None
    median = statistics.median(nets) if nets else None
    vrs = [r["vr"] for r in clean if r.get("vr") is not None]
    vr_median = statistics.median(vrs) if vrs else None
    grid_days = sum((r["exit_h"] if r.get("exit_h") is not None else R.HORIZON_S / 3600.0) / 24.0
                    for r in clean)
    s = {"evaluations": len(rows), "deployed": len(dep), "clean": len(clean),
         "unclean": len(dep) - len(clean),
         "skipped_no_buy_rung": sum(1 for r in rows if r.get("why") == "no buy rung below the deploy price"),
         "mean": mean, "median": median, "tail_p10_down": R.nearest_rank(down, 0.10),
         "down": len(down), "up": sum(1 for r in clean if r.get("exit") == "up"),
         "held": sum(1 for r in clean if r.get("exit") is None),
         "harvest_mean": statistics.fmean([r["harvest_pct"] for r in clean]) if clean else None,
         "bleed_mean": statistics.fmean([r["bleed_pct"] for r in clean]) if clean else None,
         "fills_per_grid_day": (sum(r["fills"] for r in clean) / grid_days) if grid_days else None,
         "round_trips_per_grid": statistics.fmean([r["round_trips"] for r in clean]) if clean else None,
         "ladder_levels": sorted(statistics.Counter(r["ladder_levels"] for r in clean).items()) if clean else [],
         "vr_measured": len(vrs), "vr_median": vr_median,
         "vr_share_below_1": (sum(1 for v in vrs if v < 1.0) / len(vrs)) if vrs else None}
    if len(clean) < SAMPLE_FLOOR:
        s["statement"] = f"UNDERPOWERED: {len(clean)} clean grids, bar {SAMPLE_FLOOR}; no statement"
    elif mean > 0 and median > 0:
        s["statement"] = (f"MEASURED: mean {mean:+.2f}% and median {median:+.2f}% > 0 over {len(clean)} "
                          f"clean grids: harvestable mean reversion at rung scale was measured on this sample")
    else:
        s["statement"] = (f"MEASURED: none (mean {mean:+.2f}%, median {median:+.2f}% over {len(clean)} "
                          f"clean grids; the registered statement needs both > 0)")
    if vr_median is None or mean is None:
        s["vr_cross_check"] = "unmeasured"
    else:
        held = (vr_median < 1.0) == (mean > 0)
        s["vr_cross_check"] = (f"median VR(q*) {vr_median:.3f} ({s['vr_share_below_1']:.0%} below 1) vs "
                               f"mean {mean:+.2f}%: prediction {'held' if held else 'FAILED'}")
    return s


def _fmt(v, spec="+.2f"):
    return "-" if v is None else format(v, spec)


def report_markdown(rows: list[dict], label: str) -> str:
    lines = [f"## {label}", "",
             "| pair | evals | deployed | clean | mean % | median % | worst-tenth down % | harvest % | bleed % | "
             "down / up / held | fills per grid-day | RT per grid | median VR(q*) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def line(name, rs):
        s = summarize(rs)
        lines.append(f"| {name} | {s['evaluations']} | {s['deployed']} | {s['clean']} | {_fmt(s['mean'])} | "
                     f"{_fmt(s['median'])} | {_fmt(s['tail_p10_down'])} | {_fmt(s['harvest_mean'])} | "
                     f"{_fmt(s['bleed_mean'])} | {s['down']} / {s['up']} / {s['held']} | "
                     f"{_fmt(s['fills_per_grid_day'], '.2f')} | {_fmt(s['round_trips_per_grid'], '.2f')} | "
                     f"{_fmt(s['vr_median'], '.3f')} |")

    for p in [p for p in R.PAIRS if any(r["pair"] == p for r in rows)]:
        line(p, [r for r in rows if r["pair"] == p])
    line("**pooled**", rows)
    s = summarize(rows)
    lines += ["", f"- {s['statement']}", f"- VR cross-check: {s['vr_cross_check']}",
              f"- qualified-only subset: n = {sum(1 for r in rows if r.get('qualified') and r.get('clean'))} clean",
              f"- skipped, no buy rung below the deploy price: {s['skipped_no_buy_rung']}",
              f"- ladder sizes (e+1 levels: count): {s['ladder_levels']}"]
    return "\n".join(lines)


# -------------------------------------------------------------------- CLI --
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("cmd", choices=("run", "smoke"))
    p.add_argument("--pairs", default=",".join(R.PAIRS))
    p.add_argument("--cut", default=None, help="run: data cut 'YYYY-MM-DD' (00:00 UTC, exclusive)")
    p.add_argument("--smoke-t", default=None, help="smoke: comma-separated 'YYYY-MM-DDTHH:MM' UTC times")
    p.add_argument("--smoke-hours", type=float, default=3.0, help="smoke: horizon in hours (NOT registered)")
    a = p.parse_args(argv)
    why = engine_problem()
    if why:
        print(why)
        return 2
    pairs = a.pairs.split(",")
    if a.cmd == "run":
        if not a.cut:
            print("run needs --cut (the DECIDER's verdict cut)")
            return 2
        cut = R.utc(a.cut)
        if cut < R.DECIDER_EARLIEST_CUT:
            print("DEFERRED: cut is before 2026-10-28 00:00 UTC (registered earliest); nothing replayed")
            return 0
        if cut > ARM_B_DEADLINE_CUT:
            print("REFUSED: cut is after 2026-11-27 (the DECIDER's deadline); nothing replayed")
            return 2
        rows = [r for sym in pairs for r in arm_b_pair(sym, cut)]
        label, horizon = "ARM-B MEASUREMENT (not a verdict)", R.HORIZON_S
    else:
        times = [calendar.timegm(datetime.strptime(x, "%Y-%m-%dT%H:%M").timetuple())
                 for x in (a.smoke_t or "").split(",") if x]
        if not times or max(times) >= R.DECIDER_FIRST_T:
            print("smoke times must be given and lie before 2026-09-28 (outside every registered window)")
            return 2
        horizon = int(a.smoke_hours * 3600)
        cut = max(times) + horizon
        rows = [r for sym in pairs for r in arm_b_pair(sym, cut, times=times, horizon_s=horizon)]
        label = "SMOKE -- not a measurement, not the registered horizon, no statement"
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT / f"{a.cmd}_{stamp}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    summary = summarize(rows)
    if a.cmd == "smoke":
        summary["statement"] = "SMOKE: no statement"
        summary["vr_cross_check"] = "SMOKE: not evaluated"
    print(report_markdown(rows, label))
    print()
    print(json.dumps({"label": label, "file": str(path), "cut": cut, "horizon_h": horizon / 3600,
                      "summary": summary}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
