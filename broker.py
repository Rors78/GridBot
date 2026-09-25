"""Kraken order-path plumbing for live readiness. VALIDATE-ONLY BUILD.

KrakenBroker signs and sends private Kraken REST calls. add_order() always
sends validate=true and REFUSES anything else: no code path in this file or
this repo places, amends or cancels a real order. Arming live placement is a
deliberate future change behind the go-live gate in CLAUDE.md, not a flag.

There is deliberately NO cancel_all here. The account trades manually too
(real balances, real resting orders); a blanket CancelAll would wipe orders
this bot never placed. Any future live cancel must be scoped to the grid's
userref, which every validated order already carries.

What a VALIDATE pass proves: the pair exists and the price/volume precision,
minimums and parameter shape are acceptable. What it does NOT prove: funds,
or that a post-only order would rest without crossing -- an order validated
right after a paper fill was just crossed by the market, so its live
post-only twin would often be rejected. A VALIDATE pass is a parameter
check, never "the exchange would have accepted this order".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import queue
import threading
import time
import urllib.parse
import zlib

API = "https://api.kraken.com"
NOT_ARMED = ("refusing to place a real order: this is the validate-only build; "
             "arming live placement is a deliberate code change behind the "
             "go-live gate in CLAUDE.md, not a flag")


class BrokerError(RuntimeError):
    """Kraken answered with an error list (EAPI:..., EOrder:..., EGeneral:...)."""


def trade_creds() -> tuple[str, str] | None:
    """The TRADE key pair (create-order permission, for AddOrder validate).
    Process environment first, then the user's registry environment. None
    when not configured. Never the query key: permissions stay separate."""
    k = os.environ.get("GRIDBOT_KRAKEN_TRADE_KEY")
    s = os.environ.get("GRIDBOT_KRAKEN_TRADE_SECRET")
    if (not k or not s) and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                k = k or winreg.QueryValueEx(h, "GRIDBOT_KRAKEN_TRADE_KEY")[0]
                s = s or winreg.QueryValueEx(h, "GRIDBOT_KRAKEN_TRADE_SECRET")[0]
        except OSError:
            pass
    return (k, s) if k and s else None


def grid_userref(symbol: str, deployed_at: float | None) -> int:
    """Deterministic positive int32 identifying one grid deployment. Every
    order the grid would place live carries it, so a future live cancel or
    reconcile can be scoped to THIS bot's orders and nothing else."""
    tag = f"{symbol}:{int(deployed_at or 0)}"
    return zlib.crc32(tag.encode()) & 0x7FFFFFFF


class PairRules:
    """Rounding and minimums for one pair, from Kraken AssetPairs metadata
    (oracle.PairBook.meta[key]). Prices snap to the pair's tick; volumes
    floor to lot_decimals (never round up: an order for coins you do not
    quite have is rejected live)."""

    def __init__(self, meta: dict | None):
        meta = meta or {}
        def _f(name, default=0.0):
            try:
                return float(meta.get(name) or default)
            except (TypeError, ValueError):
                return default
        def _i(name, default):
            try:
                return int(meta.get(name))
            except (TypeError, ValueError):
                return default
        self.pair_decimals = _i("pair_decimals", 8)
        self.lot_decimals = _i("lot_decimals", 8)
        self.ordermin = _f("ordermin")
        self.costmin = _f("costmin")
        self.tick = _f("tick_size") or 10.0 ** -self.pair_decimals

    def price_str(self, price: float) -> str:
        snapped = round(round(price / self.tick) * self.tick, self.pair_decimals)
        return f"{snapped:.{self.pair_decimals}f}"

    def volume_str(self, qty: float) -> str:
        scale = 10 ** self.lot_decimals
        floored = int(qty * scale + 1e-9) / scale
        return f"{floored:.{self.lot_decimals}f}"

    def check(self, price: float, volume: float) -> str | None:
        """None if Kraken would take these AFTER rounding, else the reason.
        Flooring the volume can push a rung under ordermin: check last."""
        if self.ordermin and volume < self.ordermin - 1e-12:
            return f"volume {volume:g} under Kraken's minimum order of {self.ordermin:g}"
        if self.costmin and price * volume < self.costmin - 1e-9:
            return (f"cost ${price * volume:.4f} under Kraken's minimum "
                    f"of ${self.costmin:g}")
        return None


