#!/usr/bin/env python3
# =============================================================================
# GRIDPICK ORACLE v3.1
# Kraken spot-grid pair scanner.
#
# Ranks pairs by MEASURED expected daily grid yield.
# Replay the actual OHLC path through a candidate grid, count real level
# crossings, simulate inventory, and charge fees + spread + a slippage
# haircut. Not a hand-weighted heuristic.
#
# For every pair it sweeps (band width × level count × arithmetic/geometric)
# and keeps the configuration that maximises risk-adjusted net yield after
# costs, penalised by time-outside-band and one-sided inventory excursion.
#
# Kraken public REST only. No keys. No CoinGecko.
# =============================================================================
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = "3.1"
KRAKEN = "https://api.kraken.com/0/public"

BASE_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
DISPLAY_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
USD_QUOTES = {"ZUSD", "USD", "USDT", "USDC", "PYUSD", "DAI", "RLUSD"}

VALID_INTERVALS = {1, 5, 15, 30, 60, 240, 1440, 10080, 21600}
INTERVAL_WORDS = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "60m": 60, "4h": 240, "240m": 240,
    "1d": 1440, "1440m": 1440, "1w": 10080,
}

DEFAULT_WATCH = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "LINK/USD",
    "LTC/USD", "DOGE/USD", "AVAX/USD", "SUI/USD", "ENA/USD", "PEPE/USD",
    "WIF/USD", "BONK/USD", "WLD/USD", "HBAR/USD", "SEI/USD", "TRUMP/USD",
    "EIGEN/USD", "TIA/USD", "INJ/USD", "NEAR/USD", "APT/USD", "ARB/USD",
    "OP/USD", "DOT/USD", "ATOM/USD", "FIL/USD", "RENDER/USD", "JUP/USD",
]

# Fibonacci-ish search space. Band half-width = k * ATR%.
K_MULTS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0)
N_LEVELS = (8, 12, 16, 20, 24, 30, 36, 48, 60, 72)
GRID_KINDS = ("arith", "geo")


# ----------------------------------------------------------------- terminal --
class C:
    def __init__(self, on: bool = True):
        def p(code: str) -> str:
            return code if on else ""
        self.gry = p("\033[90m"); self.red = p("\033[91m")
        self.grn = p("\033[92m"); self.ylw = p("\033[93m")
        self.blu = p("\033[94m"); self.mag = p("\033[95m")
        self.cyn = p("\033[96m"); self.wht = p("\033[97m")
        self.dim = p("\033[2m"); self.bold = p("\033[1m")
        self.rst = p("\033[0m")


FG = C(True)
BLOCKS = "▁▂▃▄▅▆▇█"


def term_width(default: int = 120) -> int:
    try:
        return max(80, shutil.get_terminal_size((default, 40)).columns)
    except Exception:
        return default


