#!/usr/bin/env python3
"""GridBot -- a paper spot-grid bot driven by the GridPick Oracle v3.1 scanner.

    run.bat                      (or:  python -X utf8 gridbot.py)

One process runs everything. It starts oracle.py (the v3.1 scanner, unchanged)
as a hidden child that rewrites scan.json every --scan-refresh seconds, deploys
grids on the scanner's qualified picks, fills them from Kraken's public trade
stream, and keeps state.json, journal.jsonl, status.json and gridbot.log in
this folder.

PAPER ONLY. No order is ever sent. The one private call is Kraken's read-only
TradeVolume, made at start with a QUERY-ONLY key (user environment variables
GRIDBOT_KRAKEN_KEY / GRIDBOT_KRAKEN_SECRET, never stored in this folder) to
use the account's real fee tier. No key, or any failure: the defaults stand.

THE GRID -- a standard spot grid, which is what the scanner's simulate_grid
models (inventory counted from a neutral start: sells above, buys below).
  * Ladder: oracle.make_levels(lo, hi, levels, kind) -- the scanner's own
    function, so the bot trades exactly the levels the scanner replayed.
  * Deploy at price P: the level nearest P is left empty. Every level below
    it rests a BUY, every level above it rests a SELL. The coins those SELLs
    need are bought at P when the grid starts, paying the taker fee.
  * Rung: allocation / levels, in USD. A BUY at L buys rung/L coins; the SELL
    one level up sells exactly those coins.
  * A BUY filling at L[j] rests a SELL of the same coins at L[j+1]; a SELL
    filling at L[j] rests a BUY at L[j-1]. Exactly one level is ever empty --
    the last one that filled -- so every fill needs a full step of movement.
  * A resting BUY at L fills when a trade prints BELOW L; a SELL fills when a
    trade prints ABOVE L (trade-through, never touch: no queue optimism).
    Both book at L, never at the print. One print can fill several levels.
  * Fees: maker (the scanner's fee_pct, --fee) on every grid fill; taker
    (--taker) on the deploy buy and on the exit sale.

EXIT: price outside [lo, hi] for --exit-after seconds (default 300): sell all
coins at the last print (taker), free the capital, and keep that pair out for
--cooldown minutes. --exit-after 0 holds grids through any break.

WHAT THE SCANNER SAYS vs WHAT THIS DOES. simulate_grid counts every level
crossing as a fill worth half a round trip. A real grid cannot fill when price
turns back through the level it just filled at -- that order has moved one
level away -- so on choppy paths the scanner counts fills no grid can get and
its yield figure runs high. test_gridbot.py pins exactly where the two agree
(one-directional moves) and where they differ (one extra fill per reversal).
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import oracle  # noqa: E402  -- the user's v3.1 scanner, unchanged

VERSION = "1.0"
BUY, SELL = "BUY", "SELL"
EXIT_ALREADY_RUNNING = 3
# skip reasons that depend on which grids are open right now; re-derived every pass
DYNAMIC_REASONS = ("already running", "cooling down", "moves with", "correlation")

log = logging.getLogger("gridbot")


# =============================================================================
# GRID ENGINE -- pure, no I/O. Everything the bot books goes through here.
# =============================================================================
class Grid:
    """One paper spot grid. Prices in quote (USD), quantities in coins."""

    def __init__(self, symbol: str, key: str, levels: list[float], kind: str,
                 alloc: float, maker_pct: float, taker_pct: float,
                 meta: dict | None = None):
        levels = [float(x) for x in levels]
        if len(levels) < 2 or any(b <= a for a, b in zip(levels, levels[1:])):
            raise ValueError("levels must be two or more, strictly increasing")
        if levels[0] <= 0:
            raise ValueError("levels must be positive")
        if alloc <= 0:
            raise ValueError("allocation must be positive")
        self.symbol = symbol
        self.key = key
        self.kind = kind
        self.levels = levels
        self.n = len(levels)
        self.alloc = float(alloc)
        self.rung = self.alloc / self.n
        self.maker = float(maker_pct) / 100.0
        self.taker = float(taker_pct) / 100.0
        self.meta = dict(meta or {})
        # orders[j] is None (the one empty level, or everything once closed),
        # {"side": BUY, "qty"} below the empty level, or
        # {"side": SELL, "qty", "entry", "entry_fee"} above it.
        self.orders: list[dict | None] = [None] * self.n
        self.empty: int | None = None
        self.cash = self.alloc
        self.base = 0.0
        self.realised = 0.0
        self.fees = 0.0
        self.fills = 0
        self.round_trips = 0
        self.net_fills = 0          # buys minus sells since deploy, in rungs
        self.status = "NEW"
        self.deployed_at: float | None = None
        self.deploy_px: float | None = None
        self.last_px: float | None = None
        self.last_ts: float | None = None
        self.last_trade_id: int | None = None
        self.outside_since: float | None = None
        # (price, ts, reason) once the exit timer has run out IN PRINT TIME --
        # set by the bot while replaying prints, so a catch-up after an outage
        # exits where the market actually was when the timer expired.
        self.exit_due: list | None = None
        # Time of the last print known before a stretch whose prints could not
        # be fetched (a failed catch-up). An exit whose outside streak spans it
        # is an estimate and is labelled so. Cleared once the streak ends.
        self.gap_at: float | None = None
        self.gap_end: float | None = None    # first print seen after that stretch
        self.closed_at: float | None = None
        self.close_px: float | None = None
        self.close_reason: str | None = None

    # ------------------------------------------------------------- lifecycle --
    @property
    def lo(self) -> float:
        return self.levels[0]

    @property
    def hi(self) -> float:
        return self.levels[-1]

    def deploy(self, price: float, ts: float) -> dict:
        """Lay the ladder around `price` and buy the coins the sells need."""
        if self.status != "NEW":
            raise RuntimeError(f"{self.symbol}: deploy on a {self.status} grid")
        if not (self.lo < price < self.hi):
            raise ValueError(f"{self.symbol}: price {price} outside band {self.lo}-{self.hi}")
        e = min(range(self.n), key=lambda j: abs(self.levels[j] - price))
        pre = [(j, self.rung / self.levels[j - 1]) for j in range(e + 1, self.n)]
        need = e * self.rung * (1.0 + self.maker)
        need += sum(q * price * (1.0 + self.taker) for _, q in pre)
        if need > self.cash + 1e-9:
            raise ValueError(f"{self.symbol}: ladder needs ${need:.2f}, allocation is ${self.cash:.2f}")
        self.empty = e
        for j in range(e):
            self.orders[j] = {"side": BUY, "qty": self.rung / self.levels[j]}
        cost = 0.0
        for j, q in pre:
            fee = q * price * self.taker
            self.cash -= q * price + fee
            self.base += q
            self.fees += fee
            cost += q * price + fee
            self.orders[j] = {"side": SELL, "qty": q, "entry": price, "entry_fee": fee}
        self.status = "ACTIVE"
        self.deployed_at = ts
        self.deploy_px = price
        self.last_px = price
        self.last_ts = ts
        return {"empty_level": e, "buys": e, "sells": len(pre),
                "coins_bought": self.base, "cost": cost}

    def on_print(self, price: float, ts: float, trade_id: int | None = None) -> list[dict]:
        """Apply one exchange trade print. Returns the fills it caused, in order.

        Prints are deduplicated by trade id (Kraken's ids are sequential per
        pair and shared by the websocket and REST), and nothing printed at or
        before the deploy counts.
        """
        if self.status != "ACTIVE":
            return []
        if trade_id is not None and self.last_trade_id is not None:
            if trade_id <= self.last_trade_id:
                return []
        elif ts <= self.deployed_at:
            return []
        if trade_id is not None:
            self.last_trade_id = trade_id
        self.last_px = price
        self.last_ts = ts
        fills = []
        while True:
            j = self.empty - 1
            if j >= 0 and price < self.levels[j]:
                fills.append(self._fill_buy(j, price, ts))
                continue
            j = self.empty + 1
            if j < self.n and price > self.levels[j]:
                fills.append(self._fill_sell(j, price, ts))
                continue
            break
        if price < self.lo or price > self.hi:
            if self.outside_since is None:
                self.outside_since = ts
        else:
            self.outside_since = None
        return fills

    def _fill_buy(self, j: int, printed: float, ts: float) -> dict:
        o = self.orders[j]
        if not o or o["side"] != BUY:
            raise AssertionError(f"{self.symbol}: expected a BUY at level {j}, found {o}")
        L = self.levels[j]
        q = o["qty"]
        fee = q * L * self.maker
        self.cash -= q * L + fee
        self.base += q
        self.fees += fee
        self.orders[j] = None
        self.orders[self.empty] = {"side": SELL, "qty": q, "entry": L, "entry_fee": fee}
        self.empty = j
        self.fills += 1
        self.net_fills += 1
        return {"side": BUY, "level": j, "price": L, "qty": q, "fee": fee,
                "print": printed, "ts": ts}

    def _fill_sell(self, j: int, printed: float, ts: float) -> dict:
        o = self.orders[j]
        if not o or o["side"] != SELL:
            raise AssertionError(f"{self.symbol}: expected a SELL at level {j}, found {o}")
        L = self.levels[j]
        q = o["qty"]
        proceeds = q * L
        fee = proceeds * self.maker
        self.cash += proceeds - fee
        self.base -= q
        if abs(self.base) < 1e-12:
            self.base = 0.0
        self.fees += fee
        pnl = proceeds - fee - (q * o["entry"] + o["entry_fee"])
        self.realised += pnl
        self.round_trips += 1
        self.orders[j] = None
        self.orders[self.empty] = {"side": BUY, "qty": self.rung / self.levels[self.empty]}
        self.empty = j
        self.fills += 1
        self.net_fills -= 1
        return {"side": SELL, "level": j, "price": L, "qty": q, "fee": fee,
                "entry": o["entry"], "pnl": pnl, "print": printed, "ts": ts}

    def close(self, price: float, ts: float, reason: str) -> dict | None:
        """Sell every coin at `price` (taker) and stop. Everything is realised."""
        if self.status != "ACTIVE":
            return None
        before = self.realised
        if self.base > 0:
            fee = self.base * price * self.taker
            self.cash += self.base * price - fee
            self.fees += fee
        self.base = 0.0
        self.realised = self.cash - self.alloc
        self.orders = [None] * self.n
        self.status = "CLOSED"
        self.closed_at = ts
        self.close_px = price
        self.close_reason = reason
        s = self.summary()
        s["exit_pnl"] = self.realised - before
        return s

    # ------------------------------------------------------------ inspection --
    def holding(self) -> int:
        """Rungs of coins held = resting SELL orders."""
        return sum(1 for o in self.orders if o and o["side"] == SELL)

    def mark(self, price: float | None = None) -> dict:
        px = price if price is not None else (self.last_px or 0.0)
        equity = self.cash + self.base * px
        total = equity - self.alloc
        return {"price": px, "equity": equity, "total": total,
                "realised": self.realised, "unrealised": total - self.realised}

    def floor_pnl(self) -> float | None:
        """Total P/L if price fell straight through the band floor now: every
        resting buy fills at its level, then everything is sold just below lo
        at the taker fee. The loss a downward pierce would lock in."""
        if self.status != "ACTIVE":
            return None
        g = Grid.from_dict(self.to_dict())
        px = self.lo * (1.0 - 1e-9)
        if self.last_px is not None and self.last_px < px:
            px = self.last_px          # already through the floor: the sale is at today's price
        g.last_trade_id = None
        g.on_print(px, (g.last_ts or 0.0) + 1.0)
        g.close(px, (g.last_ts or 0.0) + 1.0, "what-if")
        return g.realised

    def summary(self) -> dict:
        end = self.closed_at or self.last_ts or self.deployed_at
        days = ((end - self.deployed_at) / 86400.0) if (end and self.deployed_at) else None
        return {"symbol": self.symbol, "status": self.status, "kind": self.kind,
                "levels": self.n, "lo": self.lo, "hi": self.hi, "alloc": self.alloc,
                "deployed_at": self.deployed_at, "deploy_px": self.deploy_px,
                "closed_at": self.closed_at, "close_px": self.close_px,
                "close_reason": self.close_reason, "days": days,
                "realised": self.realised, "fees": self.fees, "fills": self.fills,
                "round_trips": self.round_trips,
                "predicted_yield_day": self.meta.get("yield_day")}

    _STATE = ("symbol", "key", "kind", "levels", "alloc", "meta", "orders", "empty",
              "cash", "base", "realised", "fees", "fills", "round_trips", "net_fills",
              "status", "deployed_at", "deploy_px", "last_px", "last_ts",
              "last_trade_id", "outside_since", "exit_due", "gap_at", "gap_end", "closed_at", "close_px",
              "close_reason")

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self._STATE}
        d["maker_pct"] = self.maker * 100.0
        d["taker_pct"] = self.taker * 100.0
        d["orders"] = [dict(o) if o else None for o in self.orders]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Grid":
        if "status" not in d:
            raise ValueError("missing status")
        g = cls(d["symbol"], d["key"], d["levels"], d["kind"], d["alloc"],
                d["maker_pct"], d["taker_pct"], d.get("meta"))
        for k in cls._STATE:
            if k in ("symbol", "key", "kind", "levels", "alloc", "meta"):
                continue
            if k in d:
                setattr(g, k, d[k])
        g.orders = [dict(o) if o else None for o in d["orders"]]
        g.validate()
        return g

    def validate(self) -> None:
        """Raise ValueError unless this grid can be traded as restored. A saved
        grid that fails here is kept verbatim and named, never half-loaded."""
        for k in ("status", "cash", "base"):
            if getattr(self, k, None) is None:
                raise ValueError(f"missing {k}")
        if self.status not in ("ACTIVE", "BROKEN", "CLOSED", "NEW"):
            raise ValueError(f"unknown status {self.status!r}")
        if self.status != "ACTIVE":
            return
        e = self.empty
        if not isinstance(e, int) or isinstance(e, bool) or not (0 <= e < self.n):
            raise ValueError(f"bad empty level {e!r}")
        if len(self.orders) != self.n or self.orders[e] is not None:
            raise ValueError("ladder does not match its levels")
        for j, o in enumerate(self.orders):
            if j == e:
                continue
            want = BUY if j < e else SELL
            need = ("qty",) if want == BUY else ("qty", "entry", "entry_fee")
            if not isinstance(o, dict) or o.get("side") != want or any(
                    not isinstance(o.get(k), (int, float)) for k in need):
                raise ValueError(f"level {j} is not a valid {want}")
        if self.deployed_at is None:
            raise ValueError("missing deployed_at")


def feasibility(levels: list[float], alloc: float, pair_meta: dict | None) -> str | None:
    """None if Kraken would accept every rung of this ladder, else the reason.

    Paper follows the exchange's minimums: a ladder that could not be placed
    live must not be simulated either."""
    if not pair_meta:
        return "no Kraken pair data"
    rung = alloc / len(levels)
    # The smallest order that ever rests: the top level is always a SELL (or
    # empty), sized as rung / (the level below it), so no order is ever
    # rung / levels[-1]. levels[-2] is levels[0] when there are two levels.
    smallest = rung / levels[-2]
    try:
        ordermin = float(pair_meta.get("ordermin") or 0.0)
    except (TypeError, ValueError):
        ordermin = 0.0
    try:
        costmin = float(pair_meta.get("costmin") or 0.0)
    except (TypeError, ValueError):
        costmin = 0.0
    if ordermin and smallest < ordermin:
        return (f"${rung:.2f} per level is {smallest:.6g} coins at the highest resting "
                f"order, under Kraken's minimum order of {ordermin:g}")
    if costmin and rung < costmin:
        return f"${rung:.2f} per level is under Kraken's minimum of ${costmin:g}"
    return None


# =============================================================================
# FILE HELPERS
# =============================================================================
def atomic_write_json(path: Path, obj, backup: bool = False) -> None:
    """Write-then-rename, fsynced, so a crash or power cut leaves either the
    old file or the new one -- never an empty one. `backup` keeps the previous
    good copy as <name>.bak."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    if backup and path.exists():
        try:
            import shutil
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)


