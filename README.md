# GridBot

A paper-only Kraken spot-grid bot, parked since 2026-09-27. Everything about
what it is, how it runs and what it must never do is in `CLAUDE.md`; read
that first. Its future is decided by the pre-registered Binance.US replays in
`audit/` (`PREREG_BINANCEUS_REPLAY_2026-09-27.md`: the DECIDER, verdict from
2026-10-28; `PREREG_ARM_B_2026-10-22.md`: the no-pre-buy arm, a measurement).

## Runtime state and where its backups live

The books (`state.json`, `journal.jsonl`, `status.json`, the logs) are
gitignored runtime files: they were never in git and cannot be rebuilt from
it. The copies that matter are outside the repo, on this machine:

| Location | What |
|---|---|
| `D:\_backups\GridBot_audit_bundle_2026-10-11\audit_bundle.zip` | The bundle the 2026-10-11 outside outcome audit read. **The only copy of the post-retirement state.json / status.json of the Kraken era 1 book.** Keep it. |
| `D:\_backups\GridBot_kraken_retire_2026-09-27\` | state and journal as they were before `retire.py` closed era 1 (JUP, TIA, TRUMP). |
| `D:\_backups\GridBot_era2_retire_2026-10-21\` | state, journal, status and log as they were before `retire.py` closed era 2 (RENDER, ARB, TRUMP, opened 2026-10-13, hard-killed the same day). |
| `D:\_backups\GridBot_audit_caches_2026-09-24\` | the 2026-09-23 audit pickles, `.pre-safety` copies, `audit/replay2/`, the old parked Startup shortcut. |

The replay caches (`~/.cache/gridbot_replay/binanceus/`) are rebuildable
from the public API, except `decider_daily/` (the DECIDER's state and log)
and `arm_b/`, which are run records.
