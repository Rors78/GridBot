"""Offline walk-forward of GridPick Oracle v3.1 selection + GridBot Grid execution.
Read-only: imports oracle/gridbot from D:\\GridBot (no network, no writes there)."""
import os, sys, json, math, glob, pickle
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, r"D:\GridBot")
import numpy as np, pandas as pd
import oracle, gridbot
from multiprocessing import Pool

DATA = r"D:\GridPick\data\bars_15m"
OUT = r"C:\Users\Miner\.claude\jobs\a9be3c7f\tmp"
PAIRS = ["AAVEUSD","ALGOUSD","AVAXUSD","BCHUSD","CRVUSD","DOTUSD","HBARUSD","PENGUUSD",
         "PEPEUSD","RENDERUSD","SHIBUSD","TRXUSD","WLDUSD","XLMUSD"]
ALLOC, MAKER, TAKER, SPREAD = 166.6667, 0.22, 0.38, 0.10
H = {"24h": 96, "72h": 288, "240h": 960}   # in 15m bars
START = pd.Timestamp("2025-01-01", tz="UTC").timestamp()
END = pd.Timestamp("2026-09-12", tz="UTC").timestamp()
STEP_S = 2 * 86400

def load(p):
    fs = [os.path.join(DATA, p, f) for f in ("archive.parquet", "bridge.parquet")]
    d = pd.concat([pd.read_parquet(f) for f in fs if os.path.exists(f)])
    d = d.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    return d

def to30(d):
    g = d.assign(b=(d.timestamp // 1800) * 1800).groupby("b")
    r = pd.DataFrame({"o": g.open.first(), "h": g.high.max(), "l": g.low.min(),
                      "c": g.close.last(), "v": g.volume.sum(), "n": g.trades.sum()}).reset_index()
    return r

def pts_of(o, h, l, c):
    return (o, l, h, c) if c >= o else (o, h, l, c)

def run_pair(p):
    d15 = load(p)
    d30 = to30(d15)
    t30 = d30.b.values
    t15 = d15.timestamp.values
    out = []
    t = START
    cfg = oracle.Cfg(fee=MAKER)
    while t <= END:
        i = np.searchsorted(t30, t)          # bars strictly before t
        if i < 480:
            t += STEP_S; continue
        w = d30.iloc[i - 480:i]
        if w.b.iloc[-1] < t - 3 * 3600 or w.b.iloc[0] < t - 480 * 1800 * 1.5:
            t += STEP_S; continue           # stale / gappy history
        rows = [(int(b), o, h, l, c, 0.0, v, int(n)) for b, o, h, l, c, v, n in
                zip(w.b, w.o, w.h, w.l, w.c, w.v, w.n)]
        px = rows[-1][4]
        turn = float(sum(r[6] * r[4] for r in rows[-48:]))
        tick = {"last": px, "turnover24": turn, "spread_pct": SPREAD, "vwap24": px}
        rec, why = oracle.evaluate(p, p, rows, tick, cfg)
        if not rec:
            out.append({"pair": p, "t": t, "reject": why}); t += STEP_S; continue
        levels = oracle.make_levels(rec["lo"], rec["hi"], rec["levels"], rec["kind"])
        # --- in-sample: gridbot Grid on the same lookback path vs scanner fills
        gi = gridbot.Grid(p, p, levels, rec["kind"], ALLOC, MAKER, TAKER)
        p0 = rows[0][4]
        is_fills = None
        if levels[0] < p0 < levels[-1]:
            try:
                gi.deploy(p0, rows[0][0]); k = 0
                for r in rows[1:]:
                    for q in pts_of(r[1], r[2], r[3], r[4]):
                        k += 1; gi.on_print(q, r[0] + k * 1e-3)
                is_fills = gi.fills
            except ValueError:
                pass
        # --- forward
        j = np.searchsorted(t15, t)
        fw = d15.iloc[j:j + H["240h"]]
        res = {"pair": p, "t": t, "reject": None, "qualified": bool(rec["qualified"]),
               "yield_day": rec["yield_day"], "yield_adj": rec["yield_adj"],
               "fills_day": rec["fills_day"], "rt_day": rec["rt_day"], "k": rec["k"],
               "levels": rec["levels"], "kind": rec["kind"], "span_pct": rec["span_pct"],
               "step_pct": rec["step_pct"], "containment": rec["containment"],
               "mae": rec["mae"], "vr": rec["vr"], "hurst": rec["hurst"],
               "atr_pct": rec["atr_pct"], "is_fills_scanner": rec["fills_day"] * rec["days"],
               "is_fills_grid": is_fills, "lo": rec["lo"], "hi": rec["hi"], "px": px,
               "turn": turn}
        if len(fw) < H["240h"] * 0.9 or not (levels[0] < px < levels[-1]):
            res["fw_ok"] = False; out.append(res); t += STEP_S; continue
        res["fw_ok"] = True
        ga = gridbot.Grid(p, p, levels, rec["kind"], ALLOC, MAKER, TAKER)   # exit rule
        gb = gridbot.Grid(p, p, levels, rec["kind"], ALLOC, MAKER, TAKER)   # hold
        ga.deploy(px, t); gb.deploy(px, t)
        lo, hi = levels[0], levels[-1]
        exited = None
        marks = {}
        k = 0
        for n, (ts, o, h, l, c) in enumerate(zip(fw.timestamp, fw.open, fw.high, fw.low, fw.close), 1):
            for q in pts_of(o, h, l, c):
                k += 1
                if exited is None: ga.on_print(q, t + k * 1e-3)
                gb.on_print(q, t + k * 1e-3)
            if exited is None and (c < lo or c > hi):
                side = "down" if c < lo else "up"
                s = ga.close(c, ts + 900, "pierce")
                worst = None
                exited = {"bar": n, "side": side, "pnl": s["realised"], "rt": s["round_trips"],
                          "fills": s["fills"], "hours": n * 0.25,
                          "held_rungs": None}
            for hn, hb in H.items():
                if n == hb:
                    m = gb.mark(c)
                    marks[hn] = {"hold_total": m["total"], "hold_realised": gb.realised,
                                 "hold_rt": gb.round_trips, "hold_fills": gb.fills,
                                 "outside": bool(c < lo or c > hi),
                                 "rule_total": (exited["pnl"] if exited else ga.mark(c)["total"]),
                                 "rule_rt": (exited["rt"] if exited else ga.round_trips),
                                 "rule_fills": (exited["fills"] if exited else ga.fills),
                                 "rule_realised_grid": ga.realised if not exited else None}
        res["exit"] = exited
        res["marks"] = marks
        res["min_low"] = float(fw.low.min()); res["max_high"] = float(fw.high.max())
        out.append(res)
        t += STEP_S
    return out

if __name__ == "__main__":
    with Pool(14) as pool:
        allr = pool.map(run_pair, PAIRS)
    flat = [r for rs in allr for r in rs]
    with open(os.path.join(OUT, "wf.pkl"), "wb") as f:
        pickle.dump(flat, f)
    print(len(flat), "evaluations")
