# Changelog

Newest first. Each version is also a tagged release on GitHub. The running bot shows its version on the
first line of the log ("swingbot v1.7.0 | Mode: ...") and next to the symbol on the dashboard;
`py -m swingbot --version` prints it too.

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
