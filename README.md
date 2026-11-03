# Swingbot

A day-trading bot for Public.com or Alpaca (stocks) or NinjaTrader (futures) that you run on your own computer. Each morning it scans your watchlist, picks one stock, and trades it on 5-minute bars: long by default, or also short (`direction`). It uses swing-point trend lines and is always out of the market by the close.

**You are responsible for every order it sends.** It starts in dry-run mode, which places no orders. Run the backtest and at least a few weeks of dry runs before you use `--live`.

Version history: see [CHANGELOG.md](CHANGELOG.md) and the repository's Releases page. `py -m swingbot --version` shows the installed version.

## Rules the bot follows

| Step | Rule |
|---|---|
| Swing low / high | A 5-minute bar whose low (high) is beyond the 5 bars on each side. It is confirmed only after those 5 later bars close (25 minutes), so no hindsight is used. |
| Support line | Two confirmed swing lows, the second **higher**, with no close below the line since. |
| Resistance line | Two confirmed swing highs, the second **lower**, with no close above the line before the current bar. |
| Bounce entry | The bar dips to within 0.25 ATR of rising support, closes above the line, and closes green. |
| Breakout entry | The bar closes 0.1 ATR or more above falling resistance after the prior bar was below it, on 1.2× average volume, and closes green. |
| Initial stop | 0.25 ATR below the line, and at least 0.5 ATR and at least 0.25% (`min_stop_pct`) below entry. Trades whose line-based stop is wider than 3 ATR are skipped. If the price has already fallen to the stop when the order is about to go out, the trade is skipped. Live entries are bracket orders, so the stop sits at Public from the moment the buy fills. |
| Trailing stop | After each 5-minute bar, the newest confirmed swing low since the entry signal sets a new stop 0.25 ATR below its low. The stop only moves up, never closer than 0.25% below the current price, and only if it rises at least 0.1 ATR. Live, the existing stop order is repriced in one step (cancel-and-replace), so a stop is always in place. If Public refuses a change, the old stop stays and the bot retries next bar. |
| Breakeven | Once a bar closes 1R above the entry (1R = entry minus initial stop; `breakeven_at_r`), the stop moves up to the entry price, so the trade can no longer become a loss. Swing-low trailing continues from there. `breakeven_at_r: 0` turns it off. |
| Target | None by default: winners run until the trailing stop or the 15:50 sell-off. Set `use_target: true` to also take profit at 2× the risk. |
| Time | Entries only between 9:45 and 15:30 ET. Everything is sold at 15:50. |
| Risk | 0.5% of equity at risk per trade, no position over 25% of equity or 98% of current cash buying power (checked right before each live order), at most 2 trades a day, and no new trades after a 1.5% daily loss. |
| Settled cash only | Live buys only spend cash that was settled when the session started, minus what the bot has already spent that day. Money from today's sales is never reused, so the bot can't cause a good-faith violation. It also never spends more than Public's current withdrawable cash. If Public doesn't report withdrawable cash, the bot skips all buys that day rather than guess. |
| Morning pick | Ranks the watchlist by how close each stock sits to a valid line, using data up to yesterday's close only. Stocks under $10, under $50M average daily volume, or (for bounces) below their 5-day average are skipped. |

All of these numbers can be changed in `config.yaml`.

### Shorts (`direction: short` or `both`)

`direction: long` (the default) trades long only. `short` trades short only; `both` trades either, still one position at a time. Shorts follow exactly the same rules as longs, mirrored: the bot turns the chart upside down, applies the long rules, and turns the result back.

| Long | Short |
|---|---|
| Bounce off rising support (green bar) | Bounce off falling resistance (red bar) |
| Breakout above falling resistance | Breakdown below rising support |
| Stop below the line, trails under swing lows, only moves up | Stop above the line, trails over swing highs, only moves down |
| Breakeven and target above the entry | Breakeven and target below the entry |
| Stocks above their 5-day average preferred | Stocks below their 5-day average preferred |
| 15:50: sell | 15:50: buy back |

