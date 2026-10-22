#!/usr/bin/env python3
"""Tests for audit/arm_b.py. Synthetic data only: no network, no files.

    python -X utf8 audit/test_arm_b.py            # the tests
    python -X utf8 audit/test_arm_b.py --mutants  # negative controls

Each mutant must turn at least one NAMED test red; one nothing catches fails.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (str(HERE.parent), str(HERE)):
    sys.path.insert(0, p)

import gridbot            # noqa: E402
import bnus_replay as R   # noqa: E402
import arm_b as B         # noqa: E402

LEVELS = [100.0 + i for i in range(11)]            # 100 .. 110, step 1%
T0 = R.utc("2026-01-10")


def grid(levels=LEVELS):
    return B.ArmBGrid("TESTUSDT", "TESTUSDT", levels, "arith", R.ALLOC, R.MAKER_PCT, R.TAKER_PCT)


def book_of(minutes, stamped=()):
    """minutes: [(m, bid, ask)] -> the loader's dict; mid = (bid+ask)/2."""
    rows = {m: (bid, ask, (bid + ask) / 2.0) for m, bid, ask in minutes}
    return {"rows": rows, "stamped": set(stamped), "skipped_files": []}


def bflat(t_from, count, bid, ask):
    return [(t_from + i * 60, bid, ask) for i in range(count)]


def forward(minutes, horizon_min=20, deploy=(104.5, 104.6), step_pct=1.0, levels=LEVELS):
    book = book_of([(T0 - 60, deploy[0], deploy[1])] + minutes)
    return B.book_forward("TESTUSDT", book, T0, levels, "arith", horizon_min * 60, step_pct=step_pct)


# ------------------------------------------------------------------ tests --
def t_deploy_buys_nothing():
    g = grid()
    r = g.deploy(104.6, 1.0)
    assert g.base == 0.0 and g.cash == R.ALLOC and g.fees == 0.0, (g.base, g.cash, g.fees)
    assert r["sells"] == 0 and r["coins_bought"] == 0.0 and r["cost"] == 0.0
    assert all(o is None or o["side"] == gridbot.BUY for o in g.orders)
    assert g.holding() == 0


def t_empty_level_is_the_deciders():
    for px in (100.6, 102.5, 104.6, 107.49, 109.9):
        a, d = grid(), gridbot.Grid("T", "T", LEVELS, "arith", R.ALLOC, R.MAKER_PCT, R.TAKER_PCT)
        a.deploy(px, 1.0)
        d.deploy(px, 1.0)
        assert a.empty == d.empty, (px, a.empty, d.empty)
        assert a.rung == d.rung and a.n == a.empty + 1


def t_band_is_the_full_band_ladder_is_truncated():
    g = grid()
    g.deploy(104.6, 1.0)
    assert g.lo == 100.0 and g.hi == 110.0 and g.levels[-1] == 105.0 and g.n == 6
    assert g.ladder_levels == 6


def t_fill_loop_is_inherited_unchanged():
    assert B.ArmBGrid.on_print is gridbot.Grid.on_print
    assert B.ArmBGrid._fill_buy is gridbot.Grid._fill_buy
    assert B.ArmBGrid.close is gridbot.Grid.close and B.ArmBGrid.mark is gridbot.Grid.mark


def t_rise_first_fills_nothing_then_buy_then_sell_one_step_up():
    g = grid()
    g.deploy(104.6, 1.0)
    assert g.on_print(109.0, 2.0) == [] and g.fills == 0          # nothing rests above e
    fills = g.on_print(103.9, 3.0)                                 # through level 104
    assert [f["side"] for f in fills] == [gridbot.BUY] and fills[0]["price"] == 104.0
    assert g.orders[5] and g.orders[5]["side"] == gridbot.SELL and g.orders[5]["entry"] == 104.0
    fills = g.on_print(105.1, 4.0)                                 # through level 105
    assert [f["side"] for f in fills] == [gridbot.SELL] and fills[0]["price"] == 105.0
    assert g.round_trips == 1 and g.base == 0.0
    step_pnl = g.rung / 104.0 * (105.0 - 104.0)
    assert math.isclose(g.realised_ladder, step_pnl, rel_tol=1e-9), (g.realised_ladder, step_pnl)
    assert math.isclose(g.realised, step_pnl, rel_tol=1e-9)


