# SCREEN: GridBot's grid on Binance.US, trade-price replay, 2026-09-27

> **SCREEN. Not a verdict. Nothing deploys on this.** Registered in `audit/PREREG_BINANCEUS_REPLAY_2026-09-27.md` (merged in PR #3 before any replay code existed). The only verdict is the DECIDER, a book-crossing replay whose first possible run is 2026-10-28.
>
> **What "lower bound" means here (prereg section 2).** It holds for the fill *mechanism* only: a print through a level means the book crossed it. It does not bound P&L in either direction, and at these two resolutions it does not strictly bound fill counts either. **These numbers are not a floor under the DECIDER.**

## What ran

| | |
|---|---|
| Code | `audit/bnus_replay.py` at 045b8b5: `python -X utf8 audit/bnus_replay.py screen`, then `report` |
| Run | 2026-09-27 16:38 UTC, 71 s, 9 worker processes; raw rows in `screen_20260927T163800Z.jsonl` (cache) and `audit/screen_binanceus_2026-09-27.csv` (every evaluation) |
| Data | Binance.US 1m klines, 2024-12-22 to 2026-09-27 UTC, 9 USDT pairs, 926,817 of 927,360 minutes each |
| Missing data | The 543 missing minutes are the same stretch on every pair: an exchange-wide outage in August 2026 |
| Share of minutes with a trade | 12.9% (AVAX) to 40.4% (ETH) |
| Fetch cost | about 9,100 `klines` calls (limit 1000, about 5 weight each) over roughly 1.5 h. Paced, with a back-off above 40% of the IP's 6000/min budget. IP peak 2,424/6000 (other traffic on the IP included). No 429 and no 418. |
| Engine | `gridbot.Grid`, unmodified. The scanner is `oracle.evaluate`, unmodified, with `Cfg(fee=0.0, min_turnover=10000)` and the registered spreads |
| Tests | `audit/test_bnus_replay.py`: 11 passed, 0 failed. `--mutants`: 6 of 6 caught |
| Disclosure | Before the full run, a crash and timing check ran 3 evaluation dates on SOLUSDT. One grid's result was seen (2025-01-01: +2.75%, an upside exit after 35.7 h). No code or parameter changed afterwards. |

## Registered report (prereg section 7), verbatim from `report`

Evaluations run from 2025-01-01 to 2026-09-17 UTC, every 2 days, on 9 pairs. Figures are for clean grids, in % of the grid's capital, with a 240 h horizon.

| pair | evals | skipped | scanner-rejected | ladder refused | deployed | clean | mean % | median % | worst-tenth down % | down / up / held | fills per grid-day | round trips per grid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SOLUSDT | 313 | 6 | 0 | 0 | 307 | 302 | -0.50 | +0.50 | -6.54 | 130 / 125 / 47 | 2.05 | 4.24 |
| XRPUSDT | 313 | 5 | 0 | 0 | 308 | 303 | -0.69 | +0.00 | -6.68 | 139 / 115 / 49 | 1.80 | 3.76 |
| ETHUSDT | 313 | 7 | 0 | 0 | 306 | 301 | -0.24 | +0.83 | -6.92 | 107 / 125 / 69 | 1.84 | 4.57 |
| DOGEUSDT | 313 | 8 | 0 | 0 | 305 | 300 | -0.64 | -0.72 | -6.74 | 143 / 116 / 41 | 2.51 | 4.77 |
| LTCUSDT | 313 | 7 | 28 | 0 | 278 | 273 | -0.33 | +0.47 | -7.09 | 115 / 109 / 49 | 2.41 | 5.44 |
| LINKUSDT | 313 | 10 | 32 | 0 | 271 | 266 | -0.78 | -0.50 | -5.86 | 128 / 100 / 38 | 2.44 | 4.15 |
| AVAXUSDT | 313 | 11 | 53 | 0 | 249 | 245 | -0.63 | -0.20 | -6.00 | 119 / 97 / 29 | 2.56 | 4.31 |
| SUIUSDT | 313 | 10 | 48 | 0 | 255 | 250 | -1.14 | -1.10 | -7.67 | 131 / 91 / 28 | 2.30 | 4.04 |
| ADAUSDT | 313 | 9 | 3 | 0 | 301 | 296 | -0.82 | -0.78 | -6.27 | 142 / 103 / 51 | 2.03 | 3.84 |
| **pooled** | 2817 | 73 | 164 | 0 | 2580 | 2536 | -0.63 | +0.13 | -6.71 | 1154 / 981 / 401 | 2.18 | 4.35 |

**Pass-rule conditions on this data** (for the SCREEN this is a number check, never a verdict):

- mean > 0: NOT met
- median > 0: met
- worst tenth of downside exits >= -10%: met
- sample >= 200 clean grids, >= 20 downside exits: met

**SCREEN: pass-rule conditions not met.**

- **Qualified-only subset** (never the statistic): 642 clean grids. Mean -0.81%, median +0.03%, worst tenth of downside exits -7.22% (283 downside exits).
- **Not deployed, by reason:**

  | reason | count |
  |---|---|
  | scanner reject: `liquidity < $10.0K/24h` | 163 |
  | skip: `lookback incomplete` | 45 |
  | skip: `deploy price outside the band` | 28 |
  | scanner reject: `no fee-positive grid` | 1 |

- **Unclean:** 44 grids, whose windows touched the August 2026 outage (start dates 2026-08-22 to 08-30). They are out of every statistic.
- **Ladders deployed:**
  - levels: 8 in 2,569 grids, 12 in 11;
  - kinds: geo 1,296, arith 1,284;
  - step % median 2.18 (p10 1.15, p90 4.13).
  - With a 0% maker fee the scanner still picks its widest-band, fewest-levels ladder, as it did on Kraken (8 levels in 2,518 of 2,522).

## Comparison with 2026-09-23 (Kraken), as registered

| | Kraken, 2026-09-23 | Binance.US SCREEN, 2026-09-27 |
|---|---|---|
| Grids | 2,502, 14 alt pairs | 2,536 clean, 9 USDT majors |
| Era | 2025-01-01 to 2026-09-11 | 2025-01-01 to 2026-09-17 |
| Mean | -1.25% | -0.63% |
| Median | -0.02% | +0.13% |
| Worst tenth of downside exits | -9.1% (n = 1,054) | -6.71% (n = 1,154) |

**This is not a like-for-like venue comparison.** Everything changed at once:
- the pairs (alts vs majors);
- bar resolution (15m vs 1m);
- the exit rule (first 15m close outside vs the real 300 s rule);
- fees (0.22/0.38 vs 0.00/0.02);
- spread (a fixed 0.10% vs registered 0.2-8 bps);
- the Kraken era's 15m-close exit fired less often.

Which change moved which number cannot be separated from these two runs.

**Effective n.** 10-day windows starting every 2 days overlap by up to 5x, and the 9 majors move together. The effective sample is far smaller than 2,536: 2026-09-23 estimated about 200 for its 2,502.

## Supplementary (not registered, not a headline)

These are descriptive cuts computed after the run, from the CSV above.

- **By exit type** (clean grids):

  | exit | grids | mean | median | median life |
  |---|---|---|---|---|
  | downside | 1,154 | -3.95% | -3.67% | 60.9 h |
  | upside | 981 | +2.17% | +1.81% | 48.8 h |
  | held to 240 h | 401 | +2.07% | +2.11% | 240 h |

- **By half-year** (clean grids; 2026 H2 is a partial half):

  | half | grids | mean | median |
  |---|---|---|---|
  | 2025 H1 | 801 | -0.72% | -0.56% |
  | 2025 H2 | 803 | -0.70% | +0.19% |
  | 2026 H1 | 705 | -0.73% | -0.49% |
  | 2026 H2 | 227 | +0.23% | +0.61% |

## Limits of this measurement (prereg section 8, plus what the run showed)

1. **Spreads** are fixed per pair from 6.5 hours of recorder data. Today's tick, lot, minimum and fee rules are applied to the whole era.
2. **It is a per-grid replay:** no 3-grid cap, no correlation gate, no cooldown, no book guard.
3. **Prints are 4 per traded minute**, in an assumed order. Only 12.9% to 40.4% of minutes traded at all. That is the thin tape that makes print-crossing fills a weak proxy for resting orders on this venue, and it is why the DECIDER reads the book.
