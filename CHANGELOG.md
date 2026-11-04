# Changelog

Newest first. Each version is also a tagged release on GitHub. The running bot shows its version on the
first line of the log ("swingbot v1.13.0 | Mode: ...") and next to the symbol on the dashboard;
`py -m swingbot --version` prints it too.

## v1.15.0 - 2026-11-03

- **Every backtest run is kept:** each `backtest` is also zipped into `results/` as
  `<date>_<time>-<symbol or universe>-<days>-<tries>.zip`, never overwritten. Inside: the reports and trades,
  `run.txt` (command and version) and the `config.yaml` used. New setting `backtest_results_dir`
  (default `results`; blank = off). `backtests/` still holds the latest run.
- `--try` comparisons also save the side-by-side table as `compare.txt`.

## v1.14.0 - 2026-11-03

- **`--step` switch for `--try` ranges:** `--try breakeven_at_r=1-2 --step 0.5` tries 1, 1.5 and 2.
  `--step 0.5` applies to every range without its own step; `--step swing_n=2` applies to one setting.
  Whole-number settings keep steps of 1 under a decimal `--step`, and refuse a decimal step of their own.
- Before running, each setting's values are listed (e.g. `breakeven_at_r: 1.0, 1.5, 2.0`).

## v1.13.0 - 2026-11-03

- **Ranges in `--try`:** `--try swing_n=4-8` tries 4, 5, 6, 7 and 8; `--try breakeven_at_r=1-2:0.25` tries
  1, 1.25, 1.5, 1.75 and 2 (decimals need a step); lists and ranges mix (`2,4-6`).
- Up to 64 combinations per comparison (was 16); the number is shown before it starts.

## v1.12.1 - 2026-11-03

- **Backtest one stock (or a few):** `py -m swingbot backtest --days 180 --symbol NVDA` trades just NVDA
  every day, with no scan and no midday rescan, like `run --symbol`. Several: `--symbol NVDA,AMD`.
  Works with `--try`.

## v1.12.0 - 2026-11-03

- **The backtest does the midday rescan too.** At `midday_rescan_time` it ranks the stocks again using that
  day's bars so far (and only those: no bar from after the rescan time is seen), and watches the new best
  ones, as the live bot does. In a trade it waits for the trade to close, if that is before
  `midday_rescan_by`; it's skipped after the trade or loss limit; with no setups the morning list stays.
- Each backtest trade is marked `morning` or `midday` (`scan` column in `trades.csv`), and the report has a
  **By scan** table. To see whether the rescan helps: `--try midday_rescan_time=12:45,off`.
- A backtest now runs the scan twice a day, so it takes about half as long again.

## v1.11.0 - 2026-11-03

- **Midday rescan** (`midday_rescan_time`, default 12:45 ET): the bot rescans with today's bars at midday.
  If a trade is open, the rescan waits until it closes, as long as that is before `midday_rescan_by`
  (default 15:00).

## v1.10.1 - 2026-11-03

- Faster backtests (about 30%), same results

## v1.10.0 – 2026-11-02

- **Backtest on months of real history:** `py -m swingbot backtest --days 120`. The bars come from Alpaca's
  free full-market history (older than 15 minutes; paper keys are enough) and are cached in `data/`, so
  later runs only fetch new days. `--universe` picks the stocks (default `sp500`; also `nasdaq100`, `dow`,
  `russell2000`, `nasdaq`, or `watchlist`).
- **The backtest now trades like the live bot:** it watches the best `watch_count` stocks from each day's
  scan and takes the first signal among them, one position at a time, up to `max_trades_per_day` and the
  daily loss limit. (Before, it traded only the single top pick.)
- **Side-by-side comparisons:** `--try swing_n=4,5 --try breakeven_at_r=1,1.5` runs every combination
  (up to 16) on the same data and prints one line each; saved as `backtests/compare.csv`.
- **Results by setup, entry hour, exit and month** in each backtest report (`backtests/report.txt`).
- **Results spreadsheet:** new **By setup** tab (long bounce, long breakout, short bounce, short breakdown:
  trades, win rate, net and average P&L, average R) and **Trades** tab (every finished trade and its R).
- Faster: swing points and the trend-line checks are about 3 times quicker (same results), which also
  shortens the morning scan.
- Backtest output now goes to `backtests/` (ignored by Git, like the downloaded history in `data/`).

## v1.9.5 – 2026-11-02

