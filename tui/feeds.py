"""Data sources for the lattice console, ported to GridBot. One class per source.

Every feed has .poll() -> dict (the last good payload, or {} if there has
never been one) and .age_s. A failed poll keeps the last payload and lets
age_s grow, so the screen goes stale/dead visibly instead of blanking.

GridBot has no HTTP API. The console reads the files the running bot
already writes, and nothing else:
  status.json   gridbot.py's own snapshot (grids, P/L, feed, scanner)
  scan.json     the oracle.py v3.1 --json output
  gridbot.log   the bot's log, tailed

It never opens state.json, never imports gridbot.py, never calls Kraken.

Liveness is judged by the timestamp INSIDE status.json, not by when we last
managed to read it: a dead bot leaves a readable but frozen file, and that
must show as dead, not fresh.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # D:\GridBot
STATUS_PATH = HERE / "status.json"
SCAN_PATH = HERE / "scan.json"
LOG_PATH = HERE / "gridbot.log"


class Feed:
    def __init__(self):
        self.data = {}
        self.error = None
        self._t_ok = None

    @property
    def age_s(self):
        return float("inf") if self._t_ok is None else time.monotonic() - self._t_ok

    def _ok(self, data):
        self.data, self.error, self._t_ok = data, None, time.monotonic()

    def fetch(self):                      # pragma: no cover - per subclass
        raise NotImplementedError

    def poll(self):
        try:
            got = self.fetch()
            if got is not None:
                self._ok(got)
        except Exception as e:            # keep last payload, let age grow
            self.error = f"{type(e).__name__}: {e}"
        return self.data


def _tail_bytes(path, n):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read()


def _local_age(ts):
    """Seconds since a gridbot 'YYYY-MM-DD HH:MM:SS' local timestamp."""
    try:
        return max(0.0, time.time() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ status.json --
def snapshot_from_status(st):
    """status.json -> the executor-snapshot shape lattice.derive() reads.
    Picks values out; computes nothing but sums of the bot's own figures."""
    op, cl, feed = st.get("open") or {}, st.get("closed") or {}, st.get("feed") or {}
    up = st.get("uptime_s")
    sc = st.get("scanner") or {}
    return {
        # the states in which the bot stops trading while looking fine
        "halt": st.get("halt"),
        "grids_held": st.get("grids_held") or 0,
        "unrestored": st.get("unrestored") or [],
        "feed_connected": feed.get("connected"),
        "feed_errors": feed.get("errors"),
        "feed_last_error": feed.get("last_error") or "",
        "catch_up_gaps": feed.get("catch_up_gaps") or 0,
        "scanner_running": sc.get("running"),
        "scanner_note": sc.get("note") or "",
        "scan_age_s": sc.get("last_scan_age_s"),
        "exit_after_s": st.get("exit_after_s"),
        "capital_now": st.get("capital_now"),
        "alloc_next": st.get("alloc_next"),
        "execution_mode": st.get("mode"),
        "uptime_hours": up / 3600.0 if up is not None else None,
        "ws": {"oldest_tick_age_s": feed.get("last_msg_age_s"),
               "connected": feed.get("connected")},
        "n_active_grids": st.get("grids_active"),
        "total_cycles": (op.get("round_trips") or 0) + (cl.get("round_trips") or 0),
        "total_pnl": st.get("total_pnl"),
        "total_fees": (op.get("fees") or 0.0) + (cl.get("fees") or 0.0),
        "realised": (op.get("realised") or 0.0) + (cl.get("realised") or 0.0),
        "unrealised": op.get("unrealised"),
        "closed_count": cl.get("count"),
        "reserved_usd": (op.get("deployed") or 0.0) + (op.get("held") or 0.0),
        "pool_total_usd": st.get("capital"),
        "refusals": st.get("skipped") or {},
        "scanner": st.get("scanner") or {},
        "events": st.get("events") or [],
    }


def grids_from_status(st):
    out = {}
    for g in st.get("grids") or []:
        n = g.get("levels") or 0
        e = g.get("empty_level")
        # Rungs above the empty level hold coins (resting SELLs); rungs
        # below hold cash (resting BUYs). Unknown empty level -> count only.
        held = [i > e for i in range(n)] if isinstance(e, int) else (g.get("holding") or 0)
        pos = g.get("pos")
        lo, hi = g.get("lo"), g.get("hi")
        age = g.get("age_s")
        out[g.get("symbol")] = {
            "pair": g.get("symbol"),
            "levels": n,
            "filled": g.get("holding") or 0,
            "held_mask": held,
            "px_idx": max(0.0, min(n - 1e-9, pos * n)) if isinstance(pos, (int, float)) and n else None,
            "range": f"{lo:g}-{hi:g}" if lo is not None and hi is not None else None,
            "cycles": g.get("round_trips"),
            "pnl": g.get("total"),
            "realised": g.get("realised"),
            "unrealised": g.get("unrealised"),
            "floor_pnl": g.get("floor_pnl"),
            "to_floor_pct": g.get("to_floor_pct"),
            "to_ceiling_pct": g.get("to_ceiling_pct"),
            "outside_for_s": g.get("outside_for_s"),
            "last_print_age_s": g.get("last_print_age_s"),
            "catching_up": g.get("catching_up"),
            "predicted_yield_day": g.get("predicted_yield_day"),
            "realised_yield_day": g.get("realised_yield_day"),
            "yield_basis_usd": g.get("alloc"),
            "leverage": None,
            "uptime_min": age / 60.0 if age is not None else None,
            "price": g.get("price"),
            "kind": g.get("kind"),
        }
    return out


