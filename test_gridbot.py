#!/usr/bin/env python3
"""Tests for gridbot.py. No network: every Kraken call is faked.

    python -X utf8 test_gridbot.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import oracle     # noqa: E402
import gridbot    # noqa: E402
from gridbot import Grid, BUY, SELL, feasibility  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def ladder_ok(g: Grid) -> bool:
    """Exactly one empty level; BUYs strictly below it, SELLs strictly above."""
    if g.status != "ACTIVE":
        return True
    e = g.empty
    return (g.orders[e] is None
            and all(g.orders[j] and g.orders[j]["side"] == BUY for j in range(e))
            and all(g.orders[j] and g.orders[j]["side"] == SELL for j in range(e + 1, g.n)))


def conserved(g: Grid) -> bool:
    """Cash is exactly allocation + realised - cost basis of every coin held."""
    basis = sum(o["qty"] * o["entry"] + o["entry_fee"]
                for o in g.orders if o and o["side"] == SELL)
    coins = sum(o["qty"] for o in g.orders if o and o["side"] == SELL)
    return close(g.cash, g.alloc + g.realised - basis, 1e-9) and close(g.base, coins, 1e-9)


def arith(lo=100.0, hi=110.0, n=11):
    return oracle.make_levels(lo, hi, n, "arith")      # 100, 101, ..., 110


def new_grid(levels=None, alloc=110.0, maker=0.22, taker=0.38, price=104.6, ts=1000.0):
    g = Grid("TEST/USD", "TESTUSD", levels or arith(), "arith", alloc, maker, taker)
    g.deploy(price, ts)
    return g


# -----------------------------------------------------------------------------
print("\n1. the ladder is the scanner's own")
for kind in ("arith", "geo"):
    lv = oracle.make_levels(0.9, 1.3, 24, kind)
    g = Grid("X/USD", "XUSD", lv, kind, 100, 0.22, 0.38)
    check(f"{kind}: levels identical to oracle.make_levels", g.levels == lv)
try:
    Grid("X/USD", "XUSD", [1.0, 1.0, 2.0], "arith", 100, 0.2, 0.4)
    check("non-increasing levels are refused", False)
except ValueError:
    check("non-increasing levels are refused", True)

# -----------------------------------------------------------------------------
print("\n2. deploy lays one empty level, buys below, sells above")
g = new_grid()
check("empty level is the one nearest the price (105 for 104.6)", g.empty == 5, g.empty)
check("ladder shape", ladder_ok(g))
check("5 buys below, 5 sells above", g.holding() == 5 and sum(
    1 for o in g.orders if o and o["side"] == BUY) == 5)
q_expected = sum(g.rung / g.levels[j - 1] for j in range(6, 11))
check("coins bought at deploy = one rung per sell, sized at the level below",
      close(g.base, q_expected), (g.base, q_expected))
check("deploy buy paid taker", close(g.fees, q_expected * 104.6 * 0.0038))
check("cash conserved after deploy", conserved(g))
try:
    Grid("X/USD", "XUSD", arith(), "arith", 110, 0.22, 0.38).deploy(99.0, 1.0)
    check("deploy outside the band is refused", False)
except ValueError:
    check("deploy outside the band is refused", True)

# -----------------------------------------------------------------------------
print("\n3. fills: trade-through, at the level, one step apart")
g = new_grid()
f = g.on_print(104.0, 1001, 1)
check("a print exactly AT a buy level does not fill it", f == [], f)
f = g.on_print(103.99, 1002, 2)
check("a print below it does, at the level price", len(f) == 1 and f[0]["side"] == BUY
      and f[0]["price"] == 104.0, f)
q = f[0]["qty"]
check("the bought coins now rest as a SELL one level up (105)",
      g.orders[5] == {"side": SELL, "qty": q, "entry": 104.0, "entry_fee": q * 104.0 * 0.0022})
check("re-crossing the level it just filled at does nothing",
      g.on_print(104.2, 1003, 3) == [] and g.on_print(103.9, 1004, 4) == [])
f = g.on_print(105.01, 1005, 5)
pnl = q * (105.0 - 104.0) - 0.0022 * q * (104.0 + 105.0)
check("the sell one level up completes a round trip at 105", len(f) == 1 and f[0]["side"] == SELL
      and f[0]["price"] == 105.0, f)
check("round-trip P/L = qty x step - maker on both legs", close(f[0]["pnl"], pnl), (f[0]["pnl"], pnl))
check("realised booked", close(g.realised, pnl) and g.round_trips == 1)
check("ladder shape and cash still hold", ladder_ok(g) and conserved(g))

g = new_grid()
f = g.on_print(101.5, 1001, 1)
check("one print through three levels fills three buys, top first",
      [x["price"] for x in f] == [104.0, 103.0, 102.0], [x["price"] for x in f])
check("ladder after a gap", ladder_ok(g) and g.empty == 2 and conserved(g))

# -----------------------------------------------------------------------------
print("\n4. dedupe and timing")
g = new_grid()
check("prints at or before deploy are ignored", g.on_print(99.0, 1000.0, None) == [])
g.on_print(103.5, 1001, 10)
check("a repeated trade id is ignored", g.on_print(90.0, 1002, 10) == [] and g.fills == 1)
check("an older trade id is ignored", g.on_print(90.0, 1003, 9) == [] and g.fills == 1)

# -----------------------------------------------------------------------------
print("\n5. random walk: invariants never break")
random.seed(7)
g = new_grid(levels=oracle.make_levels(0.9, 1.1, 30, "geo"), price=1.0137)
px, tid = 1.0137, 0
for i in range(20000):
    px *= 1 + random.gauss(0, 0.004)
    px = min(max(px, 0.85), 1.15)
    tid += 1
    g.on_print(px, 1001 + i, tid)
    if not (ladder_ok(g) and conserved(g)):
        break
check("20,000 prints: one empty level, buys below, sells above, cash conserved",
      ladder_ok(g) and conserved(g), (i, g.empty))
check("fills happened", g.fills > 100 and g.round_trips > 50, (g.fills, g.round_trips))
check("every round trip at a positive step clears maker fees (step 0.68% > 0.44%)",
      g.realised > 0 or g.round_trips == 0, g.realised)

# -----------------------------------------------------------------------------
print("\n6. where the scanner's simulate_grid and a real grid agree, and where not")
lv = arith()


def sim(points):
    return oracle.simulate_grid(points, lv, 0.22, 0.0)


# one-directional move down: both count 3 fills and +3 rungs of inventory
path_down = [104.6, 103.5, 102.5, 101.5]
s = sim(path_down)
g = new_grid(price=104.6)
for i, p in enumerate(path_down[1:]):
    g.on_print(p, 1001 + i, i + 1)
check("monotone move: same fill count", s["fills"] == g.fills == 3, (s["fills"], g.fills))
check("monotone move: same inventory in rungs",
      round(s["end_inv"] * (len(lv) - 1)) == g.net_fills == 3, (s["end_inv"], g.net_fills))

# reversal: the scanner counts the turn back through 102 as a fill; no grid can
path_rev = path_down + [102.5, 103.5, 104.5, 105.5]
s = sim(path_rev)
g = new_grid(price=104.6)
for i, p in enumerate(path_rev[1:]):
    g.on_print(p, 1001 + i, i + 1)
check("one reversal: the scanner counts exactly one fill more than the grid gets",
      s["fills"] - g.fills == 1, (s["fills"], g.fills))

# chop around one level: the scanner counts every crossing, the grid fills nothing
g = new_grid(price=104.6)
g.on_print(103.5, 1001, 1)              # buy at 104; the exit now rests at 105
before = g.fills
chop = [103.5] + [104.2, 103.8] * 5
s = sim(chop)
for i, p in enumerate(chop[1:]):
    g.on_print(p, 1100 + i, 100 + i)
check("chop across one level: scanner counts 10 fills, grid gets 0",
      s["fills"] == 10 and g.fills == before, (s["fills"], g.fills - before))

# -----------------------------------------------------------------------------
print("\n7. exit")
g = new_grid()
g.on_print(99.5, 1001, 1)               # through the floor: all five buys fill
check("breaking the floor fills every buy", g.holding() == 10 and g.empty == 0)
check("outside timer started", g.outside_since == 1001)
g.on_print(100.5, 1002, 2)
check("back inside clears the timer", g.outside_since is None)
g.on_print(99.99, 1003, 3)
floor_est = g.floor_pnl()                   # already below the floor: a sale at today's print
s = g.close(99.99, 1400, "test")
check("close sells every coin", g.base == 0 and g.status == "CLOSED" and all(o is None for o in g.orders))
check("close realises everything: realised = cash - allocation", close(g.realised, g.cash - g.alloc))
check("a grid closed below its floor loses money", s["realised"] < 0, s["realised"])
check("floor_pnl below the floor equals a close at the current print", close(floor_est, s["realised"]),
      (floor_est, s["realised"]))
h = new_grid()
h.on_print(101.5, 1001, 1)                  # inside the band, three buys held
est_h = h.floor_pnl()
h2 = Grid.from_dict(h.to_dict())
h2.on_print(h2.lo * (1 - 1e-9), 1002, 2)
s2 = h2.close(h2.lo * (1 - 1e-9), 1003, "test")
check("floor_pnl inside the band = every buy filled, then a sale at the floor",
      close(est_h, s2["realised"]), (est_h, s2["realised"]))
check("a closed grid ignores prints", g.on_print(105, 1500, 99) == [])

# -----------------------------------------------------------------------------
print("\n8. persistence")
g = new_grid()
for i, p in enumerate([103.2, 104.7, 102.1, 106.3]):
    g.on_print(p, 1001 + i, i + 1)
h = Grid.from_dict(json.loads(json.dumps(g.to_dict())))
check("round trip through JSON keeps every field",
      json.dumps(h.to_dict(), sort_keys=True) == json.dumps(g.to_dict(), sort_keys=True))
for i, p in enumerate([101.4, 107.9, 103.3]):
    a, b = g.on_print(p, 2000 + i, 100 + i), h.on_print(p, 2000 + i, 100 + i)
    if a != b:
        break
check("a restored grid behaves identically", g.to_dict() == h.to_dict())

# -----------------------------------------------------------------------------
print("\n9. feasibility follows Kraken's minimums")
lv = oracle.make_levels(1.0, 2.0, 20, "arith")
check("feasible ladder passes", feasibility(lv, 200, {"ordermin": "1", "costmin": "0.5"}) is None)
check("rung under ordermin is refused", "minimum order" in (feasibility(lv, 20, {"ordermin": "5"}) or ""))
check("rung under costmin is refused", "minimum" in (feasibility(lv, 5, {"ordermin": "0.1", "costmin": "0.5"}) or ""))
check("missing pair data is refused", feasibility(lv, 200, None) == "no Kraken pair data")

# -----------------------------------------------------------------------------
print("\n9b. levels snap to Kraken's tick")
lv = oracle.make_levels(1.8233912, 2.5306088, 8, "arith")
snapped = gridbot.snap_levels(lv, {"tick_size": "0.001"})
check("every snapped level is a whole number of ticks",
      all(abs(x / 0.001 - round(x / 0.001)) < 1e-6 for x in snapped), snapped)
check("each level moves at most half a tick", all(abs(a - b) <= 0.0005 + 1e-12 for a, b in zip(lv, snapped)))
check("pair_decimals is used when tick_size is missing",
      gridbot.snap_levels(lv, {"pair_decimals": 2}) == [round(x, 2) for x in lv])
try:
    gridbot.snap_levels(lv, {"tick_size": "1"})
    check("a ladder finer than the tick is refused, not simulated off-tick", False)
except ValueError:
    check("a ladder finer than the tick is refused, not simulated off-tick", True)
check("unknown tick leaves the ladder unchanged", gridbot.snap_levels(lv, {}) == lv)
check("short prices fit 11 characters, tiny ones included",
      all(len(gridbot.short_px(p)) <= 11 for p in (2.534e-06, 0.00001234, 0.000123456, 0.0123456,
                                                  1.23456, 123.456, 86130.4, 1234567.0)))
check("feasibility uses the highest RESTING order (rung / second-highest level)",
      feasibility([1.0, 2.0, 3.0], 30, {"ordermin": "4.9"}) is None
      and feasibility([1.0, 2.0, 3.0], 30, {"ordermin": "5.1"}) is not None)


# -----------------------------------------------------------------------------
print("\n10. the bot: deploy choices, resync, exits, state")


class FakeFeed:
    def __init__(self):
        self.symbols = set()
        self.connected = True
        self.last_msg = time.time()
        self.connects = 1
        self.errors = 0
        self.last_error = ""

    def set_symbols(self, s):
        self.symbols = set(s)

    def start(self):
        pass

    def stop(self):
        pass

    def check_stale(self):
        pass


def scan_row(sym, lo, hi, n=11, qualified=True, basket=True, yd=0.5):
    return {"symbol": sym, "key": sym.replace("/", ""), "lo": lo, "hi": hi, "levels": n,
            "kind": "arith", "qualified": qualified, "basket": basket, "yield_day": yd,
            "fee_pct": 0.22, "price": (lo + hi) / 2}


def write_scan(d: Path, rows, age_s=0.0):
    gen = datetime.fromtimestamp(time.time() - age_s, timezone.utc).isoformat()
    (d / "scan.json").write_text(json.dumps({"generated": gen, "version": "3.1", "interval": "30m",
                                             "fee_pct": 0.22, "results": rows}))


KEYS = ("AAAUSD", "BBBUSD", "CCCUSD", "DDDUSD", "EEEUSD", "FFFUSD", "AAAUSDT")
book = SimpleNamespace(meta={k: {"ordermin": "0.1", "costmin": "0.5"} for k in KEYS})
prices = {"AAAUSD": 104.6, "BBBUSD": 55.0, "CCCUSD": 10.4, "DDDUSD": 1.0, "EEEUSD": 5.0,
          "FFFUSD": 20.0, "AAAUSDT": 104.6}


def make_bot(tmp, capital="330", max_grids="2", feed=None, rest=None):
    a = gridbot.parse_args(["--data-dir", str(tmp), "--capital", capital, "--max-grids", max_grids,
                            "--no-scanner"])
    b = gridbot.Bot(a, feed=feed or FakeFeed(), book=book, price_fn=lambda k: prices.get(k),
                    trades_fn=rest or (lambda k, s: ([], None)), render=False)
    b.closes_cache = {}            # no correlation data unless a test supplies it
    b.skipped_gen = "pinned"
    return b


# The bot reads the scanner's bar cache for correlation; tests must never see
# the real one on this machine. Empty = "no cache" (allowed, with a note).
TEST_BARS: dict = {}
oracle.load_ohlc_cache = lambda interval, max_age_s=36 * 3600: TEST_BARS
oracle.fetch_ohlc = lambda *a, **k: (None, "offline test")      # never touch the network
gridbot.Bot._refresh_bars = lambda self, key, interval: None

tmp = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
bot = make_bot(tmp)
feed = bot.feed
write_scan(tmp, [
    scan_row("AAA/USD", 100, 110, basket=True),
    scan_row("BBB/USD", 40, 50),                         # price 55 is outside
    scan_row("CCC/USD", 10, 11, qualified=False),        # not qualified
    scan_row("DDD/USD", 0.9, 1.1, basket=False),         # qualified, NOT in the scanner's basket
    scan_row("EEE/USD", 4.5, 5.5),
])
bot.skipped_gen = None
bot.closes_cache = {}
bot.consider()
check("deploys QUALIFIED picks in scanner order, basket flag or not, up to max grids",
      sorted(bot.grids) == ["AAA/USD", "DDD/USD"], sorted(bot.grids))
check("a pick whose price is outside its band is skipped with a reason",
      "outside the band" in bot.skipped.get("BBB/USD", ""), bot.skipped)
check("each grid gets capital / max grids", all(close(g.alloc, 165.0) for g in bot.grids.values()))
check("the feed follows the active grids", feed.symbols == {"AAA/USD", "DDD/USD"})
check("state.json written on deploy", (tmp / "state.json").exists())

write_scan(tmp, [scan_row("AAA/USD", 100, 110)], age_s=4000)
before = dict(bot.grids)
bot.consider()
check("a stale scan deploys nothing and says why", "old" in bot.scan_note and bot.grids == before,
      bot.scan_note)

# resync: REST rows and socket prints that arrived meanwhile, overlapping
g = bot.grids["AAA/USD"]
t0 = g.deployed_at
bot.syncing.add("AAA/USD")
bot.pending["AAA/USD"] = []
bot.on_trade("AAA/USD", 103.5, t0 + 3, 12)          # arrives on the socket mid-resync
bot.on_trade("AAA/USD", 102.5, t0 + 4, 13)
check("socket prints are held while a pair resyncs", g.fills == 0 and len(bot.pending["AAA/USD"]) == 2)
bot.trades_fn = lambda k, s: (([(103.9, t0 + 1, 10), (103.5, t0 + 3, 12)], None)
                              if k == "AAAUSD" else ([], None))
bot.resync()
check("resync replays REST then the held prints once each, in id order",
      g.fills == 2 and g.last_trade_id == 13 and "AAA/USD" not in bot.syncing, (g.fills, g.last_trade_id))

# a reconnect DURING resync keeps the new socket's prints queued for the next pass
g2 = bot.grids["DDD/USD"]
t1 = g2.deployed_at
calls = []


def rest_with_reconnect(k, s):
    if k == "DDDUSD":
        calls.append((k, s))
    if k == "DDDUSD" and len(calls) == 1:
        feed.connects += 1                              # socket dropped and reconnected
        bot.on_trade("DDD/USD", 0.975, t1 + 50, 55)     # the NEW socket's first print
        return [(0.985, t1 + 5, 50)], None
    if k == "DDDUSD":
        return [(0.985, t1 + 5, 50), (0.955, t1 + 30, 53)], None   # the gap, found on the 2nd pass
    return [], None


bot.trades_fn = rest_with_reconnect
bot.resync()
check("a reconnect mid-resync does not dedupe away the gap before it",
      g2.last_trade_id == 55 and g2.fills >= 2, (g2.last_trade_id, g2.fills))

# exit on the wall clock in a quiet market: re-check a stale last print first
g.on_print(95.0, time.time() - 400, 20)
bot.args.exit_after = 300
prices["AAAUSD"] = 104.0                                  # already back inside
bot.check_exits()
check("a stale outside print is re-checked; price back inside resets the timer, no exit",
      "AAA/USD" in bot.grids and bot.grids["AAA/USD"].outside_since is None)
g.on_print(95.0, time.time() - 400, 21)
prices["AAAUSD"] = 95.0
bot.check_exits()
check("still outside on a fresh price: closed at that price and cooled down",
      "AAA/USD" not in bot.grids and bot.cooldown.get("AAA/USD", 0) > time.time()
      and bot.closed and bot.closed[-1]["symbol"] == "AAA/USD"
      and bot.closed[-1]["close_px"] == 95.0)
write_scan(tmp, [scan_row("AAA/USD", 90, 100)])
bot.skipped_gen = None
bot.closes_cache = {}
# this check is about the COOLDOWN: the loss above would otherwise trip the
# book guard first (section 23 covers the guard)
bot.args.max_day_loss_pct = bot.args.max_drawdown_pct = 0.0
# ...and the -12.83 exit shrinks the capital (section 23), leaving no room for
# a second grid beside DDD; lend it that room so the loop reaches the cooldown
bot.capital += 20.0
bot.consider()
bot.capital -= 20.0
check("a cooled-down pair is not redeployed", "AAA/USD" not in bot.grids
      and "cooling down" in bot.skipped.get("AAA/USD", ""), bot.skipped)

snap = bot.write_status()
check("status.json written and readable", json.loads((tmp / "status.json").read_text())["mode"] == "paper")
text = bot.render(snap)
check("console render works", "GRIDBOT" in text and "DDD/USD" in text)
check("no rendered line is wider than 118", max(len(x) for x in text.splitlines()) <= 118,
      max(len(x) for x in text.splitlines()))
bot.save_state()

bot2 = make_bot(tmp)
bot2.load_state()
check("a restart restores open grids, closed history and cooldowns",
      sorted(bot2.grids) == ["DDD/USD"] and bot2.closed and "AAA/USD" in bot2.cooldown)
check("restored grid is identical", bot2.grids["DDD/USD"].to_dict() == bot.grids["DDD/USD"].to_dict())
check("closed totals are restored", bot2.totals["count"] == 1 and close(bot2.totals["realised"],
                                                                       bot.totals["realised"]))
check("restored grids are held as catching up until the first resync",
      bot2.syncing == {"DDD/USD"})

# -----------------------------------------------------------------------------
print("\n11. restart: no exit at a stale price before the catch-up")
tmp2 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp2, capital="165", max_grids="1")
write_scan(tmp2, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b.skipped_gen = None
b.closes_cache = {}
b.consider()
G = b.grids["AAA/USD"]
T = G.deployed_at
b.on_trade("AAA/USD", 99.5, T + 10, 1)                  # below the band; timer starts
b.save_state()
ORIG = (tmp2 / "state.json").read_text()


def restarted():
    (tmp2 / "state.json").write_text(ORIG)
    return make_bot(tmp2, capital="165", max_grids="1")


b3 = restarted()
b3.args.exit_after = 300
b3.clock = lambda: T + 400                              # the bot was down for ~6 minutes
b3.load_state()
b3.check_exits()
check("a restored grid outside its band is NOT closed before the catch-up runs",
      "AAA/USD" in b3.grids)
b3.trades_fn = lambda k, s: ([(100.4, T + 120, 2), (104.9, T + 390, 3)], None)
b3.resync()
b3.check_exits()
G3 = b3.grids.get("AAA/USD")
check("the catch-up shows it recovered: grid still open, timer cleared",
      G3 is not None and G3.outside_since is None and G3.last_px == 104.9)

b4 = restarted()
b4.args.exit_after = 300
b4.load_state()
b4.grids["AAA/USD"].outside_since = None
b4.trades_fn = lambda k, s: ([(99.0, T + 500, 10), (98.5, T + 700, 11), (98.0, T + 820, 12),
                               (104.0, T + 900, 13)], None)
b4.resync()
b4.check_exits()
check("a pierce that outlasted the timer DURING the outage exits at the deadline, at the last "
      "trade before it", "AAA/USD" not in b4.grids and b4.closed[-1]["close_px"] == 98.5
      and b4.closed[-1]["closed_at"] == T + 800, b4.closed[-1] if b4.closed else None)
check("prints after the exit point are not booked", b4.closed[-1]["fills"] == 5,
      b4.closed[-1]["fills"] if b4.closed else None)

b6 = restarted()
b6.args.exit_after = 300
b6.load_state()
b6.grids["AAA/USD"].outside_since = None
# quiet pair: outside at T+500, next print is at T+2000 and back INSIDE the band
b6.trades_fn = lambda k, s: ([(99.0, T + 500, 10), (105.2, T + 2000, 11)], None)
b6.resync()
b6.check_exits()
check("a quiet pair whose timer ran out between prints is still exited (not hidden by the "
      "next print being back inside)",
      "AAA/USD" not in b6.grids and b6.closed and b6.closed[-1]["close_px"] == 99.0
      and b6.closed[-1]["closed_at"] == T + 800, b6.closed[-1] if b6.closed else None)

# a socket that has been silent is not "up": no wall-clock exit until it is heard from
b7 = restarted()
b7.args.exit_after = 300
b7.load_state()
b7.syncing.clear()
g7 = b7.grids["AAA/USD"]
g7.outside_since = time.time() - 1000
g7.last_ts = time.time() - 1000
prices["AAAUSD"] = 95.0
b7.feed.last_msg = time.time() - 120                   # asleep / dead but "connected"
b7.check_exits()
check("no wall-clock exit while the feed has been silent (sleep/wake)", "AAA/USD" in b7.grids)
b7.feed.last_msg = time.time()
b7.check_exits()
check("once the feed is heard from, the wall-clock exit proceeds on a fresh price",
      "AAA/USD" not in b7.grids and b7.closed[-1]["close_px"] == 95.0)

# connect marks every grid syncing synchronously
b8 = make_bot(Path(tempfile.mkdtemp(prefix="gridbot_test_")), capital="165", max_grids="1")
b8.grids = {"AAA/USD": Grid("AAA/USD", "AAAUSD", arith(), "arith", 165, 0.22, 0.38)}
b8.grids["AAA/USD"].deploy(104.6, time.time())
b8.mark_syncing()
b8.on_trade("AAA/USD", 103.5, time.time() + 1, 1)
check("on connect every grid queues prints until its catch-up", b8.grids["AAA/USD"].fills == 0
      and len(b8.pending["AAA/USD"]) == 1)

# -----------------------------------------------------------------------------
print("\n12. capital, correlation and one grid per coin")
tmp3 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp3, capital="330", max_grids="2")
write_scan(tmp3, [scan_row("AAA/USD", 100, 110), scan_row("AAA/USDT", 100, 110),
                  scan_row("EEE/USD", 4.5, 5.5), scan_row("FFF/USD", 18, 22)])
prices.update({"AAAUSD": 104.6, "AAAUSDT": 104.6})
b.skipped_gen = None
closes_a = [100 + (i % 7) for i in range(200)]
b.consider.__func__                                     # (bound method sanity)
T0B = 1790000000
TEST_BARS.update({"AAAUSD": {"rows": [(T0B + i * 1800, 0, 0, 0, c) for i, c in enumerate(closes_a)]},
                  "EEEUSD": {"rows": [(T0B + i * 1800, 0, 0, 0, c * 0.05) for i, c in enumerate(closes_a)]},
                  "FFFUSD": {"rows": [(T0B + i * 1800, 0, 0, 0, 20 + ((i * 3) % 11)) for i in range(200)]}})
b.closes_cache = None
b.skipped_gen = b.scan and b.scan.get("generated")
b.scan = None
blob, _ = gridbot.load_scan(tmp3 / "scan.json")
b.skipped_gen = blob["generated"]                       # keep the injected cache for this scan
b.consider()
check("a second grid on the same coin (AAA/USDT) is refused",
      "already running a AAA grid" in b.skipped.get("AAA/USDT", ""), b.skipped)
check("a pick that moves with an open grid is refused (EEE tracks AAA exactly)",
      "moves with an open grid" in b.skipped.get("EEE/USD", ""), b.skipped)
check("an uncorrelated pick is deployed", sorted(b.grids) == ["AAA/USD", "FFF/USD"], sorted(b.grids))
b.save_state()
b5 = make_bot(tmp3, capital="330", max_grids="4")        # restarted with more grids, same money
b5.load_state()
write_scan(tmp3, [scan_row("EEE/USD", 4.5, 5.5)])
b5.skipped_gen = None
b5.consider()
check("a restart with a bigger --max-grids never commits more than the capital",
      sum(g.alloc for g in b5.grids.values()) <= 330 + 1e-9, sum(g.alloc for g in b5.grids.values()))

# -----------------------------------------------------------------------------
b.cooldown["FFF/USD"] = time.time() + 3600
b.grids.pop("FFF/USD", None)
write_scan(tmp3, [scan_row("FFF/USDT", 18, 22)])
b.skipped_gen = None
b.consider()
check("the cooldown covers the coin, not just the pair (FFF/USDT after FFF/USD)",
      "cooling down (FFF)" in b.skipped.get("FFF/USDT", ""), b.skipped)

b.feed.connected = False
b.gaps = 7
b.scan_note = "scan is 3h12m old -- no new grids until it refreshes " * 3
text = b.render(b.snapshot())
check("every rendered line fits 118 columns even during an outage",
      max(len(x) for x in text.splitlines()) <= 118, max(len(x) for x in text.splitlines()))

# -----------------------------------------------------------------------------
print("\n13. records")
lines = [json.loads(x) for x in (tmp / "journal.jsonl").read_text().splitlines()]
fills = [x for x in lines if x["event"] == "FILL"]
check("journal FILL rows keep the bot's ISO ts and carry the exchange time as exch_ts",
      fills and all(isinstance(x["ts"], str) and "exch_ts" in x for x in fills))
check("DEPLOY and EXIT rows are journalled",
      any(x["event"] == "DEPLOY" for x in lines) and any(x["event"] == "EXIT" for x in lines))
check("state.json.bak keeps the previous save", (tmp / "state.json.bak").exists())

(tmp / "state.json").write_text("{not json")
bot3 = make_bot(tmp)
bot3.load_state()
check("an unreadable state file is moved aside and the backup restored",
      any(tmp.glob("state.json.unusable.*")) and "DDD/USD" in bot3.grids)

lock1 = gridbot.acquire_single_instance(tmp / "t.lock")
lock2 = gridbot.acquire_single_instance(tmp / "t.lock")
check("a second instance cannot take the lock", lock1 is not None and lock2 is None)
lock1.close()

# -----------------------------------------------------------------------------
print("\n14. round-3 hardening")
# a state file that parses but cannot be restored goes aside; the backup is used
tmp4 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp4, capital="165", max_grids="1")
write_scan(tmp4, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b.skipped_gen = None
b.consider()
b.save_state()
b.save_state()                                            # .bak now holds a good copy
good = json.loads((tmp4 / "state.json").read_text())
bad = dict(good)
bad["closed_totals"] = {"count": "not-a-number"}
(tmp4 / "state.json").write_text(json.dumps(bad))
b9 = make_bot(tmp4, capital="165", max_grids="1")
b9.load_state()
check("valid JSON that cannot be restored is set aside and the backup restored",
      "AAA/USD" in b9.grids and any(tmp4.glob("state.json.unusable.*")) and b9._state_loaded)

# if loading itself blows up, the final save must not overwrite state.json/.bak
(tmp4 / "state.json").write_text(json.dumps(good))
before_state = (tmp4 / "state.json").read_text()
before_bak = (tmp4 / "state.json.bak").read_text()
b10 = make_bot(tmp4, capital="165", max_grids="1")


def boom():
    raise RuntimeError("simulated load failure")


b10.load_state = boom
try:
    b10.run()
except RuntimeError:
    pass
check("a failed load never saves emptiness over state.json or its backup",
      (tmp4 / "state.json").read_text() == before_state
      and (tmp4 / "state.json.bak").read_text() == before_bak)

# reconnect storms coalesce into one resync worker plus at most one follow-up pass
b11 = make_bot(tmp4, capital="165", max_grids="1")
b11.load_state()
runs = []


def slow_rest(k, s_):
    runs.append(time.time())
    time.sleep(0.2)
    return [], None


b11.trades_fn = slow_rest
for _ in range(6):
    b11.request_resync()
t_end = time.time() + 5
while b11._rs_running and time.time() < t_end:
    time.sleep(0.05)
check("six reconnects in a burst run at most two catch-up passes", 1 <= len(runs) <= 2, len(runs))

# the wall-clock exit never fires for a grid that is catching up
b12 = make_bot(tmp4, capital="165", max_grids="1")
b12.load_state()
g12 = b12.grids["AAA/USD"]
g12.outside_since = time.time() - 1000
g12.last_px = 95.0
b12.args.exit_after = 300
check("close_grid refuses a timer exit while the pair is catching up",
      b12.close_grid("AAA/USD", None, 95.0, time.time(), timer_exit=True) is None
      and "AAA/USD" in b12.grids)

# -----------------------------------------------------------------------------
print("\n15. round-4 hardening")
tmp5 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp5, capital="165", max_grids="1")
write_scan(tmp5, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b.skipped_gen = None
b.consider()
b.save_state()
b.save_state()
good_bak = (tmp5 / "state.json.bak").read_text()
(tmp5 / "state.json").write_text("{corrupt")
b13 = make_bot(tmp5, capital="165", max_grids="1")
b13.load_state()
check("a state recovered from .bak is written back to state.json at once",
      json.loads((tmp5 / "state.json").read_text())["grids"].get("AAA/USD") is not None)
check("the good .bak is not overwritten by the corrupt file it replaced",
      "{corrupt" not in (tmp5 / "state.json.bak").read_text())

# both unusable: start empty, totals reset, the bad .bak set aside (not overwritten)
(tmp5 / "state.json").write_text("{corrupt")
(tmp5 / "state.json.bak").write_text(json.dumps({"closed_totals": {"count": 7, "realised": 5.0},
                                                  "grids": {"X": {"bad": 1}}, "cooldown": {"A": "x"}}))
b14 = make_bot(tmp5, capital="165", max_grids="1")
b14.load_state()
check("when the backup is unusable too the bot starts empty with zero totals",
      b14.grids == {} and b14.totals["count"] == 0 and b14.totals["realised"] == 0.0)
check("an unusable backup is set aside, not left to be overwritten",
      any(tmp5.glob("state.json.bak.unusable.*")))

# gap label: exact only when the outside streak spans an unknown stretch
tmp6 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp6, capital="165", max_grids="1")
write_scan(tmp6, [scan_row("AAA/USD", 100, 110)])
b.skipped_gen = None
b.consider()
G = b.grids["AAA/USD"]
T = G.deployed_at
b.args.exit_after = 300
b.on_trade("AAA/USD", 99.5, T + 10, 1)
b.syncing.add("AAA/USD")
b.pending["AAA/USD"] = []
b.trades_fn = lambda k, s_: ([], "EService:Unavailable")
b.resync()
check("a failed catch-up marks where the unknown stretch starts", G.gap_at == T + 10, G.gap_at)
b.on_trade("AAA/USD", 104.9, T + 900, 2)                  # first live print after the gap
b.check_exits()
check("an exit decided across an unknown stretch is labelled as an estimate",
      b.closed and "estimated" in b.closed[-1]["close_reason"], b.closed[-1] if b.closed else None)

b = make_bot(Path(tempfile.mkdtemp(prefix="gridbot_test_")), capital="165", max_grids="1")
write_scan(b.data, [scan_row("AAA/USD", 100, 110)])
b.skipped_gen = None
b.consider()
G = b.grids["AAA/USD"]
T = G.deployed_at
b.args.exit_after = 300
b.syncing.add("AAA/USD")
b.pending["AAA/USD"] = []
b.trades_fn = lambda k, s_: ([(99.5, T + 10, 1), (99.2, T + 320, 2)], None)
b.resync()
b.check_exits()
check("an exact exit from a complete catch-up carries no estimate label",
      b.closed and "estimated" not in b.closed[-1]["close_reason"], b.closed[-1] if b.closed else None)

# -----------------------------------------------------------------------------
print("\n16. round-5 hardening")
# a state.json that exists but cannot be OPENED is never treated as corrupt
tmp7 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp7, capital="165", max_grids="1")
write_scan(tmp7, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b.skipped_gen = None
b.consider()
b.save_state()
b.save_state()
state_before = (tmp7 / "state.json").read_text()
bak_before = (tmp7 / "state.json.bak").read_text()
b15 = make_bot(tmp7, capital="165", max_grids="1")
real_read = gridbot.Bot._read_json
calls = []


def locked_read(path):
    calls.append(path)
    if path.name == "state.json":
        raise PermissionError(13, "Permission denied (simulated lock)")
    return real_read(path)


b15._read_json = locked_read
gridbot.time_sleep_backup = gridbot.time.sleep
gridbot.time.sleep = lambda s_: None                      # skip the retry waits in the test
try:
    b15.load_state()
    raised = False
except RuntimeError:
    raised = True
finally:
    gridbot.time.sleep = gridbot.time_sleep_backup
check("a locked state.json stops the load instead of rolling back or starting empty",
      raised and not b15._state_loaded and b15.grids == {})
check("...and neither file is touched",
      (tmp7 / "state.json").read_text() == state_before
      and (tmp7 / "state.json.bak").read_text() == bak_before
      and not any(tmp7.glob("*.unusable.*")))

# recovery keeps state.json in place until the recovered state replaces it
(tmp7 / "state.json").write_text("{corrupt")
b16 = make_bot(tmp7, capital="165", max_grids="1")
b16.load_state()
check("recovery copies the bad file aside and replaces state.json with the recovered state",
      any(tmp7.glob("state.json.unusable.*"))
      and "AAA/USD" in json.loads((tmp7 / "state.json").read_text())["grids"])

# gap marker keeps the earliest stretch and labels a pierce that began inside it
g = Grid("AAA/USD", "AAAUSD", arith(), "arith", 165, 0.22, 0.38)
g.deploy(104.6, 1000.0)
g.gap_at, g.gap_end = 1010.0, None
g.outside_since = 1500.0                                  # (set by a print after the gap)
g.gap_end = 1500.0
check("a pierce that begins at the first print after an unknown stretch is labelled",
      "estimated" in gridbot.Bot._gap_label(g))
g.outside_since = 1600.0
check("a pierce that begins after that print is not", gridbot.Bot._gap_label(g) == "")

# -----------------------------------------------------------------------------
print("\n17. round-6 hardening")
# state.json bad AND .bak locked: stop, touch nothing (never "starting empty")
(tmp7 / "state.json").write_text("{corrupt")
bak_before = (tmp7 / "state.json.bak").read_text()
b17 = make_bot(tmp7, capital="165", max_grids="1")


def bak_locked_read(path):
    if path.name == "state.json.bak":
        raise PermissionError(13, "Permission denied (simulated lock)")
    return real_read(path)


b17._read_json = bak_locked_read
kept = []
real_keep = b17._keep_copy
b17._keep_copy = lambda p_, t_: kept.append(p_.name) or real_keep(p_, t_)
saved_sleep = gridbot.time.sleep
gridbot.time.sleep = lambda s_: None
asides_before = set(tmp7.glob("*.unusable.*"))
try:
    b17.load_state()
    raised = False
except RuntimeError:
    raised = True
finally:
    gridbot.time.sleep = saved_sleep
check("a locked backup behind a bad state.json stops the load; nothing starts empty",
      raised and not b17._state_loaded and b17.grids == {})
check("...and the good backup is untouched, with no copy of anything made",
      (tmp7 / "state.json.bak").read_text() == bak_before and kept == [], kept)

# -----------------------------------------------------------------------------
print("\n18. round-7 hardening")
# a grid this code cannot rebuild (e.g. after a state-format change) is never lost silently
tmp8 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp8, capital="330", max_grids="2")
write_scan(tmp8, [scan_row("AAA/USD", 100, 110), scan_row("FFF/USD", 18, 22)])
prices.update({"AAAUSD": 104.6, "FFFUSD": 20.0})
b.skipped_gen = None
b.consider()
b.save_state()
st = json.loads((tmp8 / "state.json").read_text())
del st["grids"]["FFF/USD"]["maker_pct"]                    # a field this code needs
(tmp8 / "state.json").write_text(json.dumps(st))
b18 = make_bot(tmp8, capital="330", max_grids="2")
b18.load_state()
partial = list(tmp8.glob("state.json.partial.*"))
check("a grid that cannot be rebuilt is named in an ERROR event",
      any("FFF/USD" in e and "could not restore" in e for e in b18.events), list(b18.events))
check("...and the whole state file is kept before any save",
      partial and "FFF/USD" in json.loads(partial[0].read_text())["grids"])
check("...while the grids that could be rebuilt still load", sorted(b18.grids) == ["AAA/USD"])

check("an unrestorable grid is carried verbatim in the next save and keeps its slot",
      True)
b18.save_state()
st2 = json.loads((tmp8 / "state.json").read_text())
check("...it is written back verbatim under grids (a fixed version can restore it)",
      "FFF/USD" in st2["grids"] and "maker_pct" not in st2["grids"]["FFF/USD"])
write_scan(tmp8, [scan_row("FFF/USD", 18, 22), scan_row("CCC/USD", 10, 11)])
prices["CCCUSD"] = 10.4
b18.skipped_gen = None
b18.consider()
check("...and its slot is held: with AAA + the held FFF the 2 slots are full, nothing deploys",
      sorted(b18.grids) == ["AAA/USD"] and "FFF/USD" in b18.unrestored, sorted(b18.grids))
b18b = make_bot(tmp8, capital="495", max_grids="3")
b18b.load_state()
write_scan(tmp8, [scan_row("FFF/USD", 18, 22)])
b18b.skipped_gen = None
b18b.consider()
check("...and with a free slot the same coin is still not redeployed over it",
      "FFF/USD" not in b18b.grids and "already running a FFF grid" in b18b.skipped.get("FFF/USD", ""),
      b18b.skipped)

# -----------------------------------------------------------------------------
print("\n19. round-8 hardening")
tmp9 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp9, capital="330", max_grids="2")
write_scan(tmp9, [scan_row("AAA/USD", 100, 110), scan_row("FFF/USD", 18, 22)])
prices.update({"AAAUSD": 104.6, "FFFUSD": 20.0})
b.skipped_gen = None
b.consider()
b.save_state()
st = json.loads((tmp9 / "state.json").read_text())
del st["grids"]["FFF/USD"]["status"]
st["grids"]["AAA/USD"]["empty"] = None                  # a broken ladder
(tmp9 / "state.json").write_text(json.dumps(st))
b19 = make_bot(tmp9, capital="330", max_grids="2")
b19.load_state()
check("a grid with no status is rejected, named and kept (not silently skipped)",
      any("FFF/USD" in e and "missing status" in e for e in b19.events), list(b19.events))
check("a grid whose saved ladder is inconsistent is rejected up front, not half-loaded",
      "AAA/USD" not in b19.grids and any("AAA/USD" in e and "empty level" in e for e in b19.events))
check("both are carried and hold their slots", sorted(b19.unrestored) == ["AAA/USD", "FFF/USD"])

# the stop fence, driven through run(): a print that arrives while the feed
# is being stopped (after Ctrl+C) must not be booked after the final save
tmp10 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b20 = make_bot(tmp10, capital="165", max_grids="1")
write_scan(tmp10, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b20.skipped_gen = None
b20.consider()
b20.save_state()
b20.syncing.clear()


class StopFeed(FakeFeed):
    ticks = 0

    def check_stale(self):
        StopFeed.ticks += 1
        if StopFeed.ticks >= 2:
            raise KeyboardInterrupt

    def stop(self):                                   # a late print races the shutdown
        G = b21.grids.get("AAA/USD")
        if G is not None:
            b21.on_trade("AAA/USD", 101.5, G.deployed_at + 99, 999)


b21 = make_bot(tmp10, capital="165", max_grids="1", feed=StopFeed())
b21.render_console = False
real_consider = b21.consider
b21.consider = lambda: None
saved_sleep2 = gridbot.time.sleep
gridbot.time.sleep = lambda s_: None
try:
    rc = b21.run()
finally:
    gridbot.time.sleep = saved_sleep2
saved = json.loads((tmp10 / "state.json").read_text())["grids"]["AAA/USD"]
journal_rows = [json.loads(x) for x in (tmp10 / "journal.jsonl").read_text().splitlines()]
stop_i = max(i for i, x in enumerate(journal_rows) if x["event"] == "STOP")
check("run() ends on Ctrl+C with code 0", rc == 0, rc)
check("a print racing the shutdown is not booked (fence before feed.stop)",
      b21.grids["AAA/USD"].fills == 0 and saved["fills"] == 0, (b21.grids["AAA/USD"].fills, saved["fills"]))
check("nothing is journalled after STOP", all(x["event"] != "FILL" for x in journal_rows[stop_i:]))


# -----------------------------------------------------------------------------
print("\n20. round-9 hardening")
tmp11 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp11, capital="330", max_grids="2")
write_scan(tmp11, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b.skipped_gen = None
b.consider()
b.save_state()
st = json.loads((tmp11 / "state.json").read_text())
st["grids"]["AAA/USD"]["alloc"] = {"usd": 165.0}          # a shape this code cannot read
(tmp11 / "state.json").write_text(json.dumps(st))
b22 = make_bot(tmp11, capital="330", max_grids="2")
b22.load_state()
write_scan(tmp11, [scan_row("FFF/USD", 18, 22)])
prices["FFFUSD"] = 20.0
b22.skipped_gen = None
try:
    b22.consider()
    ok = True
except Exception as e:
    ok = False
check("an unreadable held allocation never crashes the loop (it holds one full slot)", ok)
snap = b22.snapshot()
check("status shows the held slot and its capital",
      snap["grids_held"] == 1 and close(snap["open"]["held"], 165.0), (snap["grids_held"], snap["open"]))
b23 = make_bot(tmp11, capital="330", max_grids="2")
b23.load_state()
check("restarting on the same unreadable file keeps ONE copy, not one per restart",
      len(list(tmp11.glob("state.json.partial.*"))) == 1, list(tmp11.glob("state.json.partial.*")))

# a close interrupted after g.close() but before the grid left the book
tmp12 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp12, capital="165", max_grids="1")
write_scan(tmp12, [scan_row("AAA/USD", 100, 110)])
b.skipped_gen = None
b.consider()
G = b.grids["AAA/USD"]
G.on_print(99.0, G.deployed_at + 5, 1)
G.close(99.0, G.deployed_at + 400, "price below band for 5m00s")
b.save_state()                                            # saved with status CLOSED
b24 = make_bot(tmp12, capital="165", max_grids="1")
b24.load_state()
check("an interrupted close is finished on restart: P/L in the totals, cooldown set",
      "AAA/USD" not in b24.grids and b24.totals["count"] == 1
      and close(b24.totals["realised"], G.realised) and b24.cooldown.get("AAA/USD", 0) > 0,
      (b24.totals, b24.cooldown))
rows = [json.loads(x) for x in (tmp12 / "journal.jsonl").read_text().splitlines()]
check("...and journalled as a recovered EXIT",
      any(x["event"] == "EXIT" and x.get("recovered") for x in rows))


# -----------------------------------------------------------------------------
print("\n21. round-10 hardening")
import signal as _signal
# Ctrl+C is a request, acted on between loop iterations -- never mid-close
tmp13 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b = make_bot(tmp13, capital="165", max_grids="1")
write_scan(tmp13, [scan_row("AAA/USD", 100, 110)])
b.skipped_gen = None
b.consider()
b.save_state()


class SigFeed(FakeFeed):
    n = 0

    def check_stale(self):
        SigFeed.n += 1
        if SigFeed.n == 2:
            _signal.raise_signal(_signal.SIGINT)          # a real Ctrl+C
        if SigFeed.n > 50:
            raise RuntimeError("loop did not stop on SIGINT")


b25 = make_bot(tmp13, capital="165", max_grids="1", feed=SigFeed())
b25.consider = lambda: None
saved_sleep3 = gridbot.time.sleep
gridbot.time.sleep = lambda s_: None
try:
    rc = b25.run()
finally:
    gridbot.time.sleep = saved_sleep3
check("a real SIGINT stops the loop cleanly between iterations (rc 0)", rc == 0 and SigFeed.n <= 3,
      (rc, SigFeed.n))
check("the previous Ctrl+C handler is restored afterwards",
      _signal.getsignal(_signal.SIGINT) is _signal.default_int_handler)

# dedupe holds across a real save + restart (saved_at changes every save)
b22.save_state()
b26 = make_bot(tmp11, capital="330", max_grids="2")
b26.load_state()
check("across a real save and restart the unrestorable grid is still copied only once",
      len(list(tmp11.glob("state.json.partial.*"))) == 1, list(tmp11.glob("state.json.partial.*")))

# a recovered close is journalled once, after its state is saved
b27 = make_bot(tmp12, capital="165", max_grids="1")
b27.load_state()
rows = [json.loads(x) for x in (tmp12 / "journal.jsonl").read_text().splitlines()]
check("a recovered close is journalled exactly once across restarts",
      sum(1 for x in rows if x["event"] == "EXIT" and x.get("recovered")) == 1)


# -----------------------------------------------------------------------------
print("\n22. round-11 hardening")
# after a stop request no new Kraken call, deploy or wall exit starts
tmp14 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
calls14 = []
b28 = make_bot(tmp14, capital="330", max_grids="2")
b28.price_fn = lambda k: (calls14.append(k), prices.get(k))[1]
write_scan(tmp14, [scan_row("AAA/USD", 100, 110), scan_row("FFF/USD", 18, 22)])
prices.update({"AAAUSD": 104.6, "FFFUSD": 20.0})
b28.skipped_gen = None
b28._stop_requested = True
b28.consider()
check("after Ctrl+C consider() starts no Kraken call and no deploy", calls14 == [] and b28.grids == {})
b28._stop_requested = False
b28.consider()
G28 = b28.grids["AAA/USD"]
G28.outside_since = time.time() - 1000
G28.last_ts = time.time() - 1000
b28.syncing.clear()
b28.args.exit_after = 300
calls14.clear()
b28._stop_requested = True
b28.check_exits()
check("after Ctrl+C no wall-clock exit (which needs a REST price) starts",
      calls14 == [] and "AAA/USD" in b28.grids)

# -----------------------------------------------------------------------------
print("\n23. audit 2026-09-23: capital tracks losses, book guard, stop file, scanner")
today = datetime.now().strftime("%Y-%m-%d")
tmp15 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b30 = make_bot(tmp15, capital="330", max_grids="2")
b30.totals["realised"] = -30.0
check("a closed loss shrinks the next grid (330-30)/2 = 150",
      close(b30.grid_alloc(), 150.0) and close(b30.capital_now(), 300.0), b30.grid_alloc())
b30.totals["realised"] = 30.0
check("a closed gain does not grow it past the base share",
      close(b30.grid_alloc(), 165.0) and close(b30.capital_now(), 330.0), b30.grid_alloc())

b31 = make_bot(tmp15, capital="330", max_grids="2")
b31.args.max_day_loss_pct = b31.args.max_drawdown_pct = 0.0
b31.totals["realised"] = -30.0
write_scan(tmp15, [scan_row("AAA/USD", 100, 110)])
prices["AAAUSD"] = 104.6
b31.skipped_gen = None
b31.consider()
check("the deploy uses the shrunk allocation",
      "AAA/USD" in b31.grids and close(b31.grids["AAA/USD"].alloc, 150.0),
      {s: g.alloc for s, g in b31.grids.items()})

tmp16 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b32 = make_bot(tmp16, capital="330", max_grids="2")      # defaults: day 6%, drawdown 12%
b32.guard = {"peak": 330.0, "day": today, "day_start": 330.0}
b32.totals["realised"] = -25.0                            # 7.6% of 330 today
write_scan(tmp16, [scan_row("AAA/USD", 100, 110)])
b32.skipped_gen = None
b32.consider()
rows32 = [json.loads(x) for x in (tmp16 / "journal.jsonl").read_text().splitlines()]
check("a day loss over the limit halts new grids and journals HALT once",
      b32.grids == {} and b32.halt_reason and "today" in b32.halt_reason
      and sum(1 for x in rows32 if x["event"] == "HALT") == 1, b32.halt_reason)
b32.consider()
rows32 = [json.loads(x) for x in (tmp16 / "journal.jsonl").read_text().splitlines()]
check("a halt that holds is not journalled again",
      sum(1 for x in rows32 if x["event"] == "HALT") == 1)
check("status.json carries the halt, capital_now, alloc_next and ts_epoch",
      (lambda s_: s_["halt"] == b32.halt_reason and close(s_["capital_now"], 305.0)
       and close(s_["alloc_next"], 152.5) and s_["ts_epoch"] > 0)(b32.write_status()))
b32.save_state()
b33 = make_bot(tmp16, capital="330", max_grids="2")
b33.load_state()
check("the guard's peak and day start survive a restart",
      b33.guard.get("peak") == 330.0 and b33.guard.get("day_start") == 330.0, b33.guard)
b32.totals["realised"] = 0.0
b32.consider()
rows32 = [json.loads(x) for x in (tmp16 / "journal.jsonl").read_text().splitlines()]
check("the halt lifts when the book is back inside, journalled as RESUME, and deploys again",
      b32.halt_reason is None and any(x["event"] == "RESUME" for x in rows32)
      and "AAA/USD" in b32.grids)

b34 = make_bot(tmp16, capital="330", max_grids="2")
b34.args.max_day_loss_pct = 0.0
b34.guard = {"peak": 400.0, "day": today, "day_start": 330.0}   # equity 330 is 17.5% below
check("a drawdown from the peak over the limit halts", bool(b34.update_guard())
      and "peak" in b34.halt_reason, b34.halt_reason)
b35 = make_bot(tmp16, capital="330", max_grids="2")
b35.guard = {"peak": 330.0, "day": "2000-01-01", "day_start": 200.0}
b35.update_guard()
check("a new day resets the day start (yesterday's loss does not halt today)",
      b35.guard["day"] == today and close(b35.guard["day_start"], 330.0) and b35.halt_reason is None,
      b35.guard)
try:
    gridbot.parse_args(["--max-day-loss-pct", "150"])
    ok = False
except SystemExit:
    ok = True
check("a guard limit of 100% or more is refused", ok)


class TickFeed(FakeFeed):
    ticks = 0
    hook = None

    def check_stale(self):
        TickFeed.ticks += 1
        if TickFeed.hook:
            TickFeed.hook(TickFeed.ticks)
        if TickFeed.ticks > 50:                     # safety net: never hang the suite
            raise KeyboardInterrupt


tmp17 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
(tmp17 / "stop.request").write_text("stale")       # left behind by a forced stop
b36 = make_bot(tmp17, capital="330", max_grids="2", feed=TickFeed())
b36.consider = lambda: None
TickFeed.ticks = 0
TickFeed.hook = lambda n: (tmp17 / "stop.request").write_text("stop") if n == 3 else None
saved_sleep3 = gridbot.time.sleep
gridbot.time.sleep = lambda s_: None
try:
    rc36 = b36.run()
finally:
    gridbot.time.sleep = saved_sleep3
    TickFeed.hook = None
rows36 = [json.loads(x) for x in (tmp17 / "journal.jsonl").read_text().splitlines()]
check("a stale stop.request from before the start is ignored (the run got past it)",
      TickFeed.ticks == 3, TickFeed.ticks)
check("stop.request stops the bot cleanly: code 0, a STOP row, the file removed",
      rc36 == 0 and rows36[-1]["event"] == "STOP" and not (tmp17 / "stop.request").exists(),
      (rc36, rows36[-1]["event"]))

sc_args = gridbot.parse_args(["--data-dir", str(tmp17), "--scan-refresh", "120"])
sc = gridbot.Scanner(tmp17, tmp17 / "scan.json", sc_args)
write_scan(tmp17, [])
now_ = time.time()
sc.last_start = now_ - 5000
check("scanner: a fresh scan.json is not a hang", not sc.hung(now_))
os.utime(tmp17 / "scan.json", (now_ - 700, now_ - 700))
check("scanner: scan.json 700 s old (limit 600) with an old start is a hang", sc.hung(now_))
sc.last_start = now_ - 60
check("scanner: a scanner started a minute ago is not a hang yet", not sc.hung(now_))
check("scanner: its output is unbuffered", 'PYTHONUNBUFFERED="1"' in Path(gridbot.__file__).read_text(encoding="utf-8"))

# -----------------------------------------------------------------------------
print("\n24. fee tier from a query-only Kraken key (offline: the fetch is faked)")
TIER = {"volume": "22807.5", "fees": {"XXBTZUSD": {"fee": "0.3500"}},
        "fees_maker": {"XXBTZUSD": {"fee": "0.2000"}}}
FAKE_CREDS = ("k" * 56, "c2VjcmV0")
fa = gridbot.parse_args([])
check("no fee flags: defaults 0.22/0.38 until the tier is read",
      fa.fee == 0.22 and fa.taker == 0.38 and not fa.fee_given and not fa.taker_given)
note = gridbot.resolve_fees(fa, FAKE_CREDS, fetch=lambda c: TIER)
check("the account's tier replaces the defaults, and says where it came from",
      fa.fee == 0.20 and fa.taker == 0.35 and "Kraken tier" in fa.fee_source and "22,808" in note,
      (fa.fee, fa.taker, note))
fb = gridbot.parse_args(["--fee", "0.30"])
gridbot.resolve_fees(fb, FAKE_CREDS, fetch=lambda c: TIER)
check("a --fee given by hand wins; the taker still comes from the tier",
      fb.fee == 0.30 and fb.taker == 0.35, (fb.fee, fb.taker))
fc = gridbot.parse_args(["--fee", "0.30", "--taker", "0.50"])
called = []
gridbot.resolve_fees(fc, FAKE_CREDS, fetch=lambda c: (called.append(1), TIER)[1])
check("both fees given by hand: Kraken is not even asked", called == [] and fc.fee == 0.30 and fc.taker == 0.50)


def boom_fetch(c):
    raise RuntimeError("EAPI:Invalid key")


fd = gridbot.parse_args([])
note = gridbot.resolve_fees(fd, FAKE_CREDS, fetch=boom_fetch)
check("a failed lookup keeps the defaults and says why (no crash)",
      fd.fee == 0.22 and fd.taker == 0.38 and "failed" in note and "Invalid key" in note, note)
fe = gridbot.parse_args([])
note = gridbot.resolve_fees(fe, None, fetch=boom_fetch)
check("no key configured: defaults, and the note says so", fe.fee == 0.22 and "no GRIDBOT_KRAKEN_KEY" in note, note)
ff = gridbot.parse_args([])
gridbot.resolve_fees(ff, FAKE_CREDS, fetch=lambda c: {"volume": "1", "fees": {"X": {"fee": "38"}},
                                                      "fees_maker": {"X": {"fee": "22"}}})
check("an implausible tier (38%) is refused, defaults kept", ff.fee == 0.22 and ff.taker == 0.38, (ff.fee, ff.taker))
tmp18 = Path(tempfile.mkdtemp(prefix="gridbot_test_"))
b37 = make_bot(tmp18)
gridbot.resolve_fees(b37.args, FAKE_CREDS, fetch=lambda c: TIER)
st37 = b37.write_status()
check("status.json shows the fees in use and their source",
      st37["fees"]["maker"] == 0.20 and st37["fees"]["taker"] == 0.35
      and "Kraken tier" in st37["fees"]["source"], st37["fees"])
check("the key never lands in status.json", "k" * 56 not in (tmp18 / "status.json").read_text())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
