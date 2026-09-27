# Pre-registration: GridBot on Binance.US, two replays (SCREEN, DECIDER), 2026-09-27

**Registered before any replay code was written or run.** The commit that adds this file is the timestamp. After a replay has produced any output, changing sections 2-7 voids that replay's result.

- **Bugs:** a harness bug found after a run is fixed, the run is repeated in full, and the bug and both results are reported.
- **Allowed amendments:** only before the replay they govern first runs, each as its own dated commit to this file.

**Sources of the rules:**
- The operator + architect rulings of 2026-09-27.
- Where the rulings were silent, choices made here are marked **[registrar's choice]**. They can be amended, before a run, under the rule above.

## 1. The question

Does GridBot's grid make money on Binance.US spot at the current fee schedule, net of fees and spread, with its downside tail bounded? That means:
- the scanner's ladder, unchanged;
- GridBot's own fill engine;
- GridBot's 300 s exit.

**The prior answer on Kraken** (`audit/AUDIT_2026-09-23.md`): no.
- -1.25% of the grid's capital per 10 days (mean; median -0.02%).
- Sample: 2,502 grids on 14 pairs, 2025-01-01 to 2026-09-11. Effective n was about 200.
- Its input data (`D:\GridPick\data`) no longer exists, so that number stands as recorded.

## 2. Roles, fixed now (ruling 1)

| Replay | Runs | Role | Can it produce a verdict? |
|---|---|---|---|
| **SCREEN**: trade-price replay | now | The 2026-09-23 method on this venue, with the real 300 s exit | **No.** Its output is labelled SCREEN. Nothing deploys on it, and no pass/fail language is attached to it. The DECIDER runs on schedule whatever it shows. |
| **DECIDER**: book-crossing replay | on the first data cut meeting section 5.2 (earliest 2026-10-28 00:00 UTC) | The only verdict | **Yes**, by section 6 alone |

**What "lower bound" means for the SCREEN, and what it does not.** Ruling 1 calls the SCREEN a lower bound. That holds for the fill *mechanism* at equal resolution: a trade printed through a level means the book crossed that level, so print-crossing fills are a subset of book-crossing fills. It does **not** bound:
- **P&L, in either direction.** The extra fills a book sees include buys on the way down, which deepen downside losses.
- **Fill counts, strictly, at these two resolutions.** The SCREEN sees intra-minute trade extremes. The DECIDER sees only minute-close quotes. Each can see a crossing the other misses.

The SCREEN is reported as a different measurement, never as a floor under the DECIDER.

## 3. Design shared by both replays

1. **Universe (ruling 2, fixed):** SOLUSDT, XRPUSDT, ETHUSDT, DOGEUSDT, LTCUSDT, LINKUSDT, AVAXUSDT, SUIUSDT, ADAUSDT.
   - These are the 9 bases that passed the order's universe gate on at least one quote in the 2026-09-27 probe (`audit/bnus_probe_out.json`: one 24 h window).
   - ADAUSDT itself read 14.9% active minutes that day, just under the 15% floor. It is included because the ruling names the 9 survivors.
   - No pair is added or dropped after results.
2. **Walk-forward:** evaluation times `t` fall on a 2-day grid at 00:00 UTC. At each `t`, each pair gets one evaluation (as on 2026-09-23).
3. **Scanner lookback:** the 480 closed 30m bars before `t` (10 days). They are aggregated from Binance.US 1m klines exactly as Binance aggregates (open = first open, high = max, low = min, close = last close, volume = sum). A lookback with fewer than 480 bars, or with a missing 1m kline, skips the evaluation (counted).
4. **Scanner call:** `oracle.evaluate`, unmodified (oracle.py untouched). The venue enters only as `oracle.Cfg` overrides and tick inputs:
   - `fee = 0.0` (maker);
   - `min_turnover = 10000`: the $10k/24h floor (ruling);
   - every other `Cfg` field at oracle's default (interval 30m, limit 480, max_spread 0.25%, min_containment 0.80, max_vr 1.10, max_hurst 0.58, min_yield 0.15, min_step_mult 3.0, slip_mult 1.0);
   - tick input: `last` = the last 1m close before `t`; `turnover24` = the sum of 1m quote volume over the 1440 minutes before `t`; `spread_pct` = the pair's registered spread (table below); `vwap24` = `last`.
   - With a 0% maker fee the scanner's fee floor on level spacing is gone; only `step > 2.2 x spread` remains. Whether finer ladders churn or earn is what the replay measures. No conclusion is drawn in advance (ruling).
5. **Which evaluations deploy [registrar's choice]:** every evaluation for which `oracle.evaluate` returns a band, qualified or not. This matches the 2026-09-23 headline, and that audit found `qualified` did no better. The `qualified` flag is recorded, and a qualified-only subset is reported beside the result. It is never the pass statistic.
6. **Ladder:** GridBot's own deploy path: `oracle.make_levels(lo, hi, levels, kind)`, then `gridbot.snap_levels`, then `gridbot.feasibility`. The pair's current Binance.US rules are passed in as data (tick as `tick_size`, `minQty` as `ordermin`, `minNotional` as `costmin`; table below). A ladder refused by either check is counted, not deployed.
7. **Capital:** $166.6667 per grid ($500 / 3, as on 2026-09-23). Each grid is independent: no max-3, no correlation gate, no cooldown, no book guard (a per-grid replay, as on 2026-09-23).
8. **Engine:** `gridbot.Grid`, unmodified: `deploy()`, `on_print()` (including the loop that fills every level one print passes, each at its own price), `close()` and `mark()`. **The harness never computes a fill, a fee or a P&L itself.** It only chooses which price `Grid` sees, and when.
9. **Fees:** 0.00% maker on every grid fill; 0.02% taker on the deploy buy and the exit sale (Grid's own split).
   - This is Binance.US's published schedule since 2026-04-22.
   - It is applied to the whole era, although fees before that date were different. That is deliberate: the question is today's venue.
10. **Exit:** GridBot's rule, simulated at 1-minute resolution instead of 2026-09-23's first-15m-close approximation. If the price stays outside `[lo, hi]` for 300 s, all inventory is sold (taker).
11. **Horizon:** 240 h from deploy. A grid not exited by then is **marked** at 240 h (section 4/5 prices), not closed.
12. **Result per grid:** net return = Grid's total P&L at exit (or its mark at 240 h) / capital x 100, in % of the grid's capital.
    - **Downside exit:** an exit fired with the price below `lo`.
    - **Upside exit:** an exit fired with the price above `hi`.
13. **Unmeasured time is never interpolated.** A grid is **clean** if its window (deploy minute through exit, or 240 h) contains no unmeasured minute.
    - What counts as unmeasured is defined in sections 4 and 5.
    - Only clean grids enter the pass statistics.
    - Unclean grids are counted and reported separately. The harness steps over the unmeasured minutes and never synthesizes a value for them [registrar's choice].

### Registered per-pair inputs

- **Spread:** the median of the recorder's `spread_bps_median` over the pair's 389 recorded minutes, 2026-09-27 09:38 to 16:12 UTC (GoldenEye recorder, read offline). The half-spread `h` = spread / 2.
- **Order rules:** Binance.US `exchangeInfo`, read 2026-09-27.

| pair | spread (bps) | tick | lot step = minQty | minNotional |
|---|---|---|---|---|
| SOLUSDT | 0.812 | 0.01 | 0.001 | $1 |
| XRPUSDT | 0.655 | 0.0001 | 0.1 | $1 |
| ETHUSDT | 0.222 | 0.01 | 0.0001 | $1 |
| DOGEUSDT | 5.065 | 0.00001 | 1 | $1 |
| LTCUSDT | 4.195 | 0.01 | 0.001 | $1 |
| LINKUSDT | 6.377 | 0.001 | 0.01 | $1 |
| AVAXUSDT | 6.821 | 0.001 | 0.01 | $1 |
| SUIUSDT | 7.987 | 0.0001 | 0.1 | $1 |
| ADAUSDT | 6.072 | 0.00001 | 0.1 | $1 |

## 4. SCREEN (trade-price replay)

1. **Data:** Binance.US 1m klines from 2024-12-22 00:00 UTC (10 days of lookback before the first `t`) to 2026-09-27 00:00 UTC.
   - Evaluation times run from **2025-01-01** every 2 days to the last `t` whose 240 h window closes by 2026-09-27 00:00 UTC.
   - That is the same starting era as 2026-09-23.
2. **Prints:** each 1m kline with at least 1 trade contributes 4 prints, in the 2026-09-23 order: an up bar gives open, low, high, close; a down bar gives open, high, low, close. They are timed at the minute's open +0 / +20 / +40 / +59 s.
   - **A minute with no trade contributes no print.** Binance fills such minutes with the previous close, which is not a trade.
3. **Fill rule:** `Grid.on_print` as it is: trade-through and strict. A BUY at L fills on a print below L; a touch never fills.
4. **Deploy:** at `t`, the price is the last print before `t` x (1 + h), because the taker buy pays the ask. It must lie strictly inside (lo, hi), or the evaluation is skipped (counted).
5. **Outside and exit:** outside is `Grid.outside_since`, set by prints (the bot's own rule). The deadline is `outside_since + 300 s`. It is checked before every print and at every minute's end, as `Bot._apply` and `check_exits` do with a live feed.
   - At the deadline, the grid closes at the last print x (1 - h), the taker sale at the bid.
6. **Mark at 240 h:** the last print x (1 - h).
7. **Unmeasured minute:** a minute absent from the kline data (no row at all, e.g. exchange downtime).

## 5. DECIDER (book-crossing replay)

1. **Data:**
   - **Fills, exits and deploy prices** come from GoldenEye's recorder book candles, `D:\GoldenEye\output\binanceus\book_candles\<BASE>_USDT\<YYYY-MM-DD>.csv`. They are read offline as static files. No process depends on a process, and GridBot builds no recorder (ruling 2).
   - **The scanner lookback** comes from Binance.US 1m klines, as a live scanner's would.
2. **When it runs:** evaluation times are every 2 days at 00:00 UTC from **2026-09-28**, while `t + 240 h` is at or before the data cut.
   - The DECIDER runs on the first daily cut at or after **2026-10-28 00:00 UTC** at which there are **at least 60 clean grids and at least 5 clean downside exits** [registrar's choice].
   - If that is not reached by **2026-11-27 00:00 UTC**, it runs on that cut, and the result is **NOT PASSED (underpowered)**. The port does not proceed by default.
3. **Fill rule (ruling 1):** for each recorded minute:
   - a BUY at L fills if `ask_close <= L`;
   - a SELL at L fills if `bid_close >= L`.

   **How it is driven:** through `Grid.on_print`, with the ask (buy side) or the bid (sell side) as the print. The print is nudged by one float ulp, so `on_print`'s strict test implements `<=` / `>=`. The opposite side is masked by clamping to the neighbouring level.

   **So `Grid`'s own loop fills every crossed level, each at its own price.** Kline prints are never used for fills.
4. **Deploy:** at `t`, the `ask_close` of the minute ending at `t`. If that minute has no row, the evaluation is skipped (counted).
5. **Outside and exit:** a minute is outside when its `mid_close` is below `lo` or above `hi`.
   - The grid exits at the first recorded minute close that is 300 s or more after the first outside minute's close, with every minute in between recorded and outside.
   - The sale is at that minute's `bid_close`.
6. **Mark at 240 h:** `bid_close`.
7. **Unmeasured minute (ruling 2):** every minute counted by a row's `gap_before_min > 0`, plus any minute with no row. This covers recorder-wide gaps from `_gaps.csv` too, since they leave no row for any pair. Such minutes are UNMEASURED, never interpolated.

## 6. Pass rules

**The DECIDER passes if and only if all of these hold, on its clean grids:**
1. The mean net return per grid is above 0.
2. The median net return per grid is above 0.
3. **The worst tenth of downside exits is no worse than -10.0% of the grid's capital (ruling 4).** This is the nearest-rank 10th percentile of net return over clean grids that exited below the band.
   - Kraken measured -9.1% (2026-09-23, n = 1,054). The bar is set at the measured reality it must beat, and it never moves after results.
4. **Sample:** at least 60 clean grids and at least 5 clean downside exits [registrar's choice]. Fewer means the tail is unmeasured, which is not "bounded".

Mean alone is rejected, because grids die by their tails (ruling 4).

**The SCREEN computes the same statistics** on its clean grids, with a sample floor of 200 clean grids and 20 downside exits. It reports them as **"SCREEN: pass-rule conditions met / not met"**. That line is a number check, not a verdict. It decides nothing and gates nothing.

## 7. Reporting (both replays)

- **Per-pair rows and a pooled row:**
  - evaluations; skipped, by reason; rejected by the scanner, by reason; refused ladders; deployed; clean; unclean;
  - mean / median net %; worst-tenth downside %; downside, upside and held-to-240 h counts;
  - fills per grid-day; round trips per grid; the distribution of level counts.
- **The qualified-only subset,** with its n.
- **One comparison line with 2026-09-23 (Kraken)**, naming what differs: pairs, 15m vs 1m, first-close vs 300 s exit, fees, spreads.
- **The effective-n caveat:** 10-day windows starting every 2 days overlap about 5x, and the 9 majors move together.
- Sample size and era go next to every number. No statistic outside this section is promoted to a headline after results.

## 8. Known limitations, stated before any result

1. **Spreads** are fixed per pair from 6.5 hours of recorder data. The real spread varied over the SCREEN's era.
2. **Today's tick, lot and minimum rules and today's fee schedule** are applied to the whole SCREEN era.
3. **This is a per-grid replay:** no portfolio effects (the 3-grid cap, the correlation gate, cooldowns, the book guard).
4. **SCREEN:** the order of trade extremes within a minute is assumed.
5. **DECIDER:** it sees minute-close quotes only. A crossing within a minute that reverts before the close is missed. `mid_high/low_sampled` are sampled values, not true extremes.
6. **The DECIDER depends on GoldenEye's recorder** staying up. Each recorder gap makes every overlapping grid unclean.
7. **30 days is one market stretch.** Correlated majors in overlapping windows make the effective n much smaller than the grid count.
