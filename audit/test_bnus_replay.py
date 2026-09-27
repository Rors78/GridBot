#!/usr/bin/env python3
"""Tests for audit/bnus_replay.py. Synthetic data only: no network, temp dirs.

    python -X utf8 audit/test_bnus_replay.py            # the tests
    python -X utf8 audit/test_bnus_replay.py --mutants  # negative controls

--mutants re-runs the tests against deliberately broken harnesses. Each mutant
must turn at least one NAMED test red; a mutant nothing catches is a failure.
"""
from __future__ import annotations

import math
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import logging       # noqa: E402

import oracle        # noqa: E402
import gridbot       # noqa: E402
import bnus_replay as R  # noqa: E402

logging.getLogger("gridbot").addHandler(logging.NullHandler())   # the agreement test's Bot talks

T0 = R.utc("2026-01-10")
LEVELS = [100.0 + i for i in range(11)]            # 100 .. 110
TEST = "TESTUSDT"


def make_k(minutes):
    """minutes: [(t_open, o, h, l, c, n)] -> the loader's array dict."""
    k = {name: [] for name in R.KLINE_FIELDS}
    for t_open, o, h, l, c, n in minutes:
        k["open_ms"].append(int(t_open) * 1000)
        for name, v in (("o", o), ("h", h), ("l", l), ("c", c)):
            k[name].append(float(v))
        k["vb"].append(1.0)
        k["vq"].append(1000.0)
        k["n"].append(int(n))
    return {name: np.asarray(v, dtype=np.int64 if name in ("open_ms", "n") else np.float64)
            for name, v in k.items()}


def flat(t_from, count, px, n=1):
    return [(t_from + i * 60, px, px, px, px, n) for i in range(count)]


def with_test_pair(spread_bps=0.0):
    R.PAIRS[TEST] = {"spread_bps": spread_bps, "tick": "0.01", "step": "0.001", "min_notional": "1"}


def forward(minutes, spread_bps=0.0, t=T0, deploy_px=104.6):
    """Deploy at `deploy_px` (the print of the minute before t), then run the SCREEN."""
    with_test_pair(spread_bps)
    k = make_k([(t - 60, deploy_px, deploy_px, deploy_px, deploy_px, 1)] + minutes)
    return R.screen_forward(TEST, k, 1, t, LEVELS, "arith")


# ---------------------------------------------------------------------------- tests
def t_registered_constants_match_prereg():
    doc = (HERE / "PREREG_BINANCEUS_REPLAY_2026-09-27.md").read_text(encoding="utf-8")
    rows = dict((m[0], m[1:]) for m in re.findall(
        r"^\| (\w+USDT) \| ([\d.]+) \| ([\d.]+) \| ([\d.]+) \| \$(\d+) \|$", doc, re.M))
    ok = set(rows) == set(k for k in R.PAIRS if k != TEST) and all(
        float(rows[s][0]) == R.PAIRS[s]["spread_bps"] and rows[s][1] == R.PAIRS[s]["tick"]
        and rows[s][2] == R.PAIRS[s]["step"] and rows[s][3] == R.PAIRS[s]["min_notional"] for s in rows)
    ok = ok and (R.MAKER_PCT, R.TAKER_PCT, R.EXIT_AFTER_S, R.HORIZON_S, R.MIN_TURNOVER,
                 R.DOWNSIDE_TAIL_BAR) == (0.0, 0.02, 300.0, 240 * 3600, 10_000.0, -10.0)
    ok = ok and R.FLOORS == {"SCREEN": (200, 20), "DECIDER": (60, 5)}
    ok = ok and R.SCREEN_FIRST_T == R.utc("2025-01-01") and R.SCREEN_DATA_END == R.utc("2026-09-27")
    return ok, rows


def t_scanner_cfg_is_oracle_default_plus_two_overrides():
    a, b = vars(R.scanner_cfg()), vars(oracle.Cfg())
    diff = {k for k in a if a[k] != b[k]}
    return diff == {"fee", "min_turnover"} and a["fee"] == 0.0 and a["min_turnover"] == 10_000.0, diff


def t_minute_prints_order_and_silence():
    up = R.minute_prints(1.0, 3.0, 0.5, 2.0, 5, 100)
    down = R.minute_prints(2.0, 3.0, 0.5, 1.0, 5, 100)
    none = R.minute_prints(2.0, 2.0, 2.0, 2.0, 0, 100)
    return ([p for _, p in up] == [1.0, 0.5, 3.0, 2.0] and [p for _, p in down] == [2.0, 3.0, 0.5, 1.0]
            and [ts for ts, _ in up] == [100.0, 120.0, 140.0, 159.0] and none == []), (up, down, none)


