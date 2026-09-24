"""Data sources for the Lattice console. One class per source, all read-only.

Every feed has .poll() -> dict (the last good payload, or {} if there has
never been one) and .age_s. A failed poll keeps the last payload and lets
age_s grow, so the screen goes stale/dead visibly instead of blanking.

GridBot has no HTTP API. The console reads the files the running bot
already writes, and nothing else:
  status.json    gridbot.py's own snapshot (grids, ladders, P/L, guard, feed,
                 scanner, picks, skips, events)
  scan.json      the oracle.py v3.1 --json output
  journal.jsonl  the bot's append-only tape (DEPLOY, FILL, FIRST_RT, EXIT, ...)
  gridbot.log    the bot's log, tailed
  scanner.log    the scanner's console output, tailed for its gates and
                 rejection reasons (scan.json has neither)

It never opens state.json, never imports gridbot.py, never calls Kraken.

Liveness is judged by the timestamp INSIDE status.json, not by when we last
managed to read it: a dead bot leaves a readable but frozen file, and that
must show as dead, not fresh.

Nothing here computes a trading quantity. Every number is the bot's or the
scanner's own, picked out and given a stable name. A field the bot does not
serve stays None and the console draws a dash for it.
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
JOURNAL_PATH = HERE / "journal.jsonl"
SCANNER_LOG_PATH = HERE / "scanner.log"


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


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ------------------------------------------------------------ status.json --
def snapshot_from_status(st):
    """status.json -> one flat, stable dict. Values are the bot's own; the
    only arithmetic is summing open + closed totals the bot reports apart."""
    op, cl = st.get("open") or {}, st.get("closed") or {}
    feed, sc, fees, guard = (st.get("feed") or {}, st.get("scanner") or {},
                             st.get("fees") or {}, st.get("guard") or {})
    return {
        "version": st.get("version"),
        "mode": st.get("mode"),
        "uptime_s": _num(st.get("uptime_s")),
        "halt": st.get("halt"),
        "exit_after_s": _num(st.get("exit_after_s")),
        # capital
        "capital": _num(st.get("capital")),
        "max_grids": st.get("max_grids"),
        "alloc_per_grid": _num(st.get("alloc_per_grid")),
        "capital_now": _num(st.get("capital_now")),
        "alloc_next": _num(st.get("alloc_next")),
        "deployed": _num(op.get("deployed")),
        "held_usd": _num(op.get("held")),
        "grids_active": st.get("grids_active"),
        "grids_held": st.get("grids_held") or 0,
        "unrestored": st.get("unrestored") or [],
        # book
        "equity": _num(op.get("equity")),
        "total_pnl": _num(st.get("total_pnl")),
        "open_realised": _num(op.get("realised")),
        "unrealised": _num(op.get("unrealised")),
        "open_fees": _num(op.get("fees")),
        "open_fills": op.get("fills"),
        "open_round_trips": op.get("round_trips"),
        "closed_count": cl.get("count"),
        "closed_realised": _num(cl.get("realised")),
        "closed_fees": _num(cl.get("fees")),
        "closed_round_trips": cl.get("round_trips"),
        "closed_fills": cl.get("fills"),
        "realised": (op.get("realised") or 0.0) + (cl.get("realised") or 0.0),
        "fees": (op.get("fees") or 0.0) + (cl.get("fees") or 0.0),
        "fills": (op.get("fills") or 0) + (cl.get("fills") or 0),
        "round_trips": (op.get("round_trips") or 0) + (cl.get("round_trips") or 0),
        # fee tier
        "fee_maker": _num(fees.get("maker")),
        "fee_taker": _num(fees.get("taker")),
        "fee_source": fees.get("source") or "",
        # guard, in update_guard()'s own units
        "guard_peak": _num(guard.get("peak")),
        "guard_day": guard.get("day"),
        "guard_day_start": _num(guard.get("day_start")),
        "guard_max_day_loss_pct": _num(guard.get("max_day_loss_pct")),
        "guard_max_drawdown_pct": _num(guard.get("max_drawdown_pct")),
        "guard_equity": _num(guard.get("equity")),
        "guard_drawdown_pct": _num(guard.get("drawdown_pct")),
        "guard_day_loss_pct": _num(guard.get("day_loss_pct")),
        # trade feed
        "feed_connected": feed.get("connected"),
        "feed_msg_age_s": _num(feed.get("last_msg_age_s")),
        "feed_connects": feed.get("connects"),
        "feed_errors": feed.get("errors"),
        "feed_last_error": feed.get("last_error") or "",
        "catch_up_gaps": feed.get("catch_up_gaps") or 0,
        # scanner child
        "scanner_running": sc.get("running"),
        "scanner_pid": sc.get("pid"),
        "scanner_starts": sc.get("starts"),
        "scanner_scan_age_s": _num(sc.get("last_scan_age_s")),
        "scanner_note": sc.get("note") or "",
        # lists
        "picks": st.get("picks") or [],
        "skipped": st.get("skipped") or {},
        "recent_closed": st.get("recent_closed") or [],
        "events": st.get("events") or [],
    }


def grids_from_status(st):
    """status.json grids -> list (the bot's order), every served field kept."""
    out = []
    for g in st.get("grids") or []:
        n = g.get("levels") or 0
        e = g.get("empty_level")
        pos = _num(g.get("pos"))
        lad = g.get("ladder")
        ladder = None
        if isinstance(lad, list) and lad:
            ladder = [(_num(p), s) for p, s in lad if isinstance(p, (int, float))]
        # Rungs above the empty level hold coins (resting SELLs); rungs below
        # hold cash (resting BUYs). Unknown empty level -> count only.
        if ladder:
            held = [s == "SELL" for _, s in ladder]
        elif isinstance(e, int):
            held = [i > e for i in range(n)]
        else:
            held = g.get("holding") or 0
        out.append({
            "symbol": g.get("symbol"),
            "kind": g.get("kind"),
            "levels": n,
            "lo": _num(g.get("lo")), "hi": _num(g.get("hi")),
            "price": _num(g.get("price")),
            "pos": pos,
            "px_idx": max(0.0, min(n - 1e-9, pos * n)) if pos is not None and n else None,
            "to_floor_pct": _num(g.get("to_floor_pct")),
            "to_ceiling_pct": _num(g.get("to_ceiling_pct")),
            "holding": g.get("holding") or 0,
            "held_mask": held,
            "empty_level": e,
            "alloc": _num(g.get("alloc")),
            "realised": _num(g.get("realised")),
            "unrealised": _num(g.get("unrealised")),
            "total": _num(g.get("total")),
            "floor_pnl": _num(g.get("floor_pnl")),
            "fees": _num(g.get("fees")),
            "fills": g.get("fills"),
            "round_trips": g.get("round_trips"),
            "deployed_at": g.get("deployed_at"),
            "age_s": _num(g.get("age_s")),
            "last_print_age_s": _num(g.get("last_print_age_s")),
            "outside_for_s": _num(g.get("outside_for_s")),
            "catching_up": bool(g.get("catching_up")),
            "predicted_yield_day": _num(g.get("predicted_yield_day")),
            "realised_yield_day": _num(g.get("realised_yield_day")),
            "time_to_first_rt_s": _num(g.get("time_to_first_rt_s")),
            "first_rt_note": g.get("first_rt_note"),
            "ladder": ladder,
            "cash": _num(g.get("cash")),
            "coins": _num(g.get("base")),
            "deploy_px": _num(g.get("deploy_px")),
            "step_pct": _num(g.get("step_pct")),
            "predicted_fills_day": _num(g.get("predicted_fills_day")),
            "scanner_now": g.get("scanner_now") or "",
        })
    return out


class StatusFeed(Feed):
    """gridbot.py's status.json. age_s is the age of the bot's own 'ts'."""

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path else STATUS_PATH
        self._mtime = None
        self.snapshot, self.grids = {}, []

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
        "symbol": r.get("symbol"), "price": _num(r.get("price")),
        "lo": _num(r.get("lo")), "hi": _num(r.get("hi")), "center": _num(r.get("center")),
        "k": _num(r.get("k")), "levels": r.get("levels"), "kind": r.get("kind"),
        "step_pct": _num(r.get("step_pct")), "net_pct": _num(r.get("net_pct")),
        "span_pct": _num(r.get("span_pct")),
        "fills_day": _num(r.get("fills_day")), "rt_day": _num(r.get("rt_day")),
        "yield_day": _num(r.get("yield_day")), "yield_adj": _num(r.get("yield_adj")),
        "cont": _num(r.get("containment")),
        "mae_pct": (r["mae"] * 100.0) if _num(r.get("mae")) is not None else None,
        "vr": _num(r.get("vr")), "hurst": _num(r.get("hurst")), "atr_pct": _num(r.get("atr_pct")),
        "chop": _num(r.get("chop")), "half_life": _num(r.get("half_life")),
        "turnover24": _num(r.get("turnover24")), "spread_pct": _num(r.get("spread_pct")),
        "slip_pct": _num(r.get("slip_pct")), "days_to_5pct": _num(r.get("days_to_5pct")),
        "corr_top": _num(r.get("corr_top")), "score": _num(r.get("score")),
        "qualified": bool(r.get("qualified")), "basket": bool(r.get("basket")),
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
            "n_basket": sum(1 for r in rows if r["basket"]),
            "rows": rows,
            "fee_pct": blob.get("fee_pct"),
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

    def __init__(self, path=None, n=80):
        super().__init__()
        self.path = Path(path) if path else LOG_PATH
        self.n = n
        self._mtime = None

    def fetch(self):
        mt = self.path.stat().st_mtime
        if mt == self._mtime and self.data:
            self._t_ok = time.monotonic()
            return None
        raw = _tail_bytes(self.path, 96 * 1024).decode("utf-8", "replace")
        lines = [norm_log_line(ln.rstrip()) for ln in raw.splitlines() if ln.strip()]
        self._mtime = mt
        return {"lines": lines[-self.n:]}


# ---------------------------------------------------------- journal.jsonl --
class JournalFeed(Feed):
    """Last n rows of the bot's tape. Rows are the bot's own dicts; the first
    line of a byte tail may be a torn row and is dropped, never guessed."""

    def __init__(self, path=None, n=200):
        super().__init__()
        self.path = Path(path) if path else JOURNAL_PATH
        self.n = n
        self._mtime = None

    def fetch(self):
        mt = self.path.stat().st_mtime
        if mt == self._mtime and self.data:
            self._t_ok = time.monotonic()
            return None
        raw = _tail_bytes(self.path, 128 * 1024).decode("utf-8", "replace")
        rows = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue                    # the torn first line of the tail
            if isinstance(r, dict) and r.get("event"):
                rows.append(r)
        self._mtime = mt
        return {"rows": rows[-self.n:]}


# ------------------------------------------------------------ scanner.log --
_REJ_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(.+?)\s{2,}(.+?)\s*$")
_HDR_RE = re.compile(r"GRIDPICK ORACLE v[\d.]+.*$")


class ScannerLogFeed(Feed):
    """The scanner's last printed gates line and REJECTED block. scan.json
    lists only the pairs that survived; the reasons the others did not are
    printed to scanner.log and nowhere else."""

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path else SCANNER_LOG_PATH
        self._mtime = None

    def fetch(self):
        mt = self.path.stat().st_mtime
        if mt == self._mtime and self.data:
            self._t_ok = time.monotonic()
            return None
        raw = _tail_bytes(self.path, 64 * 1024).decode("utf-8", "replace")
        lines = raw.splitlines()
        gates, header, rejects, rej_seen = None, None, [], False
        for i in range(len(lines) - 1, -1, -1):
            s = lines[i].strip()
            if gates is None and s.startswith("Gates:"):
                gates = s[len("Gates:"):].strip()
            if header is None and _HDR_RE.search(s):
                header = _HDR_RE.search(s).group(0)
            if s == "REJECTED" and not rej_seen:
                rej_seen = True             # the LAST block only, even if it is empty
                for ln in lines[i + 1:]:
                    t = ln.rstrip()
                    if not t.strip() or t.lstrip().startswith(("═", "─", "=")):
                        break
                    m = _REJ_RE.match(t)
                    if not m:
                        break
                    pairs = [p.strip() for p in m.group(3).split(",") if p.strip()]
                    rejects.append({"n": int(m.group(1)), "why": m.group(2).strip(), "pairs": pairs})
            if gates is not None and header is not None and rej_seen:
                break
        self._mtime = mt
        return {"gates": gates, "header": header, "rejects": rejects,
                "n_rejected": sum(r["n"] for r in rejects)}
