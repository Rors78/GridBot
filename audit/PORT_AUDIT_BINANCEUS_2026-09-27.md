# GridBot: Kraken to Binance.US port audit (Phase 0), 2026-09-27

Phase 0 of the port order (`PORT_TO_BINANCEUS.md`, run with BOT_NAME GridBot, BOT_PATH D:\GridBot; QUOTE is not settled, see section 11). This pass is read-only: no code changed. It covers gridbot.py, broker.py, oracle.py (read only, as always), test_gridbot.py, tui\, the launchers and the stop script. There is also one read-only probe of Binance.US's public market data (`audit/bnus_probe.py`; its output is in `audit/bnus_probe_out.json`). Nothing was signed or sent to Kraken.

Baseline: main at 9d34c7b, test suite `199 passed, 0 failed` (2026-09-27). The red set on main is empty.

## 0. Answer

**The port is not a rename. The first ruling is whether to port a grid at all. After that, three GridBot laws conflict with the order and need rulings.**

- **The scanner is Kraken-bound, and the rule says it stays untouched.** oracle.py hard-codes Kraken's URL, pair registry, ticker fields, OHLC paging and intervals, and asset aliases (section 2). Standing rule: never modify oracle.py. **Recommendation:** a new venue scanner module that imports oracle's *pure* math (make_levels, simulate_grid, optimize_grid, the indicators) and replaces only its I/O. oracle.py then stays the frozen Kraken-era scanner.
- **On Binance.US the trade feed would reconnect every 30 seconds.**
  - Kraken sends a heartbeat every second. `TradeFeed` treats 30 s of silence as a dead socket (`gridbot.py:568`, `:609-625`).
  - A Binance `@trade` stream sends no messages between trades. Only protocol-level pings arrive, and those never reach `on_message`. On SOLUSDT, the most active pair in the probe, two consecutive trades were 275 s apart.
  - Each reconnect replays REST catch-up for every grid. That burns weight on an IP shared with GoldenEye.
  - `check_exits` would see `feed_up == False` most of the time (`gridbot.py:1884-1886`), so wall-clock exits would never fire.
  - **This is the most dangerous single trap in the code.**
- **The Kraken scanner's gates would reject almost everything on Binance.US.** Sample: 42 Binance.US listings of the 24 bases the Kraken scanner ranked, one 24 h window ending 2026-09-27 14:16 UTC.
  - The scanner's liquidity floor ($750k/24h, `oracle.py:826`) passes **2 of 42** (SOLUSDT, XRPUSDT).
  - The order's universe gate ($10k, 10 bps median-of-3 spread, >=15% active 1m bars) passes **16 of 42**, which is 9 bases: ADA, AVAX, DOGE, ETH, LINK, LTC, SOL, SUI, XRP.
  - **Every coin GridBot is trading now fails the gate.**
    - JUP: 0.6-1.0% active minutes, 0.65-0.91% spread.
    - TRUMP: 1.0-2.2% active minutes, 0.56-0.99% spread.
    - TIA was not in that scan's ranking, so it was not probed.
  - The scanner's two current qualified picks fail too. DOT: 1.5-4.6% active, 0.24-0.40% spread. OP: $58-$467 of volume per day.
  - Across all 42 listings, the median pair traded in 4.9% of minutes and showed a 0.23% spread.
- **The premise has two weaknesses before any venue question.**
  - The 2026-09-23 Kraken replay measured the strategy at **-1.25% of alloc per 10 days**. The scanner's score did not rank outcomes (Spearman -0.10). See `audit/AUDIT_2026-09-23.md` for sample and caveats.
  - On Binance.US the fill model also stops being a fair proxy (section 3).
  - **Porting will not fix the first problem and it makes the second worse.** Whether to port a grid at all is the operator's call (section 11, item 1).
- **Three laws conflict and GridBot's own laws win until the operator rules otherwise** (section 1):
  - no HTTP /health,
  - no live placement: the go-live gate restarts from zero on the new venue,
  - no importing another bot's code.

Incidental, and separate from the port (section 10): **GridBot has been down since 2026-09-26 08:05, and the Scheduled Task `GridBot` is Disabled.** A Start-menu power-off at 08:05:33 hard-killed the bot; there is a START row with no STOP. It has not come back through the three boots since. CLAUDE.md still says it auto-starts. Nothing was changed.

## 1. Where the order conflicts with GridBot's standing rules

The order was written from GoldenEye's port. GridBot's CLAUDE.md says what GridBot may not become. Each conflict below takes GridBot's rule until the operator rules otherwise.

