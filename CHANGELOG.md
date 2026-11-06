# Changelog

Newest first. Each version is also a tagged release on GitHub. The running bot shows its version on the
first line of the log ("swingbot v1.13.0 | Mode: ...") and next to the symbol on the dashboard;
`py -m swingbot --version` prints it too.

## v1.27.1 - 2026-11-06

- **Dashboard: the open trade is in "Trades today".** The list showed only finished trades; the trade still
  open is now the first row (in italics): when it went in, the last price as its "exit", "still open", and its
  P&L so far. The Trades today tile adds "2 closed, 1 open".

## v1.27.0 - 2026-11-06

- **One stock per company on the watch list.** When two share classes of the same company both rank in the top
  `watch_count` (GOOG and GOOGL, BRK.A and BRK.B, FOX and FOXA...), only the better-ranked one is watched and
  the next stock on the list is watched instead; the log says "Not watching GOOG: same company as GOOGL".
  Classes written with a dot are matched automatically and the common pairs with different tickers are built in;
  add others with `same_company: [[ABC, ABCD]]`. The midday rescan and backtests do the same.

## v1.26.0 - 2026-11-06

- **A stop you set yourself can go right up to the price:** one cent under it for a long (over it for a
  short), instead of 0.25% away. On a slow $127 stock the 0.25% buffer was 32 cents, which left no room to move
  the stop up until the trade was well ahead. The bot's own stop moves (initial stop, breakeven, trailing) still
  keep the `min_stop_pct` buffer. The price is checked again just before the order is changed; if it has
  already dropped through your level, the stop isn't moved and the message says why. A stop that close can be
  hit by an ordinary tick, so it's for locking in a gain on purpose.

## v1.25.2 - 2026-11-06

- **Dashboard: where the stop can go is always on screen.** The Stop tile shows "Can move: 126.56 – 126.98",
  updated with the price, and says when there's no room yet (the stop is already within `min_stop_pct` of the
  price). This replaces the Move button's tooltip, which was cut off and disappeared too quickly. The Move
  form's own message is no longer overwritten while you read it.

## v1.25.1 - 2026-11-06

- **Dashboard buttons after a restart:** a page opened before the bot was restarted kept showing live numbers,
  but its buttons (Close now, Move, Pause...) were turned down with a message that vanished at the next refresh,
  so it looked like nothing happened. The page now reloads itself as soon as it sees the bot has restarted, and
  if a button is pressed on an old page anyway, it reloads and says to press it again.
- **Button answers stay on screen:** what the bot says back to a button ("Closing MSTR...", or why not) shows in
  a box at the top of the page for 8 seconds (15 for a refusal), instead of being overwritten a moment later.
- **The log records refusals:** a button the bot turned down, and why, is now in the log.

## v1.25.0 - 2026-11-06

- **After you move the stop, the bot can keep tightening it:** a tick box in the Move form, "then let the bot
  keep tightening it". Ticked, breakeven and trailing keep working from your level (only ever up for a long,
  down for a short); unticked, the stop is left to you as before. The new setting `auto_stop_after_manual`
  (default `false`: left to you) decides whether it starts ticked. The Stop tile says "set by you, bot
  tightening" when it's on.

## v1.24.0 - 2026-11-06

- **Move the stop yourself:** a **Move** button on the dashboard's Stop tile while a trade is open. Type a
  price and confirm (it shows what the trade would lose or keep at that stop); the bot checks the price again
  and reprices the stop order at the broker in one step. Allowed anywhere from the initial stop (no more risk
  than planned) to `min_stop_pct` (0.25%) from the price. After that the bot doesn't move that trade's stop by
  itself (no breakeven or trailing), and the tile says "set by you". Only on the PC running the bot. Kept
  across a restart.

## v1.23.0 - 2026-11-05

- **Live vs backtest, every evening:** after the session the bot backtests the day on the stocks it watched,
  with the settings it used, and compares: the same trades in both and how much worse or better the real fills
  were, and trades only the bot or only the backtest made. In the day report, the results website and
  `logs/<day>-check.json`; by hand: `py -m swingbot.daycheck [date]`.