class KrakenBroker:
    """Signed private Kraken REST calls: Balance, OpenOrders, AddOrder
    (validate only). One instance per key; calls are paced and the nonce is
    monotonic under a lock, so threads can share it."""

    def __init__(self, key: str, secret: str, transport=None, min_interval: float = 1.0):
        self._key = key
        self._secret = secret
        self._transport = transport or self._post
        self._min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._last_nonce = 0
        self._last_call = 0.0

    # -------------------------------------------------------------- plumbing --
    @staticmethod
    def _post(path: str, data: dict, headers: dict) -> dict:
        import requests
        r = requests.post(API + path, data=data, timeout=(5, 10),
                          headers={**headers, "User-Agent": "gridbot"})
        return r.json()

    def _nonce(self) -> str:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return str(n)

    def _private(self, path: str, data: dict) -> dict:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            data = dict(data)
            data["nonce"] = self._nonce()
            post = urllib.parse.urlencode(data)
            msg = path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest()
            sig = base64.b64encode(hmac.new(base64.b64decode(self._secret), msg,
                                            hashlib.sha512).digest()).decode()
            try:
                j = self._transport(path, data, {"API-Key": self._key, "API-Sign": sig})
            finally:
                self._last_call = time.monotonic()
        if not isinstance(j, dict):
            raise BrokerError(f"{path}: malformed answer ({type(j).__name__})")
        if j.get("error"):
            raise BrokerError("; ".join(str(e) for e in j["error"]))
        return j.get("result", {})

    # ------------------------------------------------------------------ calls --
    def balance(self) -> dict[str, float]:
        """Non-zero balances, asset -> amount."""
        res = self._private("/0/private/Balance", {})
        out = {}
        for asset, amt in res.items():
            try:
                v = float(amt)
            except (TypeError, ValueError):
                continue
            if v:
                out[asset] = v
        return out

    def open_orders(self) -> dict:
        """Open orders as Kraken returns them (txid -> order)."""
        res = self._private("/0/private/OpenOrders", {})
        return res.get("open", {})

    def add_order(self, pair: str, side: str, volume: str, price: str | None = None,
                  ordertype: str = "limit", userref: int | None = None,
                  post_only: bool = True, validate: bool = True) -> dict:
        """AddOrder with validate=true: Kraken checks the order and places
        nothing. validate=False raises -- see NOT_ARMED."""
        if not validate:
            raise RuntimeError(NOT_ARMED)
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy or sell, not {side!r}")
        data = {"pair": pair, "type": side, "ordertype": ordertype,
                "volume": volume, "validate": "true"}
        if ordertype == "limit":
            if price is None:
                raise ValueError("a limit order needs a price")
            data["price"] = price
            if post_only:
                data["oflags"] = "post"
        if userref is not None:
            data["userref"] = str(int(userref))
        return self._private("/0/private/AddOrder", data)


class OrderValidator:
    """Background worker that mirrors the bot's paper orders to AddOrder
    validate=true and journals every answer. Trading never waits on it: the
    queue is bounded and a full queue drops (counted), never blocks."""

    SENTINEL = object()

    def __init__(self, broker: KrakenBroker, journal, event, maxsize: int = 64):
        self.broker = broker
        self.journal = journal
        self.event = event
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.stats = {"sent": 0, "ok": 0, "fail": 0, "dropped": 0,
                      "last_error": None, "last_ts": None}
        self._last_event_error = None
        self._thread = threading.Thread(target=self._run, name="order-validator",
                                        daemon=True)
        self._thread.start()

    def submit(self, tag: str, symbol: str, pair: str, side: str, volume: str,
               price: str | None = None, ordertype: str = "limit",
               userref: int | None = None) -> None:
        try:
            self.q.put_nowait((tag, symbol, pair, side, volume, price, ordertype, userref))
        except queue.Full:
            self.stats["dropped"] += 1

    def stop(self) -> None:
        try:
            self.q.put_nowait(self.SENTINEL)
        except queue.Full:
            pass
        self._thread.join(timeout=3)

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is self.SENTINEL:
                return
            tag, symbol, pair, side, volume, price, ordertype, userref = item
            row = {"tag": tag, "symbol": symbol, "pair": pair, "side": side,
                   "ordertype": ordertype, "volume": volume, "userref": userref}
            if price is not None:
                row["price"] = price
            self.stats["sent"] += 1
            try:
                res = self.broker.add_order(pair, side, volume, price=price,
                                            ordertype=ordertype, userref=userref)
                row["ok"] = True
                descr = (res.get("descr") or {}).get("order")
                if descr:
                    row["descr"] = descr
                self.stats["ok"] += 1
            except Exception as e:
                row["ok"] = False
                row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                self.stats["fail"] += 1
                self.stats["last_error"] = row["error"]
                if row["error"] != self._last_event_error:
                    self._last_event_error = row["error"]
                    self.event(f"VALIDATE {symbol} {side} failed: {row['error']}")
            self.stats["last_ts"] = time.time()
            try:
                self.journal("VALIDATE", **row)
            except Exception:
                pass