Risk per trade, the daily loss limit, the trade limit and the largest-position limit are the same for both sides. What's different:

- **A margin account is needed**, with at least $2,000 in it (Alpaca and Public). In a cash account every short is refused. Longs still follow the cash and settled-cash rules; a short uses no cash, and its size is limited by the broker's short buying power and never more than the account value (no leverage).
- **Only easy-to-borrow stocks.** Before each short the bot asks the broker whether the stock can be shorted; hard-to-borrow stocks (borrow fees) and stocks it can't short are skipped and logged. On Public it also skips a stock while the short-sale restriction is on (down 10% from the day before), because market orders are refused then. A dry run asks the same questions, so it skips the same trades.
- **The account must be flat in that stock**: no short while you hold shares of it yourself.
- **Alpaca:** the short is a bracket order like a long (sell, with a buy stop above and a parked or real target below). **Public:** a market short sale, then a separate buy stop; a profit target isn't placed on a Public short. **NinjaTrader:** a sell with the stop attached, like a long in reverse.
- Shorts can lose more than the stop if a stock gaps up through it. The stop is a stop order, not a guarantee.

Try shorts in a dry run and on Alpaca's paper account first.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then paste your secret key and account ID
```

**Your settings live in `config.yaml`.** It isn't part of the download: on the first run the bot copies `config.example.yaml` to `config.yaml`, and from then on that file is yours. `git pull` never changes it. When an update adds a setting, it's added to `config.example.yaml` and listed in `CHANGELOG.md`, and the log's startup lines name any setting your `config.yaml` doesn't have yet (its default is used). Copy a setting across only if you want a different value.

To get the secret key, go to public.com → Settings → Security → API. To find your account ID, put a placeholder such as `x` as the account ID, then run `python -c "from dotenv import load_dotenv; load_dotenv(); from swingbot.live import make_client; print(make_client().get_accounts())"`, or look it up in the app.

## Commands

```bash
python -m swingbot demo                 # backtest on fake data, only to confirm the code runs
python -m swingbot fetch --data data    # save the last week of 5-min bars into data/ (run once a day)
python -m swingbot backtest --days 120  # backtest your settings on 120 days of Alpaca history (see below)
python -m swingbot backtest --data data # backtest on the bars you've collected
python -m swingbot scan                 # show this morning's ranking
python -m swingbot run                  # dry run for today's session: real data, simulated fills, NO orders
python -m swingbot run --live           # real orders (asks you to type LIVE)
```

**Shorter bars or a stock you picked yourself.** If a move catches your eye during the day:

```bash
python -m swingbot run --bars 1 --symbol NVDA          # dry run: watch only NVDA, 1-minute bars, from now
python -m swingbot run --bars 1 --symbol NVDA --live   # same, with orders
```

`--symbol` skips the morning scan and starts on that symbol right away (for futures, use the contract code, e.g. `MCLZ6`). `--bars` sets the bar size for that run; `bar_minutes` in `config.yaml` sets the default. The rules are unchanged; they're counted in bars, so on 1-minute bars everything happens five times faster: swing points are confirmed after 5 minutes instead of 25, ATR-based stops are tighter, and trend lines only reach back about a day (`max_line_age_bars: 400`). Expect more signals, more noise and more stop-outs, and slippage matters more relative to the smaller stop. Try it in dry run or paper first. Bars under 5 minutes need Alpaca's or NinjaTrader's own data: Public's data (`broker: public` or `price_data: public`) has no 1-, 2- or 3-minute history.

Results go to `results/`, daily logs to `logs/`, and the bot's trades to `trades.csv`.

**Live dashboard:** while `run` is going, the bot serves a page at http://127.0.0.1:8765 and opens it in your browser. The page shows:
- today's 5-minute bars
- the support and resistance lines the bot is watching
- your entry, the trailing stop as it steps up, and the initial stop
- the last price (refreshed every 15 seconds) with open and day P&L
- today's trades, the morning scan, and recent activity

Nothing on the page can place, change or cancel orders. It is reachable from this PC only.

**Earlier days on the chart:** the **Show** dropdown at the top right of the chart switches between Today, 2 days and 5 days. The bot always uses the same history (about a week) to find swing points and trend lines; this only changes how much of it you see, so a line that started on an earlier day is drawn in full. Your choice is remembered in that browser.

**Changing the bar size during the day:** next to the chart's legend there is a **Bars** dropdown. Picking a size switches the bars the bot *trades* on, not just the chart; it is the same as restarting with `--bars`, without the restart. The bot applies it within a few seconds: it reloads history at the new size and redraws. That first redraw only catches up — trailing and new entries resume from the next completed bar. An open position keeps its stop exactly where it is; from then on the stop trails under swing lows of the new bars, and it still only moves up. If the new bars can't be loaded, the bot stays on the old size and says so in the activity log. Every change is logged. Public offers 1, 5, 10, 15 and 30 minutes; Alpaca and NinjaTrader also 2 and 3. The dropdown appears only in a browser on the PC running the bot; phones and other PCs see the bar size but can't change it (set `dashboard_lan_control: true` to allow that, or `dashboard_bar_control: false` to remove the dropdown). If the bot stops updating it for more than 2 minutes, a warning appears at the top. At the end of the session a copy is saved as `logs/<date>-dashboard.html`, which you can open later without the bot running. A picture of it is saved next to it as `logs/<date>-dashboard.jpg`, drawn with the Edge or Chrome already on the PC (no window opens; `dashboard_jpg: false` turns it off). For an older snapshot: `py -m swingbot.screenshot logs\2026-10-23-dashboard.html`. To turn the dashboard off or change its port, edit the `dashboard` settings in `config.yaml`.

**Kill switch:** create a file named `STOP` in the folder. Within 5 minutes the bot cancels its orders, sells its position, and exits.

## Settings on the dashboard

**Results spreadsheet:** after each session the bot updates `logs/results-<account>.xlsx` (for example `results-alpaca-paper.xlsx`). It has six tabs: **Summary** (starting and latest account value, total change in $ and %, trades, win rate, best and worst day, and a chart of the account value), **Last 30 days** (one row per trading day: start and end value, change in $ and %, the bot's trade P&L, trades, wins and losses, and the total change since the first day), **Weekly (older)** (the same, one row per Monday-to-Friday week, for days more than 30 days old), **All days**, **By setup** (long bounce, long breakout, short bounce and short breakdown: trades, wins, win rate, net and average P&L, average R) and **Trades** (every finished trade with its R). Only days with orders count (live, or a paper or demo account), not dry runs. Each day is also added to `logs/daily-results.csv`, which is the permanent record the spreadsheet is built from; days from before this feature are filled in from the `logs/<date>-state.json` files. If the spreadsheet is open in Excel at the end of the day it can't be replaced; the log says so and the next run catches up, or run `py -m swingbot.history` to rebuild it any time. `results_spreadsheet: false` turns it off.

**End-of-day report by email:** with `email_report: true` and `email_to:` set in `config.yaml`, the bot mails you a report after each session. It has the day's results (account at start and end, change in $ and %, total since the first day, the bot's trade P&L), a table of every trade (side, type, shares, entry and exit time and price, initial and final stop, how it ended, P&L and R), and a chart for each traded stock showing that day's bars, the trend line behind the trade, the entry and exit, the initial stop and the stop as it moved. The dashboard picture and the results spreadsheet are attached, and any errors from the day's log are listed. The mail goes through Gmail unless you set `smtp_host` and `smtp_port`; the login goes in `.env` as `SMTP_USER` and `SMTP_PASSWORD` (for Gmail, an app password: Google account → Security → 2-Step Verification → App passwords). The report is also saved as `logs/<date>-report.html` with the charts next to it, mail or not. `py -m swingbot.report 2026-10-27 --send` makes or resends one for an earlier day (within about a week, since the charts need that day's bars). If the mail can't be sent, the log says why and the bot carries on.

**Correcting a recorded sale price:** `py -m swingbot.fix_exit 2026-10-26 BUSE` looks up the real fills at Alpaca (on other brokers, add the price: `py -m swingbot.fix_exit 2026-10-26 BUSE 30.67`) and corrects that trade in the state file, `trades.csv`, the end-of-day dashboard copy and the results spreadsheet. It changes only the records, never an order.

The **Settings** card shows the rules in force: risk and limits, stops, profit and timing, as loaded from `config.yaml`. Three can be changed there for the rest of the day, from the PC running the bot (or the network too with `dashboard_lan_control: true`):

| Setting | What it does | config.yaml |
| --- | --- | --- |
| Trail distance from swing points | How far beyond each new swing point the trailing stop goes (under swing lows for a long, over swing highs for a short), in ATR. Larger = looser, rides pullbacks. Affects future moves only; the stop never moves back. | `trail_buffer_atr` (blank = `stop_buffer_atr`) |
| Move stop to entry once ahead | The breakeven trigger in R; 0 = off | `breakeven_at_r` |
| Profit target | Take profit at entry + this many R; 0 = off. On Alpaca it is also placed on the open trade (refused if the price is already there). On Public and NinjaTrader it applies from the next trade. | `use_target` / `reward_risk` |

Changes are logged, kept if the bot restarts that day, and marked "today only" on the card next to the `config.yaml` value. The next day starts from `config.yaml` again; edit the file to make a change permanent.

## Watching several stocks, and rescanning

The morning scan ranks the watchlist and the bot watches the best `watch_count` (default 5) for entries, not just the top one. At each bar close it checks every watched symbol and trades the first that signals, best-ranked first if two signal on the same bar. Still only one position at a time and at most `max_trades_per_day`: while a trade is open only that stock is followed, and the others resume once it closes. `watch_count: 1` gives the old single-pick behaviour, and `--symbol X` watches only X.

**Midday rescan.** At `midday_rescan_time` (default 12:45 ET, the middle of the session) the bot ranks the watchlist again, this time including today's bars, and watches the new best symbols - the same as the dashboard's Rescan button. If a trade is open then, the rescan waits until it closes, as long as that is before `midday_rescan_by` (default 15:00); otherwise it is dropped for the day. It is also skipped after the loss limit or trade limit is hit, or when running with `--symbol`. Set `midday_rescan_time: ""` to turn it off. The backtest does the same (with the same wait for an open trade), and marks each trade as from the morning picks or the midday rescan; `--try midday_rescan_time=12:45,off` shows whether it helps.

The dashboard's **Watching** table lists them with their morning rank, last price and where price sits against its nearest line now. Click a row, or use the **Chart** dropdown, to chart another watched symbol; that choice is per browser and changes nothing in the bot.

**Rescan** (button next to Bars, on the PC running the bot) ranks the whole watchlist again including today's bars so far, and the bot then watches the new best setups. It works only when not in a trade, takes about as long as the morning scan, and is logged. The first check afterwards only redraws; entries resume from the next completed bar.

## Large watchlists (S&P 100, Russell 2000)

The morning scan downloads `scan_workers` symbols at once (default 10). Alpaca goes further and fetches 100 symbols per request, so even 2,000 symbols take only a few dozen requests. A symbol that fails (delisted, or a ticker your broker writes differently) is logged and skipped; the rest carry on. The log reports how long the download took ("Scan data: 1987 of 2000 symbols in 74 s"), and the dashboard shows progress. The log and dashboard list the 25 and 50 best-ranked symbols, not all of them.

If the broker starts refusing requests (rate limits), lower `scan_workers`. Public's SDK already waits and retries when told to slow down. NinjaTrader always downloads one contract at a time.

**Switching lists:** keep each list in its own file and name it in `config.yaml`:

```yaml
watchlist_file: watchlist_russell2000.yaml   # or watchlist_sp100.yaml; "" = use the watchlist: line
```

The file goes in the same folder as `config.yaml`. When `watchlist_file` is set, it replaces the `watchlist:` line. Restart the bot after changing it; the log's first scan line says which list it used ("Scanning 300 symbols from watchlist_russell2000.yaml"). A watchlist file can be in the same `watchlist: [...]` form, or just symbols separated by commas, spaces or new lines; lines starting with `#` are ignored.