- **Settings over time:** each trading day's settings go into `logs/settings-history.jsonl` (with that day's
  dashboard changes). The results website shows periods with the same settings, how each did and what changed.
- **`--try` checks whether a result holds up:** every combination is also run on two random halves of the
  stocks, and split into the first and second half of the period; the new **holds up** column says in how many
  of the 4 it made money (4/4 = every way). `--quick` skips the stock halves.
- **`--from` / `--to` dates** for backtests (`--from` alone fetches enough history).

## v1.22.0 - 2026-11-05

- **Alerts** by phone push (the free ntfy app) and/or email: anything the bot logs as an error (an order it
  couldn't confirm, a stop it couldn't move...), a refused entry, a stop it had to place itself, the day report
  not sent. Set `ntfy_topic` and/or `alert_email: true`; test with `py -m swingbot.notify`. No repeats within
  15 minutes, at most 30 a day, sent in the background.
- **Trade pushes** (optional, `notify_trades: true`): a phone notification for every entry and exit.
- **Watchdog:** the always-on results website alerts if the bot hasn't started by 9:31 ET on a trading day
  (it knows the market holidays), or stops responding during the session. The bot writes
  `logs/heartbeat.json` while it runs for this.
- **Pause new trades** button on the dashboard (next to Rescan): no new entries for the rest of the day, an
  open trade still managed; **Resume** to undo. Kept across a restart the same day.
- **New version notice:** the bot checks GitHub each morning and the dashboard says when a newer swingbot is
  out (`update_check: false` turns it off).