def tick_of(pair_meta: dict | None) -> float | None:
    """Kraken's price increment for the pair, or None if unknown."""
    if not pair_meta:
        return None
    try:
        tick = float(pair_meta.get("tick_size") or 0.0) or None
    except (TypeError, ValueError):
        tick = None
    if tick is None:
        try:
            tick = 10.0 ** -int(pair_meta.get("pair_decimals"))
        except (TypeError, ValueError):
            tick = None
    return tick


def snap_levels(levels: list[float], pair_meta: dict | None) -> list[float]:
    """Round each level to the pair's tick so every rung is a price an order
    could actually rest at. Moves each level by at most half a tick. Unknown
    tick: unchanged. Raises ValueError when the ladder is finer than the tick
    (levels would merge) -- a ladder Kraken could not hold is not simulated."""
    tick = tick_of(pair_meta)
    if not tick:
        return list(levels)
    decimals = max(0, -int(f"{tick:e}".split("e")[1])) + 2
    snapped = [round(round(x / tick) * tick, decimals) for x in levels]
    if snapped[0] <= 0 or any(b <= a for a, b in zip(snapped, snapped[1:])):
        raise ValueError(f"levels are closer than Kraken's price tick ({tick:g})")
    return snapped


def append_jsonl(path: Path, obj) -> bool:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
        return True
    except OSError as e:
        log.warning("journal write failed: %s", e)
        return False


