# GridBot

## READ THIS FIRST: GridBot is standalone

GridBot has **no affiliation with the trading fleet**. Not a fleet member, not a
fleet service, not "the GridPick executor". The fleet (D:\CommandCenter, Aegis,
Gridzilla, Confluence, the shared pool, the event bus, COSMOS, fleet_config.py,
LIVE_CAPABLE_BOTS) was **dismantled on 2026-09-24 precisely because Claude kept
plugging GridPick into it**. That must never happen again.

Concretely, when working in this folder:

- **Do not** add GridBot to any fleet_config, port registry, roster, launcher
  phase, or "armable bots" list. There is nothing to register it with.
- **Do not** wire an event bus, portfolio pool, reservation, AEGIS gate,
  Command Center panel, Telegram broadcaster, or dashboard into it.
- **Do not** run the fleet skills or agents on it: `/bot-onboard`,
  `/fleet-status`, `/wire-check`, fleet-wire-master, fleet-auditor,
  capital-risk-warden, simons-fleet-philosopher, cosmos-*. They assume a fleet
  that does not exist.
- **Do not** give it an HTTP server or API. It serves nothing. It writes files.
  The TUI reads the files. That is the whole interface, by design.
- **Do not** rename anything to GridPick. "GridPick" is the Oracle scanner
  (`oracle.py`) and the desktop shortcut / Lattice console title. The bot is
  GridBot.
- A `D:\GridPick\executor` (HTTP on :8089) once existed. It is retired and
  deleted. Anything referring to it is history, not a target.

Repo: `github.com/Rors78/GridBot`, branch `main`, tracks `origin/main`. Its own
repo since 2026-09-24. The D:\ fleet checkout gitignores `GridBot/`.

## What This Is

A **paper-only** Kraken spot-grid bot. One process (`gridbot.py`) starts the
GridPick Oracle v3.1 scanner (`oracle.py`, unchanged) as a hidden child,
deploys standard spot grids on the scanner's qualified picks, fills them from
Kraken's **public** trade stream (trade-through, never touch), and keeps its
books in JSON files in this folder.

It **never sends an order**. The one private call is Kraken's read-only
`TradeVolume` at startup, with a QUERY-ONLY key from the user environment
(`GRIDBOT_KRAKEN_KEY` / `GRIDBOT_KRAKEN_SECRET`), to use the account's real fee
tier. No key or any failure: defaults (0.22% maker / 0.38% taker) stand. Keys
are never stored in this folder and never land in status.json (tested).

Non-goals: live trading, shorting, leverage, any API, any other bot.

## Quick start

    launch.bat            desktop launcher: kill everything GridBot, restart
                          run.bat hidden, turn this window into the Lattice TUI
    launch.bat stop       stop everything, start nothing
    run.bat               the bot alone, one window, restart-on-crash loop
    python -X utf8 gridbot.py --status      print status.json and exit
    python -X utf8 test_gridbot.py          tests (no network, temp dirs only)

