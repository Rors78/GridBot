#!/usr/bin/env python3
"""Daily driver for the pre-registered Binance.US DECIDER (prereg sections 5, 6).

Run once a day by the Scheduled Task `GridBot_DECIDER`. It does not decide
anything itself: it runs `bnus_replay.py decider --cut <D>` for every daily
cut still pending, IN ORDER, and stops at the first verdict that is not
DEFERRED. A day the machine was off is caught up on the next run, never
skipped, so the cut that gives the verdict is the one the prereg names.

Guards, each refusing the run rather than producing a verdict the prereg
would not accept:
- gridbot.py and audit/bnus_replay.py must be byte-identical to b29f31d
  (prereg 3.8 registers gridbot.Grid unmodified; a branch checked out in
  D:\\GridBot would otherwise be replayed).
- No cut after 2026-11-27: decider_verdict() would give PASSED / NOT PASSED
  on a later cut that meets the floors, where 11-27 was underpowered.
- Cuts are UTC dates, and a cut is only run once it is an hour old.
- A run that fails (network, no JSON) is logged and NOT advanced past: the
  same cut is retried tomorrow.

On the verdict: the full JSON is saved, the verdict line goes to
Desktop\\GRIDBOT_DECIDER_VERDICT.txt (opened in Notepad), and the task
disables itself. Nothing is sent anywhere.

    python -X utf8 audit/decider_daily.py           run pending cuts
    python -X utf8 audit/decider_daily.py --plan    show pending cuts, run nothing
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "audit" / "bnus_replay.py"
TASK = "GridBot_DECIDER"
FIRST_CUT = date(2026, 10, 28)
DEADLINE = date(2026, 11, 27)
# git blob ids at b29f31d (the registered code): `git rev-parse b29f31d:<path>`
REGISTERED = {"gridbot.py": "4b469c8483671332cdf7e2f6bbc8c04e5acc8d3e",
              "audit/bnus_replay.py": "5cc06e53fe6fc830ba318dac9d99eda9d617273f"}
OUT = Path(os.environ.get("GRIDBOT_REPLAY_CACHE")
           or Path.home() / ".cache" / "gridbot_replay" / "binanceus") / "decider_daily"
STATE = OUT / "state.json"
LOG = OUT / "decider_daily.log"
VERDICT_FILE = Path.home() / "Desktop" / "GRIDBOT_DECIDER_VERDICT.txt"
PROBLEM_FILE = Path.home() / "Desktop" / "GRIDBOT_DECIDER_PROBLEM.txt"
# The starvation note (operator ruling 2026-10-21, item 5): its wording is the
# tools file, fixed before any registered cut ran. This code only fills it in.
STARVATION_NOTE_FILE = ROOT / "tools" / "decider_starvation_note.txt"
SAMPLE_FLOOR = 60                     # clean grids, prereg 2026-09-27 section 6.4


def blob_id(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def log(msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}"
    with open(LOG, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_cut": None, "final": None}


def save_state(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, STATE)


def pending_cuts(st: dict, now: datetime) -> list[date]:
    """Every cut not yet run, oldest first, none after the deadline, none
    less than an hour old."""
    start = FIRST_CUT
    if st.get("last_cut"):
        start = max(start, date.fromisoformat(st["last_cut"]) + timedelta(days=1))
    newest = min((now - timedelta(hours=1)).date(), DEADLINE)
    out, d = [], start
    while d <= newest:
        out.append(d)
        d += timedelta(days=1)
    return out


def engine_problem() -> str | None:
    for rel, want in REGISTERED.items():
        got = blob_id(ROOT / rel)
        if want and got != want:
            return f"{rel} is {got}, registered {want}: refusing (prereg 3.8)"
    return None


def run_cut(cut: date) -> dict | None:
    """One registered DECIDER run. Returns its parsed JSON, or None."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        p = subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), "-X", "utf8",
                            str(REPLAY), "decider", "--cut", cut.isoformat()],
                           cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                           timeout=3 * 3600, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"cut {cut}: run failed ({e!r})")
        return None
    raw = OUT / f"decider_{cut}.stdout.txt"
    raw.write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""),
                   encoding="utf-8", newline="\n")
    i = p.stdout.find('{\n "label"')
    if p.returncode != 0 or i < 0:
        log(f"cut {cut}: rc={p.returncode}, no result JSON (see {raw.name})")
        return None
    try:
        res = json.loads(p.stdout[i:])
    except ValueError as e:
        log(f"cut {cut}: result JSON unreadable ({e})")
        return None
    if res.get("label") != "DECIDER" or "verdict" not in (res.get("summary") or {}):
        log(f"cut {cut}: result has no DECIDER verdict")
        return None
    (OUT / f"decider_{cut}.json").write_text(json.dumps(res, indent=1, default=str),
                                             encoding="utf-8", newline="\n")
    return res