def t_screen_touch_never_fills():
    # empty level is 105 (nearest 104.6); the BUY below rests at 104
    touch = forward([(T0, 104.6, 104.6, 104.0, 104.6, 3)] + flat(T0 + 60, 5, 104.6))
    through = forward([(T0, 104.6, 104.6, 103.99, 104.6, 3)] + flat(T0 + 60, 5, 104.6))
    return touch["fills"] == 0 and through["fills"] >= 1, (touch["fills"], through["fills"])


def t_screen_silent_minutes_never_fill():
    # a zero-trade minute carrying a price through a level books nothing
    silent = forward([(T0, 103.0, 103.0, 103.0, 103.0, 0)] + flat(T0 + 60, 5, 104.6, n=0))
    return silent["fills"] == 0, silent


def t_screen_exit_after_300s_not_299():
    below = 99.0
    stay = forward(flat(T0, 2, 104.6) + flat(T0 + 120, 10, below))
    # outside at T0+120 (first print), back inside 299 s later: no exit
    back = forward(flat(T0, 2, 104.6) + [(T0 + 120, below, below, below, below, 1)]
                   + flat(T0 + 180, 3, below, n=0)
                   + [(T0 + 360, 100.5, 100.5, 100.5, 100.5, 1)] + flat(T0 + 420, 10, 100.5))
    ok = (stay["exit"] == "down" and abs(stay["exit_h"] * 3600 - (120 + 300)) < 1e-6
          and back["exit"] is None)
    return ok, (stay["exit"], stay["exit_h"], back["exit"])


def t_screen_deploy_and_exit_pay_the_half_spread():
    # 10 bps spread: deploy at print x (1 + 5bp); exit at last print x (1 - 5bp)
    r = forward(flat(T0, 2, 104.6) + flat(T0 + 120, 10, 99.0), spread_bps=10.0)
    g = gridbot.Grid(TEST, TEST, LEVELS, "arith", R.ALLOC, R.MAKER_PCT, R.TAKER_PCT)
    g.deploy(104.6 * 1.0005, float(T0))
    for i in range(10):
        for ts, p in R.minute_prints(99.0, 99.0, 99.0, 99.0, 1, T0 + 120 + i * 60):
            if ts >= T0 + 120 + 300:
                break
            g.on_print(p, ts)
    g.close(99.0 * 0.9995, T0 + 420, "x")
    return (abs(r["deploy_px"] - 104.6 * 1.0005) < 1e-9
            and abs(r["net_pct"] - g.realised / R.ALLOC * 100) < 1e-9), (r["net_pct"], g.realised / R.ALLOC * 100)


def t_screen_missing_minutes_are_unmeasured_never_filled():
    mins = flat(T0, 30, 104.6)
    del mins[10:14]                                 # 4 minutes the exchange never returned
    r = forward(mins)
    return r["clean"] is False and r["unmeasured_min"] == R.HORIZON_S // 60 - 26, r