**Scanning whole indexes, downloaded each morning:** list them in `config.yaml`:

```yaml
watchlist_indexes: [sp500, dow, nasdaq100, nasdaq, russell2000]
```

Choose any of `sp500` (S&P 500), `dow` (Dow Jones Industrial Average), `nasdaq100` (Nasdaq-100), `nasdaq` (Nasdaq Composite) and `russell2000` (Russell 2000). Each morning the bot downloads their current members (iShares IVV and IWM holdings, SPDR DIA holdings, Nasdaq's own lists) and combines them with your `watchlist_file` / `watchlist`, each stock once. Stocks under `min_price` are dropped straight away where the list has prices. Then only stocks your broker can trade are kept: on Alpaca, active and tradable at Alpaca and, with Public's price data, also tradable on Public; on Public, tradable on Public. The log shows each list's size, the total, and what was left out. Lists are saved in `logs/indexes/` and downloaded again once a day; if a download fails, yesterday's copy is used (with a warning). The Dow and Nasdaq-100 are almost entirely inside the S&P 500 and the Nasdaq Composite, so they add little on their own. All five come to about 4,500 stocks, roughly 10 minutes with Public's price data at 8 requests a second: start the bot by about 9:15. A Rescan reuses the day's list.

Ready-made lists and a script to build or refresh them, from the swingbot folder:

| Index | Command | File | Size |
| --- | --- | --- | --- |
| S&P 100 | (ships with the bot; edit by hand) | `watchlist_sp100.yaml` | 100 |
| Dow Jones Industrial Average | `py tools\index_watchlist.py dow` (ships as of June 29, 2026) | `watchlist_dow30.yaml` | 30 |
| S&P 500 | `py tools\index_watchlist.py sp500` | `watchlist_sp500.yaml` | ~500 |
| Russell 2000 | `py tools\index_watchlist.py russell2000 --top 300 --min-price 10` | `watchlist_russell2000.yaml` | ~2,000 |
| Nasdaq Composite | `py tools\index_watchlist.py nasdaq` | `watchlist_nasdaq.yaml` | ~3,000 |

`--top N` keeps the N largest by index weight and `--min-price P` drops stocks under $P, where the source has weights and prices (S&P 500, Russell 2000; `--top` also works for the Dow). The Nasdaq list has neither; the bot's own `min_price` still applies when it scans. Sources: iShares IVV and IWM holdings, SPDR DIA holdings (with a built-in Dow list if that download fails), and Nasdaq's own directory of Nasdaq-listed securities (common stocks and ADRs only: no ETFs, warrants, units, rights, preferreds, notes or companies flagged deficient). If a download is blocked, the script says which address to open in your browser; then pass the saved file with `--file`. With Public's price data the scan runs at about 8 symbols a second, so the full Nasdaq Composite takes about 6–7 minutes; start the bot early. Switch lists with `watchlist_file:` in `config.yaml`.

## Using Alpaca instead of Public

The strategy, risk rules, trailing stop and dashboard are identical; only the broker connection changes.

1. **Open an account** at alpaca.markets. A paper (fake money) account comes with every signup.
2. **Get API keys.** In the Alpaca dashboard, switch to your **Paper** account and generate an API key and secret. Paper and live accounts have *different* keys.
3. **Add the keys** to `.env`: `ALPACA_API_KEY=...` and `ALPACA_SECRET_KEY=...`
4. **Switch the broker** in `config.yaml`: `broker: alpaca` (keep `alpaca_paper: true`).
5. **Install the Alpaca library** (once): `py -m pip install --user -r requirements.txt`

| Command | With `alpaca_paper: true` | With `alpaca_paper: false` |
|---|---|---|
| `run` | dry run: real data, simulated fills, no orders | same |
| `run --live` | **real orders in your paper account (fake money)** | **real orders with real money** (use your live keys) |

Run `run --live` against the paper account for a few weeks: unlike the dry run, that exercises Alpaca's real order handling (fills, the bracket stop, repricing it) with no money at risk.

How it differs from Public:

- **Entry order.** Alpaca can reprice the stop of a full bracket order (buy + stop + profit target) but not of a stop-only order, so the bot places a bracket with the target parked 50% above the price, where it won't fill in a day (`alpaca_far_target_pct`). The stop is attached from the moment the buy fills and is repriced in place as it trails, as on Public.
- **Settled cash.** Alpaca doesn't report settled cash separately. The bot's limit for the day is the cash it had at the start of the session, and money from that day's sales is never reused (sales settle the next business day).
- **Market data.** The free plan's live bars come from one exchange (IEX), which carries a small share of each stock's trading, so bars are thinner and some quiet 5-minute periods have no bar. The morning volume screen uses full-market volume (the free plan allows it for past days). For real money, consider Alpaca's paid full-market data and set `alpaca_feed: sip`.
- **Your own orders.** Alpaca rejects an order that could trade against one of your own open orders in the same stock. If you have your own order open on the stock the bot picks, the bot's order may be rejected (it shows in the log, and nothing is bought).
- **Margin accounts.** Alpaca accounts, including paper ones, are usually margin accounts. The bot still only spends cash (`use_margin: false`). To make the account itself cash-only, set its maximum margin multiplier to 1 in Alpaca's account settings.

### Alpaca orders with Public's price data

Alpaca's free data comes from one exchange (IEX). For smaller stocks that can mean minutes with no trades, so no bars, stalled charts and weak signals. With an Alpaca account for orders (paper or real) and a Public account for prices, set:

```yaml
broker: alpaca
price_data: public      # bars and quotes from Public.com; orders, positions and cash stay at Alpaca
```

Both sets of keys go in `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `PUBLIC_API_SECRET_KEY`, `PUBLIC_ACCOUNT_ID`). Use `bar_minutes` 5, 10, 15 or 30: Public's 1-minute bars cover only the current day, so there is no 1-, 2- or 3-minute history for the scan and the trend lines (the bot refuses to start with those sizes). Public refuses bursts of requests (it answers "401 Unauthorized"), so its requests are paced to `public_rate_limit` per second (default 8) and retried after a pause if refused; the scan of 300 symbols takes about 40 seconds, 1,400 about 3 minutes. The volume filter uses Public's bar volume. `price_data: broker` goes back to Alpaca's own feed.

## Using NinjaTrader for futures

Same strategy (swing-low trend lines, bounce and breakout entries, trailing stop), same session hours (09:35–15:50 ET, flat overnight) and the same dashboard. It connects through NinjaTrader's web Trade API.

**Before you start (on NinjaTrader's side):**
1. **API Access add-on.** In NinjaTrader's web platform go to Settings → API Access, subscribe, and generate an API key. You get a **CID** and a **secret**, shown once. For Tradovate-backed accounts this needs a live account with more than $1,000 and costs about $25/month.
2. **CME market data license for API use.** The bot needs live prices, and API data is licensed separately from what you see on screen. Gold and silver trade on COMEX; crude, heating oil and gasoline on NYMEX.
3. **Put your login in `.env`:** `NINJA_USERNAME`, `NINJA_PASSWORD`, `NINJA_CID`, `NINJA_SECRET`.
4. **Settings in `config.yaml`:** `broker: ninjatrader`, keep `ninja_env: demo` (simulated money), and list your contracts in `futures_watchlist`.
5. Install the extra libraries once: `py -m pip install --user -r requirements.txt`

| Command | `ninja_env: demo` | `ninja_env: live` |
|---|---|---|
| `run` | dry run: real prices, simulated fills, no orders | same |
| `run --live` | **orders in the NinjaTrader demo account (simulated money)** | **real orders with real, leveraged money** |

**Contracts.** Use exact contract codes: root + month letter + last digit of the year (F Jan, G Feb, H Mar, J Apr, K May, M Jun, N Jul, Q Aug, U Sep, V Oct, X Nov, Z Dec). Examples: `MGCZ6` Micro Gold Dec 2026 ($10 per $1), `SILZ6` Micro Silver ($1,000 per $1), `MCLZ6` Micro Crude ($100 per $1); full-size `GC` ($100), `SI`, `CL` ($1,000), `HO`, `RB`. Micros are much smaller and the sensible place to start.

**Rolling.** Contracts expire. The bot stops trading a contract `futures_roll_days` (5) days before its first notice or expiry and logs a warning; when you see it, replace the code with the next month (for example `MCLZ6` → `MCLF7`). Crude rolls every month; gold and silver every couple of months.

**How futures differ from the stock version:**
- **Size is in contracts.** Risk per contract = stop distance × the contract's dollars per point. The bot trades only if one contract fits within `risk_per_trade_pct`, and never more than `futures_max_contracts` (default 1). If even one contract would risk too much, the trade is skipped.
- **Prices round to the contract's tick** (e.g. $0.10 gold, $0.01 crude), read from NinjaTrader's product data.
- **The stop is attached at entry** (an OSO order) and trailed with a modify order on the same stop, so it's never removed.
- **Futures always trade on margin.** The cash-only and settled-cash rules don't apply; the contract cap, risk per trade and the daily loss limit do. A futures loss can exceed the stop in a fast market, and leverage makes it larger.
- **The stock filters (min price, dollar volume) are off** for futures; the morning scan ranks your contracts by the same trend-line setup score.
- **Sessions.** NinjaTrader allows two logins at once per user. If the bot is running and you also log in on the website and the phone app, the oldest session is closed — possibly the bot's (it logs in again automatically). A separate API user for the bot avoids this.
- **Backtest.** The backtester reports P&L per share, not per contract; for futures use it to judge the signals, not dollar results.

Run several weeks in the demo account before considering `ninja_env: live`. In the first demo session, check the log and dashboard against your NinjaTrader chart to confirm the bars line up (the bot assumes a bar's time marks its start).

## Backtesting on months of history

The quickest way: Alpaca's free data plan includes full-market 5-minute history, years back (anything more than 15 minutes old). Paper keys in `.env` are enough; nothing is traded.

```
py -m swingbot backtest --days 120
```

- **What it tests:** your `config.yaml` exactly as the live bot uses it. Each day it ranks the stocks on the days before (the morning scan), watches the best `watch_count`, ranks again at `midday_rescan_time` with that day's bars so far, takes the first signal among them, one position at a time, up to `max_trades_per_day` and the daily loss limit, and sells at 15:50. Entries fill at the next bar's open plus `slippage_pct`; inside a bar the stop is assumed to fill before the target.
- **Which stocks:** `--universe sp500` (the default), or any of `sp500, nasdaq100, dow, russell2000, nasdaq` joined with commas, or `watchlist` for your own list and `watchlist_indexes`. At most 800 stocks (`--max-symbols`): months of bars for thousands of stocks need a lot of memory.
- **Time:** the first run downloads the bars (a few minutes for 500 stocks) into `data/`; later runs only fetch new days. The test itself takes about a minute or two per run for 500 stocks and 3 months.
- **Results** (in `backtests/`): `report.txt` with the totals (win rate, average R, profit factor, return, largest drawdown) and the same split **by setup** (long/short bounce and breakout/breakdown), **by scan** (morning picks or midday rescan), **by entry hour**, **by exit** and **by month**; every trade in `trades.csv`; the equity curve in `equity.csv`.

**Comparing settings side by side:**

```
py -m swingbot backtest --days 120 --try swing_n=4,5 --try breakeven_at_r=1,1.5
```

Every combination (here 4, up to 16) runs on the same data and prints one line each: trades, win rate, average R, profit factor, net, return and drawdown. It's saved as `backtests/compare.csv`, with each run's full report and trades next to it. Any setting in `config.yaml` can be tried this way.

**Reading the results:** average R (the average trade's result in multiples of its initial risk) is the number to watch. Above 0 means the winners pay for the losers; a few hundred trades are needed before the difference between two settings means much. A setting that only wins in one month, or only with one setup, is more likely luck than an edge. And a backtest is the best case: real fills (CELC filled 28 cents worse than its signal price on 2026-10-27) and spreads cost more than `slippage_pct`.

Other data: `py -m swingbot backtest --data data` still tests on one `SYMBOL.csv` per stock in `data/` (columns `timestamp,open,high,low,close,volume`; timestamps in UTC or with an offset, marking the bar's start), for example built with `fetch` each evening.

## Running it daily

Start the bot any time before 9:35 ET on trading days; 9:00 is a good choice. It runs the morning scan straight away (the scan only uses data up to yesterday's close), shows the pick and the earlier days on the dashboard, waits for 9:35, trades, flattens at 15:50, and exits. Weekends exit immediately, and so do holidays once no bars show up by 10:15. On a Mac or Linux machine, a cron entry works. Cron uses the computer's local time, so for San Diego (Pacific) 6:00 AM is 9:00 ET:

```
0 6 * * 1-5  cd /path/to/swingbot && .venv/bin/python -m swingbot run >> logs/cron.log 2>&1
```

On Windows, use Task Scheduler. Only add `--live --yes` once you trust the dry runs. The computer must stay awake and online all session.

## Things to know

- **Day-trading rules.** FINRA's pattern-day-trader rule and its $25,000 minimum were eliminated effective June 4, 2026. Brokers have until October 2027 to update their systems, so check Public's current policy. With `settled_cash_only: true` (the default), the bot buys only with settled cash, which avoids good-faith violations. In practice, after one round trip it usually can't make a second trade the same day, because that sale's proceeds don't settle until the next business day.
- **Your own shares are safe.** If you already own the stock the bot picks, the bot records how many you had before it bought. It only ever sells the shares it bought, and only cancels orders it created.
- **If the bot stops running mid-trade** (PC sleeps, crash), the last stop it set stays at the broker and still protects the position, but nothing trails it or sells at 15:50 until the bot runs again. Alpaca and NinjaTrader stops are day orders and expire at the close, so don't leave it stopped overnight.
- **If the 15:50 sale fails** (the broker refuses it), the bot retries until 15:59:30. If the shares still aren't sold, the log has a CRITICAL line and the dashboard a red banner, and the next morning the bot reports the leftover position again. It never trades a leftover position itself: close it in the broker's app.
- **Raising the stop by hand.** You can raise the bot's stop order yourself in Public's or Alpaca's app. At the next bar the bot notices the higher stop, adopts it ("Stop for BOOT is 114.23 at the broker … the bot now uses 114.23"), and from then on only moves it higher. It never lowers a stop you raised. A stop you lower by hand is left alone until the bot's next raise. (Not yet for NinjaTrader.)
- **Restarting during the day is safe.** The bot saves its day to `logs/<date>-state.json` after every check. Started again the same day, in the same mode (live or dry run) and with the same broker, it carries on: same trade count, same daily-loss baseline, same settled cash (sale proceeds still aren't reused), and the trades so far on the dashboard. An open position is taken over again: the bot trades that symbol, recognises its own stop, keeps trailing it and sells at 15:50. If the stop fired while the bot was off, that exit is recorded. A file from another day or mode is ignored. Deleting the file makes the next start a fresh day (trade count back to 0).
- **The daily loss limit** in live mode compares total account value to the morning's value. Other holdings moving can trip it.
- **Bar timestamps.** The bot assumes Public's bar timestamps mark the bar's start. In dry run, check `logs/` to confirm that signals line up with your chart.
- **Not advice.** This is a tool that carries out your rules. Trend-line trading is discretionary in practice, and these are just one mechanical version of it.

## Tests

```bash
python -m pytest -q
```

The tests cover swing detection, a no-lookahead check (a signal at a given bar is the same whether or not later bars exist), backtest rules (one stock a day, flat by 15:50, trade limits), a full dry-run session against a fake account that fails if any order is sent, and live order handling against a fake broker: settled-cash limits, the stop-only bracket, trailing without ever leaving the position unprotected, and never touching shares or orders you already had.
