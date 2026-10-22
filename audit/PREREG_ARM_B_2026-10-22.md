# Pre-registration: ARM-B, the no-pre-buy deploy on the DECIDER's data, 2026-10-22

**Registered before any ARM-B code was run on a registered window.** The commit that adds this file is the timestamp. `audit/PREREG_BINANCEUS_REPLAY_2026-09-27.md` is not changed by this file and stays byte-identical: ARM-B is a second, separately registered arm, not an amendment to the DECIDER.

- **Freeze:** after ARM-B has produced output on any registered window, changing sections 2-7 voids that output.
- **Bugs:** a harness bug found after a run is fixed, the run is repeated in full, and the bug and both results are reported.
- **Allowed amendments:** only before the first registered run, each as its own dated commit to this file.
- **Sources of the rules:** the operator rulings of 2026-10-21 (arm B = the no-pre-buy deploy, a measurement of mean reversion at rung scale, not a fix; one change against the DECIDER; the harvest/bleed split registered; prediction written down first; VR cross-check alongside). Where the ruling was silent, choices are marked **[registrar's choice]**.

## 1. The question

GridBot's deploy buys, as a taker, the coins every SELL rung above the deploy price needs (`Grid.deploy`, the "pre-buy"). The 2026-10-11 outcome audit traced the strategy's losses to that long: downside exits −3.95% against upside exits +2.17% on the SCREEN, so the grid behaves as a disguised long. ARM-B removes the pre-buy and nothing else, and asks:

**With no inventory bought at deploy, does the ladder alone earn anything on Binance.US at rung scale?** A positive answer measures short-horizon mean reversion at the grid's own spacing (harvest exceeding what the inventory bought on the way down loses). A zero or negative answer measures that there is none to harvest.

ARM-B is **a measurement, not a verdict**. It gates nothing, decides nothing about going live, and is never read as a bound on the DECIDER in either direction.

## 2. Role

| Arm | Runs | Role | Can it produce a verdict? |
|---|---|---|---|
| DECIDER (prereg 2026-09-27) | daily cuts from 2026-10-28 | the only verdict | yes, by its own section 6 |
| **ARM-B** (this file) | once, on the DECIDER's verdict cut (section 7) | measurement of harvestable mean reversion at rung scale | **No.** Its output is labelled ARM-B MEASUREMENT. No pass/fail language, no go-live implication either way. |

## 3. Design: the DECIDER's, with one change

Everything in prereg 2026-09-27 sections 3 and 5 applies to ARM-B unchanged and is **imported from `audit/bnus_replay.py`**, not reimplemented: the universe (the 9 pairs), the 2-day walk-forward from 2026-09-28, the scanner lookback and call, which evaluations deploy, the ladder (`make_levels`, `snap_levels`, `feasibility`), capital ($166.6667 per grid), fees (0.00% maker / 0.02% taker), the recorder book candles as the only fill/exit/mark data, the fill rule (BUY at L if `ask_close <= L`, SELL at L if `bid_close >= L`, driven through `Grid.on_print` by `apply_book_minute`), the band exit on `mid_close` outside `[lo, hi]` for 300 s of recorded minutes with the sale at `bid_close`, the 240 h mark at `bid_close`, and the unmeasured-minute rule (gap stamps and missing rows, never interpolated; only clean grids count).

**The one change: `deploy()` buys nothing.**

1. **Engine:** `ArmBGrid`, a subclass of `gridbot.Grid` in `audit/arm_b.py`. `gridbot.py` is not modified (its blob must equal the registered `b29f31d` one; the runner refuses otherwise, as `decider_daily.py` does). `on_print`, `_fill_buy`, `_fill_sell`, `close` and `mark` are inherited unchanged: the subclass overrides `deploy()` and adds bookkeeping only.
2. **Deploy:** at `t`, the price is the `ask_close` of the minute ending at `t`, exactly as the DECIDER's (same skip if that minute has no row, same skip if it lies outside the band). It is used only to pick the empty level `e`, chosen as `Grid.deploy` chooses it (the level nearest the price). Then:
   - a BUY rests at every level below `e`, each for one rung of capital (`alloc / levels`, the DECIDER's rung);
   - **no coins are bought**; every level at or above `e` holds nothing;
   - cash stays at the full allocation; fees at deploy are zero.
3. **Sells:** a SELL rests only on a filled lot, one step above the level that filled, which is what the inherited `_fill_buy` does. Nothing ever rests above level `e`, so the trading ladder is levels `0..e`; the subclass truncates its working ladder to those levels so the inherited fill loop's invariant (one empty level, BUYs below it, SELLs above it) holds without change.
4. **Band:** `lo` and `hi` stay the scanner's full band (the exit and the deploy-inside-band check use the full `hi`, not the top of the truncated ladder).
5. **No buy rung below the deploy price** (`e == 0`, the price nearest the bottom level): the evaluation is **skipped and counted** with that reason **[registrar's choice]**. Such a grid could never fill; counting it as a deployed zero would dilute the statistic toward 0 without measuring anything.
6. **Capital basis:** net return is still Grid's total P&L divided by the full allocation ($166.6667), although at most `e` rungs of it are ever committed. This keeps the number on the same basis as the DECIDER's per-grid return **[registrar's choice]**.
7. **Harvest / bleed split (the f049594 decomposition, registered here):** per grid,
   - **harvest** = the sum of P&L over completed ladder pairs (a BUY at level `j` then the SELL of that lot at level `j+1`), booked in `_fill_sell`;
   - **bleed** = total P&L − harvest: what the inventory still held is worth at the exit sale (taker, at `bid_close`) or at the 240 h mark, relative to what it cost.
   - `harvest + bleed == total` by construction. With no pre-buy every SELL is a ladder sell, so `round_trips` counts ladder pairs exactly (the MED #4 overcount does not arise).

## 4. Reporting

Per pair and pooled, over the same rows the DECIDER reports: evaluations; skipped by reason (including "no buy rung below the deploy price"); rejected by the scanner; refused ladders; deployed; clean; unclean; mean / median net %; worst-tenth downside %; downside / upside / held-to-240 h counts; **mean harvest % and mean bleed %**; fills per grid-day; round trips per grid; the distribution of ladder sizes (`e`). The qualified-only subset with its n. Sample size and era next to every number.

## 5. Prediction, written before any registered window is replayed

1. **Under a random walk at rung scale**, the ladder's harvest is offset in expectation by the bleed on the inventory it accumulates on the way down. Expected total ≈ 0 minus the taker cost of the exit sale (0.02% of whatever inventory is sold) and minus the half-spread the exit crosses. So: **mean ≈ 0 or slightly negative, median ≈ 0.**
2. **Shape:** upside exits should carry no inventory (nothing rests above the deploy price) and so be ≈ harvest only, small and non-negative; downside exits should carry the inventory bought on the way down and so be negative, but **smaller in magnitude than the DECIDER's downside exits** on the same windows, because the pre-buy's long is gone.
3. **The registered statement rule** (made only on clean grids, and only if there are at least 60 of them, the DECIDER's sample bar; fewer means "UNDERPOWERED: no statement"):
   - **mean > 0 AND median > 0** → "harvestable mean reversion at rung scale was measured on this sample";
   - **otherwise** → "none was measured on this sample".
   Measurement language only. Neither outcome has a go-live implication.

## 6. Cross-check registered alongside: variance ratio at rung scale

From the same recorder `mid_close` series, the same pairs and the same windows (each clean grid's window: deploy minute through exit or 240 h):

1. One-minute log returns of `mid_close` over the window.
2. **Rung scale** `q*`: the smallest `q` in {5, 10, 15, 30, 60, 120, 240, 480} minutes at which the root-mean-square overlapping `q`-minute return is at least the grid's step (the ladder's `step_pct`). If none reaches it, the coarsest `q` the window can measure (the window must hold at least 10·`q` returns, else the VR is unmeasured for that grid) **[registrar's choice]**.
3. `VR(q*) = Var(overlapping q*-minute returns) / (q* · Var(1-minute returns))`, sample variances, no small-sample adjustment **[registrar's choice]**. `VR(60)` is reported beside it as a fixed-scale reference.
4. **Pooled:** the median `VR(q*)` over clean grids with a measured VR, and the share below 1.
5. **Prediction:** `median VR(q*) < 1` **if and only if** the ARM-B mean is > 0. The cross-check line reports "held" or "FAILED". A failed cross-check does not change the section 5 statement; it is reported next to it.

## 7. When it runs

- **Once, by hand, on the cut the DECIDER gives its verdict on** (the cut named in `GRIDBOT_DECIDER_VERDICT.txt`): `python -X utf8 audit/arm_b.py run --cut <that cut>`. Earliest 2026-10-28; the runner refuses an earlier cut without replaying anything, and refuses a cut after 2026-11-27 (the DECIDER's deadline).
- **The runner refuses** unless `gridbot.py` and `audit/bnus_replay.py` are byte-identical to the registered `b29f31d` blobs.
- **Smoke runs** on times before 2026-09-28 (outside every registered window), on short horizons, labelled SMOKE, are allowed before and after this commit and are not measurements. The DECIDER's own smoke times (2026-09-27 09:45 and 12:00 UTC, 3 h) are the ones used.
- Nothing in `D:\GridBot` is checked out for this: ARM-B is developed and run from a separate worktree, so the DECIDER's daily task never sees a changed tree.

## 8. Known limitations, stated before any result

1. Everything in prereg 2026-09-27 section 8 (fixed spreads, today's rules over the whole era, per-grid replay, minute-close quotes, recorder dependence, one market stretch, small effective n).
2. **Every DECIDER window up to the 2026-10-20 evaluation contains a recorder gap** (checked 2026-10-21 from gap stamps only). The clean sample at any cut is therefore biased toward grids that exited before their first gap. ARM-B inherits that bias exactly, which is the point of sharing the windows.
3. The exit rule is the DECIDER's (300 s outside the band). With no inventory above the deploy price it is harmless on the upside; on the downside it still sells the accumulated inventory as a taker. Whether a hold would do better is **not** measured here (ARM-HOLD is deferred by ruling).
4. A ladder of `e` BUY rungs commits at most `e / levels` of the allocation. The return is on the full allocation (section 3.6), so it understates the return on capital actually at risk by that factor; the ladder-size distribution is reported so the reader can rescale.
5. The VR cross-check is one estimator at one scale per grid on overlapping windows; it is a consistency check, not an independent measurement. The scanner already gates its picks on a lookback variance ratio (`oracle.Cfg.max_vr`, 1.10) *before* `t`; the cross-check here is on the *forward* window the grid lived through, at the grid's own spacing.
6. **No registered window had been replayed by anything before this registration.** The two DECIDER result files on disk at registration time (`results/decider_20260927T164136Z.jsonl`, `results/decider_20261015T033047Z.jsonl`) hold zero rows: both runs were at cuts before any evaluation's 240 h window could close, so `decider_times()` was empty. The ARM-B design came from the 2026-10-21 ruling and the SCREEN's downside/upside asymmetry, not from any DECIDER row. The only recorder replays before this commit are the DECIDER's and ARM-B's 3 h smokes on 2026-09-27 times, outside every registered window.