def spark(vals: list[float], width: int = 22) -> str:
    if not vals:
        return ""
    if len(vals) > width:
        stride = len(vals) / width
        vals = [vals[min(len(vals) - 1, int(i * stride))] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return BLOCKS[0] * len(vals)
    rng = hi - lo
    return "".join(BLOCKS[min(7, int((v - lo) / rng * 7.999))] for v in vals)


def gauge(px: float, lo: float, hi: float, width: int = 18) -> str:
    if hi <= lo:
        return "-" * width
    f = min(1.0, max(0.0, (px - lo) / (hi - lo)))
    i = int(f * (width - 1))
    return "".join("●" if j == i else "─" for j in range(width))


def fmt_px(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    if p >= 1e-5:
        return f"{p:.8f}"
    return f"{p:.10f}"


def fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= div:
            return f"{sign}${v / div:.1f}{unit}"
    return f"{sign}${v:.0f}"


def clamp(x: float, a: float, b: float) -> float:
    return a if x < a else b if x > b else x


# --------------------------------------------------------------- http layer --
class Pace:
    """Global minimum spacing between Kraken calls, thread-safe."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"GridPickOracle/{VERSION}"})
_retry = Retry(
    total=4,
    backoff_factor=0.9,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)
SESSION.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=24))
PACE = Pace(0.22)


def kget(path: str, params: dict | None = None, timeout: float = 18) -> tuple[Any, str | None]:
    """Returns (result, error_string). Honours Kraken's error array."""
    PACE.wait()
    try:
        r = SESSION.get(f"{KRAKEN}/{path}", params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return None, f"{e.__class__.__name__}: {e}"
    errs = j.get("error") or []
    if errs:
        return None, "; ".join(str(x) for x in errs)
    res = j.get("result")
    if res is None:
        return None, "empty result"
    return res, None


# -------------------------------------------------------------- disk cache --
def cache_dir() -> Path:
    env = os.environ.get("GRIDPICK_CACHE")
    if env:
        p = Path(env)
    else:
        p = Path.home() / ".cache" / "gridpick_oracle"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_load(name: str, max_age_s: float) -> Any | None:
    path = cache_dir() / name
    try:
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age_s:
            return None
        with path.open("rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _cache_save(name: str, obj: Any) -> None:
    path = cache_dir() / name
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("wb") as f:
            pickle.dump(obj, f, protocol=4)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ------------------------------------------------------------- pair registry --
class PairBook:
    def __init__(self):
        self.meta: dict[str, dict] = {}
        self.by_ws: dict[str, str] = {}
        self.by_alt: dict[str, str] = {}

    def load(self, max_age_s: float = 6 * 3600) -> str | None:
        cached = _cache_load("assetpairs.pkl", max_age_s)
        if isinstance(cached, dict) and cached.get("meta"):
            self.meta = cached["meta"]
            self.by_ws = cached["by_ws"]
            self.by_alt = cached["by_alt"]
            return None
        res, err = kget("AssetPairs")
        if err:
            return err
        for key, p in res.items():
            if p.get("status") != "online":
                continue
            self.meta[key] = p
            ws = (p.get("wsname") or "").upper()
            alt = (p.get("altname") or "").upper()
            if ws:
                self.by_ws[ws] = key
            if alt:
                self.by_alt[alt] = key
        _cache_save("assetpairs.pkl", {
            "meta": self.meta, "by_ws": self.by_ws, "by_alt": self.by_alt,
        })
        return None

    def resolve(self, sym: str) -> tuple[str | None, str | None]:
        """'SOL/USDT' -> (kraken_key, 'SOL/USDT') with quote fallback."""
        base, _, quote = sym.upper().partition("/")
        quote = quote or "USD"
        bases = list(dict.fromkeys([base, BASE_ALIASES.get(base, base)]))
        quotes = list(dict.fromkeys([quote, "USD", "USDT", "USDC"]))
        cands = [f"{b}/{q}" for b in bases for q in quotes]
        for c in cands:
            k = self.by_ws.get(c)
            if k:
                return k, c
        for c in cands:
            k = self.by_alt.get(c.replace("/", ""))
            if k:
                return k, c
        return None, None

    def label(self, key: str) -> str:
        p = self.meta.get(key, {})
        raw = (p.get("wsname") or p.get("altname") or key).upper()
        return self.pretty(raw)

    @staticmethod
    def pretty(sym: str) -> str:
        """XBT/USD -> BTC/USD so the board reads like a human wrote it."""
        s = (sym or "").upper()
        for src, dst in DISPLAY_ALIASES.items():
            if s.startswith(src + "/") or s.startswith(src):
                return dst + s[len(src):]
        return s

    def lot_decimals(self, key: str) -> int:
        try:
            return int(self.meta.get(key, {}).get("lot_decimals", 8))
        except Exception:
            return 8

    def pair_decimals(self, key: str) -> int:
        try:
            return int(self.meta.get(key, {}).get("pair_decimals", 5))
        except Exception:
            return 5


# ------------------------------------------------------------- market data --
def fetch_ticker(keys: list[str] | None) -> dict[str, dict]:
    """{key: last, turnover24, spread_pct, vwap24, vol24}."""
    out: dict[str, dict] = {}
    batches = [None] if keys is None else [keys[i:i + 50] for i in range(0, len(keys), 50)]
    for b in batches:
        params = {"pair": ",".join(b)} if b else None
        res, err = kget("Ticker", params)
        if err or not res:
            continue
        for k, t in res.items():
            try:
                last = float(t["c"][0])
                vol24 = float(t["v"][1])
                vwap24 = float(t["p"][1])
                ask = float(t["a"][0])
                bid = float(t["b"][0])
                mid = (ask + bid) / 2.0
                out[k] = {
                    "last": last,
                    "vwap24": vwap24 or last,
                    "vol24": vol24,
                    "turnover24": vol24 * (vwap24 or last),
                    "spread_pct": ((ask - bid) / mid * 100.0) if mid > 0 else 99.0,
                    "ask": ask,
                    "bid": bid,
                }
            except Exception:
                continue
    return out


def fetch_ohlc(key: str, interval: int, cache: dict, max_bars: int) -> tuple[list | None, str | None]:
    """Incremental OHLC with `since` resume. rows = (ts, o, h, l, c, vwap, vol)."""
    params: dict[str, Any] = {"pair": key, "interval": interval}
    prev = cache.get(key)
    if prev and prev.get("rows"):
        params["since"] = prev["last"]
    res, err = kget("OHLC", params)
    if err:
        return None, err
    last_id = res.get("last")
    series = None
    for k, v in res.items():
        if k != "last":
            series = v
            break
    if series is None:
        return None, "no series"

    fresh = []
    for r in series:
        try:
            # Kraken: time, open, high, low, close, vwap, volume, count
            fresh.append((
                int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                float(r[4]), float(r[5]), float(r[6]),
            ))
        except Exception:
            continue

    if prev and prev.get("rows"):
        merged = {row[0]: row for row in prev["rows"]}
        merged.update({row[0]: row for row in fresh})
        rows = [merged[t] for t in sorted(merged)]
    else:
        rows = fresh

    rows = rows[-max_bars:]
    cache[key] = {"rows": rows, "last": last_id}
    return rows, None


def persist_ohlc_cache(cache: dict, interval: int) -> None:
    _cache_save(f"ohlc_{interval}.pkl", cache)


def load_ohlc_cache(interval: int, max_age_s: float = 36 * 3600) -> dict:
    cached = _cache_load(f"ohlc_{interval}.pkl", max_age_s)
    return cached if isinstance(cached, dict) else {}


# -------------------------------------------------------------- indicators --
def wilder_atr_pct(rows: list, period: int = 14) -> float:
    if len(rows) < period + 2:
        return 0.0
    trs = []
    for i in range(1, len(rows)):
        h, l = rows[i][2], rows[i][3]
        pc = rows[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    px = rows[-1][4]
    return (atr / px) * 100.0 if px > 0 else 0.0


def efficiency_ratio(closes: list[float]) -> float:
    """Kaufman ER. 0 = pure noise (grid heaven), 1 = pure trend (grid hell)."""
    if len(closes) < 3:
        return 1.0
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net / path if path > 0 else 1.0


def variance_ratio(closes: list[float], q: int = 8) -> float:
    """Lo-MacKinlay VR. <1 mean-reverting, \~1 random walk, >1 trending."""
    rets = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    n = len(rets)
    if n < q * 4:
        return 1.0
    m1 = sum(rets) / n
    v1 = sum((r - m1) ** 2 for r in rets) / (n - 1)
    if v1 <= 0:
        return 1.0
    agg = [sum(rets[i:i + q]) for i in range(0, n - q + 1)]
    if len(agg) < 3:
        return 1.0
    mq = sum(agg) / len(agg)
    vq = sum((a - mq) ** 2 for a in agg) / (len(agg) - 1)
    return vq / (q * v1)


def hurst_rs(closes: list[float], min_win: int = 16) -> float:
    """Hurst via rescaled range. 0.5 RW, <0.5 mean-revert, >0.5 trend."""
    if len(closes) < min_win * 2:
        return 0.5
    logp = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            logp.append(math.log(closes[i] / closes[i - 1]))
    n = len(logp)
    if n < min_win:
        return 0.5
    sizes, logs = [], []
    win = min_win
    while win <= n // 2:
        chunks = n // win
        if chunks < 2:
            break
        rs_vals = []
        for c in range(chunks):
            sl = logp[c * win:(c + 1) * win]
            mu = sum(sl) / win
            dev = [x - mu for x in sl]
            y = 0.0
            path = []
            for d in dev:
                y += d
                path.append(y)
            r = max(path) - min(path)
            var = sum(d * d for d in dev) / win
            if var > 0 and r > 0:
                rs_vals.append(r / math.sqrt(var))
        if rs_vals:
            sizes.append(math.log(win))
            logs.append(math.log(sum(rs_vals) / len(rs_vals)))
        win *= 2
    if len(sizes) < 2:
        return 0.5
    # simple OLS slope
    mx = sum(sizes) / len(sizes)
    my = sum(logs) / len(logs)
    num = sum((x - mx) * (y - my) for x, y in zip(sizes, logs))
    den = sum((x - mx) ** 2 for x in sizes)
    if den <= 0:
        return 0.5
    return clamp(num / den, 0.15, 0.85)


def half_life_bars(closes: list[float]) -> float | None:
    """Ornstein-Uhlenbeck half-life in bars via AR(1) on demeaned log price."""
    if len(closes) < 30:
        return None
    ys = [math.log(c) for c in closes if c > 0]
    if len(ys) < 30:
        return None
    mu = sum(ys) / len(ys)
    x = [ys[i] - mu for i in range(len(ys) - 1)]
    y = [ys[i + 1] - mu for i in range(len(ys) - 1)]
    xx = sum(a * a for a in x)
    xy = sum(a * b for a, b in zip(x, y))
    if xx <= 0:
        return None
    phi = xy / xx
    if phi <= 0 or phi >= 0.999:
        return None
    hl = math.log(2.0) / -math.log(phi)
    return hl if 0.5 < hl < 500 else None


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return 0.0
    return num / (da * db)


# ------------------------------------------------------------- grid engine --
def path_points(rows: list) -> list[float]:
    """Approximate the intrabar path so level crossings can be counted.

    Up bar -> open, low, high, close
    Down bar-> open, high, low, close
    Opens are included so overnight gaps count as crossings.
    """
    pts: list[float] = []
    for row in rows:
        _, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        if c >= o:
            pts.extend((o, l, h, c))
        else:
            pts.extend((o, h, l, c))
    return pts


def close_points(rows: list) -> list[float]:
    return [row[4] for row in rows]


def make_levels(lo: float, hi: float, n: int, kind: str) -> list[float]:
    """n price levels from lo to hi inclusive."""
    if n < 2 or hi <= lo:
        return []
    if kind == "geo":
        if lo <= 0:
            return []
        ratio = (hi / lo) ** (1.0 / (n - 1))
        lv = [lo]
        x = lo
        for _ in range(n - 2):
            x *= ratio
            lv.append(x)
        lv.append(hi)
        return lv
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def crossings_uniform(pts: list[float], lo: float, hi: float, n_intervals: int) -> int:
    """Exact count of uniform-grid interval crossings. O(len(pts))."""
    if hi <= lo or n_intervals < 1 or not pts:
        return 0
    inv = n_intervals / (hi - lo)
    total = 0
    p0 = pts[0]
    prev = 0 if p0 <= lo else (n_intervals if p0 >= hi else int((p0 - lo) * inv))
    for p in pts:
        if p <= lo:
            i = 0
        elif p >= hi:
            i = n_intervals
        else:
            i = int((p - lo) * inv)
        if i != prev:
            total += i - prev if i > prev else prev - i
            prev = i
    return total


def simulate_grid(pts: list[float], levels: list[float], fee_pct: float,
                  slip_pct: float) -> dict:
    """Walk the path through discrete levels.

    Inventory is measured in 'rungs' (one unit per level). Cash marks the
    realised grid edge after fees. MAE is peak |inventory| as a fraction of
    (n-1) rungs — i.e. how one-sided the book got.
    """
    n = len(levels)
    empty = {
        "fills": 0, "round_trips": 0.0, "realised_pct": 0.0,
        "mae_rungs": 0.0, "end_inv": 0.0, "flip_rate": 0.0,
    }
    if n < 2 or len(pts) < 2:
        return empty

    # locate starting rung
    def rung_of(px: float) -> int:
        if px <= levels[0]:
            return 0
        if px >= levels[-1]:
            return n - 1
        # binary search last level <= px
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if levels[mid] <= px:
                lo = mid
            else:
                hi = mid - 1
        return lo

    cost = (fee_pct + slip_pct) / 100.0
    inv = 0 # +long base / -short base, in rungs from mid
    mid_r = (n - 1) / 2.0
    fills = 0
    realised = 0.0 # in units of (rung notional)
    prev = rung_of(pts[0])
    peak_abs = 0
    direction_changes = 0
    last_dir = 0

    for p in pts[1:]:
        cur = rung_of(p)
        if cur == prev:
            continue
        step_dir = 1 if cur > prev else -1
        r = prev
        while r != cur:
            nxt = r + step_dir
            # crossing from r -> nxt means we traded the level we passed
            # moving up: sell one rung; moving down: buy one rung
            inv += -step_dir
            fills += 1
            # each completed opposite fill realises (step% - 2*cost) of one
            # rung notional. Approximate step as local spacing.
            a, b = levels[min(r, nxt)], levels[max(r, nxt)]
            if a > 0:
                step = (b - a) / a
                edge = step - 2.0 * cost
                # We accumulate raw edge * 0.5 so a full RT = one step-2fee.
                realised += max(0.0, edge) * 0.5
            if last_dir and step_dir != last_dir:
                direction_changes += 1
            last_dir = step_dir
            r = nxt
            if abs(inv) > peak_abs:
                peak_abs = abs(inv)
        prev = cur

    span_rungs = max(1, n - 1)
    rt = fills / 2.0
    # realised is in "fraction of one-rung notional". Convert to % of full
    # stack by dividing by n (equal capital per level).
    realised_stack_pct = (realised / n) * 100.0
    return {
        "fills": fills,
        "round_trips": rt,
        "realised_pct": realised_stack_pct,
        "mae_rungs": peak_abs / span_rungs,
        "end_inv": inv / span_rungs,
        "flip_rate": direction_changes / max(1, fills),
    }


def time_in_band(rows: list, lo: float, hi: float) -> tuple[float, float]:
    """Return (close-containment, volume-weighted containment)."""
    if not rows:
        return 0.0, 0.0
    inside = 0
    v_in = 0.0
    v_all = 0.0
    for r in rows:
        c, vol = r[4], r[6] if len(r) > 6 else 1.0
        v_all += vol
        if lo <= c <= hi:
            inside += 1
            v_in += vol
    return inside / len(rows), (v_in / v_all if v_all > 0 else 0.0)


def optimize_grid(rows: list, px: float, atr_pct: float, fee_pct: float,
                  bars_per_day: float, min_step_mult: float,
                  spread_pct: float, slip_mult: float) -> dict | None:
    """Sweep band × levels × kind. Keep best risk-adjusted realised yield."""
    pts = path_points(rows)
    closes = [r[4] for r in rows]
    lows = [r[3] for r in rows]
    highs = [r[2] for r in rows]
    days = max(0.25, len(rows) / bars_per_day)
    # centre the band on a robust location, not a wick print
    tail = closes[-max(20, len(closes) // 5):]
    center = median(tail) if tail else px
    if center <= 0:
        center = px

    slip_pct = max(0.0, spread_pct * 0.5 * slip_mult)
    cost_one_way = fee_pct + slip_pct
    best = None
    best_score = -1e18

    hh, ll = max(highs), min(lows)

    for k in K_MULTS:
        half = k * atr_pct
        if half < 0.35 or half > 28.0:
            continue
        lo = center * (1.0 - half / 100.0)
        hi = center * (1.0 + half / 100.0)
        if lo <= 0 or hi <= lo:
            continue
        cont, vcont = time_in_band(rows, lo, hi)
        # blend close + volume containment; require a pulse of presence
        containment = 0.65 * cont + 0.35 * vcont
        if containment < 0.08:
            continue
        span_pct = 2.0 * half

        for n in N_LEVELS:
            step_pct = span_pct / max(1, n - 1)
            if step_pct < fee_pct * min_step_mult:
                continue
            if step_pct <= spread_pct * 2.2:
                continue
            net_pct = step_pct - 2.0 * cost_one_way
            if net_pct <= 0:
                continue

            for kind in GRID_KINDS:
                levels = make_levels(lo, hi, n, kind)
                if len(levels) < 2:
                    continue
                # cheap reject via uniform crossing count (geo ≈ arith locally)
                cx = crossings_uniform(pts, lo, hi, n - 1)
                if cx < 3:
                    continue

                sim = simulate_grid(pts, levels, fee_pct, slip_pct)
                if sim["fills"] < 3:
                    continue

                active_days = max(days * containment, days * 0.10)
                fills_day = sim["fills"] / days
                rt_day = sim["round_trips"] / active_days

                # primary: realised stack % per calendar day, availability weighted
                raw_day = sim["realised_pct"] / days
                yield_day = raw_day * (0.55 + 0.45 * containment)

                # inventory / trend haircut
                mae = sim["mae_rungs"]
                inv_pen = 1.0 / (1.0 + 1.6 * mae)
                # one-sided finish is a parked inventory tax
                park_pen = 1.0 / (1.0 + 0.8 * abs(sim["end_inv"]))
                # excursion beyond the band (had to eat the trend)
                exc_up = max(0.0, (hh - hi) / hi * 100.0)
                exc_dn = max(0.0, (lo - ll) / lo * 100.0)
                exc_pen = 1.0 / (1.0 + 0.04 * (exc_up + exc_dn))

                adj = yield_day * inv_pen * park_pen * exc_pen
                # mild preference for more balanced two-way flow
                adj *= 0.85 + 0.15 * sim["flip_rate"]

                if adj > best_score:
                    best_score = adj
                    best = {
                        "lo": lo, "hi": hi, "levels": n, "kind": kind,
                        "center": center, "k": k,
                        "half_pct": half, "span_pct": span_pct,
                        "step_pct": step_pct, "net_pct": net_pct,
                        "fee_pct": fee_pct, "slip_pct": slip_pct,
                        "fills_day": fills_day,
                        "rt_day": rt_day,
                        "yield_day": yield_day,
                        "yield_adj": adj,
                        "realised_pct": sim["realised_pct"],
                        "mae": mae,
                        "end_inv": sim["end_inv"],
                        "flip_rate": sim["flip_rate"],
                        "containment": containment,
                        "cont_close": cont,
                        "cont_vol": vcont,
                        "exc_up_pct": exc_up,
                        "exc_dn_pct": exc_dn,
                        "days": days,
                    }

    if best:
        yd = best["yield_day"]
        best["days_to_5pct"] = (5.0 / yd) if yd > 0 else None
        best["capital_per_level_pct"] = 100.0 / best["levels"]
        best["score"] = best["yield_adj"]
    return best


# ------------------------------------------------------------------ scanner --
class Reject:
    def __init__(self):
        self.reasons: dict[str, list[str]] = {}

    def add(self, sym: str, why: str) -> None:
        self.reasons.setdefault(why, []).append(sym)

    def summary(self, limit: int = 6) -> list[tuple[str, int, str]]:
        out = []
        for why, syms in sorted(self.reasons.items(), key=lambda kv: -len(kv[1])):
            shown = ", ".join(syms[:limit])
            more = f" +{len(syms) - limit}" if len(syms) > limit else ""
            out.append((why, len(syms), shown + more))
        return out


@dataclass
class Cfg:
    interval: int = 30
    interval_label: str = "30m"
    bars_per_day: float = 48.0
    limit: int = 480
    refresh: int = 30
    top: int = 12
    fee: float = 0.25
    min_step_mult: float = 3.0
    min_turnover: float = 750_000.0
    max_spread: float = 0.25
    min_containment: float = 0.80
    max_vr: float = 1.10
    min_yield: float = 0.15
    max_hurst: float = 0.58
    slip_mult: float = 1.0
    auto: int | None = None
    watchlist: list[str] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)
    workers: int = 4
    pace: float = 0.22
    once: bool = False
    json_out: str | None = None
    csv_out: str | None = None
    webhook: str | None = None
    no_color: bool = False
    no_clear: bool = False
    diagnostics: bool = True
    persist_cache: bool = True


def evaluate(key: str, label: str, rows: list, tick: dict | None, cfg: Cfg) -> tuple[dict | None, str | None]:
    if not rows or len(rows) < 48:
        return None, "insufficient history"
    px = tick["last"] if tick else rows[-1][4]
    if px <= 0:
        return None, "bad price"
    turnover = tick["turnover24"] if tick else 0.0
    spread = tick["spread_pct"] if tick else 99.0
    if turnover < cfg.min_turnover:
        return None, f"liquidity < {fmt_usd(cfg.min_turnover)}/24h"
    if spread > cfg.max_spread:
        return None, f"spread > {cfg.max_spread}%"

    closes = [r[4] for r in rows]
    atr = wilder_atr_pct(rows)
    if atr <= 0:
        return None, "flat / no ATR"
    er = efficiency_ratio(closes)
    vr = variance_ratio(closes)
    hu = hurst_rs(closes)
    hl = half_life_bars(closes)

    grid = optimize_grid(
        rows, px, atr, cfg.fee, cfg.bars_per_day,
        cfg.min_step_mult, spread, cfg.slip_mult,
    )
    if not grid:
        return None, "no fee-positive grid"

    rec = {
        "symbol": label, "key": key, "price": px,
        "atr_pct": atr, "er": er, "chop": 1.0 - er, "vr": vr,
        "hurst": hu, "half_life": hl,
        "turnover24": turnover, "spread_pct": spread,
        "bars": len(rows), "closes": closes[-180:],
        "vwap24": tick.get("vwap24", px) if tick else px,
    }
    rec.update(grid)
    rec["qualified"] = (
        grid["containment"] >= cfg.min_containment
        and vr <= cfg.max_vr
        and hu <= cfg.max_hurst
        and grid["yield_day"] >= cfg.min_yield
        and grid["mae"] <= 0.72
    )
    return rec, None


def _align_tickers(keys: list[str], ticks: dict, book: PairBook) -> dict:
    if all(k in ticks for k in keys):
        return ticks
    alt_index: dict[str, str] = {}
    for k in keys:
        alt = (book.meta.get(k, {}).get("altname") or "").upper()
        ws = (book.meta.get(k, {}).get("wsname") or "").upper()
        if alt:
            alt_index[alt] = k
        if ws:
            alt_index[ws] = k
            alt_index[ws.replace("/", "")] = k
    for tk, tv in list(ticks.items()):
        mapped = alt_index.get(tk.upper()) or alt_index.get(tk.upper().replace("/", ""))
        if mapped and mapped not in ticks:
            ticks[mapped] = tv
    return ticks


def scan(book: PairBook, cfg: Cfg, cache: dict) -> tuple[list, Reject, int]:
    rej = Reject()

    if cfg.auto:
        ticks = fetch_ticker(None)
        pool = []
        for k, t in ticks.items():
            if k not in book.meta:
                continue
            if book.meta[k].get("quote") not in USD_QUOTES:
                continue
            lab = book.label(k)
            if lab in cfg.exclude or k in cfg.exclude:
                continue
            pool.append((t["turnover24"], k))
        pool.sort(reverse=True)
        keys = [k for _, k in pool[:cfg.auto]]
        labels = {k: book.label(k) for k in keys}
    else:
        keys, labels = [], {}
        for sym in cfg.watchlist:
            if sym in cfg.exclude:
                continue
            k, ws = book.resolve(sym)
            if not k:
                rej.add(sym, "not listed on Kraken")
                continue
            if k in labels:
                continue
            keys.append(k)
            labels[k] = book.pretty(ws) if ws else book.label(k)
        ticks = fetch_ticker(keys)

    ticks = _align_tickers(keys, ticks, book)

    results: list[dict] = []
    lock = threading.Lock()

    def work(key: str) -> None:
        rows, err = fetch_ohlc(key, cfg.interval, cache, cfg.limit)
        if err:
            with lock:
                rej.add(labels[key], f"ohlc: {err}")
            return
        rec, why = evaluate(key, labels[key], rows, ticks.get(key), cfg)
        with lock:
            if rec:
                results.append(rec)
            else:
                rej.add(labels[key], why or "rejected")

    workers = max(1, min(cfg.workers, len(keys) or 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, k) for k in keys]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                with lock:
                    rej.add("?", f"worker: {e.__class__.__name__}")

    results.sort(key=lambda r: (r.get("yield_adj", r["yield_day"]), r["yield_day"]), reverse=True)
    decorate_correlation(results)
    return results, rej, len(keys)


def decorate_correlation(results: list[dict]) -> None:
    """Attach corr-vs-top and a cheap diversification flag."""
    if not results:
        return
    top_closes = results[0].get("closes") or []
    for i, r in enumerate(results):
        if i == 0:
            r["corr_top"] = 1.0
            continue
        r["corr_top"] = pearson(top_closes, r.get("closes") or [])
    # greedy basket: walk down the list, keep names corr < 0.72 vs already kept
    kept: list[list[float]] = []
    for r in results:
        cls = r.get("closes") or []
        if any(abs(pearson(cls, k)) >= 0.72 for k in kept):
            r["basket"] = False
        else:
            r["basket"] = True
            kept.append(cls)
            if len(kept) >= 5:
                # still tag the rest
                pass


# ------------------------------------------------------------------ render --
def color_for_yield(y: float) -> str:
    if y >= 1.00:
        return FG.cyn
    if y >= 0.50:
        return FG.grn
    if y >= 0.20:
        return FG.ylw
    return FG.gry


def render(results: list, rej: Reject, scanned: int, cfg: Cfg, elapsed: float,
           prev_rank: dict[str, int] | None = None) -> None:
    W = term_width()
    if not cfg.no_clear:
        sys.stdout.write("\033[H\033[J")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f" GRIDPICK ORACLE v{VERSION} "
    print(FG.cyn + "═" * W + FG.rst)
    print(
        f"{FG.bold}{title}{FG.rst}{FG.gry}│{FG.rst} "
        f"{cfg.interval_label} × {cfg.limit} bars "
        f"({cfg.limit / cfg.bars_per_day:.1f}d) {FG.gry}│{FG.rst} "
        f"{scanned} pairs {FG.gry}│{FG.rst} fee {cfg.fee:.2f}% + slip×{cfg.slip_mult:.1f} "
        f"{FG.gry}│{FG.rst} {ts} {FG.gry}({elapsed:.1f}s){FG.rst}"
    )
    print(
        f"{FG.gry}Gates: cont ≥ {cfg.min_containment:.0%} │ "
        f"VR ≤ {cfg.max_vr:.2f} │ H ≤ {cfg.max_hurst:.2f} │ "
        f"yield ≥ {cfg.min_yield:.2f}%/d │ "
        f"liq ≥ {fmt_usd(cfg.min_turnover)}/24h │ spread ≤ {cfg.max_spread:.2f}% │ "
        f"step ≥ {cfg.fee * cfg.min_step_mult:.2f}%{FG.rst}"
    )
    print(FG.cyn + "═" * W + FG.rst)

    wide = W >= 136
    hdr = (
        f"{'#':<3}{'':<3}{'PAIR':<12}{'YLD/d':>7}{'ADJ':>7}{'FILL/d':>8}"
        f"{'STEP%':>7}{'LVL':>5}{'KIND':>5}{'BAND%':>7}"
        f"{'CONT':>6}{'MAE':>5}{'ATR%':>7}{'CHOP':>6}{'VR':>6}"
    )
    if wide:
        hdr += f"{'H':>5}{'SPRD':>7}{'LIQ24':>9} {'SHAPE':<22}"
    print(FG.bold + hdr + FG.rst)
    print(FG.gry + "─" * W + FG.rst)

    if not results:
        print(FG.red + " no pair produced a fee-positive grid this pass" + FG.rst)

    for i, r in enumerate(results[:cfg.top], 1):
        flag = "🟢" if r["qualified"] else "⚠️ "
        if prev_rank and r["symbol"] in prev_rank:
            d = prev_rank[r["symbol"]] - i
            if d > 0:
                flag = "▲" + flag
            elif d < 0:
                flag = "▼" + flag
        yc = color_for_yield(r["yield_day"])
        ac = color_for_yield(r.get("yield_adj", r["yield_day"]))
        vrc = FG.grn if r["vr"] <= 0.90 else (FG.ylw if r["vr"] <= 1.10 else FG.red)
        cc = FG.grn if r["containment"] >= 0.85 else (FG.ylw if r["containment"] >= 0.70 else FG.red)
        kind = r.get("kind", "ar")[:3]
        basket = "★" if r.get("basket") else " "
        line = (
            f"{FG.gry}{i:<3}{FG.rst}{flag:<3}{FG.bold}{r['symbol']:<11}{FG.rst}{basket}"
            f"{yc}{r['yield_day']:>7.2f}{FG.rst}"
            f"{ac}{r.get('yield_adj', 0):>7.2f}{FG.rst}"
            f"{r['fills_day']:>8.1f}"
            f"{r['step_pct']:>7.2f}"
            f"{r['levels']:>5}"
            f"{FG.mag}{kind:>5}{FG.rst}"
            f"{r['span_pct']:>7.2f}"
            f"{cc}{r['containment'] * 100:>5.0f}%{FG.rst}"
            f"{r.get('mae', 0) * 100:>5.0f}"
            f"{r['atr_pct']:>7.2f}"
            f"{r['chop']:>6.2f}"
            f"{vrc}{r['vr']:>6.2f}{FG.rst}"
        )
        if wide:
            hc = FG.grn if r.get("hurst", 0.5) <= 0.50 else (FG.ylw if r.get("hurst", 0.5) <= 0.58 else FG.red)
            sc = FG.grn if r["spread_pct"] <= 0.10 else FG.ylw
            line += (
                f"{hc}{r.get('hurst', 0):>5.2f}{FG.rst}"
                f"{sc}{r['spread_pct']:>7.3f}{FG.rst}"
                f"{fmt_usd(r['turnover24']):>9} "
                f"{FG.blu}{spark(r['closes']):<22}{FG.rst}"
            )
        print(line)

    if results:
        print()
        detail(results[0], cfg, W)
        basket = [r for r in results if r.get("basket") and r.get("qualified")][:4]
        if len(basket) >= 2:
            names = " · ".join(
                f"{b['symbol']} {b['yield_day']:.2f}%/d" for b in basket
            )
            print(f"\n {FG.cyn}★ DIVERSIFIED BASKET{FG.rst} {names}")
            print(f" {FG.gry}keep corr < 0.72 vs names already in the bag{FG.rst}")

    if rej.reasons and cfg.diagnostics:
        print(FG.gry + "─" * W + FG.rst)
        print(f"{FG.gry}REJECTED{FG.rst}")
        for why, n, syms in rej.summary():
            print(f"{FG.gry} {n:>3} × {why:<36} {syms}{FG.rst}")


def detail(r: dict, cfg: Cfg, W: int) -> None:
    print(FG.cyn + "─" * W + FG.rst)
    tag = (
        f"{FG.grn}QUALIFIED{FG.rst}"
        if r["qualified"]
        else f"{FG.ylw}BEST AVAILABLE — gate failed{FG.rst}"
    )
    kind = "geometric" if r.get("kind") == "geo" else "arithmetic"
    print(f"{FG.bold}▎TOP PICK {r['symbol']}{FG.rst} {tag} {FG.gry}{kind} grid{FG.rst}")
    print()
    print(
        f" {FG.gry}{fmt_px(r['lo'])}{FG.rst} "
        f"{FG.cyn}{gauge(r['price'], r['lo'], r['hi'], 34)}{FG.rst} "
        f"{FG.gry}{fmt_px(r['hi'])}{FG.rst} px {FG.wht}{fmt_px(r['price'])}{FG.rst}"
        f" mid {FG.gry}{fmt_px(r.get('center', r['price']))}{FG.rst}"
    )
    print()
    yc = color_for_yield(r["yield_day"])
    d5 = f"{r['days_to_5pct']:.1f}d" if r.get("days_to_5pct") else "—"
    hl = r.get("half_life")
    hl_s = f"{hl:.1f} bars" if isinstance(hl, (int, float)) else "—"
    print(
        f" {'expected yield':<22}{yc}{r['yield_day']:.3f}%{FG.rst} / day"
        f" {FG.gry}risk-adj {r.get('yield_adj', 0):.3f}%/d{FG.rst}"
    )
    print(f" {'time to +5%':<22}{d5}")
    print(
        f" {'grid':<22}{r['levels']} lvls · {kind} · step {r['step_pct']:.3f}% · "
        f"net/fill {r['net_pct']:.3f}% after {2 * (cfg.fee + r.get('slip_pct', 0)):.2f}% rt cost"
    )
    print(f" {'capital':<22}{r['capital_per_level_pct']:.2f}% of stack per level")
    print(
        f" {'measured fills':<22}{r['fills_day']:.1f}/day "
        f"({r['rt_day']:.1f} RT / active day) over {r['bars']} bars"
    )
    print(
        f" {'containment':<22}{r['containment'] * 100:.0f}% "
        f"{FG.gry}(close {r.get('cont_close', 0) * 100:.0f}% · vol {r.get('cont_vol', 0) * 100:.0f}%)"
        f" · excursion +{r['exc_up_pct']:.2f}% / -{r['exc_dn_pct']:.2f}%{FG.rst}"
    )
    print(
        f" {'inventory':<22}MAE {r.get('mae', 0) * 100:.0f}% of book · "
        f"end {r.get('end_inv', 0) * 100:+.0f}% · two-way {r.get('flip_rate', 0) * 100:.0f}%"
    )
    print(
        f" {'regime':<22}ATR {r['atr_pct']:.2f}% · chop {r['chop']:.2f} · "
        f"VR {r['vr']:.2f} · H {r.get('hurst', 0):.2f} · HL {hl_s}"
    )
    print(
        f" {'book':<22}spread {r['spread_pct']:.3f}% · slip {r.get('slip_pct', 0):.3f}% · "
        f"liq {fmt_usd(r['turnover24'])}"
    )
    print()
    payload = export_payload(r, cfg)
    print(FG.gry + "EXPORT " + json.dumps(payload, separators=(",", ":")) + FG.rst)


def export_payload(r: dict, cfg: Cfg) -> dict:
    return {
        "symbol": r["symbol"],
        "kraken_pair": r["key"],
        "grid_kind": r.get("kind", "arith"),
        "low": round(r["lo"], 12),
        "high": round(r["hi"], 12),
        "center": round(r.get("center", r["price"]), 12),
        "levels": r["levels"],
        "step_pct": round(r["step_pct"], 5),
        "net_pct_per_fill": round(r["net_pct"], 5),
        "expected_yield_pct_day": round(r["yield_day"], 5),
        "risk_adjusted_yield_pct_day": round(r.get("yield_adj", r["yield_day"]), 5),
        "fills_per_day": round(r["fills_day"], 3),
        "containment": round(r["containment"], 4),
        "mae": round(r.get("mae", 0), 4),
        "atr_pct": round(r["atr_pct"], 4),
        "vr": round(r["vr"], 4),
        "hurst": round(r.get("hurst", 0), 4),
        "interval": cfg.interval_label,
        "fee_pct": cfg.fee,
        "qualified": bool(r.get("qualified")),
    }


def write_json(path: str, results: list, cfg: Cfg) -> None:
    blob = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "interval": cfg.interval_label,
        "fee_pct": cfg.fee,
        "results": [
            {k: v for k, v in r.items() if k != "closes"} | {"export": export_payload(r, cfg)}
            for r in results
        ],
    }
    with open(path, "w") as f:
        json.dump(blob, f, indent=2)


def write_csv(path: str, results: list) -> None:
    cols = [
        "symbol", "qualified", "kind", "price", "lo", "hi", "levels",
        "step_pct", "net_pct", "yield_day", "yield_adj", "fills_day",
        "containment", "mae", "atr_pct", "chop", "vr", "hurst",
        "spread_pct", "turnover24",
    ]
    with open(path, "w") as f:
        f.write(",".join(cols) + "\n")
        for r in results:
            row = []
            for c in cols:
                v = r.get(c, "")
                if isinstance(v, bool):
                    v = int(v)
                elif isinstance(v, float):
                    v = f"{v:.6f}"
                row.append(str(v))
            f.write(",".join(row) + "\n")


def notify_webhook(url: str, results: list, cfg: Cfg) -> None:
    if not results:
        return
    top = results[0]
    q = [r for r in results if r.get("qualified")][:5]
    lines = [
        f"**GridPick Oracle v{VERSION}** `{cfg.interval_label}`",
        f"Top: **{top['symbol']}** {top['yield_day']:.2f}%/d "
        f"{top['levels']} {top.get('kind', '')} cont {top['containment']*100:.0f}%",
    ]
    for r in q:
        lines.append(
            f"• {r['symbol']} {r['yield_day']:.2f}%/d step {r['step_pct']:.2f}% "
            f"{r['levels']}lvl VR {r['vr']:.2f}"
        )
    body = {"content": "\n".join(lines)[:1800]}
    try:
        requests.post(url, json=body, timeout=8)
    except Exception:
        pass


# -------------------------------------------------------------------- main --
def build_config() -> Cfg:
    p = argparse.ArgumentParser(
        description="Kraken grid-trading pair scanner — measured yield, not vibes",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--interval", default=os.environ.get("INTERVAL", "30m"))
    p.add_argument("--limit", type=int, default=int(os.environ.get("LIMIT", "480")))
    p.add_argument("--refresh", type=int, default=int(os.environ.get("REFRESH", "45")))
    p.add_argument("--top", type=int, default=int(os.environ.get("TOPN", "12")))
    p.add_argument(
        "--fee", type=float, default=float(os.environ.get("FEE_PCT", "0.25")),
        help="one-way maker fee %%. Kraken base tier is 0.25",
    )
    p.add_argument("--min-step-mult", type=float, default=float(os.environ.get("MIN_GRID_MULT", "3.0")))
    p.add_argument(
        "--min-turnover", type=float,
        default=float(os.environ.get("MIN_TURNOVER_USD", "750000")),
        help="24h USD turnover floor",
    )
    p.add_argument("--max-spread", type=float, default=float(os.environ.get("MAX_SPREAD_PCT", "0.25")))
    p.add_argument("--min-containment", type=float, default=float(os.environ.get("MIN_CONTAINMENT", "0.80")))
    p.add_argument("--max-vr", type=float, default=float(os.environ.get("MAX_VR", "1.10")))
    p.add_argument("--max-hurst", type=float, default=float(os.environ.get("MAX_HURST", "0.58")))
    p.add_argument("--min-yield", type=float, default=float(os.environ.get("MIN_YIELD_PCT", "0.15")))
    p.add_argument("--slip-mult", type=float, default=float(os.environ.get("SLIP_MULT", "1.0")),
                   help="slippage haircut as a multiple of half-spread")
    p.add_argument("--auto", type=int, nargs="?", const=40, default=None,
                   help="ignore watchlist, scan top-N USD pairs by 24h turnover")
    p.add_argument("--watchlist", default=os.environ.get("WATCHLIST", ""))
    p.add_argument("--watchlist-file", default=os.environ.get("WATCHLIST_FILE", ""),
                   help="path to a file with one PAIR/QUOTE per line")
    p.add_argument("--exclude", default=os.environ.get("EXCLUDE", ""),
                   help="comma-separated symbols to drop")
    p.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "4")))
    p.add_argument("--pace", type=float, default=float(os.environ.get("PACE", "0.22")))
    p.add_argument("--once", action="store_true")
    p.add_argument("--json", dest="json_out", default=None, help="write full results JSON")
    p.add_argument("--csv", dest="csv_out", default=None, help="write results CSV")
    p.add_argument("--webhook", default=os.environ.get("GRIDPICK_WEBHOOK", "") or None,
                   help="Discord/Slack incoming webhook for the top card")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--no-clear", action="store_true")
    p.add_argument("--no-diagnostics", dest="diagnostics", action="store_false")
    p.add_argument("--no-cache", dest="persist_cache", action="store_false")
    ns = p.parse_args()

    key = str(ns.interval).lower()
    if key in INTERVAL_WORDS:
        mins = INTERVAL_WORDS[key]
    else:
        try:
            mins = int(key)
        except ValueError:
            p.error(f"bad --interval {ns.interval!r}")
    if mins not in VALID_INTERVALS:
        p.error(f"Kraken supports only {sorted(VALID_INTERVALS)} minute intervals")

    watch = [s.strip().upper() for s in ns.watchlist.split(",") if s.strip()]
    if ns.watchlist_file:
        try:
            text = Path(ns.watchlist_file).read_text()
            watch.extend(s.strip().upper() for s in text.splitlines() if s.strip() and not s.startswith("#"))
        except Exception as e:
            p.error(f"cannot read --watchlist-file: {e}")
    # de-dupe preserve order
    seen = set()
    watch_u = []
    for s in watch:
        if s not in seen:
            seen.add(s)
            watch_u.append(s)

    exclude = {s.strip().upper() for s in ns.exclude.split(",") if s.strip()}

    cfg = Cfg(
        interval=mins,
        interval_label=key if key in INTERVAL_WORDS else f"{mins}m",
        bars_per_day=1440.0 / mins,
        limit=max(80, min(720, ns.limit)),
        refresh=max(10, ns.refresh),
        top=max(1, ns.top),
        fee=ns.fee,
        min_step_mult=ns.min_step_mult,
        min_turnover=ns.min_turnover,
        max_spread=ns.max_spread,
        min_containment=ns.min_containment,
        max_vr=ns.max_vr,
        max_hurst=ns.max_hurst,
        min_yield=ns.min_yield,
        slip_mult=max(0.0, ns.slip_mult),
        auto=ns.auto,
        watchlist=watch_u or DEFAULT_WATCH,
        exclude=exclude,
        workers=max(1, min(12, ns.workers)),
        pace=max(0.08, ns.pace),
        once=ns.once,
        json_out=ns.json_out,
        csv_out=ns.csv_out,
        webhook=ns.webhook,
        no_color=ns.no_color,
        no_clear=ns.no_clear,
        diagnostics=ns.diagnostics,
        persist_cache=ns.persist_cache,
    )
    return cfg


def fingerprint(results: list) -> str:
    raw = json.dumps(
        [(r["symbol"], round(r["yield_day"], 3), r["levels"], r.get("kind")) for r in results[:8]],
        separators=(",", ":"),
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def main() -> int:
    global FG, PACE
    cfg = build_config()
    FG = C(not cfg.no_color and sys.stdout.isatty())
    PACE = Pace(cfg.pace)

    book = PairBook()
    err = book.load()
    if err:
        print(f"{FG.red}AssetPairs failed: {err}{FG.rst}")
        return 1
    print(f"{FG.gry}loaded {len(book.meta)} online Kraken pairs · cache {cache_dir()}{FG.rst}")

    cache = load_ohlc_cache(cfg.interval) if cfg.persist_cache else {}
    prev_rank: dict[str, int] = {}
    last_fp = ""

    while True:
        t0 = time.monotonic()
        results, rej, scanned = scan(book, cfg, cache)
        elapsed = time.monotonic() - t0
        render(results, rej, scanned, cfg, elapsed, prev_rank or None)

        if cfg.persist_cache:
            persist_ohlc_cache(cache, cfg.interval)
        if cfg.json_out:
            write_json(cfg.json_out, results, cfg)
        if cfg.csv_out:
            write_csv(cfg.csv_out, results)

        fp = fingerprint(results)
        if cfg.webhook and fp != last_fp:
            notify_webhook(cfg.webhook, results, cfg)
        last_fp = fp
        prev_rank = {r["symbol"]: i for i, r in enumerate(results[: cfg.top], 1)}

        if cfg.once:
            return 0
        time.sleep(cfg.refresh)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nhalt.")
        sys.exit(0)