| Order says | GridBot rule | Proposed resolution |
|---|---|---|
| Publish `X-MBX-USED-WEIGHT-1m` on **/health** (1.6); watchdog gates on health keys (5.1) | "Do not give it an HTTP server or API. It writes files." | Weight goes in `status.json` (`venue.weight_1m`, `weight_pct`, `weight_at`), and the Lattice header shows it with a warning above 70%. No endpoint. |
| "Paper boot first; then **live with small sizing**" (5.4) | Paper only until the go-live gate passes. The `validate=False` refusal is load-bearing. | The port ends at paper + validate-only. Binance.US's `POST /api/v3/order/test` takes over from Kraken's `validate=true` (documented; **not yet exercised**, it needs a key). The gate's counters (30 RTs, 14 days, one reboot, 7 days with no VALIDATE failure) **restart at zero on Binance.US**: Kraken measurements don't transfer. |
| "Port GoldenEye's universe_filter pattern" (3.3); budget "against GoldenEye's published usage" (1.6) | Standalone: no fleet, no shared anything. | Copy the *pattern* (median-of-3 book spread, closed-bar tape gate, atomic last-known-good file, EMPTY rather than a fallback list) into GridBot's own code. Never `import` from D:\GoldenEye. The weight budget is a number in the PR body, not runtime coupling. |
| "Never modify oracle.py" (GridBot) vs a venue port of the scanner | Never modify oracle.py; pass `--scan-arg`. | New scanner module, oracle.py untouched (section 2). An operator ruling is needed if they would rather lift the rule. |
| ccxt `binanceus` with the listed options (1.1) | None. GridBot uses raw `requests` + `websocket-client` and has no ccxt. | Operator choice (section 11). GridBot needs about 8 public endpoints + 3 signed ones. Raw REST keeps the dependency surface where it is today. The ccxt options only matter if ccxt is chosen. |
| "Boot/logon tasks stay off; the watchdog is the one starter" (5.2) | CLAUDE.md: the Scheduled Task `GridBot` (onlogon) is the boot starter. There is no watchdog; run.bat's loop restarts on a crash. | Today the task is **Disabled** (section 10). Operator ruling: which one thing starts GridBot? The instance lock already makes a second start exit 3. |

## 2. Every Kraken touchpoint

Line numbers are at 9d34c7b. "Port" is the Phase 1-3 action; "Delete" is Phase 6, on order only.

### gridbot.py

