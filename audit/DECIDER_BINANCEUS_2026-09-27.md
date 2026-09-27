# DECIDER: book-crossing replay. Built and tested; VERDICT DEFERRED

**This is the only replay whose result is a verdict** (`audit/PREREG_BINANCEUS_REPLAY_2026-09-27.md`, sections 2, 5 and 6). As of 2026-09-27 it has **no verdict and cannot have one yet.** Its data, 30 days of recorded Binance.US book, will first exist on 2026-10-28 00:00 UTC.

## How to run it, and when

    python -X utf8 audit/bnus_replay.py decider --cut 2026-10-28

1. **Run it on the first daily cut at or after 2026-10-28 00:00 UTC.** Each run fetches about 40 days of 1m klines per pair for the scanner lookback (about 600 calls, about 3k weight, paced). It reads the recorder's candles as static files.
2. **What the command prints** (`decider_verdict`):
   - `DEFERRED: cut is before 2026-10-28` for any earlier cut;
   - `DEFERRED: <n> clean grids / <d> downside exits, floors 60 / 5; next daily cut` if the sample floors are not met yet;
   - `NOT PASSED (underpowered at the 2026-11-27 deadline)` if they are still not met on 2026-11-27;
   - otherwise `PASSED` or `NOT PASSED`, by the registered rule: mean > 0, median > 0, and a worst tenth of downside exits >= -10% of the grid's capital, on clean grids.
3. **Rerun daily until it says something other than DEFERRED.** Nothing in sections 3-7 of the pre-registration may change between runs.

## What it depends on

- **GoldenEye's recorder** (`D:\GoldenEye\output\binanceus\book_candles`) must keep running. That is a file dependency only; no GridBot process depends on it (ruling 2).
- **Every recorder gap makes every grid whose window overlaps it unclean.** That can be up to 5 start dates x 9 pairs for one restart.
- **So far there is one gap:** 2026-09-27 09:59 to 10:04 UTC, 6 minutes, `recorder_down`. It falls before the first registered start date (2026-09-28).
- **If gaps keep the clean count under 60,** the DECIDER waits for more data, and at 2026-11-27 the result is NOT PASSED (underpowered). The default is not to port.

## What was tested (`audit/test_bnus_replay.py`, 21 tests; `--mutants`, 11 of 11 caught)

- **Fills are book crossings, presented to `Grid.on_print` so the bot's own loop does the filling.**
  - A BUY fills iff `ask_close <= level`; a SELL fills iff `bid_close >= level`.
  - Equality counts, and one ulp above or below does not.
  - One minute can fill several levels, each at its own price.
  - A mid or trade-like price crossing a level fills nothing.
  - A quote beyond the opposite side's level fills nothing.
- **Unmeasured minutes are stepped over,** never filled and never timed through. That covers the recorder's `gap_before_min` stamps (even over rows that exist) and missing rows.
- **The 300 s exit runs on recorded closes,** and a hole in the outside streak restarts it. The sale is at the bid.
- **The deploy is at the ask of the minute ending at `t`;** if that minute is missing, the evaluation is skipped.
- **The recorder's CSV schema is parsed from the file,** with torn lines and wrong-header files skipped and named.
- **No verdict is given before 2026-10-28;** an underpowered run at the deadline gives NOT PASSED.

| mutant | caught by |
|---|---|
| B: fills on the mid (a print-like price) | the book-fill tests |
| B2: both sides unclamped | `t_book_sides_never_cross_fill` |
| C2: interpolates across stamped gaps | `t_book_gap_stamps_and_missing_rows_are_unmeasured` |
| E2: exits after 240 s | `t_book_exit_after_300s_of_recorded_closes` |
| D: gives a verdict before 2026-10-28 | `t_decider_gives_no_verdict_early_or_underpowered` |

## Smoke run on the recorder data that exists (NOT a measurement)

    python -X utf8 audit/bnus_replay.py smoke --smoke-t 2026-09-27T09:45,2026-09-27T12:00 --smoke-hours 3

**Label: SMOKE. Not a measurement, not the registered horizon, no verdict.** It covers 2 start times on 2026-09-27, a 3 h horizon and 9 pairs: 18 grids deployed.

- **The gap handling worked on real data.** The 9 grids started at 09:45 are unclean, with exactly 6 unmeasured minutes each: the recorder's real 09:59-10:04 gap, stepped over and never filled. The 9 started at 12:00 are clean.
- **The run is deterministic:** it gave the same numbers when repeated after the rebase.
- **The harness runs end to end** on real recorder files and real Binance.US klines: every scanner call, ladder check and fill path is exercised.
- **Its P/L numbers mean nothing.** It is 9 clean grids over 3 hours, 1 fill in total.
- **Every deploy picked 8 levels,** as in the SCREEN and on Kraken.