def t_screen_exit_agrees_with_the_bot():
    """The same path through GridBot's own Bot (_apply + check_exits, 0.5 s
    ticks) and through the harness: same exit time (to the tick) and price."""
    path = (flat(T0, 3, 104.6) + [(T0 + 180, 104.6, 104.6, 99.4, 99.4, 2)]
            + flat(T0 + 240, 2, 99.2, n=0) + [(T0 + 360, 99.1, 99.3, 99.0, 99.3, 1)]
            + flat(T0 + 420, 6, 99.3, n=0))
    h = forward(path)
    clock = [float(T0)]
    feed = SimpleNamespace(connected=True, last_msg=time.time(), connects=1, errors=0, last_error="",
                           set_symbols=lambda s: None, stop=lambda: None, check_stale=lambda: None)
    prints = [pp for (t_open, o, hi, lo, c, n) in path for pp in R.minute_prints(o, hi, lo, c, n, t_open)]
    def last_print(key):
        """The ticker's 'last' in a trade-price world: the newest print so far."""
        return [p for ts, p in [(T0 - 60, 104.6)] + prints if ts <= clock[0]][-1]
    d = Path(tempfile.mkdtemp(prefix="bnus_replay_test_"))
    args = gridbot.parse_args(["--data-dir", str(d), "--no-scanner", "--exit-after", "300"])
    bot = gridbot.Bot(args, feed=feed, clock=lambda: clock[0], price_fn=last_print,
                      trades_fn=lambda k, s: ([], None), render=False)
    g = gridbot.Grid(TEST, TEST, LEVELS, "arith", R.ALLOC, R.MAKER_PCT, R.TAKER_PCT)
    g.deploy(104.6, float(T0))
    bot.grids[TEST] = g
    events = sorted(prints)
    i = 0
    tick = float(T0)
    while TEST in bot.grids and tick < T0 + 1200:
        while i < len(events) and events[i][0] <= tick:
            clock[0] = events[i][0]
            bot.on_trade(TEST, events[i][1], events[i][0], None)
            i += 1
        clock[0] = tick
        feed.last_msg = time.time()
        bot.check_exits()
        tick += 0.5
    ex = bot.closed[-1] if bot.closed else None
    ok = (ex is not None and h["exit"] == "down"
          and abs(ex["closed_at"] - (T0 + h["exit_h"] * 3600)) <= 0.5
          and abs(ex["realised"] / R.ALLOC * 100 - h["net_pct"]) < 1e-9)
    return ok, (ex and (ex["closed_at"] - T0, ex["close_px"], ex["realised"]), h)


def t_lookback_rows_aggregate_and_refuse_holes():
    t = T0
    n = R.LOOKBACK_BARS * 30
    mins = [(t - n * 60 + i * 60, 10 + i % 7, 12 + i % 7, 9 + i % 7, 11 + i % 7, 1) for i in range(n)]
    k = make_k(mins)
    rows = R.lookback_rows(k, n, t)
    b0 = mins[:30]
    ok = (rows is not None and len(rows) == 480 and rows[0][0] == t - n * 60
          and rows[0][1:5] == (b0[0][1], max(m[2] for m in b0), min(m[3] for m in b0), b0[-1][4]))
    holey = make_k(mins[:100] + mins[101:])
    ok = ok and R.lookback_rows(holey, n - 1, t) is None
    return ok, rows and rows[0]