- **Fix: bars across a daylight-saving change.** Daylight saving time ended on Sunday, so a week of Public's
  bars now mixes -04:00 (up to Friday) and -05:00 (from Monday) times. pandas refused to combine them
  ("Tz-aware datetime.datetime cannot be converted to datetime64 unless utc=True"), so the 9:35 rescan lost
  2,184 of 2,535 stocks and picked from the few that had no bars yet today. Without the fix, every scan
  until the old bars age out (about a week) would have failed the same way, and again in March when
  daylight saving starts. Times are now converted through UTC; tests cover both changes.

## v1.9.4 – 2026-10-28

- **Dashboard: the Position, Open P&L and Stop tiles follow the Chart picker too.** On another stock's chart
  they show that stock: "Flat" with "no trade in TMUS", or "the bot is in a trade in VMRK" when the bot's
  trade is in a different stock. Day P&L and Trades today stay for the whole day.

## v1.9.3 – 2026-10-28

- **Dashboard: the name at the top follows the Chart picker.** Choosing another watched stock changed the
  chart but left the top-ranked stock's name, setup and last price above it. Now the header shows the
  stock on the chart with its morning setup and "bot's pick: ..." next to it, and Last price is that
  stock's latest bar close. The Position, Open P&L and Stop tiles still show the bot's own trade, labelled
  with its symbol when another stock is on the chart.

## v1.9.2 – 2026-10-27

- **Nasdaq Composite list:** Nasdaq's symbol file answers scripts with a web page now, and its "ftp" address
  doesn't take web requests at all (60 s wasted). The list now comes from Nasdaq's stock screener, the same
  site the Nasdaq-100 list already comes from, with the symbol file as the second try. The screener has
  prices, so Nasdaq stocks under `min_price` are dropped before the scan (fewer downloads each morning).
- **The day report is emailed once a day.** Starting the bot again after the close (to try settings) made
  the report and spreadsheet again, which is fine, but also mailed the report each time. Now a second run
  says it was already sent; `py -m swingbot.report --send` still resends on purpose.

## v1.9.1 – 2026-10-27

Fixes from the first run with `watchlist_indexes`:

- **Tradable-stock check failed at Alpaca:** alpaca-py rejected Alpaca's whole asset list because some stocks
  are now on an exchange it doesn't know yet (TXSE, the Texas Stock Exchange), so nothing was filtered. The
  list is now read directly, without that check. The short check reads its stock the same way.
- **Nasdaq Composite list:** "not Nasdaq's symbol directory file". The file is now read even with a
  byte-order mark at the start, Nasdaq's second address is tried, and what came back is saved in
  `logs/indexes/` for a look if it still fails. If there's no saved copy yet, a `watchlist_nasdaq.yaml`
  made with the index tool is used instead (the same for each index).
- **Dow list:** "found 118 stocks, expected 30". Empty spreadsheet cells were read as a stock called "NAN"
  and the reading carried on past the holdings table. It now stops at the end of the table and skips
  repeats. (The built-in Dow list was used meanwhile, so nothing was missed.)
- The startup note about settings missing from `config.yaml` no longer lists ones you commented out.

## v1.9.0 – 2026-10-27

- **Scan whole indexes, downloaded each morning:** new setting `watchlist_indexes`, any of `sp500`, `dow`,
  `nasdaq100`, `nasdaq` (Composite) and `russell2000`. Their current members are combined with your
  `watchlist_file` / `watchlist`, each stock once. Lists are saved in `logs/indexes/` and refreshed daily; if
  a download fails, the last saved copy is used.
- **Only tradable stocks are scanned** (with or without indexes): Alpaca's active, tradable US stocks and,
  with Public's price data, stocks tradable on Public; on Public, stocks tradable there. The log lists what
  was left out. If the broker's list can't be fetched, nothing is dropped and the log says so.
- Stocks under `min_price` are dropped before downloading their bars where the index list has prices
  (S&P 500, Nasdaq-100, Russell 2000).
- The scan log shows the estimated time for long lists. All five indexes: about 4,500 stocks, roughly
  10 minutes with Public's price data.
- New: Nasdaq-100 in `tools/index_watchlist.py` too. The download code moved into the bot
  (`swingbot/indexes.py`); the tool works as before.
- New setting in `config.example.yaml`: `watchlist_indexes` (default: none).

## v1.8.1 – 2026-10-27