def load_scan(path: Path) -> tuple[dict | None, str | None]:
    """The scanner's newest --json output, or (None, why)."""
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except FileNotFoundError:
        return None, "no scan yet"
    except (ValueError, OSError) as e:
        # oracle.write_json is not atomic; a read mid-write lands here.
        return None, f"scan unreadable ({e.__class__.__name__}), retrying"
    try:
        blob["_generated_ts"] = datetime.fromisoformat(blob["generated"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None, "scan has no timestamp"
    if not isinstance(blob.get("results"), list):
        return None, "scan has no results"
    return blob, None


def acquire_single_instance(path: Path):
    """Hold an OS lock on `path` for the life of the process, or return None."""
    f = open(path, "a+")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def iso(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def short_px(p: float | None) -> str:
    """A price in at most 11 characters with 4-5 significant digits."""
    if not p:
        return "-"
    if p >= 1_000_000:
        return f"{p:.4g}"
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}"
    if p < 1e-4:
        return f"{p:.3e}"                      # PEPE/BONK: 2.534e-06
    import math
    decimals = -int(math.floor(math.log10(p))) + 4
    return f"{p:.{decimals}f}"


def hms(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(max(0, seconds))
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


# =============================================================================
# KRAKEN PUBLIC TRADE STREAM (websocket v2, no keys)
# =============================================================================
class TradeFeed:
    """Every public trade print for the subscribed pairs, via Kraken WS v2.

    Message shape, checked live on 2026-09-23:
      {"channel":"trade","type":"update"|"snapshot","data":[{"symbol":"BTC/USD",
       "side":"buy","price":86130.4,"qty":0.00015,"ord_type":"limit",
       "trade_id":108764062,"timestamp":"2026-09-23T08:05:49.528278Z"}]}
    Heartbeats arrive about once a second, so silence means a dead socket.
    """
    URL = "wss://ws.kraken.com/v2"
    STALE_S = 30.0

    def __init__(self, on_trade, on_connect=None, on_connect_sync=None):
        self.on_trade = on_trade
        self.on_connect = on_connect
        # Runs ON the socket thread before any message is read: marks every
        # grid as catching up, so no print of the new connection is applied
        # ahead of the gap that came before it.
        self.on_connect_sync = on_connect_sync
        self._want: set[str] = set()
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._ws = None
        self._stop = threading.Event()
        self._thread = None
        self.connected = False
        self.last_msg = 0.0
        self.connects = 0
        self.errors = 0
        self.last_error = ""
        self._attempt_started = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="trade-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def set_symbols(self, symbols) -> None:
        with self._lock:
            self._want = set(symbols)
            if self.connected:
                self._sync_locked()

    def check_stale(self) -> None:
        """Recover a socket that has gone quiet or never finished connecting.

        Kraken sends heartbeats only while something is subscribed, so silence
        means death only then; an idle socket is watched by the ping/pong."""
        now = time.time()
        with self._lock:
            armed = self.connected and bool(self._subscribed)
            stuck = (not self.connected and self._attempt_started
                     and now - self._attempt_started > 60)
        if armed and now - self.last_msg > self.STALE_S:
            log.warning("trade feed silent for %.0fs -- reconnecting", now - self.last_msg)
            self._kill_socket()
        elif stuck:
            log.warning("trade feed connect stuck for %.0fs -- retrying", now - self._attempt_started)
            self._attempt_started = now
            self._kill_socket()

    def _kill_socket(self) -> None:
        ws = self._ws
        if ws is None:
            return
        # Take the raw socket BEFORE ws.close(): WebSocketApp.close() sets
        # ws.sock to None, and then there is nothing left to shut down.
        raw = getattr(getattr(ws, "sock", None), "sock", None)
        try:
            ws.close()
        except Exception:
            pass
        if raw is not None:
            import socket
            try:
                raw.shutdown(socket.SHUT_RDWR)     # wakes a thread blocked in recv
            except Exception:
                pass
            try:
                raw.close()
            except Exception:
                pass

    def _send(self, obj) -> None:
        ws = self._ws
        if ws is not None:
            ws.send(json.dumps(obj))

    def _sync_locked(self) -> None:
        add = sorted(self._want - self._subscribed)
        rem = sorted(self._subscribed - self._want)
        try:
            if add:
                self._send({"method": "subscribe", "params": {
                    "channel": "trade", "symbol": add, "snapshot": False}})
                if not self._subscribed:
                    # silence is only meaningful once something is subscribed:
                    # start the clock now, not at the last idle message
                    self.last_msg = max(self.last_msg, time.time())
                self._subscribed |= set(add)
            if rem:
                self._send({"method": "unsubscribe", "params": {
                    "channel": "trade", "symbol": rem}})
                self._subscribed -= set(rem)
        except Exception as e:
            self.last_error = f"subscribe: {e}"
            log.warning("trade feed subscribe failed: %s", e)

    def _run(self) -> None:
        import websocket  # websocket-client
        # Without a timeout the TCP/TLS handshake can block forever and no
        # reconnect ever happens. 20s covers connect, handshake and sends.
        websocket.setdefaulttimeout(20)
        backoff = 1.0
        while not self._stop.is_set():
            started = time.time()
            self._attempt_started = started
            ws = websocket.WebSocketApp(
                self.URL, on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close)
            self._ws = ws
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self.errors += 1
                self.last_error = str(e)
                log.warning("trade feed crashed: %s", e)
            with self._lock:
                self.connected = False
                self._subscribed = set()
                self._attempt_started = 0.0      # not "stuck" while backing off
            self._ws = None
            if self._stop.is_set():
                break
            if time.time() - started > 60:
                backoff = 1.0
            self._stop.wait(backoff)
            backoff = min(60.0, backoff * 2)

    def _on_open(self, ws) -> None:
        # Order matters: count the connection, THEN mark every grid as catching
        # up, and only THEN report the feed as up. Anything that sees
        # connected=True (the wall-clock exit) also sees the grids syncing.
        with self._lock:
            self.connects += 1
        if self.on_connect_sync:
            try:
                self.on_connect_sync()
            except Exception as e:
                log.warning("on_connect_sync failed: %s", e)
        with self._lock:
            self.connected = True
            self.last_msg = time.time()
            self._attempt_started = 0.0
            self._subscribed = set()
            self._sync_locked()
        log.info("trade feed connected (%d pairs)", len(self._want))
        if self.on_connect:
            try:
                self.on_connect()          # must return at once (it only requests a resync)
            except Exception as e:
                log.warning("on_connect failed: %s", e)

    def _on_message(self, ws, raw) -> None:
        self.last_msg = time.time()
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("channel") == "trade":
            for d in msg.get("data") or []:
                try:
                    ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                    self.on_trade(d["symbol"], float(d["price"]), ts, int(d["trade_id"]))
                except Exception as e:
                    self.errors += 1
                    self.last_error = f"trade: {e}"
                    log.warning("bad trade message %s: %s", d, e)
        elif msg.get("method") in ("subscribe", "unsubscribe") and msg.get("success") is False:
            self.errors += 1
            self.last_error = str(msg.get("error"))
            log.warning("trade feed %s refused: %s", msg.get("method"), msg.get("error"))

    def _on_error(self, ws, err) -> None:
        self.errors += 1
        self.last_error = str(err)
        log.info("trade feed error: %s", err)

    def _on_close(self, ws, code, reason) -> None:
        with self._lock:
            self.connected = False
        log.info("trade feed closed (%s %s)", code, reason)


def fetch_trades_since(key: str, since_ts: float, max_pages: int = 20) -> tuple[list, str | None]:
    """REST trades after `since_ts` as [(price, ts, trade_id)], oldest first.

    count=1000 on purpose: Kraken's Trades endpoint can leave a trade out of a
    SHORT page (found on HBARUSD while building D:/GridPick/data); full pages
    have not shown it."""
    out: list = []
    since = str(max(0, int(since_ts) - 1))
    for _ in range(max_pages):
        res, err = oracle.kget("Trades", {"pair": key, "since": since, "count": 1000})
        if err:
            return out, err
        rows = next((v for k, v in res.items() if k != "last"), [])
        for r in rows:
            try:
                out.append((float(r[0]), float(r[2]), int(r[6])))
            except (IndexError, TypeError, ValueError):
                continue
        if len(rows) < 1000 or not res.get("last"):
            return out, None
        since = str(res["last"])
    return out, f"stopped after {max_pages} pages"


# =============================================================================
# SCANNER CHILD PROCESS
# =============================================================================
class Scanner:
    """oracle.py as a hidden child writing scan.json. Dies with this process."""

    MAX_LOG_BYTES = 20 * 1024 * 1024

    def __init__(self, data_dir: Path, scan_path: Path, args):
        self.pidfile = data_dir / "scanner.pid"
        self.logpath = data_dir / "scanner.log"
        self.cmd = [sys.executable, "-X", "utf8", str(HERE / "oracle.py"),
                    "--json", str(scan_path), "--refresh", str(args.scan_refresh),
                    "--pace", str(args.scan_pace), "--fee", str(args.fee),
                    "--no-clear", "--no-color"] + list(args.scan_arg or [])
        self.scan_path = Path(scan_path)
        # scan.json older than this while the child is alive = hung scanner
        self.hang_after = max(600.0, 3.0 * float(args.scan_refresh))
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.last_start = 0.0
        self.last_exit = None
        self._job = None
        self._logf = None
        self._rotate_block_until = 0.0

    def kill_orphan(self) -> None:
        """A scanner left behind by a crashed run would double the REST load."""
        try:
            pid = int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            import psutil
            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline())
            if "oracle.py" in cmd and str(HERE).lower() in cmd.lower():
                log.warning("killing orphaned scanner pid %d", pid)
                p.kill()
                p.wait(5)
        except Exception:
            pass

    def ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if self.hung():
                log.warning("scanner alive but scan.json not written for over %ds -- restarting it",
                            int(self.hang_after))
                self.stop()
                self._start()
                return
            # The child holds scanner.log open, and Windows will not rename an
            # open file, so the cap is enforced by a restart between scans.
            try:
                big = self.logpath.stat().st_size > self.MAX_LOG_BYTES
            except OSError:
                big = False
            # After a failed rename, do not keep killing the scanner: it would
            # never finish a scan. Try again an hour later.
            if not big or time.time() < self._rotate_block_until:
                return
            log.info("scanner.log over %d MB -- restarting the scanner to rotate it",
                     self.MAX_LOG_BYTES // 1_000_000)
            self.stop()
            self._start()
            return
        if self.proc is not None:
            self.last_exit = self.proc.returncode
            log.warning("scanner exited with %s", self.proc.returncode)
            self.proc = None
        if time.time() - self.last_start < 30:
            return
        self._start()

    def hung(self, now: float | None = None) -> bool:
        """Alive, but neither scan.json nor this child's start is recent."""
        now = time.time() if now is None else now
        try:
            written = self.scan_path.stat().st_mtime
        except OSError:
            written = 0.0
        return now - max(written, self.last_start) > self.hang_after

    def _start(self) -> None:
        if self._logf:                       # close BEFORE rotating: Windows
            try:                             # cannot rename an open file
                self._logf.close()
            except OSError:
                pass
            self._logf = None
        try:
            if self.logpath.exists() and self.logpath.stat().st_size > self.MAX_LOG_BYTES:
                os.replace(self.logpath, self.logpath.with_suffix(".log.1"))
        except OSError as e:
            self._rotate_block_until = time.time() + 3600
            log.warning("scanner.log rotation failed (%s); retrying in an hour", e)
        self._logf = open(self.logpath, "ab")
        # Unbuffered: a killed scanner otherwise takes its last scans' output
        # with it, and scanner.log looks hours stale (audit 2026-09-23).
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(self.cmd, cwd=str(HERE), stdout=self._logf,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=env, creationflags=flags)
        self.starts += 1
        self.last_start = time.time()
        self._bind_to_parent()
        try:
            self.pidfile.write_text(str(self.proc.pid))
        except OSError:
            pass
        log.info("scanner started, pid %d", self.proc.pid)

    def _bind_to_parent(self) -> None:
        """Windows: put the child in a kill-on-close job so closing the bot's
        window (which never runs Python cleanup) takes the scanner with it."""
        if os.name != "nt" or self.proc is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            ok = ok and k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(self.proc._handle)))
            self._close_job()                 # a previous child's job, if any
            if ok:
                self._job = job      # the handle must outlive the child
            else:
                k32.CloseHandle(job)
                log.info("scanner not bound to a job (error %d); orphan cleanup covers it",
                         ctypes.get_last_error())
        except Exception as e:
            log.info("scanner job binding unavailable: %s", e)

    def _close_job(self) -> None:
        """Release the job handle of a scanner that is already gone."""
        if self._job is None or os.name != "nt":
            return
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._job))
        except Exception:
            pass
        self._job = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None
        self._close_job()
        try:
            self.pidfile.unlink()
        except OSError:
            pass
        if self._logf:
            try:
                self._logf.close()
            except OSError:
                pass
            self._logf = None