def t_nearest_rank_and_pass_rule_fail_closed():
    ok = R.nearest_rank([-12.0, -3.0, 1.0, 2.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 0.10) == -12.0
    ok = ok and R.nearest_rank(list(range(1, 21)), 0.10) == 2 and R.nearest_rank([], 0.1) is None
    good = ([{"outcome": "deployed", "clean": True, "net_pct": 1.0, "exit": None}] * 60
            + [{"outcome": "deployed", "clean": True, "net_pct": -4.0, "exit": "down"}] * 5)
    s = R.summarize(good, "DECIDER")
    ok = ok and s["all_met"] and s["clean"] == 65 and s["down"] == 5
    few = R.summarize(good[:40] + good[-5:], "DECIDER")               # 45 clean < 60
    tail = R.summarize(good + [{"outcome": "deployed", "clean": True, "net_pct": -30.0, "exit": "down"}] * 2,
                       "DECIDER")                                     # worst tenth of 7 downs = -30
    dirty = R.summarize([dict(r, clean=False) for r in good], "DECIDER")
    ok = ok and not few["all_met"] and not tail["all_met"] and dirty["clean"] == 0 and not dirty["all_met"]
    return ok, (s["conditions"], few["conditions"], tail["tail_p10_down"])


TESTS = [t_registered_constants_match_prereg, t_scanner_cfg_is_oracle_default_plus_two_overrides,
         t_minute_prints_order_and_silence, t_screen_touch_never_fills,
         t_screen_silent_minutes_never_fill, t_screen_exit_after_300s_not_299,
         t_screen_deploy_and_exit_pay_the_half_spread,
         t_screen_missing_minutes_are_unmeasured_never_filled, t_screen_exit_agrees_with_the_bot,
         t_lookback_rows_aggregate_and_refuse_holes, t_nearest_rank_and_pass_rule_fail_closed]


def run_tests(quiet=False) -> dict:
    """{test name: passed}. A test that raises FAILS (never passes on an
    exception in its own setup)."""
    res = {}
    for t in TESTS:
        try:
            ok, detail = t()
        except Exception as e:
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        res[t.__name__] = bool(ok)
        if not quiet:
            print(f"  {'PASS' if ok else 'FAIL'}  {t.__name__}" + ("" if ok else f"   {str(detail)[:300]}"))
        R.PAIRS.pop(TEST, None)
    return res


# -------------------------------------------------------------------------- mutants
def _mut_touch_fills():
    """SCREEN fills on touch: every print nudged one ulp toward crossing."""
    real = R.minute_prints

    def m(o, h, l, c, n, t_open):
        return [(ts, math.nextafter(p, -math.inf) if p <= min(o, c) else math.nextafter(p, math.inf)
                 if p >= max(o, c) else p) for ts, p in real(o, h, l, c, n, t_open)]
    R.minute_prints = m
    return lambda: setattr(R, "minute_prints", real)


def _mut_silent_minutes_print():
    """SCREEN treats Binance's filled zero-trade minutes as prints."""
    real = R.minute_prints
    R.minute_prints = lambda o, h, l, c, n, t_open: real(o, h, l, c, max(n, 1), t_open)
    return lambda: setattr(R, "minute_prints", real)


def _mut_interpolate_missing():
    """The harness forward-fills minutes the exchange never returned."""
    real = R.screen_forward

    def m(sym, k, i_t, t, levels, kind, meta=None):
        om = k["open_ms"]
        full = np.arange(om[0], om[-1] + 60_000, 60_000, dtype=np.int64)
        idx = np.searchsorted(om, full, side="right") - 1
        filled = {name: v[idx] for name, v in k.items()}
        filled["open_ms"] = full
        horizon_pad = np.arange(full[-1] + 60_000, (t + R.HORIZON_S) * 1000, 60_000, dtype=np.int64)
        for name in filled:
            filled[name] = np.concatenate([filled[name], np.repeat(filled[name][-1:], len(horizon_pad))]) \
                if name != "open_ms" else np.concatenate([filled[name], horizon_pad])
        return real(sym, filled, int(np.searchsorted(full, t * 1000)), t, levels, kind, meta)
    R.screen_forward = m
    return lambda: setattr(R, "screen_forward", real)


def _mut_exit_at_299():
    real = R.EXIT_AFTER_S
    R.EXIT_AFTER_S = 299.0
    return lambda: setattr(R, "EXIT_AFTER_S", real)


def _mut_no_half_spread():
    real = R.half_spread
    R.half_spread = lambda sym: 0.0
    return lambda: setattr(R, "half_spread", real)


def _mut_mean_only():
    real = R.summarize

    def m(rows, role):
        s = real(rows, role)
        s["all_met"] = s["mean"] is not None and s["mean"] > 0
        return s
    R.summarize = m
    return lambda: setattr(R, "summarize", real)


MUTANTS = [
    ("A  SCREEN fills on touch", _mut_touch_fills, {"t_screen_touch_never_fills"}),
    ("A2 SCREEN prints in zero-trade minutes", _mut_silent_minutes_print, {"t_screen_silent_minutes_never_fill"}),
    ("C  forward-fills missing minutes", _mut_interpolate_missing,
     {"t_screen_missing_minutes_are_unmeasured_never_filled"}),
    ("E  exit after 299 s", _mut_exit_at_299, {"t_screen_exit_after_300s_not_299"}),
    ("S  no half-spread on taker legs", _mut_no_half_spread, {"t_screen_deploy_and_exit_pay_the_half_spread"}),
    ("P  pass on the mean alone", _mut_mean_only, {"t_nearest_rank_and_pass_rule_fail_closed"}),
]


def run_mutants() -> bool:
    ok_all = True
    for name, apply, must_fail in MUTANTS:
        undo = apply()
        try:
            res = run_tests(quiet=True)
        finally:
            undo()
        red = {t for t, ok in res.items() if not ok}
        caught = must_fail & red
        ok_all &= bool(caught)
        print(f"  {'CAUGHT' if caught else 'MISSED'}  mutant {name}: red = {sorted(red) or 'none'}")
    return ok_all


if __name__ == "__main__":
    if "--mutants" in sys.argv:
        base = run_tests(quiet=True)
        if not all(base.values()):
            print("the unmutated tests are not all green; mutants mean nothing")
            sys.exit(1)
        ok = run_mutants()
        print(f"\nmutants: {'all caught' if ok else 'SOME MISSED'}")
        sys.exit(0 if ok else 1)
    res = run_tests()
    bad = sum(1 for v in res.values() if not v)
    print(f"\n{len(res) - bad} passed, {bad} failed")
    sys.exit(1 if bad else 0)