- The test run no longer shows "DeprecationWarning: websockets.legacy is deprecated". It comes from the
  alpaca-py library (still in its latest version, 0.44.0), not from swingbot, and is harmless; `pytest.ini`
  now hides that one warning. Nothing else changed.

## v1.8.0 – 2026-10-27

- **End-of-day report by email** (optional, `email_report: true` plus `email_to:`). After each session:
  - the day's results: account at start and end, change in $ and %, total since the first day, the bot's
    trade P&L, trades used;
  - every trade: side, type, shares, entry and exit time and price, initial and final stop, how it ended,
    P&L and R;
  - a chart per traded stock: the day's bars, the trend line behind each trade, entry and exit, the
    initial stop and the stop as it moved (and the target, if any);
  - the dashboard picture and the results spreadsheet attached; errors from the day's log listed, and
    warnings in the subject when something needs your attention (a position left open, errors).
- Sent through Gmail by default (`smtp_host`, `smtp_port` for another provider). The login goes in `.env`:
  `SMTP_USER` and `SMTP_PASSWORD` (Gmail: an app password). See `.env.example`.
- The report is always saved as `logs/<date>-report.html` with `logs/<date>-chart-<SYMBOL>.png`, even
  without mail. `py -m swingbot.report [DATE] [--send]` makes or resends one by hand.
- A mail problem is logged as a warning; it never affects trading.
- Trades now also record their initial and final stop, the stop's path and the trend line (for the charts).
- New requirement: `matplotlib`. Run `py -m pip install --user -r requirements.txt` once.
- New settings in `config.example.yaml`: `email_report`, `email_to`, `smtp_host`, `smtp_port`.

## v1.7.0 – 2026-10-26

- **Shorts.** New setting `direction`: `long` (default, unchanged), `short` or `both`. Still one position at a
  time. Shorts use exactly the long rules on the chart turned upside down:
  - bounce off falling resistance, breakdown below rising support;
  - stop above the line, trailing over swing highs and only moving down;
  - breakeven and target below the entry; bought back at 15:50.
  Risk per trade, the daily loss limit, the trade limit and the largest position apply to both sides.
- **Shorting rules:** a margin account is needed (at least $2,000 at Alpaca and Public). Only easy-to-borrow
  stocks; hard-to-borrow and non-shortable stocks are skipped. No short while you hold shares of that stock.
  Short size is limited by the broker's short buying power and never more than the account value. Longs keep
  the cash and settled-cash rules. A dry run checks the same things, so it skips the same trades.
- **Brokers:** Alpaca shorts are bracket orders like longs. Public: market short sale plus a separate buy
  stop (no target on a Public short; skipped while the short-sale restriction is on). NinjaTrader: a sell
  with the stop attached.
- The morning scan ranks short setups too ("short: near resistance", "short: near support") and keeps each
  stock's better setup. The backtest trades shorts and reports long and short results separately.
- Dashboard: "Short" on the position tile, the stop shown above the price, and wording that fits both sides.
  `fix_exit` handles shorts.
- `config.example.yaml` has the new `direction` line; without it your `config.yaml` stays long only.
- Two positions at once (one long, one short) is planned for a later version.

## v1.6.2 – 2026-10-26

- Fix: `py -m swingbot.fix_exit` said "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env" although `.env`
  had them. It didn't read `.env`; now it does, like `py -m swingbot run`.
- `.env` is also read from the folder you start the bot in, not only the bot's own folder.

## v1.6.1 – 2026-10-26

Fix: a trade's sale price could be read from an earlier trade's order (BUSE, 2026-10-26).

- When the broker's stop or target sells the shares, the bot looks up the sale price among its orders. It
  looked at **all of the day's orders**, so BUSE's sale was read as EVRG's earlier sale at 84.32 instead of
  about $30. The dashboard showed +$41,741.50 for the trade and +$41,606.11 for the day; the account itself
  was right. Only the records were wrong: no order was affected.
- Now only the open trade's own orders count (its entry, stop and target legs, replaced stops and its
  sale), for the right symbol, averaged over partial fills. A sale price below half or above twice the
  entry price is rejected with a warning and the last price is used instead.
- **`py -m swingbot.fix_exit DATE SYMBOL [PRICE]`** corrects a recorded trade: the state file, `trades.csv`,
  the end-of-day dashboard copy (.html and .jpg) and the results spreadsheet. With Alpaca it looks the real
  fills up; with other brokers give the price.

## v1.6.0 – 2026-10-24