class StatusFeed(Feed):
    """gridbot.py's status.json. age_s is the age of the bot's own 'ts'."""

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path else STATUS_PATH
        self._mtime = None
        self.snapshot, self.grids = {}, {}

    @property
    def age_s(self):
        # ts_epoch is unambiguous. The local 'ts' repeats an hour when DST
        # ends, which would show a live bot as dead; it is the fallback only.
        d = self.data or {}
        ep = d.get("ts_epoch")
        if isinstance(ep, (int, float)):
            return max(0.0, time.time() - ep)
        a = _local_age(d.get("ts"))
        return float("inf") if a is None else a

    def fetch(self):
        mt = self.path.stat().st_mtime
        if mt == self._mtime and self.data:
            return None
        st = json.loads(self.path.read_text(encoding="utf-8"))
        self.snapshot, self.grids = snapshot_from_status(st), grids_from_status(st)
        self._mtime = mt
        return st


# -------------------------------------------------------------- scan.json --
def _norm_json_row(r):
    return {
        "symbol": r.get("symbol"), "price": r.get("price"),
        "lo": r.get("lo"), "hi": r.get("hi"), "k": r.get("k"),
        "levels": r.get("levels"),
        "yield_day": r.get("yield_day"), "oos_yield_day": None,
        "cont": r.get("containment"), "oos_cont": None,
        "inv_dd_pct": (r["mae"] * 100.0) if isinstance(r.get("mae"), (int, float)) else None,
        "qualified": bool(r.get("qualified")),
        "boundary": "basket" if r.get("basket") else "",
        "full": r,        # the oracle's own record, for its detail() view
    }


class OracleFeed(Feed):
    """The running scanner's latest results, read from scan.json -- never a scan."""

    def __init__(self, json_path=None):
        super().__init__()
        self.json_path = Path(json_path) if json_path else SCAN_PATH
        self._mtime = None

    @property
    def source(self):
        return self.json_path

    def reload(self):
        """Force a re-read on the next poll (the 'r' key)."""
        self._mtime = None

    def fetch(self):
        mt = self.json_path.stat().st_mtime
        if mt == self._mtime and self.data:
            self._t_ok = time.monotonic()       # file reachable, unchanged
            return None
        blob = json.loads(self.json_path.read_text(encoding="utf-8"))
        rows = [_norm_json_row(r) for r in blob.get("results", [])]
        out = {
            "ts": blob.get("generated"), "version": blob.get("version"),
            "interval": blob.get("interval"),
            "n_scanned": len(rows), "n_qualified": sum(1 for r in rows if r["qualified"]),
            "rejects": None,
            "rows": rows,
            "fee_pct": blob.get("fee_pct"), "oos_days": None,
        }
        self._mtime = mt
        return out

    def scan_age_s(self, now=None):
        """Age of the SCAN (its own timestamp), not of our last read."""
        ts = (self.data or {}).get("ts")
        if not ts:
            return None
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.astimezone()
        return (now - t).total_seconds()


# ------------------------------------------------------------ gridbot.log --
_LOG_RE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ (\w+) (.*)$")


def norm_log_line(s):
    """'2026-09-23 09:55:24,864 INFO msg' -> '09:55:24 [INFO] msg' (lattice's form)."""
    m = _LOG_RE.match(s)
    return f"{m.group(1)} [{m.group(2)}] {m.group(3)}" if m else s


class LogFeed(Feed):
    """Last n lines of gridbot.log (read-only tail)."""

    def __init__(self, path=None, n=40):
        super().__init__()
        self.path = Path(path) if path else LOG_PATH
        self.n = n
        self._mtime = None

    def fetch(self):
        mt = self.path.stat().st_mtime
        if mt == self._mtime and self.data:
            self._t_ok = time.monotonic()
            return None
        raw = _tail_bytes(self.path, 64 * 1024).decode("utf-8", "replace")
        lines = [norm_log_line(ln.rstrip()) for ln in raw.splitlines() if ln.strip()]
        self._mtime = mt
        return {"lines": lines[-self.n:]}
