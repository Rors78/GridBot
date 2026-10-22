#!/usr/bin/env python3
"""Tests for audit/decider_daily.py: the cut schedule, the engine guard and
the starvation note. No network, no task changes, nothing written.

    python -X utf8 audit/test_decider_daily.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import decider_daily as D   # noqa: E402

UTC = timezone.utc
UNDERPOWERED = "NOT PASSED (underpowered at the 2026-11-27 deadline)"


def t_no_cut_before_the_first_is_an_hour_old():
    assert D.pending_cuts({"last_cut": None}, datetime(2026, 10, 28, 0, 30, tzinfo=UTC)) == []
    assert D.pending_cuts({"last_cut": None}, datetime(2026, 10, 28, 1, 0, tzinfo=UTC)) == [date(2026, 10, 28)]


def t_missed_days_are_caught_up_in_order_never_skipped():
    cuts = D.pending_cuts({"last_cut": "2026-10-29"}, datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert cuts == [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]


def t_no_cut_after_the_deadline():
    cuts = D.pending_cuts({"last_cut": "2026-11-25"}, datetime(2026, 12, 15, tzinfo=UTC))
    assert cuts == [date(2026, 11, 26), date(2026, 11, 27)]
    assert D.pending_cuts({"last_cut": "2026-11-27"}, datetime(2026, 12, 15, tzinfo=UTC)) == []


def t_engine_guard_matches_the_registered_blobs():
    assert D.REGISTERED["gridbot.py"] == "4b469c8483671332cdf7e2f6bbc8c04e5acc8d3e"
    assert D.REGISTERED["audit/bnus_replay.py"] == "5cc06e53fe6fc830ba318dac9d99eda9d617273f"
    assert D.engine_problem() is None, D.engine_problem()


def t_starvation_note_only_when_underpowered_with_unclean_grids():
    s = {"verdict": UNDERPOWERED, "clean": 12, "unclean": 80}
    note = D.starvation_note(date(2026, 11, 27), s)
    assert note and note.startswith("NOT PASSED by starvation") and "12 clean grids" in note
    assert "80 deployed grids were unclean" in note and "2026-11-27" in note and "floor of 60" in note
    assert D.starvation_note(date(2026, 11, 27), {"verdict": "PASSED", "clean": 70, "unclean": 5}) is None
    assert D.starvation_note(date(2026, 11, 27), {"verdict": "NOT PASSED", "clean": 70, "unclean": 5}) is None
    assert D.starvation_note(date(2026, 11, 1), {"verdict": "DEFERRED: 3 clean grids / 0 downside exits",
                                                  "clean": 3, "unclean": 9}) is None
    assert D.starvation_note(date(2026, 11, 27), {"verdict": UNDERPOWERED, "clean": 12, "unclean": 0}) is None


def t_starvation_wording_is_the_tools_file():
    text = D.STARVATION_NOTE_FILE.read_text(encoding="utf-8")
    assert "{clean}" in text and "{unclean}" in text and "{floor}" in text and "{cut}" in text
    note = D.starvation_note(date(2026, 11, 27), {"verdict": UNDERPOWERED, "clean": 1, "unclean": 2})
    assert note == text.format(cut="2026-11-27", clean=1, unclean=2, floor=60)


def run_tests() -> dict:
    res = {}
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            try:
                fn()
                res[name] = True
                print(f"  PASS  {name}")
            except Exception as e:                  # noqa: BLE001
                res[name] = False
                print(f"  FAIL  {name}: {e!r}")
    return res


if __name__ == "__main__":
    res = run_tests()
    bad = sum(1 for v in res.values() if not v)
    print(f"\n{len(res) - bad} passed, {bad} failed")
    sys.exit(1 if bad else 0)