def t_split_sums_to_total_open_and_closed():
    g = grid()
    g.deploy(104.6, 1.0)
    g.on_print(101.9, 2.0)                        # buys at 104, 103, 102
    g.on_print(103.1, 3.0)                        # sells the 102 lot at 103
    sp = g.split(101.0)
    m = g.mark(101.0)["total"]
    assert math.isclose(sp["harvest"] + sp["bleed"], sp["total"]) and math.isclose(sp["total"], m)
    assert sp["harvest"] > 0 and sp["bleed"] < 0                   # two lots held, marked below cost
    g.close(99.0, 4.0, "test")
    sp = g.split()
    assert math.isclose(sp["total"], g.realised) and math.isclose(sp["harvest"] + sp["bleed"], g.realised)
    s = g.summary()
    assert math.isclose(s["realised_ladder"] + s["realised_inventory"], g.realised)
    assert s["levels"] == 11 and s["hi"] == 110.0


def t_no_buy_rung_is_a_skip():
    g = grid()
    try:
        g.deploy(100.3, 1.0)
        raise AssertionError("deploy accepted a ladder with no buy rung")
    except ValueError as e:
        assert "no buy rung" in str(e)
    res = forward(bflat(T0, 5, 100.2, 100.3), deploy=(100.2, 100.3))
    assert res == {"skip": "no buy rung below the deploy price"}, res


def t_forward_shares_the_deciders_driver_and_exit():
    # ask drops to 103.99 for one minute: BUY at 104 fills (ask <= L); SELL rests at 105
    res = forward(bflat(T0, 3, 103.98, 103.99) + bflat(T0 + 180, 17, 104.4, 104.5))
    assert res["fills"] == 1 and res["round_trips"] == 0 and res["exit"] is None
    assert res["bleed_pct"] != 0.0 and res["harvest_pct"] == 0.0 and res["clean"]
    # upside: mid above the FULL hi (110) for 300 s of recorded closes exits with no inventory
    res = forward(bflat(T0, 2, 104.4, 104.5) + bflat(T0 + 120, 18, 110.4, 110.5))
    assert res["exit"] == "up" and res["fills"] == 0 and res["net_pct"] == 0.0, res
    # a stamped gap: unclean, never interpolated, no VR
    book = book_of([(T0 - 60, 104.5, 104.6)] + bflat(T0, 20, 104.4, 104.5), stamped=[T0 + 300])
    res = B.book_forward("TESTUSDT", book, T0, LEVELS, "arith", 20 * 60, step_pct=1.0)
    assert res["unmeasured_min"] == 1 and not res["clean"] and "vr" not in res


def t_downside_exit_sells_inventory_as_taker():
    # 104, 103, 102 fill at 102.0; then 101 and 100 fill on the way through the floor
    res = forward(bflat(T0, 2, 101.9, 102.0) + bflat(T0 + 120, 18, 99.4, 99.5))
    assert res["exit"] == "down" and res["fills"] == 5 and res["round_trips"] == 0, res
    assert res["net_pct"] < 0 and math.isclose(res["harvest_pct"], 0.0)
    assert math.isclose(res["net_pct"], res["harvest_pct"] + res["bleed_pct"])


def t_vr_rung_scale_picks_q_and_signs_reversion():
    import numpy as np
    alt = [100.0 if i % 2 == 0 else 102.0 for i in range(700)]      # alternating 2%: rms(5) = 2% >= 1% step
    v = B.vr_rung_scale(alt, 1.0)
    assert v["vr_q"] == 5 and v["vr"] is not None and v["vr"] < 1.0, v
    assert v["vr60"] is not None and v["vr60"] < 1.0

    def ar1(phi, seed=1, n=700, sigma=0.003):                        # AR(1) returns: phi>0 persistent, <0 reverting
        rs, r = np.random.RandomState(seed), [0.0]
        for _ in range(n):
            r.append(phi * r[-1] + sigma * rs.randn())
        return list(100.0 * np.exp(np.cumsum(r)))
    v = B.vr_rung_scale(ar1(+0.6), 1.0)
    assert v["vr_q"] in B.Q_SET and v["vr"] > 1.0, v
    v = B.vr_rung_scale(ar1(-0.6), 1.0)
    assert v["vr_q"] in B.Q_SET and v["vr"] < 1.0, v
    assert B.vr_rung_scale([100.0] * 30, 1.0)["vr"] is None            # flat: unmeasured
    small = [100.0 + 0.001 * (i % 2) for i in range(700)]
    v = B.vr_rung_scale(small, 5.0)                                     # never reaches the step
    assert v["vr_q"] == 60 and "coarsest" in v["vr_note"], v