def problem(why: str) -> None:
    """A refusal or a failed cut must not be silent: say so on the Desktop.
    The task stays enabled; the next good run removes the note."""
    log(why)
    PROBLEM_FILE.write_text(
        f"GridBot Binance.US DECIDER -- the daily run could not complete\r\n\r\n  {why}\r\n\r\n"
        f"No verdict was skipped: the same cut is retried on the next daily run.\r\n"
        f"log: {LOG}\r\n"
        f"written {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by audit/decider_daily.py\r\n",
        encoding="utf-8", newline="")


def clear_problem() -> None:
    try:
        PROBLEM_FILE.unlink()
    except OSError:
        pass


def starvation_note(cut: date, summary: dict) -> str | None:
    """The registered wording, when and only when the deadline verdict is
    underpowered AND at least one deployed grid was unclean. An underpowered
    run with no unclean grid is not starvation by gaps and gets no note."""
    verdict = summary.get("verdict") or ""
    if not verdict.startswith("NOT PASSED (underpowered"):
        return None
    if not (summary.get("unclean") or 0) > 0:
        return None
    text = STARVATION_NOTE_FILE.read_text(encoding="utf-8")
    return text.format(cut=cut.isoformat(), clean=summary.get("clean", 0),
                       unclean=summary["unclean"], floor=SAMPLE_FLOOR)


def announce(cut: date, res: dict) -> None:
    s = res["summary"]
    fmt = lambda v: "-" if v is None else f"{v:+.2f}%"
    note = ("\r\n" + s["note"].replace("\n", "\r\n") + "\r\n") if s.get("note") else ""
    text = (f"GridBot Binance.US DECIDER -- registered verdict\r\n\r\n"
            f"  {s['verdict']}\r\n\r\n"
            f"cut {cut} (00:00 UTC, exclusive), horizon {res.get('horizon_h')} h\r\n"
            f"clean grids {s['clean']}, downside exits {s['down']}, up {s['up']}, held {s['held']}\r\n"
            f"mean {fmt(s['mean'])}, median {fmt(s['median'])}, "
            f"worst tenth of downside exits {fmt(s['tail_p10_down'])}\r\n"
            f"conditions: {json.dumps(s['conditions'])}\r\n"
            f"{note}\r\n"
            f"full result: {OUT / f'decider_{cut}.json'}\r\n"
            f"per-grid rows: {res.get('file')}\r\n"
            f"written {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by audit/decider_daily.py\r\n")
    VERDICT_FILE.write_text(text, encoding="utf-8", newline="")
    subprocess.run(["schtasks", "/change", "/tn", TASK, "/disable"], capture_output=True)
    subprocess.Popen(["notepad.exe", str(VERDICT_FILE)])


def main(argv: list[str]) -> int:
    st = load_state()
    if st.get("final"):
        log(f"verdict already given at cut {st['final']['cut']}: {st['final']['verdict']}")
        return 0
    cuts = pending_cuts(st, datetime.now(timezone.utc))
    if "--plan" in argv:
        print(json.dumps({"state": st, "pending": [c.isoformat() for c in cuts],
                          "engine": engine_problem() or "matches b29f31d"}, indent=1))
        return 0
    why = engine_problem()
    if why:
        problem(why)
        return 2
    clear_problem()
    if not cuts:
        log("no cut due yet")
        return 0
    for cut in cuts:
        log(f"cut {cut}: running DECIDER")
        res = run_cut(cut)
        if res is None:
            problem(f"cut {cut}: the DECIDER run failed (details in the log)")
            return 1                      # retried tomorrow; this cut is not advanced past
        verdict = res["summary"]["verdict"]
        log(f"cut {cut}: {verdict}")
        note = starvation_note(cut, res["summary"])
        if note:
            res["summary"]["note"] = note     # the results file carries it (ruling 5)
            (OUT / f"decider_{cut}.json").write_text(json.dumps(res, indent=1, default=str),
                                                     encoding="utf-8", newline="\n")
            log(f"cut {cut}: starvation note attached")
        st["last_cut"] = cut.isoformat()
        if not verdict.startswith("DEFERRED"):
            st["final"] = {"cut": cut.isoformat(), "verdict": verdict}
            save_state(st)
            announce(cut, res)
            return 0
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