# =============================================================================
# THE BOT
# =============================================================================
class Bot:
    def __init__(self, args, *, feed=None, scanner=None, book=None,
                 price_fn=None, trades_fn=None, clock=time.time, render=True):
        self.args = args
        self.clock = clock
        self.render_console = render
        # Absolute: the scanner child runs with cwd=HERE, so a relative path
        # would send it to a different scan.json than the one the bot reads.
        self.data = Path(args.data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "state.json"
        self.status_path = self.data / "status.json"
        self.journal_path = self.data / "journal.jsonl"
        self.scan_path = self.data / "scan.json"
        self.capital = float(args.capital)
        self.max_grids = int(args.max_grids)
        self.alloc = self.capital / self.max_grids
        self.lock = threading.RLock()
        self.grids: dict[str, Grid] = {}
        self.closed: list[dict] = []           # recent summaries, display only
        # Running totals over EVERY closed grid. The list above is trimmed;
        # totals computed from it would forget old exits after a restart.
        self.totals = {"count": 0, "realised": 0.0, "fees": 0.0, "round_trips": 0, "fills": 0}
        self.cooldown: dict[str, float] = {}
        self.skipped: dict[str, str] = {}
        self.skipped_gen = None
        self.attempted: set = set()
        self.events = collections.deque(maxlen=14)
        self.syncing: set[str] = set()
        self.pending: dict[str, list] = {}
        self._resync_lock = threading.Lock()
        self.gaps = 0
        self.closes_cache: dict | None = None
        self.dirty = False
        self.scan: dict | None = None
        self.scan_note = "waiting for the first scan"
        self.started = self.clock()
        self.book = book
        self.price_fn = price_fn or self._rest_price
        self.trades_fn = trades_fn or fetch_trades_since
        self.feed = feed if feed is not None else TradeFeed(self.on_trade, self.request_resync,
                                                            self.mark_syncing)
        self.scanner = scanner
        self._ticker_retry: dict[str, float] = {}
        self.book_loaded_at = time.time()
        self._book_reloading = False
        self._rs = threading.Lock()
        self._rs_wanted = False
        self._rs_running = False
        self.corr_note = None
        self._corr_refreshed: set[str] = set()
        self._state_loaded = False
        self._backup_ok = True
        self.unrestored: dict = {}      # saved grids this code cannot run; slot held
        self._stopping = False
        self._stop_requested = False    # Ctrl+C: finish nothing new, then stop
        # Book guard (audit 2026-09-23): the whole book, not one grid, can stop
        # taking new grids. peak = highest book equity seen; day/day_start =
        # local date and book equity at its first look. Persisted in state.
        self.guard: dict = {"peak": None, "day": None, "day_start": None}
        self.halt_reason: str | None = None
        # A graceful stop from outside (gridbot_stop.ps1): same path as Ctrl+C.
        self.stop_path = self.data / "stop.request"

    def request_resync(self) -> None:
        """Ask for a catch-up. At most one worker runs; connects that arrive
        while it runs fold into one more pass instead of queueing a thread each."""
        def worker():
            while True:
                with self._rs:
                    if not self._rs_wanted:
                        self._rs_running = False
                        return
                    self._rs_wanted = False
                try:
                    self.resync()
                except Exception:
                    log.exception("resync failed")

        with self._rs:
            self._rs_wanted = True
            if self._rs_running:
                return
            # Start first, then claim: a thread that fails to start must not
            # leave the worker "running" forever with every grid syncing. The
            # worker's first step waits on _rs, so it cannot miss the flag.
            try:
                threading.Thread(target=worker, name="resync", daemon=True).start()
            except Exception:
                log.exception("resync worker could not start")
                return
            self._rs_running = True

    def mark_syncing(self) -> None:
        """Called on the socket thread the moment it connects: every open grid
        queues its prints until the catch-up has replayed the gap."""
        with self.lock:
            for s, g in self.grids.items():
                if g.status == "ACTIVE":
                    self.syncing.add(s)
                    self.pending.setdefault(s, [])

    # ---------------------------------------------------------------- events --
    def event(self, text: str, level=logging.INFO) -> None:
        self.events.append(f"{datetime.now().strftime('%H:%M:%S')} {text}")
        log.log(level, text)

    def journal(self, event: str, **fields) -> None:
        # ts and event last: a caller's field of the same name cannot overwrite
        # them. Exchange times travel as exch_ts.
        append_jsonl(self.journal_path, {**fields, "ts": iso(self.clock()), "event": event})

    # --------------------------------------------------------------- persist --
    @staticmethod
    def _read_json(path: Path) -> dict:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("not a JSON object")
        return st

    def _set_aside(self, path: Path, tag: str) -> str:
        """Move an unusable file out of the way -- or copy it if it cannot be
        moved -- so nothing overwrites it. Says what actually happened."""
        dest = path.with_name(f"{path.name}.{tag}.{int(self.clock())}")
        try:
            os.replace(path, dest)
            return f"moved it to {dest.name}"
        except FileNotFoundError:
            return "it was already gone"
        except OSError as e:
            try:
                import shutil
                shutil.copy2(path, dest)
                return f"copied it to {dest.name} (could not move it: {e})"
            except OSError as e2:
                return f"could not set it aside ({e2})"

    def load_state(self) -> None:
        """Restore grids, history and cooldowns. Never starts over silently:
        an unusable state.json is kept, the last good .bak is restored and
        written back at once, and a file that could not be loaded is never
        copied over that good backup."""
        bak = self.state_path.with_name(self.state_path.name + ".bak")
        try:
            st = self._read_locked_aware(self.state_path)
        except FileNotFoundError:
            self._state_loaded = True               # a genuinely fresh start
            return
        except ValueError as e:                     # bad CONTENT (never "can't open")
            st, problem = None, f"state.json unreadable ({e})"
        if st is not None:
            try:
                dropped = self._restore(st)
            except Exception as e:
                log.exception("state.json could not be restored")
                problem = f"state.json could not be restored ({e})"
            else:
                self._note_dropped(self.state_path, dropped)   # raises -> stop, save nothing
                self._state_loaded = True
                self._finish_recovered()
                return
        # Read the backup FIRST. A backup that exists but cannot be opened
        # raises RuntimeError from here and stops the load with both files
        # untouched -- it must never fall into "starting empty".
        try:
            st_bak, bak_problem = self._read_locked_aware(bak), None
        except FileNotFoundError:
            st_bak, bak_problem = None, "there is none"
        except ValueError as e2:
            st_bak, bak_problem = None, f"its content is unusable ({e2})"
        # COPY the bad file aside and leave state.json in place: the recovered
        # state replaces it in one atomic step, so a crash in between finds the
        # same bad file (and an untouched .bak) and simply recovers again.
        what = self._keep_copy(self.state_path, "unusable")
        self._backup_ok = False                     # never back up the bad file over .bak
        if st_bak is None:
            self._reset_restored()
            what_bak = (self._keep_copy(bak, "unusable") if bak_problem != "there is none"
                        else "nothing to keep")
            self.event(f"{problem}; {what}. No usable backup ({bak_problem}); {what_bak}. "
                       f"Starting empty.", logging.ERROR)
            self._state_loaded = True
            return
        try:
            dropped_bak = self._restore(st_bak)
        except Exception as e2:                     # restore of .bak failed half way
            self._reset_restored()
            what_bak = self._keep_copy(bak, "unusable")
            self.event(f"{problem}; {what}. The backup could not be restored either ({e2}); "
                       f"{what_bak}. Starting empty.", logging.ERROR)
            self._state_loaded = True
            return
        self._note_dropped(bak, dropped_bak)
        self.event(f"{problem}; {what}. Restored the previous save from {bak.name}.",
                   logging.ERROR)
        self._state_loaded = True
        self.save_state()                           # the recovered state on disk at once
        self._finish_recovered()

    def _finish_recovered(self) -> None:
        """Interrupted closes finished by _restore: save the finished state
        FIRST, then journal their EXIT rows."""
        recs = getattr(self, "_pending_recovered", None) or []
        if not recs:
            return
        self.save_state()
        for s in recs:
            self.journal("EXIT", **s)
            self.event(f"{s['symbol']}: finished an interrupted close (grid P/L "
                       f"{(s['realised'] or 0.0):+.2f}, reason: {s['close_reason']})",
                       logging.WARNING)
        self._pending_recovered = []

    def _read_locked_aware(self, path: Path) -> dict:
        """_read_json, but a file that exists and cannot be OPENED (locked by a
        sync client, antivirus, an editor) is not a corrupt file: retry, then
        stop without touching anything. run.bat restarts in 15 s and tries again.
        Raises FileNotFoundError (absent) and ValueError (bad content) as is."""
        last = None
        for attempt in range(5):
            try:
                return self._read_json(path)
            except (FileNotFoundError, ValueError):
                raise
            except OSError as e:
                last = e
                time.sleep(1.0)
        raise RuntimeError(f"{path.name} exists but cannot be read ({last}); refusing to "
                           f"replace it -- will retry on restart")

    def _keep_copy(self, path: Path, tag: str) -> str:
        """Copy an unusable file aside so its content survives, leaving the file
        itself in place. Stops the bot if the copy cannot be made."""
        dest = path.with_name(f"{path.name}.{tag}.{int(self.clock())}")
        try:
            import shutil
            shutil.copy2(path, dest)
            return f"kept a copy as {dest.name}"
        except FileNotFoundError:
            return "it was already gone"
        except OSError as e:
            raise RuntimeError(f"could not keep a copy of {path.name} ({e}); refusing to "
                               f"replace it -- will retry on restart")

    def _reset_restored(self) -> None:
        self.unrestored = {}
        self.grids, self.closed, self.cooldown = {}, [], {}
        self.totals = {"count": 0, "realised": 0.0, "fees": 0.0, "round_trips": 0, "fills": 0}
        self.syncing, self.pending = set(), {}

    def _restore(self, st: dict) -> None:
        """Build everything from `st` and only then install it: a state that
        fails half way leaves the bot untouched."""
        grids = {}
        dropped = []
        recovered = []          # CLOSED grids still in the book: an interrupted close
        unrestored = {}         # grids this code cannot run: carried verbatim, slot held
        for key, d in (st.get("grids") or {}).items():
            try:
                g = Grid.from_dict(d)
            except Exception as e:
                sym = d.get("symbol") if isinstance(d, dict) else None
                dropped.append(f"{sym or key} ({e!r})")
                unrestored[key] = d
                continue
            if g.status in ("ACTIVE", "BROKEN"):
                grids[g.symbol] = g
            elif g.status == "CLOSED":
                # a close interrupted after g.close() but before the grid left
                # the book: finish it now -- P/L into the totals, cooldown, EXIT
                recovered.append(g)
            else:
                dropped.append(f"{g.symbol} (status {g.status!r})")
                unrestored[key] = d
        closed = list(st.get("closed") or [])[-100:]
        totals = {"count": 0, "realised": 0.0, "fees": 0.0, "round_trips": 0, "fills": 0}
        saved_totals = st.get("closed_totals")
        if isinstance(saved_totals, dict):
            for k in totals:
                if k in saved_totals:
                    totals[k] = type(totals[k])(saved_totals[k])
        else:                                   # older state: rebuild from the list
            for c in closed:
                totals["count"] += 1
                totals["realised"] += c.get("realised") or 0.0
                totals["fees"] += c.get("fees") or 0.0
                totals["round_trips"] += c.get("round_trips") or 0
                totals["fills"] += c.get("fills") or 0
        cooldown = {k: float(v) for k, v in (st.get("cooldown") or {}).items()}
        saved_guard = st.get("guard")
        if isinstance(saved_guard, dict):
            for k in ("peak", "day", "day_start"):
                if k in saved_guard:
                    self.guard[k] = saved_guard[k]
        rec_summaries = []
        for g in recovered:
            s = g.summary()
            s["exit_pnl"] = None                     # not known: the close was interrupted
            s["recovered"] = True
            closed.append(s)
            closed = closed[-100:]
            totals["count"] += 1
            totals["realised"] += s["realised"] or 0.0
            totals["fees"] += s["fees"] or 0.0
            totals["round_trips"] += s["round_trips"] or 0
            totals["fills"] += s["fills"] or 0
            when = g.closed_at or self.clock()
            cooldown[g.symbol] = max(cooldown.get(g.symbol, 0.0), when + self.args.cooldown * 60.0)
            rec_summaries.append(s)
        self.grids, self.closed, self.totals, self.cooldown = grids, closed, totals, cooldown
        self.unrestored = unrestored
        # journalled only AFTER the recovered state is saved (_finish_recovered):
        # a start that dies in between must not write the same EXIT twice
        self._pending_recovered = rec_summaries
        self.syncing, self.pending = set(), {}
        # Restored grids are NOT current: prints were missed while the bot was
        # down. Hold them as syncing -- live prints queue, exits wait -- until
        # the first catch-up after connect replays the gap from REST.
        for s in self.grids:
            self.syncing.add(s)
            self.pending.setdefault(s, [])
        if self.grids:
            self.event(f"restored {len(self.grids)} grid(s): {', '.join(sorted(self.grids))}"
                       f" -- catching up before any exit")
        if (st.get("capital") not in (None, self.capital)
                or st.get("max_grids") not in (None, self.max_grids)):
            self.event(f"capital settings changed since last run (was ${st.get('capital')} over "
                       f"{st.get('max_grids')} grids); open grids keep their allocation",
                       logging.WARNING)
        return dropped

    def _held_alloc(self, d) -> float:
        """Capital an unrestorable grid holds. Whatever its saved 'alloc' is,
        this never raises: an unreadable value holds one full slot."""
        try:
            v = float(d.get("alloc")) if isinstance(d, dict) else 0.0
        except (TypeError, ValueError):
            v = 0.0
        return v if (v == v and 0.0 < v < float("inf")) else self.alloc

    def _held_symbols(self) -> list[str]:
        """Symbols (and state keys) of unrestorable grids, for one-grid-per-coin."""
        out = []
        for k, d in self.unrestored.items():
            out.append(str(k))
            sym = d.get("symbol") if isinstance(d, dict) else None
            if isinstance(sym, str) and sym and sym != k:
                out.append(sym)
        return out

    def _note_dropped(self, path: Path, dropped: list) -> None:
        """Grids the file held but this code could not rebuild (e.g. after a
        change to the state format). Keep the whole file before the next save
        writes the grid-less state over it, and say which grids."""
        if not dropped:
            return
        # One copy per distinct SET OF UNRESTORABLE GRIDS (not per file: every
        # save stamps a new saved_at), so a restart loop cannot fill the disk.
        import hashlib
        try:
            blob = json.dumps(self.unrestored, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = repr(sorted(self.unrestored))
        digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
        existing = sorted(path.parent.glob(f"{path.name}.partial.{digest}.*"))
        if existing:
            what = f"a copy is already kept as {existing[0].name}"
        else:
            what = self._keep_copy(path, f"partial.{digest}")  # raises -> load stops
        self.event(f"{path.name}: could not restore grid(s) {'; '.join(dropped)}; {what}",
                   logging.ERROR)

    def save_state(self) -> None:
        with self.lock:
            st = {"version": VERSION, "saved_at": iso(self.clock()),
                  "capital": self.capital, "max_grids": self.max_grids,
                  # unrestorable grids go back VERBATIM: code that can read them
                  # (a fix, a rollback) restores them on its next start
                  "grids": {**self.unrestored,
                            **{s: g.to_dict() for s, g in self.grids.items()}},
                  "closed": self.closed[-100:], "closed_totals": dict(self.totals),
                  "cooldown": self.cooldown, "guard": dict(self.guard)}
            self.dirty = False
        try:
            atomic_write_json(self.state_path, st, backup=self._backup_ok)
            self._backup_ok = True           # state.json now holds a state we wrote
        except OSError as e:
            log.error("state save failed: %s", e)
            self.dirty = True

    # ------------------------------------------------------------ price feed --
    def on_trade(self, symbol: str, price: float, ts: float, trade_id: int | None) -> None:
        with self.lock:
            g = self.grids.get(symbol)
            if g is None:
                return
            if symbol in self.syncing:
                self.pending.setdefault(symbol, []).append((price, ts, trade_id))
                return
            self._apply(g, price, ts, trade_id)

    def _apply(self, g: Grid, price: float, ts: float, trade_id) -> None:
        """One print into one grid. Caller holds self.lock."""
        if self._stopping or g.status != "ACTIVE" or g.exit_due:
            return                           # after the stop fence nothing is booked
        ea = self.args.exit_after
        new = ((trade_id is None or g.last_trade_id is None or trade_id > g.last_trade_id)
               and ts > (g.deployed_at or 0.0))
        if new and ea > 0 and g.outside_since is not None and ts - g.outside_since >= ea:
            # The exit timer ran out BEFORE this print (a quiet pair: no print
            # at the deadline). Exit at the last trade seen before it, at the
            # deadline -- this print must not first book fills or pull the
            # grid back inside and hide the pierce.
            side = "below" if (g.last_px or 0) < g.lo else "above"
            why = f"price {side} band for {hms(ea)}" + self._gap_label(g)
            g.exit_due = [g.last_px, g.outside_since + ea, why]
            self.dirty = True
            return
        try:
            fills = g.on_print(price, ts, trade_id)
        except AssertionError as e:
            self.event(f"{g.symbol}: ladder invariant broken ({e}) -- grid parked", logging.ERROR)
            g.status = "BROKEN"
            self.dirty = True
            return
        for f in fills:
            self.dirty = True
            row = dict(f)
            row["exch_ts"] = row.pop("ts")
            self.journal("FILL", symbol=g.symbol, **row)
            pnl = f" {f['pnl']:+.4f}" if "pnl" in f else ""
            self.event(f"{g.symbol} {f['side']} L{f['level']} @ {oracle.fmt_px(f['price'])}{pnl}")
        # The exit timer runs in PRINT time, so a catch-up replaying an outage
        # exits where the market was when the timer ran out -- not at whatever
        # the price is by the time the bot is back, and not at a stale print.
        if g.gap_at is not None and new:
            if g.gap_end is None:
                g.gap_end = ts               # first print after the unknown stretch
            if g.outside_since is None or g.outside_since > g.gap_end:
                g.gap_at = g.gap_end = None  # the outside streak no longer touches the gap
        ea = self.args.exit_after
        if ea > 0 and g.outside_since is not None and ts - g.outside_since >= ea:
            side = "below" if price < g.lo else "above"
            g.exit_due = [price, ts, f"price {side} band for {hms(ts - g.outside_since)}"
                          + self._gap_label(g)]
            self.dirty = True

    @staticmethod
    def _gap_label(g: Grid) -> str:
        """Exits whose outside-the-band streak spans prints that could not be
        fetched are estimates; label them so pierce statistics can leave them out."""
        if g.gap_at is None or g.outside_since is None:
            return ""
        # the streak spans the gap, or began inside it (at or before the first
        # print seen after it)
        if g.outside_since <= (g.gap_end if g.gap_end is not None else float("inf")):
            return " (catch-up incomplete: estimated)"
        return ""

    def resync(self) -> None:
        """After a (re)connect, replay what the socket missed from REST, then
        whatever the socket delivered meanwhile. Trade ids make overlap safe.

        Resyncs run one at a time. If the socket reconnects while one runs, the
        prints queued so far belong to the NEW connection and the gap before it
        is not yet replayed: applying them would move the trade-id watermark
        past the gap and the next catch-up would dedupe it away. So they stay
        queued and the pass runs again from the old watermark."""
        with self._resync_lock:
            while True:
                epoch = getattr(self.feed, "connects", 0)
                with self.lock:
                    todo = {s: (g.key, g.last_ts or g.deployed_at)
                            for s, g in self.grids.items() if g.status == "ACTIVE"}
                    for s in todo:
                        self.syncing.add(s)
                        self.pending.setdefault(s, [])
                    # a grid closed since load/connect must not stay "syncing"
                    for s in list(self.syncing):
                        if s not in self.grids:
                            self.syncing.discard(s)
                            self.pending.pop(s, None)
                for s, (key, since) in todo.items():
                    try:
                        rows, err = self.trades_fn(key, since or self.clock())
                    except Exception as e:           # never leave a pair syncing
                        rows, err = [], f"{e.__class__.__name__}: {e}"
                    with self.lock:
                        g = self.grids.get(s)
                        if g is not None and g.status == "ACTIVE":
                            if (not err and g.gap_at is not None and since is not None
                                    and since <= g.gap_at):
                                g.gap_at = g.gap_end = None   # this fetch covers the gap
                            for price, ts, tid in rows:
                                self._apply(g, price, ts, tid)
                            if err:
                                # prints after the last one known are unknown;
                                # keep the EARLIEST unknown stretch
                                if g.gap_at is None:
                                    g.gap_at = g.last_ts or since
                                    g.gap_end = None
                                self.gaps += 1
                                self.event(f"{s}: catch-up incomplete ({err}); prints may be "
                                           f"missing", logging.WARNING)
                            elif rows:
                                log.info("%s: caught up %d print(s) from REST", s, len(rows))
                        if getattr(self.feed, "connects", 0) != epoch:
                            continue                 # newer socket: keep its prints queued
                        if g is not None and g.status == "ACTIVE":
                            for price, ts, tid in self.pending.get(s, []):
                                self._apply(g, price, ts, tid)
                        self.syncing.discard(s)
                        self.pending.pop(s, None)
                if getattr(self.feed, "connects", 0) == epoch:
                    break

    # ----------------------------------------------------------------- deploy --
    def _rest_price(self, key: str) -> float | None:
        """Last trade price from Kraken's public ticker: ONE short attempt.
        Deliberately not oracle's session, which retries 4x with 18 s timeouts
        -- up to ~100 s -- and this runs on the main loop."""
        import requests
        try:
            r = requests.get(f"{oracle.KRAKEN}/Ticker", params={"pair": key}, timeout=(3, 5))
            j = r.json()
            if j.get("error"):
                return None
            res = j.get("result") or {}
            rec = res.get(key) or next(iter(res.values()))
            return float(rec["c"][0])
        except Exception:
            return None

    # ------------------------------------------------------------ book guard --
    def book_pnl(self) -> float:
        """Closed realised + every open grid's total -- status.json's total_pnl."""
        with self.lock:
            return self.totals["realised"] + sum(g.mark()["total"] for g in self.grids.values())

    def capital_now(self) -> float:
        """Capital the book may commit: the start, less closed LOSSES. Closed
        gains do not raise it (no compounding into size without a decision)."""
        return self.capital + min(0.0, self.totals["realised"])

    def grid_alloc(self) -> float:
        """Capital for the next grid: the base share, shrunk after losses."""
        return max(0.0, min(self.alloc, self.capital_now() / self.max_grids))

    def update_guard(self, now: float | None = None) -> str | None:
        """Track peak and day-start book equity; return why new grids are
        halted, or None. Open grids keep their own exits either way -- the
        halt only stops the book from adding exposure into a losing run."""
        now = self.clock() if now is None else now
        eq = self.capital + self.book_pnl()
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        g = self.guard
        if g.get("peak") is None or eq > g["peak"]:
            g["peak"] = eq
        if g.get("day") != day or g.get("day_start") is None:
            g["day"], g["day_start"] = day, eq
        why = None
        dd = getattr(self.args, "max_drawdown_pct", 0.0)
        dl = getattr(self.args, "max_day_loss_pct", 0.0)
        if dd > 0 and eq < g["peak"] * (1.0 - dd / 100.0):
            why = (f"book equity ${eq:.2f} is more than {dd:g}% below its peak "
                   f"${g['peak']:.2f} -- no new grids")
        elif dl > 0 and eq < g["day_start"] - self.capital * dl / 100.0:
            why = (f"book down ${g['day_start'] - eq:.2f} today (limit {dl:g}% of "
                   f"${self.capital:.0f}) -- no new grids until tomorrow")
        if why != self.halt_reason:
            if why:
                self.event("HALT: " + why, logging.WARNING)
                self.journal("HALT", reason=why, equity=eq, peak=g["peak"],
                             day_start=g["day_start"])
            else:
                self.event("halt lifted: book back inside its limits")
                self.journal("RESUME", equity=eq)
            self.halt_reason = why
            self.dirty = True
        return why

    def consider(self) -> None:
        """Fill free grid slots from the newest scan's qualified basket picks."""
        blob, why = load_scan(self.scan_path)
        if blob is None:
            if self.scan is None or not why.startswith("scan unreadable"):
                self.scan_note = why
            return
        self.scan = blob
        now = self.clock()
        age = now - blob["_generated_ts"]
        limit = max(600.0, 3.0 * self.args.scan_refresh)
        n_q = sum(1 for r in blob["results"] if r.get("qualified"))
        if age > limit:
            self.scan_note = f"scan is {hms(age)} old -- no new grids until it refreshes"
            return
        self.scan_note = f"{len(blob['results'])} pairs ranked, {n_q} qualified"
        gen = blob["generated"]
        if gen != self.skipped_gen:            # reasons belong to one scan
            self.skipped = {}
            self.skipped_gen = gen
            self.closes_cache = None
            self._corr_refreshed = set()
        # reasons that depend on the current open grids are re-derived each pass
        self.skipped = {k: v for k, v in self.skipped.items()
                        if not v.startswith(DYNAMIC_REASONS)}
        if self.update_guard(now):
            return
        alloc = self.grid_alloc()
        for r in blob["results"]:
            if self._stop_requested:
                break                        # no new REST calls or deploys after Ctrl+C
            with self.lock:
                # grids that could not be restored still hold their slot, coin
                # and capital: redeploying over them would overwrite them
                committed = (sum(g.alloc for g in self.grids.values())
                             + sum(self._held_alloc(d) for d in self.unrestored.values()))
                n_open = len(self.grids) + len(self.unrestored)
                open_syms = list(self.grids) + self._held_symbols()
            if (n_open >= self.max_grids or alloc <= 0
                    or committed + alloc > self.capital_now() + 1e-9):
                break
            if not r.get("qualified"):
                continue
            sym = r.get("symbol")
            if not sym or sym in self.grids:
                continue
            base = sym.split("/")[0]
            if any(s.split("/")[0] == base for s in open_syms):
                self.skipped[sym] = f"already running a {base} grid"
                continue
            # cooldown is per COIN: a pierced X/USD must not come straight back as X/USDT
            cool = max((t for k, t in self.cooldown.items() if k.split("/")[0] == base), default=0)
            if cool > now:
                self.skipped[sym] = f"cooling down ({base}) until {iso(cool)[11:16]}"
                continue
            corr, why = self._max_corr(r, blob)
            if why:
                self.skipped[sym] = why
                continue
            if corr is not None and corr >= 0.72:
                self.skipped[sym] = f"moves with an open grid (corr {corr:.2f} >= 0.72)"
                continue
            if (sym, gen) in self.attempted:
                continue
            self.attempted.add((sym, gen))
            why = self._deploy_pick(r, blob, alloc)
            if why:
                self.skipped[sym] = why
            else:
                self.skipped.pop(sym, None)
        if len(self.attempted) > 2000:
            self.attempted = {a for a in self.attempted if a[1] == gen}

    def _max_corr(self, r: dict, blob: dict) -> tuple[float | None, str | None]:
        """(highest |correlation| of this pick with any open grid, refusal).

        Uses the scanner's own pearson() on the bars it cached this scan,
        matched BY BAR TIME over the last 180 common bars. The scanner's
        'basket' flag can't be used: it is computed over ALL ranked pairs, so
        an unqualified leader knocks qualified picks out.

        No bar cache at all: (None, None) -- deploy, and say once that the
        check was not possible. An open grid's bars stale or too short to
        compare: refuse the pick -- unknown is not "uncorrelated"."""
        with self.lock:
            open_keys = [(g.symbol, g.key) for g in self.grids.values()]
        if not open_keys:
            return None, None
        label = str(blob.get("interval") or "30m").lower()
        try:
            interval = oracle.INTERVAL_WORDS.get(label) or int(label.rstrip("m"))
        except ValueError:
            interval = 30
        if self.closes_cache is None:
            try:
                self.closes_cache = oracle.load_ohlc_cache(interval)
            except Exception as e:
                log.info("no bar cache for correlation: %s", e)
                self.closes_cache = {}
        if not self.closes_cache:
            if not getattr(self, "corr_note", None):
                self.corr_note = "correlation not checked: no scanner bar cache"
                self.event(self.corr_note, logging.WARNING)
            return None, None

        def bars(key):
            rows = (self.closes_cache.get(key) or {}).get("rows") or []
            return {row[0]: row[4] for row in rows}
        mine = bars(r.get("key"))
        if len(mine) < 48:
            return None, "correlation unknown: too few bars for this pair"
        best = None
        newest = max(mine)
        step = interval * 60
        for sym, key in open_keys:
            theirs = bars(key)
            if ((not theirs or max(theirs) < newest - 2 * step)
                    and key not in self._corr_refreshed):
                # The open grid's pair has dropped out of the scanner's fetch
                # set, so its cached bars froze. Fetch them once per scan.
                self._corr_refreshed.add(key)
                self._refresh_bars(key, interval)
                theirs = bars(key)
            if not theirs or max(theirs) < newest - 2 * step:
                # still behind after the refresh attempt: unknown, not "uncorrelated".
                # Re-derived every pass; the refresh is retried next scan.
                end = iso(max(theirs)) if theirs else "never"
                return None, f"correlation unknown: {sym}'s bars end {end}"
            common = sorted(set(mine) & set(theirs))[-180:]
            if len(common) < 48:
                return None, f"correlation unknown: under 48 bars in common with {sym} this scan"
            v = abs(oracle.pearson([mine[t] for t in common], [theirs[t] for t in common]))
            best = v if best is None else max(best, v)
        return best, None

    def _refresh_bars(self, key: str, interval: int) -> None:
        """One short Kraken OHLC call into this bot's copy of the bar cache.
        Not oracle.fetch_ohlc: that goes through the retrying session (up to
        ~100 s) and this runs on the main loop."""
        import requests
        try:
            r = requests.get(f"{oracle.KRAKEN}/OHLC", params={"pair": key, "interval": interval},
                             timeout=(3, 5))
            j = r.json()
            if j.get("error"):
                raise ValueError(j["error"])
            series = next(v for k, v in (j.get("result") or {}).items() if k != "last")
            fresh = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                      float(x[5]), float(x[6])) for x in series]
            prev = (self.closes_cache.get(key) or {}).get("rows") or []
            merged = {row[0]: row for row in prev}
            merged.update({row[0]: row for row in fresh})
            self.closes_cache[key] = {"rows": [merged[t] for t in sorted(merged)][-480:],
                                      "last": (j.get("result") or {}).get("last")}
        except Exception as e:
            log.info("bar refresh for %s failed: %s", key, e)

    def _deploy_pick(self, r: dict, blob: dict, alloc: float | None = None) -> str | None:
        alloc = self.alloc if alloc is None else alloc
        sym, key = r["symbol"], r["key"]
        try:
            levels = oracle.make_levels(float(r["lo"]), float(r["hi"]), int(r["levels"]), r["kind"])
        except (KeyError, TypeError, ValueError) as e:
            return f"bad scan row ({e})"
        if len(levels) < 2:
            return "scanner band has no ladder"
        meta = (self.book.meta.get(key) if self.book is not None else None)
        try:
            levels = snap_levels(levels, meta)
        except ValueError as e:
            return str(e)
        why = feasibility(levels, alloc, meta)
        if why:
            return why
        px = self.price_fn(key)
        if not px:
            return "no price from Kraken right now"
        if not (levels[0] < px < levels[-1]):
            return f"price {oracle.fmt_px(px)} is outside the band"
        fee = float(r.get("fee_pct", blob.get("fee_pct", self.args.fee)))
        keep = ("yield_day", "yield_adj", "step_pct", "net_pct", "containment", "mae",
                "fills_day", "rt_day", "vr", "hurst", "atr_pct", "center", "k")
        meta_g = {k: r.get(k) for k in keep}
        meta_g["scan_generated"] = blob.get("generated")
        g = Grid(sym, key, levels, r["kind"], alloc, fee, self.args.taker, meta_g)
        try:
            info = g.deploy(px, self.clock())
        except ValueError as e:
            return str(e)
        with self.lock:
            self.grids[sym] = g
            self.dirty = True
        self.feed.set_symbols(self.grids.keys())
        self.journal("DEPLOY", symbol=sym, key=key, kind=g.kind, levels=g.n, lo=g.lo, hi=g.hi,
                     price=px, alloc=g.alloc, rung=g.rung, maker_pct=fee,
                     taker_pct=self.args.taker, **info, scanner=meta_g)
        self.event(f"DEPLOY {sym} {g.kind} {g.n} lvls {oracle.fmt_px(g.lo)}-{oracle.fmt_px(g.hi)} "
                   f"@ {oracle.fmt_px(px)} (${g.alloc:.0f}, scanner {r.get('yield_day', 0):.2f}%/d)")
        self.save_state()
        return None

    # ------------------------------------------------------------------- exit --
    STALE_PRINT_S = 60.0

    def check_exits(self) -> None:
        """Close grids whose exit timer has run out.

        Two ways a timer runs out: in print time (set by _apply, exact price
        and time), or on the wall clock in a market too quiet to print. The
        wall-clock path never acts on a grid that is catching up or while the
        feed is down -- the last print could be long out of date -- and if the
        last print IS old it asks Kraken for the price now before selling."""
        now = self.clock()
        # "Connected" is not enough: after a sleep/wake the socket still claims
        # to be up for 10-30 s while nothing has arrived. A live, subscribed
        # socket hears a heartbeat every second.
        feed_up = (bool(getattr(self.feed, "connected", False))
                   and time.time() - (getattr(self.feed, "last_msg", 0.0) or 0.0)
                   <= TradeFeed.STALE_S)
        exact, wall = [], []
        with self.lock:
            for s, g in self.grids.items():
                if g.status == "BROKEN":
                    exact.append((s, None, now, "ladder invariant broken"))
                elif g.exit_due:
                    px, ts, why = g.exit_due
                    exact.append((s, px, ts, why))
                elif (self.args.exit_after > 0 and g.status == "ACTIVE"
                      and s not in self.syncing and feed_up
                      and g.outside_since is not None
                      and now - g.outside_since >= self.args.exit_after
                      and self._ticker_retry.get(s, 0.0) <= now):
                    wall.append(s)
        for s, px, ts, why in exact:                 # exact exits first, never delayed
            self.close_grid(s, why, px, ts)
        for s in wall:
            if self._stop_requested:
                break                        # a wall exit needs REST: not after Ctrl+C
            with self.lock:
                g = self.grids.get(s)
                if g is None or g.outside_since is None:
                    continue
                last_px, last_ts, key, lo, hi = g.last_px, g.last_ts, g.key, g.lo, g.hi
            if last_ts is None or now - last_ts > self.STALE_PRINT_S:
                fresh = self.price_fn(key)
                if not fresh:
                    # stamp AFTER the call: a slow call must not leave the
                    # back-off already expired
                    self._ticker_retry[s] = self.clock() + 30.0
                    continue
                if lo <= fresh <= hi:
                    with self.lock:
                        g = self.grids.get(s)
                        if g is not None:
                            g.outside_since = None
                    self.event(f"{s}: last print was outside the band but the price is "
                               f"back inside ({oracle.fmt_px(fresh)}); exit timer reset")
                    continue
                last_px = fresh
            self.close_grid(s, None, last_px, self.clock(), timer_exit=True)

    def close_grid(self, symbol: str, reason: str | None, price: float | None = None,
                   ts: float | None = None, timer_exit: bool = False) -> dict | None:
        with self.lock:
            g = self.grids.get(symbol)
            if g is None:
                return None
            if timer_exit:
                # Re-decide under the lock: a print may have brought the price
                # back inside (or restarted the timer) since check_exits looked.
                now = self.clock()
                if (symbol in self.syncing or g.exit_due or g.status != "ACTIVE"
                        or g.outside_since is None
                        or now - g.outside_since < self.args.exit_after):
                    return None          # catching up / exact exit pending / back inside
                side = "below" if (price if price is not None else g.last_px) < g.lo else "above"
                reason = f"price {side} band for {hms(now - g.outside_since)}" + self._gap_label(g)
            if g.status == "BROKEN":
                g.status = "ACTIVE"
            px = price if price is not None else g.last_px
            when = ts if ts is not None else self.clock()
            s = g.close(px, when, reason)
            del self.grids[symbol]
            self.syncing.discard(symbol)
            self.pending.pop(symbol, None)
            if s:
                self.closed.append(s)
                self.closed = self.closed[-100:]
                self.totals["count"] += 1
                self.totals["realised"] += s["realised"]
                self.totals["fees"] += s["fees"]
                self.totals["round_trips"] += s["round_trips"]
                self.totals["fills"] += s["fills"]
            self.cooldown[symbol] = max(when, self.clock()) + self.args.cooldown * 60.0
            self.dirty = True
            syms = list(self.grids)
        self.feed.set_symbols(syms)
        if s:
            self.journal("EXIT", **s)
            self.event(f"EXIT {symbol} @ {oracle.fmt_px(px)}: {reason}; grid P/L "
                       f"{s['realised']:+.2f} ({s['round_trips']} round trips)", logging.WARNING)
        self.save_state()
        return s

    # ----------------------------------------------------------------- status --
    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            rows = []
            tot = {"equity": 0.0, "realised": 0.0, "unrealised": 0.0, "fees": 0.0,
                   "fills": 0, "round_trips": 0}
            for s, g in sorted(self.grids.items()):
                m = g.mark()
                days = (now - g.deployed_at) / 86400.0 if g.deployed_at else 0.0
                real_yd = (g.realised / g.alloc * 100.0 / days) if days > 0.02 else None
                scan_row = next((r for r in (self.scan or {}).get("results", [])
                                 if r.get("symbol") == s), None)
                rows.append({
                    "symbol": s, "kind": g.kind, "levels": g.n, "lo": g.lo, "hi": g.hi,
                    "price": g.last_px, "pos": ((g.last_px - g.lo) / (g.hi - g.lo)) if g.last_px else None,
                    "to_floor_pct": ((g.last_px - g.lo) / g.last_px * 100.0) if g.last_px else None,
                    "to_ceiling_pct": ((g.hi - g.last_px) / g.last_px * 100.0) if g.last_px else None,
                    "holding": g.holding(), "empty_level": g.empty, "alloc": g.alloc,
                    "realised": g.realised, "unrealised": m["unrealised"], "total": m["total"],
                    "floor_pnl": g.floor_pnl(), "fees": g.fees, "fills": g.fills,
                    "round_trips": g.round_trips, "deployed_at": iso(g.deployed_at),
                    "age_s": now - g.deployed_at if g.deployed_at else None,
                    "last_print_age_s": (now - g.last_ts) if g.last_ts else None,
                    "outside_for_s": (now - g.outside_since) if g.outside_since else None,
                    "catching_up": s in self.syncing,
                    "predicted_yield_day": g.meta.get("yield_day"),
                    "realised_yield_day": real_yd,
                    "scanner_now": ("qualified" if scan_row and scan_row.get("qualified")
                                    else ("ranked, not qualified" if scan_row else "not ranked")),
                })
                tot["equity"] += m["equity"]
                tot["realised"] += g.realised
                tot["unrealised"] += m["unrealised"]
                tot["fees"] += g.fees
                tot["fills"] += g.fills
                tot["round_trips"] += g.round_trips
            closed_pnl = self.totals["realised"]
            committed = sum(g.alloc for g in self.grids.values())
            picks = []
            for r in (self.scan or {}).get("results", [])[:8]:
                picks.append({"symbol": r.get("symbol"), "yield_day": r.get("yield_day"),
                              "qualified": bool(r.get("qualified")),
                              "basket": bool(r.get("basket")), "levels": r.get("levels"),
                              "kind": r.get("kind")})
            scan_age = (now - self.scan["_generated_ts"]) if self.scan else None
            sc = self.scanner
            return {
                "ts": iso(now), "version": VERSION, "mode": "paper",
                "uptime_s": now - self.started,
                "capital": self.capital, "max_grids": self.max_grids, "alloc_per_grid": self.alloc,
                "capital_now": self.capital_now(), "alloc_next": self.grid_alloc(),
                "halt": self.halt_reason,
                "exit_after_s": self.args.exit_after,
                "fees": {"maker": self.args.fee, "taker": self.args.taker,
                         "source": getattr(self.args, "fee_source", "default")},
                "guard": {**self.guard,
                          "max_day_loss_pct": getattr(self.args, "max_day_loss_pct", 0.0),
                          "max_drawdown_pct": getattr(self.args, "max_drawdown_pct", 0.0)},
                "ts_epoch": now,
                "grids_active": len(self.grids),
                "grids_held": len(self.unrestored),
                "open": {**tot, "deployed": committed,
                         "held": sum(self._held_alloc(d) for d in self.unrestored.values())},
                "closed": dict(self.totals),
                "total_pnl": closed_pnl + tot["realised"] + tot["unrealised"],
                "grids": rows,
                "feed": {"connected": bool(getattr(self.feed, "connected", False)),
                         "last_msg_age_s": (now - self.feed.last_msg) if getattr(self.feed, "last_msg", 0) else None,
                         "connects": getattr(self.feed, "connects", 0),
                         "errors": getattr(self.feed, "errors", 0),
                         "last_error": getattr(self.feed, "last_error", ""),
                         "catch_up_gaps": self.gaps},
                "scanner": {"running": bool(sc and sc.running()),
                            "pid": (sc.proc.pid if sc and sc.proc else None),
                            "starts": (sc.starts if sc else 0),
                            "last_scan_age_s": scan_age, "note": self.scan_note},
                "picks": picks,
                "skipped": dict(self.skipped),
                "unrestored": sorted(str((d or {}).get("symbol") or k) if isinstance(d, dict) else k
                                     for k, d in self.unrestored.items()),
                "recent_closed": self.closed[-5:],
                "events": list(self.events),
            }

    def write_status(self) -> dict:
        snap = self.snapshot()
        try:
            atomic_write_json(self.status_path, snap)
        except OSError as e:
            log.warning("status write failed: %s", e)
        return snap

    def render(self, snap: dict) -> str:
        """The console screen. Fits the window: no line wider than it, and the
        oldest events are dropped first so the header never scrolls away."""
        import shutil
        size = shutil.get_terminal_size((120, 30))
        W = max(80, min(118, size.columns - 1))
        H = max(20, size.lines - 1)
        o, c = snap["open"], snap["closed"]
        top = []
        top.append("=" * W)
        top.append(f" GRIDBOT v{VERSION}  PAPER  {snap['ts']}  up {hms(snap['uptime_s'])}"
                   f"  scanner: GridPick Oracle v{oracle.VERSION}")
        held_s = (f"+{snap['grids_held']} held" if snap.get("grids_held") else "")
        top.append(f" capital ${snap['capital']:.0f}  grids {snap['grids_active']}{held_s}/"
                   f"{snap['max_grids']} (${o['deployed'] + o.get('held', 0.0):.0f} in use)"
                   f"  P/L {snap['total_pnl']:+.2f} = open realised "
                   f"{o['realised']:+.2f} + inventory {o['unrealised']:+.2f} + closed {c['realised']:+.2f}")
        top.append(f" fees ${o['fees'] + c['fees']:.2f}  fills {o['fills'] + c['fills']}"
                   f"  round trips {o['round_trips'] + c['round_trips']}  grids closed {c['count']}")
        fd, sc = snap["feed"], snap["scanner"]
        feed_s = "connected" if fd["connected"] else "DISCONNECTED"
        if fd["last_msg_age_s"] is not None:
            feed_s += f" ({fd['last_msg_age_s']:.0f}s)"
        if fd["catch_up_gaps"]:
            feed_s += f", {fd['catch_up_gaps']} gap(s)"
        scan_s = "running" if sc["running"] else "NOT RUNNING"
        if sc["last_scan_age_s"] is not None:
            scan_s += f", scan {hms(sc['last_scan_age_s'])} ago"
        top.append(f" feed: {feed_s}  scanner: {scan_s}: {sc['note']}")
        top.append("-" * W)
        top.append(f" {'PAIR':<10}{'GRID':<8}{'BAND':<23}{'PRICE':>11} {'POSITION':<10}"
                   f"{'HELD':>6}{'REAL':>8}{'INV':>8}{'FLOOR':>8}{'RT':>5} {'%/d real/pred':<13}")
        if not snap["grids"]:
            if snap.get("grids_held"):
                top.append(f"   no running grids: {snap['grids_held']} slot(s) held by unrestored "
                           f"grid(s) -- see the UNRESTORED line below")
            else:
                top.append("   no grids yet: waiting for a qualified pick that fits the capital")
        for r in snap["grids"]:
            band = f"{short_px(r['lo'])}-{short_px(r['hi'])}"
            pos = oracle.gauge(r["price"] or r["lo"], r["lo"], r["hi"], 10)
            yd_now = f"{r['realised_yield_day']:.2f}" if r["realised_yield_day"] is not None else "-"
            yd_pr = f"{r['predicted_yield_day']:.2f}" if r["predicted_yield_day"] is not None else "-"
            flag = " SYNC" if r["catching_up"] else (" OUT" if r["outside_for_s"] else "")
            fl = r["floor_pnl"]
            body = (
                f" {r['symbol']:<10}{(r['kind'][:3] + ' ' + str(r['levels'])):<8}{band:<23}"
                f"{short_px(r['price'] or 0):>11} {pos:<10}"
                f"{str(r['holding']) + '/' + str(r['levels'] - 1):>6}"
                f"{r['realised']:>+8.2f}{r['unrealised']:>+8.2f}"
                f"{(f'{fl:+.2f}' if fl is not None else '-'):>8}"
                f"{r['round_trips']:>5} {yd_now + '/' + yd_pr:<13}")
            top.append(body[:W - len(flag)] + flag)      # the flag always survives the cut
        top = [x[:W] for x in top]
        top.append("-" * W)
        picks = "  ".join(
            f"{p['symbol']} {p['yield_day'] or 0:.2f}%{'*' if p['qualified'] else ''}"
            for p in snap["picks"][:6])
        top.append(f" scanner top (* = qualified): {picks or '-'}"[:W])
        skipped = [f"   skipped {s}: {why}"[:W] for s, why in list(snap["skipped"].items())[:3]]
        if snap.get("unrestored"):
            skipped.insert(0, (f" UNRESTORED (kept in state.json, slot held, see gridbot.log): "
                               f"{', '.join(snap['unrestored'])}")[:W])
        legend = (" REAL realised, INV open inventory, FLOOR total if price broke the floor now."
                  " Ctrl+C stops.")[:W]
        room = H - len(top) - len(skipped) - 2
        events = [f" {e}"[:W] for e in snap["events"]][-max(0, room):] if room > 0 else []
        return "\n".join(top + skipped + ["-" * W] + events + [legend])

    # ------------------------------------------------------------------- run --
    def _maybe_reload_book(self, now: float) -> None:
        """Refresh Kraken pair data every 6 h (the start may have fallen back
        to an old cached copy while the network was down)."""
        if (now - self.book_loaded_at < 6 * 3600 or self.book is None
                or self._book_reloading):
            return
        self._book_reloading = True
        self.book_loaded_at = now

        def reload():                       # off the main loop: AssetPairs can be slow
            try:
                nb = oracle.PairBook()
                if nb.load() is None and nb.meta:
                    self.book = nb
                    log.info("Kraken pair data refreshed (%d pairs)", len(nb.meta))
                else:
                    self.book_loaded_at = time.time() - 5 * 3600   # try again in an hour
            except Exception as e:
                self.book_loaded_at = time.time() - 5 * 3600
                log.info("pair data refresh failed: %s", e)
            finally:
                self._book_reloading = False
        threading.Thread(target=reload, name="book-reload", daemon=True).start()

    def _install_stop_handlers(self):
        """Ctrl+C (and Ctrl+Break) only REQUEST a stop; the loop acts on it
        between iterations. A KeyboardInterrupt raised mid-close could leave a
        grid half-booked; a flag never lands in the middle of anything."""
        import signal
        prev = {}
        if threading.current_thread() is not threading.main_thread():
            return prev

        def request(signum, frame):
            if not self._stop_requested:
                try:
                    sys.stdout.write("\nstop requested -- finishing this pass...\n")
                    sys.stdout.flush()
                except Exception:
                    pass
            self._stop_requested = True
        for name in ("SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    prev[sig] = signal.signal(sig, request)
                except (ValueError, OSError):
                    pass
        return prev

    def run(self) -> int:
        import signal
        last = {"scanner": 0.0, "consider": 0.0, "status": 0.0, "save": 0.0}
        self._stop_requested = False
        prev_handlers = self._install_stop_handlers()
        try:
            # A request left over from a forced stop must not stop THIS run.
            try:
                self.stop_path.unlink()
            except OSError:
                pass
            self.load_state()
            if self.scanner is not None:
                self.scanner.kill_orphan()
            self.feed.set_symbols(list(self.grids))
            self.feed.start()
            self.journal("START", version=VERSION, capital=self.capital,
                         max_grids=self.max_grids, grids=sorted(self.grids))
            self.event(f"started: ${self.capital:.0f} over up to {self.max_grids} grids, paper")
            while not self._stop_requested:
                now = time.time()
                if self.stop_path.exists():
                    # gridbot_stop.ps1 asks nicely first: same exit as Ctrl+C,
                    # so the final save and the STOP row are written
                    try:
                        self.stop_path.unlink()
                    except OSError:
                        pass
                    self.event("stop requested (stop.request)")
                    self._stop_requested = True
                    break
                if self.scanner is not None and now - last["scanner"] >= 5:
                    self.scanner.ensure()
                    last["scanner"] = now
                if now - last["consider"] >= 5 and not self._stop_requested:
                    self.consider()
                    last["consider"] = now
                self._maybe_reload_book(now)
                self.feed.check_stale()          # notice a dead socket BEFORE judging exits
                if (self.syncing and getattr(self.feed, "connected", False)
                        and not self._rs_running and now - last.get("heal", 0.0) >= 30):
                    # self-heal: grids waiting to catch up with nothing working on it
                    last["heal"] = now
                    self.request_resync()
                self.check_exits()
                if self.dirty or now - last["save"] >= 30:
                    self.save_state()
                    last["save"] = now
                if now - last["status"] >= 5:
                    snap = self.write_status()
                    if self.render_console:
                        sys.stdout.write("\033[H\033[J" + self.render(snap) + "\n")
                        sys.stdout.flush()
                    last["status"] = now
                time.sleep(0.5)
            self.event("stopping")
            return 0
        except KeyboardInterrupt:              # e.g. a non-main-thread run, or tests
            self.event("stopping (Ctrl+C)")
            return 0
        except Exception as e:
            # run.bat restarts after a crash and the new screen hides the old
            # traceback, so it goes to the log and the journal first.
            log.exception("GridBot crashed")
            self.journal("CRASH", error=repr(e))
            raise
        finally:
            # Only save a state that was actually loaded: if loading itself
            # failed, saving now would write emptiness over state.json AND
            # state.json.bak, and the restart would begin clean.
            # Fence first: no print is applied after this point, so the final
            # save really is final (nothing booked after it, nothing journalled
            # after STOP, nothing booked twice on the next start).
            with self.lock:
                self._stopping = True
            self.feed.stop()
            if self._state_loaded:
                self.save_state()
                self.journal("STOP", grids=sorted(self.grids))
            if self.scanner is not None:
                self.scanner.stop()
            for sig, h in prev_handlers.items():
                try:
                    signal.signal(sig, h)
                except (ValueError, OSError, TypeError):
                    pass


# =============================================================================
# ENTRY
# =============================================================================
def parse_args(argv=None):
    env_cap = os.environ.get("GRIDPICK_POOL_USD", "").strip()
    p = argparse.ArgumentParser(
        description="Paper spot-grid bot on the GridPick Oracle v3.1 scanner.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--capital", type=float, default=float(env_cap) if env_cap else 500.0,
                   help="paper capital in USD (default: GRIDPICK_POOL_USD, else 500)")
    p.add_argument("--max-grids", type=int, default=3, help="grids at once; each gets capital/max-grids")
    p.add_argument("--fee", type=float, default=None,
                   help="maker fee %% (also given to the scanner); default: the account's Kraken tier, else 0.22")
    p.add_argument("--taker", type=float, default=None,
                   help="taker fee %% for deploy buys and exits; default: the account's Kraken tier, else 0.38")
    p.add_argument("--exit-after", type=float, default=300.0,
                   help="seconds outside the band before the grid is closed; 0 = never")
    p.add_argument("--cooldown", type=float, default=60.0, help="minutes a pair sits out after an exit")
    p.add_argument("--max-day-loss-pct", type=float, default=6.0,
                   help="no new grids once the book is down this %% of capital since the day's start; 0 = off")
    p.add_argument("--max-drawdown-pct", type=float, default=12.0,
                   help="no new grids while book equity is this %% below its peak; 0 = off")
    p.add_argument("--scan-refresh", type=int, default=120, help="scanner --refresh seconds")
    p.add_argument("--scan-pace", type=float, default=0.5, help="scanner --pace seconds between REST calls")
    p.add_argument("--scan-arg", action="append", help="extra argument passed to oracle.py (repeatable)")
    p.add_argument("--no-scanner", action="store_true", help="do not start oracle.py; read scan.json only")
    p.add_argument("--data-dir", default=str(HERE), help="where state, journal, status and logs live")
    p.add_argument("--status", action="store_true", help="print status.json and exit")
    a = p.parse_args(argv)
    if a.capital <= 0 or a.max_grids < 1:
        p.error("--capital must be > 0 and --max-grids >= 1")
    if not (0 <= a.max_day_loss_pct < 100 and 0 <= a.max_drawdown_pct < 100):
        p.error("--max-day-loss-pct and --max-drawdown-pct must be in [0, 100)")
    # A fee given on the command line always wins over the Kraken tier lookup.
    a.fee_given, a.taker_given = a.fee is not None, a.taker is not None
    a.fee = DEFAULT_MAKER if a.fee is None else a.fee
    a.taker = DEFAULT_TAKER if a.taker is None else a.taker
    a.fee_source = "command line" if (a.fee_given or a.taker_given) else "default"
    return a


# ------------------------------------------------------------ fee tier --
DEFAULT_MAKER, DEFAULT_TAKER = 0.22, 0.38


def kraken_query_creds() -> tuple[str, str] | None:
    """The QUERY-ONLY key pair: process environment first, then the user's
    registry environment (a bot started before the variables were set has a
    stale environment block). None when not configured."""
    k, s = os.environ.get("GRIDBOT_KRAKEN_KEY"), os.environ.get("GRIDBOT_KRAKEN_SECRET")
    if (not k or not s) and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                k = k or winreg.QueryValueEx(h, "GRIDBOT_KRAKEN_KEY")[0]
                s = s or winreg.QueryValueEx(h, "GRIDBOT_KRAKEN_SECRET")[0]
        except OSError:
            pass
    return (k, s) if k and s else None


def fetch_trade_volume(creds: tuple[str, str], pair: str = "XBTUSD") -> dict:
    """Kraken private TradeVolume (read-only). Returns the 'result' dict;
    raises on any error. The key never reaches a log or a file."""
    import base64
    import hashlib
    import hmac
    import urllib.parse
    import requests
    key, secret = creds
    path = "/0/private/TradeVolume"
    data = {"nonce": str(int(time.time() * 1000)), "pair": pair}
    post = urllib.parse.urlencode(data)
    msg = path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest()
    sig = base64.b64encode(hmac.new(base64.b64decode(secret), msg, hashlib.sha512).digest()).decode()
    r = requests.post("https://api.kraken.com" + path, data=data, timeout=(5, 10),
                      headers={"API-Key": key, "API-Sign": sig, "User-Agent": "gridbot"})
    j = r.json()
    if j.get("error"):
        raise RuntimeError("; ".join(j["error"]))
    return j["result"]


def resolve_fees(args, creds=None, fetch=fetch_trade_volume) -> str:
    """Set args.fee / args.taker from the account's Kraken tier unless they
    were given on the command line. Returns a one-line note for the log.
    Fails OPEN to the defaults: paper fills do not need the key to be right."""
    if args.fee_given and args.taker_given:
        return f"fees {args.fee:.2f}/{args.taker:.2f} from the command line"
    if creds is None:
        return f"fees {args.fee:.2f}/{args.taker:.2f} (default: no GRIDBOT_KRAKEN_KEY)"
    try:
        res = fetch(creds)
        maker = float(next(iter(res["fees_maker"].values()))["fee"])
        taker = float(next(iter(res["fees"].values()))["fee"])
        vol = float(res.get("volume") or 0.0)
    except Exception as e:
        args.fee_source = "default (tier lookup failed)"
        return (f"fees {args.fee:.2f}/{args.taker:.2f} (default: Kraken tier lookup failed: "
                f"{type(e).__name__}: {str(e)[:120]})")
    if not (0 <= maker < 5 and 0 <= taker < 5):
        args.fee_source = "default (tier lookup implausible)"
        return f"fees {args.fee:.2f}/{args.taker:.2f} (default: Kraken said {maker}/{taker})"
    if not args.fee_given:
        args.fee = maker
    if not args.taker_given:
        args.taker = taker
    args.fee_source = f"Kraken tier (30-day volume ${vol:,.0f})"
    return f"fees {args.fee:.2f}/{args.taker:.2f} from the {args.fee_source}"


def setup_logging(data_dir: Path) -> None:
    h = logging.handlers.RotatingFileHandler(data_dir / "gridbot.log", maxBytes=5_000_000,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(h)


def main(argv=None) -> int:
    args = parse_args(argv)
    data = Path(args.data_dir).resolve()
    args.data_dir = str(data)
    data.mkdir(parents=True, exist_ok=True)
    if args.status:
        try:
            print((data / "status.json").read_text(encoding="utf-8"))
            return 0
        except OSError as e:
            print(f"no status yet: {e}")
            return 1
    lock = acquire_single_instance(data / "gridbot.lock")
    if lock is None:
        print("GridBot is already running from this folder.")
        return EXIT_ALREADY_RUNNING
    setup_logging(data)
    if os.name == "nt":
        os.system("")          # turn on ANSI escapes in the Windows console
    # Pair data (minimum order sizes, tick sizes). A network blip at start must
    # not end the bot: use the scanner's cached copy however old (minimums
    # rarely change), else keep retrying.
    book = oracle.PairBook()
    delay = 15
    from_stale_cache = False
    while True:
        err = book.load()
        if not err:
            break
        stale = oracle.PairBook()
        if stale.load(max_age_s=float("inf")) is None and stale.meta:
            book = stale
            from_stale_cache = True
            log.warning("AssetPairs failed (%s); using the cached pair list", err)
            break
        print(f"Kraken AssetPairs failed: {err} -- retrying in {delay}s (Ctrl+C to stop)")
        log.error("AssetPairs failed: %s; retrying in %ds", err, delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0
        delay = min(300, delay * 2)
    # Before the scanner starts: it is handed the same maker fee (--fee).
    fee_note = resolve_fees(args, kraken_query_creds())
    log.info(fee_note)
    scanner = None if args.no_scanner else Scanner(data, data / "scan.json", args)
    bot = Bot(args, scanner=scanner, book=book)
    if from_stale_cache:
        bot.book_loaded_at = time.time() - 5 * 3600     # try a live reload in an hour
    log.info("GridBot %s starting: capital %.2f, max grids %d, fee %.2f/%.2f",
             VERSION, args.capital, args.max_grids, args.fee, args.taker)
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
