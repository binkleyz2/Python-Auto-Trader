# swingbot

A day-trading bot for Public.com or Alpaca (stocks) or NinjaTrader (futures) that you run on your own computer. Each morning it scans your watchlist, picks one stock, and trades it long-only on 5-minute bars. It uses swing-point trend lines and is always out of the market by the close.

**You are responsible for every order it sends.** It starts in dry-run mode, which places no orders. Run the backtest and at least a few weeks of dry runs before you use `--live`.

## Rules the bot follows

| Step | Rule |
|---|---|
| Swing low / high | A 5-minute bar whose low (high) is beyond the 5 bars on each side. It is confirmed only after those 5 later bars close (25 minutes), so no hindsight is used. |
| Support line | Two confirmed swing lows, the second **higher**, with no close below the line since. |
| Resistance line | Two confirmed swing highs, the second **lower**, with no close above the line before the current bar. |
| Bounce entry | The bar dips to within 0.25 ATR of rising support, closes above the line, and closes green. |
| Breakout entry | The bar closes 0.1 ATR or more above falling resistance after the prior bar was below it, on 1.2× average volume, and closes green. |
| Initial stop | 0.25 ATR below the line, and at least 0.5 ATR below entry. Trades needing a stop wider than 3 ATR are skipped. Live entries are bracket orders, so the stop sits at Public from the moment the buy fills. |
| Trailing stop | After each 5-minute bar, the newest confirmed swing low since the entry signal sets a new stop 0.25 ATR below its low. The stop only moves up, never to or above the current price, and only if it rises at least 0.1 ATR. Live, the existing stop order is repriced in one step (cancel-and-replace), so a stop is always in place. If Public refuses a change, the old stop stays and the bot retries next bar. |
| Target | None by default: winners run until the trailing stop or the 15:50 sell-off. Set `use_target: true` to also take profit at 2× the risk. |
| Time | Entries only between 9:45 and 15:30 ET. Everything is sold at 15:50. |
| Risk | 0.5% of equity at risk per trade, no position over 25% of equity or 98% of current cash buying power (checked right before each live order), at most 2 trades a day, and no new trades after a 1.5% daily loss. |
| Settled cash only | Live buys only spend cash that was settled when the session started, minus what the bot has already spent that day. Money from today's sales is never reused, so the bot can't cause a good-faith violation. It also never spends more than Public's current withdrawable cash. If Public doesn't report withdrawable cash, the bot skips all buys that day rather than guess. |
| Morning pick | Ranks the watchlist by how close each stock sits to a valid line, using data up to yesterday's close only. Stocks under $10, under $50M average daily volume, or (for bounces) below their 5-day average are skipped. |

All of these numbers can be changed in `config.yaml`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then paste your secret key and account ID
```

To get the secret key, go to public.com → Settings → Security → API. To find your account ID, put a placeholder such as `x` as the account ID, then run `python -c "from dotenv import load_dotenv; load_dotenv(); from swingbot.live import make_client; print(make_client().get_accounts())"`, or look it up in the app.

## Commands

```bash
python -m swingbot demo                 # backtest on fake data, only to confirm the code runs
python -m swingbot fetch --data data    # save the last week of 5-min bars into data/ (run once a day)
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

`--symbol` skips the morning scan and starts on that symbol right away (for futures, use the contract code, e.g. `MCLZ6`). `--bars` sets the bar size for that run; `bar_minutes` in `config.yaml` sets the default. The rules are unchanged; they're counted in bars, so on 1-minute bars everything happens five times faster: swing points are confirmed after 5 minutes instead of 25, ATR-based stops are tighter, and trend lines only reach back about a day (`max_line_age_bars: 400`). Expect more signals, more noise and more stop-outs, and slippage matters more relative to the smaller stop. Try it in dry run or paper first.

Results go to `results/`, daily logs to `logs/`, and the bot's trades to `trades.csv`.

**Live dashboard:** while `run` is going, the bot serves a page at http://127.0.0.1:8765 and opens it in your browser. The page shows:
- today's 5-minute bars
- the support and resistance lines the bot is watching
- your entry, the trailing stop as it steps up, and the initial stop
- the last price (refreshed every 15 seconds) with open and day P&L
- today's trades, the morning scan, and recent activity

Nothing on the page can place, change or cancel orders. It is reachable from this PC only.

**Earlier days on the chart:** the **Show** dropdown at the top right of the chart switches between Today, 2 days and 5 days. The bot always uses the same history (about a week) to find swing points and trend lines; this only changes how much of it you see, so a line that started on an earlier day is drawn in full. Your choice is remembered in that browser.