- **Results spreadsheet** `logs/results-<account>.xlsx` (e.g. `results-alpaca-paper.xlsx`), updated at the
  end of every session:
  - **Summary:** starting and latest account value, total change ($ and %), the bot's trade P&L, trades,
    win rate, best, worst and average day, and a chart of the account value.
  - **Last 30 days:** one row per trading day: start and end value, change ($ and %), the bot's trade
    P&L, trades, wins, losses, and the total change since the first day.
  - **Weekly (older):** the same per Monday-to-Friday week, for days more than 30 days old.
  - **All days:** every day, for your own sums and charts.
- Changes and totals are Excel formulas; the recorded numbers are in blue.
- Only days with orders count (live, or a paper or demo account), not dry runs. One spreadsheet per
  account, so paper and real-money results never mix.
- Each day is first added to `logs/daily-results.csv`, the permanent record. Days from before this version
  are filled in from the `logs/<date>-state.json` files, with the end value estimated (noted on the row).
- If the spreadsheet is open in Excel when the session ends, the log says so and the next run catches up.
  `py -m swingbot.history` rebuilds it any time.
- New setting `results_spreadsheet` (default `true`); copy it from `config.example.yaml` only to turn it off.

## v1.5.1 – 2026-10-24

- **`config.yaml` is no longer part of the repository, so `git pull` never touches your settings.** The
  repository has `config.example.yaml` instead, and Git ignores `config.yaml`.
  - On the first run with no `config.yaml`, the bot copies the example to `config.yaml`.
  - New settings in future versions are added to the example and listed here. At startup the log names
    any setting your `config.yaml` doesn't have yet; the bot uses its default until you copy it over.
- **Updating to this version:** back up your `config.yaml` first (`copy config.yaml config.mine.yaml`),
  because Git removes its copy of the file during this one pull. Then put yours back
  (`move /Y config.mine.yaml config.yaml`).

## v1.5.0 – 2026-10-24

- **Picture of the end-of-day dashboard:** besides `logs/<date>-dashboard.html`, the bot now saves
  `logs/<date>-dashboard.jpg`, the whole page as one image (chart, trades, settings, activity). It's drawn by
  the Edge or Chrome already on the PC, started hidden; no window opens and nothing is downloaded.
  - `dashboard_jpg: true` in `config.yaml` (set `false` to turn it off). `dashboard_jpg_browser` can name a
    browser if the automatic search doesn't find one.
  - If the picture can't be made, the log says why; the bot and the .html copy are unaffected.
  - For snapshots from earlier days: `py -m swingbot.screenshot logs\2026-10-23-dashboard.html` (several
    files at once work too).
- New requirement: `pillow`. Run `py -m pip install --user -r requirements.txt` once.

## v1.4.2 – 2026-10-22

- **Files are read and written as UTF-8 everywhere.** On Windows, Python otherwise uses the old cp1252
  encoding, so two tests failed when reading back the dashboard snapshot (it contains characters such as
  "…"). The bot itself had the same weakness when reading `config.yaml`: a character like "–" or "…" in
  a comment could have stopped it at startup. `config.yaml` saved by Notepad with a byte-order mark also
  works now.

## v1.4.1 – 2026-10-22

Fix: the 15:50 sale could fail and leave a position open overnight with no stop (LIVN, 310 shares, paper).

- At 15:50 the bot cancels its stop and target legs, then sells. On Alpaca's paper account the stop leg sat
  in "pending cancel" for more than 10 seconds, Alpaca refused the sale because the shares were still held
  by that order, and the error ended the session without a sale.
- Now the bot waits up to 30 s for the cancels, retries a refused sale (cancelling again first, so it can
  never sell twice), and keeps trying until 15:59:30. If the shares still can't be sold, the log says so
  as CRITICAL and the dashboard shows a red banner.
- The next morning, a position the bot failed to sell on an earlier day is reported in the log and on the
  dashboard. The bot won't trade those shares; close them yourself.
- Errors that stop the bot are now written to the log file, not just the console.

## v1.4.0 – 2026-10-22

