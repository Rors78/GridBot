#!/usr/bin/env python3
"""Retire GridBot's open paper grids through the bot's OWN close path.

    python -X utf8 retire.py --killed-at "2026-09-26 08:05:33" \\
        --evidence "what proves the process was hard-killed" \\
        --era-note "why the book is being retired" [--data-dir DIR] [--dry-run]

Written for the end of the Kraken era (operator ruling 2026-09-27): the bot
was hard-killed by a power-off, the book sat frozen, and the venue is being
left, so there is nothing to resume.

What it writes, in this order, and nothing else:
  1. ONE `KILLED` journal row, reconstructed=true, naming the kill time and
     the evidence. NEVER a STOP row: STOP means a clean stop with the final
     save on disk, and the START/STOP pairing is how hard kills are found.
     A STOP forged after the fact would erase the very evidence that audit
     exists to catch.
  2. One `EXIT` row per open grid, written by Bot.close_grid -- the code path
     every exit takes -- so state.json, the closed totals and the EXIT rows
     agree by construction. Each grid closes at the LAST PRICE THE BOT SAW
     (its last print), closed_at = the kill time, and the exit reason names
     that price's age. Not today's price: that would book the hours nobody
     managed into the record.
  3. state.json (saved by close_grid) and one status.json snapshot.

It refuses, having written nothing, when:
  * GridBot is running from the data dir (the instance lock is held);
  * state.json is missing, unreadable, or holds a grid this code cannot
    restore or that is not ACTIVE;
  * the journal's last lifecycle row (START / STOP / KILLED) is not a START:
    a STOP means the bot stopped cleanly (nothing was killed), a KILLED
    means this was already done;
  * a grid has no last print, or its last print is after the kill time.

--dry-run copies state.json and journal.jsonl to a temp dir (holding the lock
on the real dir while it copies), retires the copy, prints the result and
leaves the real files untouched.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import gridbot  # noqa: E402

LIFECYCLE = ("START", "STOP", "KILLED")


class RetireRefused(RuntimeError):
    """A precondition failed; nothing was written."""


class _NoFeed:
    """The feed a retired book has: none. close_grid only tells it the symbol
    set; snapshot() reads these attributes."""
    connected = False
    last_msg = 0.0
    connects = 0
    errors = 0
    last_error = "retired: no feed"

    def set_symbols(self, symbols) -> None:
        pass

    def stop(self) -> None:
        pass


def _last_lifecycle(journal: Path) -> dict | None:
    """The newest START / STOP / KILLED row, or None if there is none."""
    last = None
    try:
        with open(journal, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("event") in LIFECYCLE:
                    last = row
    except FileNotFoundError:
        return None
    return last


def _check_state(state_path: Path, killed_at: float) -> dict:
    """Refuse unless every saved grid restores, is ACTIVE and has a last
    print at or before the kill. Read-only."""
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        raise RetireRefused(f"no {state_path.name}: nothing to retire")
    except (OSError, ValueError) as e:
        raise RetireRefused(f"{state_path.name} unreadable ({e})")
    grids = st.get("grids") or {}
    if not grids:
        raise RetireRefused("state.json has no open grids: nothing to retire")
    for key, d in grids.items():
        try:
            g = gridbot.Grid.from_dict(d)
        except Exception as e:
            raise RetireRefused(f"grid {key} cannot be restored by this code ({e!r})")
        if g.status != "ACTIVE":
            raise RetireRefused(f"grid {key} is {g.status}, not ACTIVE")
        if g.last_px is None or g.last_ts is None:
            raise RetireRefused(f"grid {key} has no last print to close at")
        if g.last_ts > killed_at:
            raise RetireRefused(f"grid {key} saw a print at {gridbot.iso(g.last_ts)}, "
                                f"after the stated kill time {gridbot.iso(killed_at)}")
    return st


def retire(data_dir, killed_at: float, evidence: str, era_note: str,
           clock=time.time, dry_run: bool = False) -> dict:
    """Retire every open grid in `data_dir`. Returns what was written.
    Raises RetireRefused (nothing written) when a precondition fails."""
    real = Path(data_dir).resolve()
    lock = gridbot.acquire_single_instance(real / "gridbot.lock")
    if lock is None:
        raise RetireRefused(f"GridBot is running from {real} (instance lock held)")
    try:
        if killed_at > clock():
            raise RetireRefused("the stated kill time is in the future")
        last = _last_lifecycle(real / "journal.jsonl")
        if last is None or last.get("event") != "START":
            what = "no START row" if last is None else f"a {last['event']} row ({last.get('ts')})"
            raise RetireRefused(f"the journal's last lifecycle row is {what}, not an unmatched "
                                f"START: there is no hard kill to record")
        st = _check_state(real / "state.json", killed_at)
        work = real
        if dry_run:
            work = Path(tempfile.mkdtemp(prefix="gridbot_retire_dry_"))
            for name in ("state.json", "journal.jsonl"):
                if (real / name).exists():
                    shutil.copy2(real / name, work / name)
        return _retire_locked(work, st, last, killed_at, evidence, era_note, clock, dry_run)
    finally:
        lock.close()


def _retire_locked(work: Path, st: dict, start_row: dict, killed_at: float,
                   evidence: str, era_note: str, clock, dry_run: bool) -> dict:
    args = gridbot.parse_args(["--data-dir", str(work),
                               "--capital", str(st.get("capital") or 500.0),
                               "--max-grids", str(st.get("max_grids") or 3),
                               "--no-scanner", "--no-live-validate"])
    handler = logging.FileHandler(work / "gridbot.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    gridbot.log.addHandler(handler)
    if gridbot.log.level == logging.NOTSET:
        gridbot.log.setLevel(logging.INFO)
    try:
        bot = gridbot.Bot(args, feed=_NoFeed(), scanner=None, book=None,
                          price_fn=lambda key: None,
                          trades_fn=lambda key, since: ([], "retired: no catch-up"),
                          clock=clock, render=False)
        bot.load_state()
        if bot.unrestored:
            raise RetireRefused(f"state.json holds unrestorable grids {sorted(bot.unrestored)}")
        symbols = sorted(bot.grids)
        totals_before = dict(bot.totals)
        bot.journal("KILLED", reconstructed=True,
                    killed_at=gridbot.iso(killed_at), killed_at_epoch=killed_at,
                    evidence=evidence, unmatched_start=start_row.get("ts"),
                    open_grids=symbols, written_by="retire.py",
                    note="no STOP row: the process was hard-killed; STOP is reserved "
                         "for a clean stop with the final save on disk")
        bot.event(f"KILLED (reconstructed): hard kill at {gridbot.iso(killed_at)}; "
                  f"retiring {len(symbols)} grid(s)", logging.WARNING)
        closes = []
        for sym in symbols:
            g = bot.grids[sym]
            px, seen = g.last_px, g.last_ts
            age_h = (clock() - seen) / 3600.0
            reason = (f"retired: {era_note}. Closed at the last print the bot saw, "
                      f"{px:g} at {gridbot.iso(seen)} local, {age_h:.1f} h before this booking "
                      f"-- a stale price, not a market price; the process was hard-killed "
                      f"at {gridbot.iso(killed_at)} (see the KILLED row)")
            s = bot.close_grid(sym, reason, px, killed_at)
            if s is None:
                raise RuntimeError(f"close_grid returned nothing for {sym}")
            closes.append({"symbol": sym, "close_px": px, "last_print": gridbot.iso(seen),
                           "age_h": round(age_h, 1), "realised": s["realised"],
                           "exit_pnl": s["exit_pnl"], "round_trips": s["round_trips"]})
        bot.write_status()
        return {"data_dir": str(work), "dry_run": dry_run, "killed_at": gridbot.iso(killed_at),
                "closes": closes, "totals_before": totals_before, "totals_after": dict(bot.totals)}
    finally:
        gridbot.log.removeHandler(handler)
        handler.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Retire GridBot's open paper grids (own close path).")
    p.add_argument("--killed-at", required=True, help='local time of the hard kill, "YYYY-MM-DD HH:MM:SS"')
    p.add_argument("--evidence", required=True, help="what proves the kill (journalled verbatim)")
    p.add_argument("--era-note", required=True, help="why the book is retired (goes in every exit reason)")
    p.add_argument("--data-dir", default=str(HERE))
    p.add_argument("--dry-run", action="store_true", help="retire a temp copy; leave the real files alone")
    a = p.parse_args(argv)
    try:
        killed = time.mktime(datetime.strptime(a.killed_at, "%Y-%m-%d %H:%M:%S").timetuple())
    except ValueError as e:
        p.error(f"--killed-at: {e}")
    try:
        res = retire(a.data_dir, killed, a.evidence, a.era_note, dry_run=a.dry_run)
    except RetireRefused as e:
        print(f"REFUSED, nothing written: {e}")
        return 3
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