**Changing the bar size during the day:** next to the chart's legend there is a **Bars** dropdown. Picking a size switches the bars the bot *trades* on, not just the chart; it is the same as restarting with `--bars`, without the restart. The bot applies it within a few seconds: it reloads history at the new size and redraws. That first redraw only catches up — trailing and new entries resume from the next completed bar. An open position keeps its stop exactly where it is; from then on the stop trails under swing lows of the new bars, and it still only moves up. If the new bars can't be loaded, the bot stays on the old size and says so in the activity log. Every change is logged. Public offers 1, 5, 10, 15 and 30 minutes; Alpaca and NinjaTrader also 2 and 3. The dropdown appears only in a browser on the PC running the bot; phones and other PCs see the bar size but can't change it (set `dashboard_lan_control: true` to allow that, or `dashboard_bar_control: false` to remove the dropdown). If the bot stops updating it for more than 2 minutes, a warning appears at the top. At the end of the session a copy is saved as `logs/<date>-dashboard.html`, which you can open later without the bot running. To turn the dashboard off or change its port, edit the `dashboard` settings in `config.yaml`.

**Kill switch:** create a file named `STOP` in the folder. Within 5 minutes the bot cancels its orders, sells its position, and exits.

## Large watchlists (S&P 100, Russell 2000)

The morning scan downloads `scan_workers` symbols at once (default 10). Alpaca goes further and fetches 100 symbols per request, so even 2,000 symbols take only a few dozen requests. A symbol that fails (delisted, or a ticker your broker writes differently) is logged and skipped; the rest carry on. The log reports how long the download took ("Scan data: 1987 of 2000 symbols in 74 s"), and the dashboard shows progress. The log and dashboard list the 25 and 50 best-ranked symbols, not all of them.

If the broker starts refusing requests (rate limits), lower `scan_workers`. Public's SDK already waits and retries when told to slow down. NinjaTrader always downloads one contract at a time.

**Switching lists:** keep each list in its own file and name it in `config.yaml`:

```yaml
watchlist_file: watchlist_russell2000.yaml   # or watchlist_sp100.yaml; "" = use the watchlist: line
```

The file goes in the same folder as `config.yaml`. When `watchlist_file` is set, it replaces the `watchlist:` line. Restart the bot after changing it; the log's first scan line says which list it used ("Scanning 300 symbols from watchlist_russell2000.yaml"). A watchlist file can be in the same `watchlist: [...]` form, or just symbols separated by commas, spaces or new lines; lines starting with `#` are ignored.

`watchlist_sp100.yaml` (the S&P 100 as of the Sept 21, 2026 rebalance) comes with the bot. `tools/russell2000_watchlist.py` builds `watchlist_russell2000.yaml` from the current iShares IWM holdings: `py tools\russell2000_watchlist.py` (all), or add `--top 300` and/or `--min-price 10` for a shorter list. Rerun it now and then to pick up index changes.

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

## Getting backtest data

Public's API serves recent 5-minute bars (about a week at a time), not years of history. You have two options:

1. Run `fetch` every evening. This builds your own archive in `data/`, which grows a week at a time.
2. Buy or download 5-minute history elsewhere and save one `SYMBOL.csv` per stock in `data/`. Each file needs these columns: `timestamp,open,high,low,close,volume`. Timestamps should be UTC or carry an offset, and mark the bar's start time.

Treat any backtest under a few hundred trades as noise.

## Running it daily

Start the bot any time before 9:35 ET on trading days; 9:00 is a good choice. It runs the morning scan straight away (the scan only uses data up to yesterday's close), shows the pick and the earlier days on the dashboard, waits for 9:35, trades, flattens at 15:50, and exits. Weekends exit immediately, and so do holidays once no bars show up by 10:15. On a Mac or Linux machine, a cron entry works. Cron uses the computer's local time, so for San Diego (Pacific) 6:00 AM is 9:00 ET:

```
0 6 * * 1-5  cd /path/to/swingbot && .venv/bin/python -m swingbot run >> logs/cron.log 2>&1
```

On Windows, use Task Scheduler. Only add `--live --yes` once you trust the dry runs. The computer must stay awake and online all session.

## Things to know

- **Day-trading rules.** FINRA's pattern-day-trader rule and its $25,000 minimum were eliminated effective June 4, 2026. Brokers have until October 2027 to update their systems, so check Public's current policy. With `settled_cash_only: true` (the default), the bot buys only with settled cash, which avoids good-faith violations. In practice, after one round trip it usually can't make a second trade the same day, because that sale's proceeds don't settle until the next business day.
- **Your own shares are safe.** If you already own the stock the bot picks, the bot records how many you had before it bought. It only ever sells the shares it bought, and only cancels orders it created.
- **If the bot stops running mid-trade** (PC sleeps, crash), the last stop it set stays at Public and still protects the position, but it won't trail further or sell at 15:50. Check the account if that happens.
- **The daily loss limit** in live mode compares total account value to the morning's value. Other holdings moving can trip it.
- **Bar timestamps.** The bot assumes Public's bar timestamps mark the bar's start. In dry run, check `logs/` to confirm that signals line up with your chart.
- **Not advice.** This is a tool that carries out your rules. Trend-line trading is discretionary in practice, and these are just one mechanical version of it.

## Tests

```bash
python -m pytest -q
```

The tests cover swing detection, a no-lookahead check (a signal at a given bar is the same whether or not later bars exist), backtest rules (one stock a day, flat by 15:50, trade limits), a full dry-run session against a fake account that fails if any order is sent, and live order handling against a fake broker: settled-cash limits, the stop-only bracket, trailing without ever leaving the position unprotected, and never touching shares or orders you already had.