- **More index watchlists:** `tools/index_watchlist.py` builds a watchlist file for the **S&P 500**
  (iShares IVV holdings), the **Dow Jones Industrial Average** (SPDR DIA holdings, with a built-in list as a
  fallback), the **Nasdaq Composite** (Nasdaq's list of Nasdaq-listed common stocks and ADRs) and the
  **Russell 2000** (iShares IWM). `--top` and `--min-price` where the source has weights and prices;
  `--file` for a file saved in the browser.
- `watchlist_dow30.yaml` ships with the 30 Dow stocks as of June 29, 2026 (Alphabet in, Verizon out).
- `tools/russell2000_watchlist.py` still works; it now calls the new script.
- New requirement: `openpyxl` (reads the Dow holdings spreadsheet). Run
  `py -m pip install --user -r requirements.txt` once.

## v1.3.0 – 2026-10-22

- **Settings card on the dashboard** showing the rules in force from `config.yaml`: risk and limits, stops,
  profit, timing.
- **Three settings adjustable from the dashboard for the rest of the day** (PC running the bot only, like
  the other controls; kept across a restart; tomorrow starts from `config.yaml` again):
  - **Trail distance under swing lows** (new setting `trail_buffer_atr`; blank = `stop_buffer_atr`, 0.25
    ATR). A looser trail only affects future stop raises; a stop never moves down.
  - **Move stop to entry once up** (`breakeven_at_r`, R multiple; 0 = off).
  - **Profit target** (R multiple; 0 = off). On Alpaca it is also set on the open trade by moving the
    bracket's take-profit leg; a target at or below the current price is refused. On Public and
    NinjaTrader it applies from the next trade.
- Each change is logged with the old value and the `config.yaml` value.

## v1.2.1 – 2026-10-22

- **Dashboard tooltip shows the current stop.** Hovering a bar showed the stop in force during that bar,
  which looked wrong once the stop had been raised since (NJR: $52.69 shown, stop already at $52.87).
  It now shows "Stop then" and "Stop now" when they differ.
- **Version number** in the log, on the dashboard and via `--version`; this changelog.

## v1.2.0 – 2026-10-22

Fixes for Public's price data, after the first morning using it got no data at all.

- **Bar size 5, 10, 15 or 30 minutes with Public's data.** Public gives 1-minute bars for the current day
  only ("Period WEEK is not compatible with aggregation ONE_MINUTE"), so there's no 1-, 2- or 3-minute
  history. The bot now refuses to start with those sizes and says why. `config.yaml`: `bar_minutes` 3 → 5.
- **Public requests are paced** (`public_rate_limit`, default 8 per second) and retried after a pause:
  Public answered "401 Unauthorized" to bursts of about 25 requests a second. At most 8 downloads run at
  once (no more "connection pool is full" warnings). A 300-symbol scan takes about 45 seconds.
- **Clearer scan errors.** Repeated failures are summarised instead of one line per symbol, and an ERROR
  says so when no symbol could be downloaded at all.

## v1.1.0 – 2026-10-21

- **Watch several stocks** (`watch_count`, default 5): the bot watches the best-ranked symbols from the
  scan and trades whichever signals first. Still one position at a time.
- **Rescan** button on the dashboard: rank the watchlist again with today's prices (only when not in a
  trade). New **Watching** table and **Chart** picker.
- **Restart-safe:** the day is saved to `logs/<date>-state.json`; a restart keeps the trade count, loss
  limit, settled cash and takes over an open position.
- **Alpaca orders with Public's price data** (`price_data: public`), instead of Alpaca's one-exchange
  IEX feed.
- **Breakeven stop** (`breakeven_at_r`, default 1.0): once price is 1R above the entry, the stop moves to
  the entry.
- **Minimum stop distance** (`min_stop_pct`, default 0.25%): no stops inside the bid–ask spread on short
  bars.
- **Follows a stop raised by hand** at the broker and never lowers it.
- **Stale-data warning** when the data feed has no trades in a symbol for a while.
- **Entry fixes:** skip the trade if the price has already fallen to the stop (Alpaca refused such an
  order on 2026-10-20); a refused order no longer counts as a trade.
- Watching-table wording fix ("above resistance" instead of a negative distance).

## v1.0.0 – 2026-10-19

First version on GitHub.

- Morning scan of a watchlist (S&P 100 and Russell 2000 lists, `watchlist_file`, parallel downloads),
  run as soon as the bot starts.
- Swing-point trend lines; long-only bounce and breakout entries; swing-low trailing stop; flat by 15:50.
- Cash only, settled cash only, risk-based position size, daily loss and trade limits.
- Public.com, Alpaca and NinjaTrader (futures) support.
- Live dashboard (local or home network with a key), bar-size dropdown, 1/2/5-day chart view.