The desktop shortcut targets `launch.bat`. GridBot IS auto-started at boot:
`Startup\GridBot.lnk` runs `D:\BotLaunch\launch_min.ps1 -Title GridBot -Dir D:\GridBot`,
which opens a Git Bash tab running `run_gitbash.sh` (restart loop). The old parked
`startup_disabled\GridBot.lnk` (pointed at run.bat) was moved to
`D:\_backups\GridBot_audit_caches_2026-09-24\` on 2026-09-24, with the audit pickles,
the `.pre-safety` copies and `audit/replay2/` (its RESULTS.md and rp.py were never in git).

Exit codes from gridbot.py, honoured by run.bat / run_gitbash.sh:
`0` clean stop, `2` could not start (see gridbot.log), `3` already running
from this folder (instance lock), anything else = crash, relaunched in 15 s.

Stopping: `gridbot_stop.ps1` writes `stop.request`, waits up to 12 s for a
clean exit (final save + STOP journal row), then force-kills launcher cmd
first, python second. It matches on `D:\GridBot` paths, never on "python".

## Architecture

| File | Owns |
|---|---|
| `gridbot.py` | Everything at runtime. `Grid` (pure engine, no I/O), `TradeFeed` (Kraken public WS + REST catch-up), `Scanner` (oracle.py child: start, hang detection, log rotation, kill-orphan), `Bot` (state, journal, status, guard, deploy/exit decisions). |
| `oracle.py` | GridPick Oracle v3.1 scanner. **Read-only: do not modify.** The bot imports `oracle.make_levels` and `oracle.PairBook` so it trades exactly the ladder the scanner replayed. |
| `test_gridbot.py` | Self-contained test runner (`check()` PASS/FAIL, exits 1 on any failure). Fakes every Kraken call. |
| `tui/lattice.py` | The GRIDPICK LATTICE console (Rich Live). Read-only: reads status.json, scan.json, journal.jsonl, gridbot.log and scanner.log via `tui/feeds.py`. Never imports gridbot, never calls Kraken. Header = bot/feed/scanner liveness, capital, book, guard. Cards = one per grid with its resting ladder (B/S/○/◆). Oracle = every scan metric. Bottom = RISK, P/L, TAPE, LOG. Keys: enter detail, l log, t tape, ? help. Layouts at 176+/140/110/90 columns. |
| `tui/feeds.py`, `glyphs.py`, `theme.py`, `sixelimg.py`, `assets/` | TUI data feeds (StatusFeed, OracleFeed, JournalFeed, LogFeed, ScannerLogFeed), glyph map, theme, logo rendering. Feeds pick values out; they never compute a trading quantity. |
| `run.bat`, `run_gitbash.sh` | Restart loops (Windows / Git Bash). |
| `launch.bat`, `gridbot_stop.ps1` | Kill-then-launch desktop entry point and its stopper. |
| `audit/` | 2026-09-23 read-only audit: report + the replay scripts that produced it. Pickles are gitignored. |

**Single source of truth for config:** `parse_args()` in `gridbot.py` and its
env fallbacks. Nothing else. `GRIDPICK_POOL_USD` sets the default `--capital`.

Runtime files (all gitignored, all written atomically tmp + os.replace):

| File | What |
|---|---|
| `state.json` (+`.bak`) | The books. Restored on start; grids catch up from REST before any exit decision. |
| `status.json` | Snapshot every few seconds: equity, per-grid P/L, feed, scanner, picks, events. **This is how you answer "how is GridBot doing".** |
| `journal.jsonl` | Append-only: START, STOP, FILL, DEPLOY, EXIT rows with exchange timestamps. |
| `scan.json` | Scanner output, rewritten every `--scan-refresh` s. |
| `gridbot.log`, `scanner.log` | Rotating logs. Scanner runs unbuffered so its log is never stale. |
| `gridbot.lock`, `scanner.pid`, `stop.request` | Instance lock, child pid, graceful-stop flag. |

## CLI surface (gridbot.py)

`--capital` (500 / `GRIDPICK_POOL_USD`), `--max-grids` (3), `--fee`, `--taker`
(account tier, else 0.22 / 0.38), `--exit-after` (300 s outside band; 0 =
hold), `--cooldown` (60 min), `--max-day-loss-pct` (6), `--max-drawdown-pct`
(12), `--scan-refresh` (120), `--scan-pace` (0.5), `--scan-arg` (repeatable,
passed to oracle.py), `--no-scanner`, `--data-dir`, `--status`.

## Testing

    python -X utf8 test_gridbot.py

Plain script, no pytest. Prints `N passed, M failed`, exit 1 on any failure.
Touches only temp dirs, never this folder's state. Run it before any change to
`Grid`, fill logic, state restore, fees, or the guard.

Runtime verification is the files: after a change, watch `status.json` tick,
`journal.jsonl` gain rows, and `gridbot.log` say what happened. A file
existing is not done; observable output in status.json is done.

## Standing rules

- **Paper only. No order path exists and none is to be added.**
- **No fake stats.** The scanner's `yield_day` is a replay count, not a
  forecast: the 2026-09-23 audit measured it at roughly 9.75x too many fills
  and a Spearman of about -0.10 against outcomes. Never quote it as expected
  return. `realised_yield_day` in status.json is the measured number.
- **A demo artifact is not a measurement.** Sample size and era go next to any
  number that leaves this folder.
- **Never modify oracle.py.** Pass `--scan-arg` instead.
- **Never overwrite working code based on assumptions.** Read it first.
- **Identify processes by path or port owner, never by "python".**
- Keep the launchers ASCII-only: cmd.exe misparses non-ASCII comment lines.
- Commit only when asked. Never commit runtime state or credentials.

## Key Patterns & Gotchas (scars)

- **Hard kills lost every STOP row.** Journal showed 18 STARTs, 0 STOPs. Fix:
  the `stop.request` graceful path in `gridbot_stop.ps1` (audit 2026-09-23).
- **Scanner log looked hours stale.** The child was buffered; a kill took its
  output with it. Now `PYTHONUNBUFFERED=1`.
- **Windows cannot rename an open file.** Close the scanner log before
  rotating it; on failure back off an hour rather than crash-loop.
- **Kraken WS drops every 15-45 min** ("connection lost", "ping/pong timed
  out"). Expected. REST catch-up fills the gap; `catch_up_gaps` in status.json
  must stay 0. Reconnect noise in the log is not a bug.
- **Grid vs scanner disagreement is by design.** A real grid cannot refill the
  level it just filled; the scanner counts that crossing. `test_gridbot.py`
  pins where they agree and where they differ.
- **AssetPairs failure at start must not end the bot.** Use the scanner's
  cached pair list however old, else retry with backoff.
- **Fee tier sanity.** An implausible tier from TradeVolume (e.g. 38%) is
  refused and defaults kept (tested).
- **Line endings.** Everything is LF on disk, enforced by `.gitattributes`
  (`* text=auto eol=lf`) and `core.autocrlf=false`. Python's text-mode
  `write_text()` on Windows emits CRLF and turns a 10-line edit into a
  whole-file diff (it happened to commit 5cf6d9c). Edit with `newline=""`
  or bytes, and check `git diff --stat` before committing.
- **Every icon click left the old Lattice window open.** Windows Terminal
  keeps a tab whose process was killed ("[process exited with code -1]").
  Fix: `gridbot_stop.ps1` writes `lattice.stop`, `tui/lattice.py` exits 0 on
  it, launch.bat's loop ends, cmd exits 0, the tab closes. Backstop: the
  GridPick terminal profile has `closeOnExit: always`. The stop script also
  kills the Git Bash `run_gitbash.sh` loop, which it never matched before:
  a force-killed gridbot.py under that loop would have been respawned 15 s
  later. Every stop is logged to `launcher.log` with the Lattice's exit code;
  read that before theorising about a window.
- **A window that predates a launcher fix will not benefit from it.**
  `closeOnExit` is resolved when the tab is created, so the one Lattice
  window open before the profile change stayed open once more and had to be
  closed by hand. Verified after: two consecutive shortcut launches kept the
  window count at exactly one.
- **Never edit launch.bat while a console is running from it.** cmd reads a
  batch file by byte offset as it goes; an edit shifts the offsets under the
  running copy and it does something else (a 3 s wait for it to end on its
  own timed out during the 2026-09-24 repro). Stop, edit, relaunch.