- **Results website:** a status line (bot running / finished / not started / stopped, last report and
  whether it was emailed, file names OneDrive can't sync, version) and the **activity log** for any day,
  with keys and passwords hidden.
- **Every command now checks its switches.** A switch the command doesn't use stops it with a message saying
  which command it belongs to (`site --live`: "--live is for run"). `run` only warns and carries on, so a
  scheduled start never fails over a stray switch.
- Dashboard: the Resume state of the button is shown in red.

## v1.21.1 - 2026-11-05

- **`fetch` says when a switch does nothing.** It only gets the last week of bars for your config's
  watchlist, so `--universe`, `--days`, `--symbol` and the like used to be silently ignored
  (`fetch --universe dow,sp500` just fetched the watchlist). Now it stops and shows the `download` command
  that does what you asked, e.g. `py -m swingbot download --days 180 --universe dow,sp500`.

## v1.21.0 - 2026-11-05

- **Close a winning trade by hand:** a **Close now** button on the dashboard's Open P&L tile, only while the
  trade is ahead. After you confirm, the bot checks a fresh price, and if the trade is still ahead it cancels
  the stop and target and sells (or buys back a short) at the market; the trade is recorded as "closed by
  you". If it has slipped back by then, nothing is sold and the stop stays. Shown only on the PC running the
  bot (or network viewers with `dashboard_lan_control: true`). The bot keeps looking for new trades after.

## v1.20.1 - 2026-11-05

- **Dashboard: the "not used - trail_stop is off" note no longer repeats.** On the Trail step row it was
  added again on every refresh (every 3 seconds), until it filled the page. It's now written once.

## v1.20.0 - 2026-11-04

- **Results website, always on:** `py -m swingbot site --install` runs a second website, separate from the
  daily dashboard, that shows every trading day's results as they build up: account value and daily P&L
  charts (hover for the numbers), totals, every day/week/month with links to that day's report and dashboard
  picture, every trade, what's working by setup / exit / entry hour, best and worst stocks, downloads and the
  backtest zips. It reads the bot's own files each time it's opened, so each session shows up by itself.
- https on port 4334 with `site_cert`: one PEM file holding the certificate and its private key (a key
  password goes in `.env` as `SITE_CERT_PASSWORD`). A renewed certificate is used without a restart.
- Access needs a key (`site_key`, or `dashboard_key` if blank), given once as `?key=`; the browser keeps it
  for 30 days. 10 wrong keys lock that address out for 10 minutes. It serves only its own page and the bot's
  results files - never config.yaml, .env or anything else - and it has nothing that can place orders.
- `--install` starts it now and at every Windows logon (Startup folder, no window, no admin). Problems such
  as a certificate OneDrive hasn't synced yet are retried. `--stop` and `--uninstall` too. Log: `logs/site.log`.

## v1.19.3 - 2026-11-04

- **No more files Windows and OneDrive can't handle.** CON (Concentra) is a real ticker, but CON is also a
  name Windows keeps for the console (as are PRN, AUX, NUL, COM1-9 and LPT1-9), whatever the extension, so
  `CON.csv.gz` in the history cache stopped OneDrive syncing. Files named after such a symbol now get an
  underscore (`CON_.csv.gz`); the history cache and `fetch` use it, and a `CON.csv.gz` from before is renamed
  the next time it's used. Other symbols are unchanged, and the symbol itself is still CON everywhere else.

## v1.19.2 - 2026-11-04

- **Dashboard: trailing-stop settings greyed out when `trail_stop: false`.** "Trail distance from swing
  points" and "Trail step" are dimmed with "not used - trail_stop is off in config.yaml", and the trail
  distance can't be changed for today (it would do nothing). The "With no target" / "This trade" lines no
  longer mention a trailing stop when there isn't one, and use your `flatten_time`.

## v1.19.1 - 2026-11-04

- **Breakeven stop logged once, not every bar.** When a trade filled between cents (a short at 175.8725),
  the stop rounded to 175.88, and the bot kept thinking it still had to move the stop to the entry price,
  so it wrote "BREAKEVEN ... stop moved to the entry price" every 5 minutes. The stop itself was only ever
  moved once, which is what the chart and "moved 1x" showed. Now the message appears once, when it moves.
- The stop-move line in the log now says why: "BREAKEVEN stop PLTR 179.20 -> 175.88" or "TRAIL stop ...".
  (Before, a breakeven move was logged as TRAIL, even with trail_stop off.)
- Dashboard legend: "Stop" instead of "Stop (trails)", since it may not trail.

## v1.19.0 - 2026-11-03

- **`download` command:** `py -m swingbot download --days 180 --universe sp500,dow,nasdaq100,nasdaq,russell2000`
  saves Alpaca history for whole indexes into the backtest cache without running a backtest. 100 stocks at a
  time (low memory, no 800-stock cap), skips stocks already up to date, and can be stopped and rerun to carry on.

## v1.18.0 - 2026-11-03

- **Interactive backtest chart:** `backtest-chart.html` / `compare-chart.html` in each run's zip. Opens in any
  browser, fills the window (made for a 32" 2560x1440 screen), with a point at every trade exit. Hovering shows
  a box at the cursor with the account value, date and time, and that trade (symbol, setup, exit price and
  reason, P&L, R). Drag to zoom into some days, double-click to reset; comparison lines can be shown or hidden
  from the legend (best 10 by net at first). Self-contained: no outside scripts, works offline.
- The value labels on every point of the Excel charts (v1.17.0) are gone again; the Excel charts are plain
  lines, and the hover details live in the HTML chart.

## v1.17.0 - 2026-11-03

- **Data labels on the backtest charts:** every point shows that day's account value (whole dollars, 7 pt,
  above the line) in `backtest.xlsx` and `compare.xlsx`. Charts get wider with more days (up to 120 cm) so
  the labels have room.

## v1.16.1 - 2026-11-03

- `compare.xlsx` opens in Excel without the "We found a problem... repaired records: View" message (the
  Compare tab had a leftover frozen-pane setting).

## v1.16.0 - 2026-11-03

- **Backtest results in Excel, with line charts.** A run adds `backtest.xlsx` (Summary with the account value
  chart and breakdown tables, Equity by day, Trades). A `--try` comparison adds `compare.xlsx` (the table
  with the best net shaded, one chart line per combination - the 10 best by net when there are more -
  Equity by day per combination, and all trades with their settings). CSVs are still written, and
  everything goes into the run's zip in `backtests/Results/`.

## v1.15.1 - 2026-11-03

- Backtest zips now go to `backtests/Results/` (was `results/`), and the run's files are **moved** into the
  zip instead of copied, so `backtests/` no longer fills up with loose reports. `backtest_results_dir`
  still changes the folder; blank leaves the files loose as before.

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