| Lines | What | Port action |
|---|---|---|
| 2-53 | Docstring: Kraken public trade stream, TradeVolume, AddOrder validate | Rewrite as venue-neutral. The venue is a key value. |
| 73-74 | `import oracle`, `import broker` | Keep oracle for its pure math. Venue I/O moves to a venue module chosen by `--exchange`. |
| 187-221 | `Grid.on_print` dedupes by trade id: "Kraken's ids are sequential per pair and shared by the websocket and REST" | Transfers **only if** one id space is used. Binance `@trade` `t`, `/api/v3/trades` `id` and `/historicalTrades` `id` share one space: verified, trade id 11889121 appeared in all three. `aggTrades` ids (`a`) are a **different** space, so never mix them into `last_trade_id`. |
| 395-420 | `feasibility()`: Kraken `ordermin` (coins) / `costmin` (USD), "no Kraken pair data" | Map to `LOT_SIZE.minQty` / `stepSize` and `MIN_NOTIONAL.minNotional` (or `NOTIONAL`). SOLUSDT: minQty 0.001, step 0.001, minNotional $1.00 with `avgPriceMins: 5` (one symbol checked). |
| 444-472 | `tick_of` / `snap_levels`: Kraken `tick_size` / `pair_decimals` | `PRICE_FILTER.tickSize` (SOLUSDT 0.01). |
| 558-757 | `TradeFeed`: `wss://ws.kraken.com/v2`, v2 subscribe/unsubscribe JSON, message shape, `STALE_S = 30` on a 1/s heartbeat | **Rewrite, not rename.** `wss://stream.binance.us:9443`, combined stream `<sym>@trade` + `<sym>@bookTicker`. Liveness from the book stream, not trades. Section 3 covers staleness and exits. |
| 760-781 | `fetch_trades_since`: `oracle.kget("Trades", since=…, count=1000)`, rows `r[0]`/`r[2]`/`r[6]` | `GET /api/v3/historicalTrades?fromId=<last_trade_id+1>&limit=1000` (keyless on Binance.US, verified). Weight is ~25 per call from the counter delta; [RE-VERIFY] in exchangeInfo docs. Pages by id, so no timestamp windows are needed. |
| 1120-1148 | `reconcile_async`: `bal.get("ZUSD", bal.get("USD"))` | Binance asset codes (`USD`, `USDT`). Balance in the **configured quote**. |
| 1150-1183 | `_pair_rules` / `_validate_order` / `_validate_deploy`: Kraken meta, `ordertype="market"`, userref | Rules from exchangeInfo. Post-only = `LIMIT_MAKER` (offered on SOLUSDT). userref (int32) becomes `newClientOrderId` (string, <= 36 chars [RE-VERIFY on Binance.US]). Scoping is kept. |
| 1603-1617 | `_rest_price`: Kraken `Ticker` `c[0]` (**last trade**) | Book price. See section 3. |
| 1740-1822 | `_max_corr` / `_refresh_bars`: `oracle.load_ohlc_cache`, Kraken `OHLC` | Venue klines, venue-keyed cache. |
| 1824-1867 | `_deploy_pick`: `make_levels`, snap, feasibility, `price_fn` (last trade), fee from the scan row | Keep make_levels. The rest follows the rows above. |
| 1870-1927 | `check_exits`: `feed_up` from `last_msg <= STALE_S`; a stale print goes to `price_fn` (last trade) | Section 3. |
| 2159-2181 | `_maybe_reload_book`: `oracle.PairBook()` (AssetPairs) every 6 h | exchangeInfo reload, same stale-cache fallback. |
| 2337-2376 | `parse_args`: help text "Kraken tier"; **no `--exchange` / `--quote`** | Add `--exchange` / `--quote` (env fallbacks) as the only source of venue truth (order law 6; CLAUDE.md: parse_args is the single source of config). |
| 2380 | `DEFAULT_MAKER, DEFAULT_TAKER = 0.22, 0.38` | Keep for Kraken. Binance.US gets its own per-venue defaults [RE-VERIFY], section 4. |
| 2383-2446 | `kraken_query_creds`, `fetch_trade_volume` (`api.kraken.com`, `pair=XBTUSD`), `resolve_fees` | Binance.US account commission via a signed call [RE-VERIFY which endpoint returns this account's maker/taker]. Same plausibility gate, same fail-to-defaults. |
| 2476-2498 | AssetPairs load with stale-cache fallback and backoff | Same shape against exchangeInfo (scar: this failure must not end the bot). |
| 2508-2520 | `broker.trade_creds`, `KrakenBroker`, validator, recon | Venue broker chosen by `--exchange`. Keys under new names: never reuse `GRIDBOT_KRAKEN_*`. |

### broker.py (all of it is Kraken)

| Lines | What | Port action |
|---|---|---|
| 32 | `API = "https://api.kraken.com"` | Venue broker class with its own base URL. |
| 33-35, 183-184 | `NOT_ARMED`: `validate=False` raises | **Carried unchanged into the Binance broker, and tested.** The Binance "validate" is `/api/v3/order/test`. The real `/api/v3/order` path must not exist in the new class either. |
| 42-56 | `trade_creds`: `GRIDBOT_KRAKEN_TRADE_KEY` / `_SECRET` | New names, e.g. `GRIDBOT_BINANCEUS_TRADE_KEY`. A Kraken key must never be read by the Binance path. |
| 59-64 | `grid_userref` (crc32, int32) | Keep the idea. Render as `newClientOrderId` (`gb-<crc>-<level>`). |
| 67-108 | `PairRules` from Kraken meta (`pair_decimals`, `lot_decimals`, `ordermin`, `costmin`, `tick_size`) | Constructor from exchangeInfo filters. `volume_str` floors to `stepSize` (not decimals). The minimums check uses minQty + minNotional. |
| 111-198 | `KrakenBroker`: nonce + HMAC-SHA512 over the path, `API-Key` / `API-Sign`, `oflags=post` | Binance signing is HMAC-SHA256 over the query string + `timestamp` + `recvWindow`, with an `X-MBX-APIKEY` header and server-time offset handling. Deliberately still **no cancel path**. |
| 201-266 | `OrderValidator` | Venue-neutral: keep it. |

### oracle.py (read-only: listed so the new scanner module knows what to replace)

| Lines | What |
|---|---|
| 40 | `KRAKEN = "https://api.kraken.com/0/public"` |
| 42-44 | `BASE_ALIASES` / `DISPLAY_ALIASES` (XBT, XDG), `USD_QUOTES` incl. `ZUSD` |
| 46-51 | `VALID_INTERVALS`: Kraken's minute set (1, 5, 15, 30, 60, 240, 1440, 10080, 21600) |
| 53-59 | `DEFAULT_WATCH`: 30 Kraken USD pairs |
| 155-183 | `SESSION`, `PACE(0.22)`, `kget` (Kraken's error array) |
| 187-223 | Cache dir `~/.cache/gridpick_oracle` (or `GRIDPICK_CACHE`): `assetpairs.pkl`, `ohlc_<n>.pkl`, **not venue-keyed** |
| 226-298 | `PairBook` (AssetPairs, wsname/altname resolution) |
| 302-330 | `fetch_ticker`: `c[0]` last, `v`/`p` volume/vwap, `a`/`b` book |
| 333-371 | `fetch_ohlc`: `since` resume; Kraken caps a call at 720 bars (clamped at 1332) |
| 816-845, 1259-1297 | `Cfg` defaults: fee 0.25 ("Kraken base tier"), min turnover $750k, max spread 0.25%, min step 3 x fee |
| 848-893 | `evaluate`: `px = tick["last"]` |
| 915-978 | `scan`: USD_QUOTES filter, "not listed on Kraken" |
| 1170-1192 | `export_payload`: `kraken_pair` key |
| 1308-1309 | Interval validation message names Kraken |

**The math is venue-free and reusable as is:** `make_levels`, `crossings_uniform`, `simulate_grid`, `optimize_grid`, `time_in_band`, `wilder_atr_pct`, `efficiency_ratio`, `variance_ratio`, `hurst_rs`, `half_life_bars`, `median`, `pearson`, `path_points`, `decorate_correlation`, `fmt_px`, `gauge`, `spark`. All of them are rows-in, numbers-out. The new scanner imports them and replaces only the I/O: lines 40-59 (constants), 137-380 (HTTP, cache, PairBook, ticker, OHLC) and 915-978 (`scan`).

### Not Kraken-coupled

`tui/*` (display text only: "never calls Kraken"), `run.bat`, `run_gitbash.sh`, `start_hidden.vbs`, `launch.bat`, `gridbot_stop.ps1`. They are coupled to **paths** instead: see sections 5 and 7.

### Tests with Kraken-shaped fixtures (they stay as Kraken-venue tests; Binance twins are added)

Section 9/9b (`ordermin`/`costmin`/`tick_size`/`pair_decimals`), 24 (TradeVolume `XXBTZUSD`), and the broker block (roughly lines 1281-1344: Kraken signing, `ZUSD` balance, `oflags=post`, no-cancel), plus 1422-1425.

## 3. Every LAST-TRADE read, and what it decides

This is the table the order ranks highest. On Binance.US the last trade can be minutes or hours old, and it can sit on either side of the spread.

| # | Where | Reads | Decides | Proposed read |
|---|---|---|---|---|
| 1 | `_rest_price` `gridbot.py:1603-1617` (`Ticker c[0]`) called from `_deploy_pick` `:1841` | Last trade | (a) the **paper price of the deploy's taker buy** for every SELL rung's coins; (b) which level is left empty; (c) whether the price is inside the band | **Ask** for the buy, and book the half-spread as a cost. The band check uses mid. |
| 2 | Same, called from `check_exits` `:1912` when the last print is over 60 s old | Last trade | Whether a grid is **back inside** (timer reset) and the **exit sale price** | **Bid** for the sale. Inside/outside uses the book, not a stale print. |
| 3 | Trade prints into `Grid.on_print` `:187-221` | Each print | **Fills** (trade-through: BUY at L when a print is < L); `last_px`; `outside_since`, which starts the **exit timer** | The fill model is an operator ruling (below). `outside_since` should be driven by the book: with no prints, the timer never starts on a pair that has moved. |
| 4 | `_apply` `:1482-1490` and `:1527-1531` | `g.last_px` / the print | `exit_due` = the **exit sale price**, in print time | Exit at the **bid** at the deadline, not at the last print. |
| 5 | `close_grid` `:1947` | `price or g.last_px` | Exit booking | Same as #4. |
| 6 | `Grid.mark` `:292-297` via `snapshot` `:1980`, `book_pnl` `:1620-1623` | `last_px` | Equity, unrealised P/L, `floor_pnl` and **the book guard's HALT decision** (`update_guard` `:1634-1665`) | Mark at the **bid** (the price inventory could actually be sold at). A stale print can hide or fake a drawdown halt. |
| 7 | `oracle.evaluate` `:851` | Ticker last | `scan.json` `price` (display) and the band centre fallback | Mid, in the new scanner. |
| 8 | `_max_corr` / `_refresh_bars` `:1740-1822`, oracle `path_points` / closes | **Kline close** | The correlation gate (0.72), and **every scanner metric**: band, levels, containment, ATR, VR, Hurst, `qualified` | Only on pairs that pass the tape gate. Thin-pair bars are silence-then-jump. |

**The fill model is the port's core strategy question.** Today a resting BUY at L fills only when a *trade prints below L*. That is a fair proxy on a dense tape. On a sparse tape it breaks both ways:
- The book can reprice through L with no print, so paper misses the fill. A real bid at L would have been the inside quote, and arbitrageurs would have sold into it at L.
- Missed fills on the way down understate inventory in a sell-off, so paper's floor loss reads smaller than live.

Options:
- (a) trade-through as today, knowingly optimistic about risk;
- (b) **book-through**: BUY at L fills when the best ask <= L, SELL at L when the best bid >= L;
- (c) fill on whichever comes first.

(b) or (c) needs `@bookTicker`, which is also the only reliable liveness signal on this venue. An operator ruling is needed. Whatever is chosen gets a test pinned in the style of the existing "grid vs scanner" tests.

## 4. Every fee reader

| Where | Source | Currency | Note |
|---|---|---|---|
| `DEFAULT_MAKER/TAKER` `gridbot.py:2380` | Constant 0.22 / 0.38 | Rate | Kraken era |
| `resolve_fees` `:2421-2446` | Kraken TradeVolume (account tier), plausibility-gated, falls back to the defaults | Rate | Kraken only |
| Scanner child `--fee args.fee` `:798`; oracle `Cfg.fee` 0.25 default | CLI | Rate | Drives `optimize_grid`'s step floor (`step >= fee x 3.0`, `oracle.py:724`) and net edge. **At a 0% maker fee the floor disappears**, and only `step > spread x 2.2` (`:726`) remains. |
| `_deploy_pick` `:1846` | `r["fee_pct"]` / scan `fee_pct` / `args.fee`, becoming Grid maker | Rate | Booked per fill as `q x L x maker` |
| `Grid.deploy` `:173`, `Grid.close` `:273` | `args.taker` | Rate, **in USD** | Deploy buy and exit sale |
| `Grid._fill_buy` `:229`, `_fill_sell` `:248` | maker | Rate, **in USD** | Every grid fill |
| `status.json fees` `:2035`, Lattice | args | Display | Shows `source` |

**Every fee today is a rate estimate booked in USD; none is billed.** That is correct for paper.

For Binance.US, three things change:
1. **Maker/taker values [RE-VERIFY].** The order says 0% / 0.02% for GoldenEye's account. Read this account's schedule with a signed call before any number is used. Unverified here: no key.
2. **In-kind billing.** BUY fees billed in the coin (or BNB) mean a live BUY of q coins delivers `q - fee`. `Grid` pairs every BUY with a SELL of *exactly those coins* (`gridbot.py:33-34`, `:234`). Live, that SELL is over-sized by the fee, and the venue trims it to the step. The fix is the order's Phase 2.4:
   - floor the SELL to `stepSize` against the **received** quantity,
   - keep a per-coin dust ledger,
   - model the same in paper so paper and live book the same quantity.

   At a 0% maker fee the rung buys are unaffected. The deploy's taker buy is not.
3. **Spread.** The deploy buy and the exit sale are the only taker legs. On the 16 gate-passing listings the median-of-3 spread ran 0.0015-0.0911%. On the pairs GridBot trades now it runs 0.56-0.99%, **28-50x the order's claimed taker fee**. Book those legs at the ask/bid (section 3, rows 1-2) so the spread is a booked cost, not an invisible one.

## 5. Files the bot writes, and venue mixing

All of these live in `--data-dir`, which defaults to the repo root:

`state.json` (+`.bak`, `.tmp`, `.unusable.*`, `.partial.*`), `status.json`, `journal.jsonl`, `scan.json` (the child writes it non-atomically), `gridbot.log` (+`.1-.3`), `scanner.log` (+`.log.1`), `gridbot.lock`, `scanner.pid`, `stop.request`. The launchers write `lattice.stop` and `launcher.log`.

Outside the folder: oracle's cache `~/.cache/gridpick_oracle/` (`assetpairs.pkl`, `ohlc_30.pkl`). The bot also *reads* `ohlc_30.pkl` for the correlation gate.

**It would mix if the paths stayed shared:**
- `state.json` keys grids and cooldowns by symbol. `JUP/USD` on Kraken and `JUP/USD` on Binance.US are the same key. Cooldown is per coin *base* (`:1718`). `closed_totals` would add Kraken P/L into Binance P/L. `guard.peak` would carry Kraken equity.
- `journal.jsonl` has no venue field. Every FILL, EXIT, FIRST_RT and VALIDATE row would need an era filter forever.
- The oracle cache pickles are named per interval, not per venue. A Binance scanner pointed at the same dir would overwrite Kraken bars, and the correlation gate would read the other venue's bars.

**Proposal (order Phase 4):**
- `--data-dir output/binanceus/paper/` for the new venue. Fresh state, fresh guard peak, fresh totals.
- The Kraken root files are archived read-only as they are.
- The new scanner uses `GRIDPICK_CACHE=<data-dir>/cache`.
- Journal rows gain `venue`.

**Path traps that come with the move (the stop scar again):**
- `gridbot_stop.ps1:43` writes `stop.request` to `D:\GridBot\`, but the bot polls `<data-dir>\stop.request` (`gridbot.py:1058`). Move the data dir without the script and **every stop becomes a hard kill: no final save, no STOP row.** That is the exact failure the 2026-09-23 audit fixed.
- `gridbot_stop.ps1:69` writes `lattice.stop` to the root. The Lattice looks for it next to the `--status` it reads (`tui/lattice.py:1146-1149`). `launch.bat` passes no paths, so `tui/feeds.py:36-41` reads the root.
- All three must move together, and each gets a test (a negative control: the old path must fail the test).

**The open Kraken paper book:**
- 3 grids (JUP, TIA, TRUMP), $500, deployed 2026-09-23.
- 5 round trips, total P/L **+$13.15** at the last status (2026-09-26 08:05).
- Frozen since the bot went down (section 10).
- Operator ruling: restart on Kraken long enough to close it on Kraken prices, or archive it as-is. It cannot be carried to another venue.
- Sample: 3 grids, 5 RTs, about 3 days. It is not a measurement.

## 6. Import structure (the second-module trap)

- Nothing in the bot's process imports `gridbot`. It runs as `__main__`, and broker.py and oracle.py never import it. **No second-module instance exists today.**
- **Rule for the port:** venue and quote are resolved once, in `parse_args`, and *passed* (constructor args, CLI args to the scanner child) to broker, the feed, the scanner and PairRules. No module may `from gridbot import VENUE`: that would build a second `gridbot` module next to `__main__` and resolve its own venue. A test should assert that broker/oracle/venue modules never import gridbot.
- **oracle.py exists in three processes:**
  - the bot, which imports it and uses `oracle.kget`, `PairBook`, `load_ohlc_cache`, `make_levels`, `pearson`, `fmt_px` and `gauge`;
  - the scanner child, where it runs as `__main__`;
  - the Lattice, where `tui/glyphs.py:26` imports it for `spark`/`detail`.

  Each has its own `PACE`. The bot's copy paces at 0.22 s (the module default, not `--scan-pace`). **Their request rates add up on the IP.** Budget them together.
- `audit/wf.py` and `audit/an3.py` import `oracle` and `gridbot`. They are Kraken replay tooling in separate processes, and harmless.

## 7. Sidecars and tooling

| Thing | Venue-named keys/paths? | Note |
|---|---|---|
| `run.bat`, `run_gitbash.sh` | No | Exit-code contract (0/2/3/else) is venue-neutral |
| `launch.bat` | Paths (root) | Kill-then-launch. By design it is a second starter that kills first. It must pass `--status/--oracle-json/--log/--journal/--scanner-log` once data moves. |
| `start_hidden.vbs` + Scheduled Task `GridBot` | No | Task is **Disabled** (section 10) |
| `gridbot_stop.ps1` | Paths (`$dir` root for `stop.request` / `lattice.stop`) | Section 5 trap. It matches processes by path, never by "python". |
| `tui/lattice.py`, `tui/feeds.py` | Paths only | Health = age of `status.json`'s own `ts`. No venue keys. Header shows `fees.source` text ("Kraken tier…") |
| status.json | **No venue-named keys** (unlike GoldenEye's `kraken_api`) | Add `venue`, `quote`, `venue.weight_1m`. `mode` stays `"paper"`. |
| `audit/wf.py` replay harness | Kraken bars | Order 3.2 requires a replay that executes *this bot's code* on Binance.US data before any tuned constant changes. None exists. |
| Out of repo | `~/.cache/gridpick_oracle`; `D:\GridPick\data` (Kraken trade archive, read by the audit replays) | Neither is venue-keyed |

Nothing in GridBot gates on a venue-named health key, so the order's 5.1 rename scope is empty here.

## 8. What GridBot does that GoldenEye doesn't (each is a port decision)

1. **Resting two-sided maker ladders** (8 levels, 5-8% steps on the live grids). The fill model is covered in section 3.
2. **The deploy taker buy.** It buys the coins for every SELL rung in one market leg. That is about half the grid's capital when deployed mid-band. On a thin book it walks levels, and the book's top only shows the first level. `@bookTicker` is top-of-book only, so a deploy that needs depth needs a `depth` snapshot (weight [RE-VERIFY]) or a refusal when rung x SELL count exceeds the top level's size.
3. **The exit is a market sale of all inventory after 300 s outside the band.** It is timed in *print time*, and a sparse tape stalls print time. The wall-clock path depends on `feed_up`, and `feed_up` depends on a heartbeat that does not exist (section 0).
4. **The scanner runs as a child process** re-scanning every 120 s, with its own REST budget.
5. **The correlation gate** on 30m bars. The 9 gate-passing bases are majors that move together, so expect 1-2 concurrent grids, not 3.
6. **The book guard** (6% day loss, 12% drawdown) is marked on last trade (section 3, row 6).
7. **Coin, not pair, is the unit.** Cooldown and one-grid-per-coin already treat X/USD and X/USDT as one coin (`:1713-1721`). Keep this: Binance.US lists many bases on both quotes.
8. **A validate mirror instead of live placement.** The Binance twin is `/order/test`. It proves parameters only, like Kraken's validate (see broker.py's docstring).

## 9. Binance.US measurements (read-only, public, keyless)

Sample and era:
- One 24 h window, 2026-09-26 14:16 to 2026-09-27 14:16 UTC.
- 42 listings: every USD/USDT listing of the 24 bases the Kraken scanner ranked in `scan.json` (generated 2026-09-26 14:05 UTC). All 24 bases are listed. TIA was not in that ranking and was not probed.
- Spread is the median of 3 `bookTicker` snapshots, 15 s apart. Only valid books count (both sides > 0, bid < ask). Tape = the share of the 1440 **closed** 1m bars with >= 1 trade.

`audit/bnus_probe.py` reproduces it. Raw rows are in `audit/bnus_probe_out.json`.

| Fact | Measured | Order's claim |
|---|---|---|
| Weight limit | `REQUEST_WEIGHT` 6000 / 1 min; `ORDERS` 100 / 10 s and 200,000 / day; `RAW_REQUESTS` 300,000 / 5 min | 6000/min per IP: **confirmed** |
| Listings | 202 USDT, 54 USD, 5 BTC, 4 USDC (TRADING) | ~202 / ~54: **confirmed** |
| Median pair activity | 4.9% of minutes carry a trade; median spread 0.23% | "Sparse tape, live book": **confirmed** |
| Most active pair | SOLUSDT: 43.0% of minutes; two consecutive trades 275 s apart | |
| Keyless trade-id catch-up | `historicalTrades` answered 200 with no key | |
| Post-only | `LIMIT_MAKER` offered (SOLUSDT) | |
| Pair filters (SOLUSDT) | tick 0.01, lot step 0.001 / minQty 0.001, MIN_NOTIONAL $1.00 (`avgPriceMins` 5) | min cost ~$1: **confirmed for 1 symbol** |
| Fees | **Not measured** (needs a signed call) | 0% / 0.02%: [RE-VERIFY] |
| `/api/v3/order/test` | **Not exercised** (needs a key) | |
| IP weight in use | The counter read **113** before the probe's first call, and 255 after its 91st call about 40 s later. The probe's share can't be separated from other traffic on the IP. | GoldenEye + recorder ~80/min: one reading, not a budget |

Against the gates (per listing):

| Gate | Passes |
|---|---|
| Kraken scanner's own floor, $750k/24h (`oracle.py:826`) | 2 / 42: SOLUSDT, XRPUSDT |
| Kraken scanner's spread cap, 0.25% (`:827`) | 22 / 42 |
| Order's universe gate: $10k + 10 bps + 15% tape | **16 / 42**: ADAUSD, AVAXUSD, AVAXUSDT, DOGEUSD, DOGEUSDT, ETHUSD, ETHUSDT, LINKUSD, LINKUSDT, LTCUSDT, SOLUSD, SOLUSDT, SUIUSD, SUIUSDT, XRPUSD, XRPUSDT |

**USD and USDT survive about equally:** 8 USD and 8 USDT listings pass. ADAUSDT misses the tape floor at 14.9%.

**GridBot's estimated steady weight on Binance.US** (to be replaced by the measured `X-MBX-USED-WEIGHT-1m` before the Phase 1 PR):
- Scanner at 120 s over ~16-30 pairs, 30m klines incremental: ~2 each, plus one `ticker/24hr` batch and one `bookTicker`. Roughly 40-110 per scan, **~20-55/min**.
- Tape gate hourly: 2 kline pages per pair, about **1/min**.
- Bot: WS streams cost no weight. Catch-up is ~25 per grid per reconnect, and ~75 per reconnect with 3 grids.

Sum: **~25-60/min, about 1% of 6000.** With the one reading of other traffic (113) that stays under 3% of the limit. **This holds only once the 30 s reconnect trap is fixed.** Unfixed, it adds up to ~150/min of catch-up plus reconnect churn.

## 10. Incidental finding: GridBot is down and its boot task is disabled

- The last `status.json` is `2026-09-26 08:05:32`. `journal.jsonl` ends with `START` at 2026-09-25 20:14:57 and a `RECON`, with **no STOP row**.
- The System log shows a Start-menu **power-off** (User32 event 1074) at **2026-09-26 08:05:33**, one second later. Fast Startup brought the machine back at 08:09 (boot type 0x1). Full boots followed at 2026-09-26 19:54 and 2026-09-27 04:00. None of the three started GridBot.
- This is CLAUDE.md's predicted case: SHUTDOWN is not reliably delivered, so a power-off or reboot is a hard kill. Live mode must rely on startup reconciliation, as CLAUDE.md already says.
- **The journal's lifetime count is 47 START / 23 STOP.** Most of those predate the stop-path fixes. Recounting since 2026-09-24 is left for the operator.
- `schtasks /query /tn GridBot`: **Status: Disabled**, last run 2026-09-24 22:58, result 0.
- CLAUDE.md ("GridBot IS auto-started at boot") no longer matches the machine. If the task was disabled on purpose (the order's "one starter" rule), CLAUDE.md needs that sentence changed. If not, it needs re-enabling.
- **Nothing was changed.** Starting the bot or the task is the operator's call.
- `gridbot.log` also shows one `status write failed: [WinError 5] Access is denied` at 2026-09-26 02:15:21 (`os.replace` onto `status.json` while a reader held it). It was transient: later writes succeeded. It is noted because live mode's state writes use the same `atomic_write_json`.

## 11. Decisions only the operator can make (the order's list, answered for GridBot where the code can answer)

1. **Port a grid at all?**
   - Against: the Kraken replay lost -1.25%/10d; the scanner doesn't rank outcomes; Binance.US leaves 9 majors.
   - For: fees may be ~0 on maker legs [RE-VERIFY], and the gate-passing books are tight.
   - The order's own instruction applies: "don't port a dead premise." A cheaper first step is a **Binance.US replay that runs GridBot's `Grid` over Binance.US 1m/trade data** for the 9 bases, the same design as the 2026-09-23 audit. Do that before any scanner work.
2. **QUOTE: USD or USDT.** Paper needs no balance. The eventual account's holdings decide. Both quotes pass the gate on 8 listings. GridBot already treats the two as one coin.
3. **Account and keys.** GoldenEye's account is taken (one bot per account). Paper runs keyless. Keys are only for fees, recon and the `/order/test` mirror, so they need a separate account or none.
4. **Scanner:** a new venue module reusing oracle's math (recommended), or lift "never modify oracle.py".
5. **Fill model on a sparse tape:** trade-through, book-through, or either (section 3).
6. **Weight share** on this IP (estimate in section 9).
7. **Kraken-era features:** keep Kraken as a working `--exchange kraken` value (the order's law 6), or dormant-only.
8. **The frozen Kraken paper book** (section 5) and **the disabled boot task** (section 10).
9. **ccxt or raw REST** (section 1).
10. **Confirm the section 1 resolutions:** no /health, no live leg, no GoldenEye imports.

## 12. Proposed PR sequence (each off main, one per phase; nothing lands before the operator merges)

- **P0 (this PR):** this audit, the probe and its output. Docs only.
- **P0.5 (recommended, gated on ruling 1):** Binance.US replay harness executing `gridbot.Grid` on Binance.US data for the 9 gate-passing bases, under both fill models. A go/no-go number with sample and era.
- **P1, venue mechanics:**
  - `--exchange` / `--quote` in `parse_args`;
  - Binance.US public client with the weight header in status.json, 429 Retry-After and 418 = exit (`EXIT_BANNED`, a new run.bat code that does **not** restart);
  - `TradeFeed` rewrite (`@trade` + `@bookTicker`, book-driven liveness);
  - `historicalTrades` catch-up;
  - exchangeInfo PairRules;
  - the new scanner module.
- **P2, money mechanics:** book-priced deploy/exit legs, received-quantity SELL sizing + dust ledger (paper), per-venue fee defaults, account commission read.
- **P3, tape:** the universe gate (GridBot's own copy of the pattern, last-known-good file, EMPTY on first-boot outage); last-trade reads moved per section 3; the fill-model ruling implemented and pinned.
- **P4, records:** `output/<venue>/paper/`, `venue` on journal rows, venue-keyed scanner cache, **stop script + launch.bat + Lattice paths moved together** with negative-control tests.
- **P5, supervision:** one-starter ruling applied, preflight (config, symbols resolve, weight stated, "trade permission unproven until the first order"; for GridBot, until the first `/order/test` with a key), and CLAUDE.md era stamps.
- **P6, Kraken deletion:** only on order, after the Binance.US paper run is proven.

The go-live gate is unchanged in content and **restarts on Binance.US**. `validate=False` stays refused in every broker.