def t_statement_fails_closed_under_the_bar():
    row = {"outcome": "deployed", "clean": True, "net_pct": 1.0, "harvest_pct": 1.0, "bleed_pct": 0.0,
           "exit": "up", "exit_h": 1.0, "fills": 2, "round_trips": 1, "ladder_levels": 4, "vr": 0.8}
    s = B.summarize([dict(row) for _ in range(59)])
    assert s["statement"].startswith("UNDERPOWERED") and s["vr_cross_check"].endswith("held")
    s = B.summarize([dict(row) for _ in range(60)])
    assert s["statement"].startswith("MEASURED: mean") and "was measured" in s["statement"]
    neg = [dict(row, net_pct=-0.1) for _ in range(60)]
    s = B.summarize(neg)
    assert s["statement"].startswith("MEASURED: none") and s["vr_cross_check"].endswith("FAILED")
    mixed = [dict(row, net_pct=(5.0 if i < 20 else -0.1)) for i in range(60)]    # mean > 0, median < 0
    assert B.summarize(mixed)["statement"].startswith("MEASURED: none")


def t_registered_constants_are_the_deciders():
    assert B.REGISTERED["gridbot.py"] == "4b469c8483671332cdf7e2f6bbc8c04e5acc8d3e"
    assert B.SAMPLE_FLOOR == 60 and B.ARM_B_DEADLINE_CUT == R.utc("2026-11-27")
    assert B.engine_problem() is None, B.engine_problem()


def run_tests(quiet=False) -> dict:
    res = {}
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            try:
                fn()
                res[name] = True
                if not quiet:
                    print(f"  PASS  {name}")
            except Exception as e:                  # noqa: BLE001
                res[name] = False
                if not quiet:
                    print(f"  FAIL  {name}: {e!r}")
    return res


# ---------------------------------------------------------------- mutants --
def _mut_pre_buy_returns():
    """deploy() falls back to the parent's: the pre-buy is back."""
    orig = B.ArmBGrid.deploy

    def m(self, price, ts):
        out = gridbot.Grid.deploy(self, price, ts)
        self.ladder_levels = self.n
        return out
    B.ArmBGrid.deploy = m
    return lambda: setattr(B.ArmBGrid, "deploy", orig)


def _mut_harvest_counts_bleed():
    """harvest includes the exit sale: the split no longer sums."""
    orig = B.ArmBGrid.split

    def m(self, price=None):
        total = self.realised if self.status == "CLOSED" else self.mark(price)["total"]
        return {"total": total, "harvest": total, "bleed": total - self.realised_ladder}
    B.ArmBGrid.split = m
    return lambda: setattr(B.ArmBGrid, "split", orig)


def _mut_statement_on_mean_only():
    orig = B.summarize

    def m(rows):
        s = orig(rows)
        if s["clean"] >= B.SAMPLE_FLOOR and s["mean"] is not None and s["mean"] > 0:
            s["statement"] = "MEASURED: mean > 0: harvestable mean reversion at rung scale was measured"
        return s
    B.summarize = m
    return lambda: setattr(B, "summarize", orig)


def _mut_truncated_hi():
    orig = B.ArmBGrid.hi
    B.ArmBGrid.hi = property(lambda self: self.levels[-1])
    return lambda: setattr(B.ArmBGrid, "hi", orig)


MUTANTS = [("pre-buy returns", _mut_pre_buy_returns, {"t_deploy_buys_nothing"}),
           ("harvest counts bleed", _mut_harvest_counts_bleed, {"t_split_sums_to_total_open_and_closed"}),
           ("statement on mean only", _mut_statement_on_mean_only, {"t_statement_fails_closed_under_the_bar"}),
           ("hi is the truncated top", _mut_truncated_hi,
            {"t_band_is_the_full_band_ladder_is_truncated", "t_forward_shares_the_deciders_driver_and_exit"})]


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
