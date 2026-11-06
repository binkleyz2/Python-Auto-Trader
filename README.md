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

## How the strategy works, step by step

This is the whole strategy in plain words, in the order the bot does things each day. Setting names are in
`code` with their default values; every one can be changed in `config.yaml` (and backtested with `--try`).

### The idea

Prices rarely move in straight lines: they swing up and down. When the lows of those swings keep rising, a
line drawn through two of them (**rising support**) often holds the next dip: buyers step in at about the
same slope. When the highs keep falling, a line through two of them (**falling resistance**) caps the
rallies, until the price finally pushes through it. The bot trades two moments:

- **Bounce:** the price dips to rising support, holds it, and turns up: buy, with the stop just under the line.
- **Breakout:** the price closes clearly through falling resistance on heavy volume: buy, with the stop under
  the line it just broke.

Shorts (`direction: short` or `both`) are the same two trades upside down: a bounce down off falling
resistance, and a breakdown through rising support.

### 1. Before the open: pick the stocks to watch

At start-up (the scheduled task starts it at 8:00 ET) the bot ranks every stock on its watchlist (your
`watchlist`, plus every stock in `watchlist_indexes` such as the Dow or Nasdaq-100) using **5-minute bars
up to yesterday's close only**, so it never sees today's prices before trading them.

A stock is dropped if its last price is under `min_price` ($10), its average daily dollar volume is under
`min_avg_dollar_volume` ($50M), or there isn't enough history. For each remaining stock it looks for the
current trend lines (below) and scores how close the price sits to one:

- **near rising support** (a bounce could come): only if the stock closed **above** its 5-day average close
  (shorts: below). Score = 1 / (1 + distance to the line in ATRs), for a distance of 0 to 2 ATR.
- **near falling resistance** (a breakout could come): a distance of 0 to 1.5 ATR. Same score, +0.1 if
  the stock is above its 5-day average, -0.2 if not.
- either way, +0.15 for each extra time the price has touched the line since it was drawn (up to 3): a
  line that has held more often counts for more.

The `watch_count` (5) best-scoring stocks are watched today. The log and dashboard show the list with each
stock's setup ("0.47 ATR above rising support, 4 extra touches").

### 2. Swing points

Every 5-minute bar (regular hours only; `bar_minutes`) is checked:

- a **swing low** is a bar whose low is below the lows of the `swing_n` (5) bars before it and no higher
  than the 5 bars after it; a **swing high** is the same with highs.
- it only counts once those 5 later bars have closed (25 minutes on 5-minute bars). Until then nobody could
  know it was a swing, so the bot doesn't use it either. This is what keeps the backtest honest.

**ATR** (average true range, `atr_period` = 14 bars) measures how far the stock normally moves in one bar. All
distances below are in ATRs, so the same rules fit a $10 stock and a $500 one, a quiet day and a wild one.

### 3. Trend lines

At each bar the bot draws at most one support line and one resistance line:

- **Support:** through the **newest** confirmed swing low and an older swing low that is **lower** (so the
  line rises), at least `min_anchor_gap_bars` (6) bars apart and no more than `max_line_age_bars` (400 bars,
  about 5 days) old. No close since the first point may be below the line by more than
  `break_tolerance_atr` (0.1 ATR); a broken line isn't a line any more. If the nearest older low doesn't
  qualify, the bot walks back through older ones until one does.
- **Resistance:** the mirror image: the newest swing high and an older, **higher** one (so the line falls),
  with no close above it before the current bar (the current bar is allowed to break it; that's the signal).
- **Touches:** bars after the second point that came within `touch_tolerance_atr` (0.25 ATR) of the line.
  They don't change the trades, only the morning score.

Because the line always uses the newest swing, it updates through the day as new swings form.

### 4. Entry signals (checked when each bar closes, from 9:45 to 15:30)

**Bounce** (`enable_bounce`), all of these on one bar:
1. the bar's low came within 0.25 ATR of the rising support line (or below it),
2. it closed **above** the line,
3. it closed **higher than it opened** (a green bar: buyers won that bar),
4. the line's second point is more than `swing_n` (5) bars back, so the bounce isn't the swing that made the line.

**Breakout** (`enable_breakout`), all of these on one bar:
1. it closed more than 0.1 ATR **above** the falling resistance line,
2. the bar before closed at or below it (a fresh break, not a stock that's been above it for a while),
3. its volume is at least `breakout_volume_mult` (1.2) times the average of the 20 bars before it,
4. it closed higher than it opened.

The watched stocks are checked best-ranked first, longs before shorts, and the **first signal is taken**.
The bot holds **one position at a time**; while it's in a trade it doesn't look for another.

### 5. The stop and the size of the trade

The **initial stop** is the lower of:
- `stop_buffer_atr` (0.25 ATR) below the line at that bar, and
- `min_stop_atr` (0.5 ATR) below the entry bar's close,

and it is always at least `min_stop_pct` (0.25%) below the price, so it can't sit inside the bid-ask spread.
If that puts the stop more than `max_stop_atr` (3 ATR) away, the trade is skipped: too much risk for one bar's
setup. **1R** is the distance from the entry to this stop, the amount the trade risks per share.

**Size:** the number of shares that loses `risk_per_trade_pct` (0.5%) of the account if the stop is hit,
cut down if needed so the position is no more than `max_position_pct` (25%) of the account and fits in the
cash and settled-cash limits (and, for shorts, short buying power). With $100,000 and a stop $2 away, that's
$500 / $2 = 250 shares.

**The order:** live, right after the signal bar closes, a market order goes in **with the stop attached**
(a bracket order on Alpaca, and on Public for longs; an OSO order on NinjaTrader; a Public short gets its stop
order right after the fill), so the stop is at the broker from the start and protects the trade even if the
bot or the PC stops. If the price has already reached the stop
by then, the trade is skipped. The backtest fills at the next bar's open plus `slippage_pct` (0.02%).

### 6. Managing the trade (after every bar)

- **Profit target** (`use_target`, off by default): a limit order at `reward_risk` (2) times the risk
  beyond the entry. On Alpaca, with the target off, the bracket's take-profit is parked far away (+50%) so the
  stop and target stay linked.
- **Breakeven** (`breakeven_at_r`, 1R): once a bar **closes** that far in profit, the stop moves to the entry
  price. From then on the worst case is about a scratch.
- **Trailing stop** (`trail_stop`, on): the newest confirmed swing low since the entry signal moves the stop
  to `trail_buffer_atr` (if blank, the 0.25 ATR stop buffer) under that low. It only moves up (shorts: down
  over swing highs), only by `trail_min_step_atr` (0.1 ATR) or more at a time, and never closer than 0.25%
  to the price.
- Every stop move reprices the stop order at the broker in one step, so there's never a moment without a stop.
  If the broker refuses a change, the old stop stays and the bot tries again next bar.

### 7. How a trade ends

Whichever comes first:
- the **stop** fills at the broker (the initial stop, the breakeven stop, or a trailed stop),
- the **target** fills (if `use_target` is on),
- **15:50 ET** (`flatten_time`): the bot sells everything (buys back shorts) at the market, so it never holds
  overnight,
- you press **Close now** on the dashboard (only while the trade is ahead), or use the kill switch.

You can also move the stop yourself with **Move** on the dashboard's Stop tile (anywhere between the initial stop
and 0.25% from the price); after that, the bot leaves that trade's stop where you put it, unless you tick
"let the bot keep tightening it" (or set `auto_stop_after_manual: true`).

### 8. The rest of the day

After a trade closes the bot looks for the next signal on its watched stocks, until one of these stops it:
- `max_trades_per_day` (2) trades taken,
- the day is down `daily_loss_limit_pct` (1.5%) of the starting account,
- it's past `last_entry_time` (15:30),
- you pressed **Pause new trades**.

At `midday_rescan_time` (12:45) it ranks the watchlist again, this time including the morning's bars, and
watches the new best stocks. If it's in a trade then, it rescans when the trade closes (if that's before
`midday_rescan_by`, 15:00).

After 15:50: the day report and email, the results spreadsheet, the dashboard snapshot, the settings record and
the evening live-vs-backtest check.

### A real example

The bot's short in PLTR on November 4, 2026 (settings: `direction: both`, `swing_n: 4`, `min_stop_atr: 1.5`,
`reward_risk: 2.5`, `breakeven_at_r: 1.5`):

| Time (ET) | What happened |
|---|---|
| 8:00 | Morning scan: PLTR was 7th of the 10 stocks watched that day. |
| 10:15 bar | PLTR bounced down off falling resistance: the bar's high came up to the line at 176.26 and it closed below the line, lower than it opened. **Short bounce signal.** |
| 10:20 | Stop: the higher of 0.25 ATR above the line (about 176.78) and 1.5 ATR above the 176.09 close (179.19), rounded up to **179.20**. Size: 0.5% of the $98,312 account is $492 of risk, which at $3.11 a share would be 158 shares, but the 25% position limit ($24,578) allows only **139**. Target: 2.5R below the expected 176.09 entry = **168.33**. Bracket order sent; filled at **175.87**, so 1R = $3.33 x 139 = $462.52. |
| 11:25 | A bar closed at 170.65, more than 1.5R in profit: **breakeven**. The stop moved down to the entry, 175.88. The trade could no longer lose more than a few cents. |
| 15:50 | Neither the stop nor the target had filled: **end of day**. Bought back at 170.50, **+$746.68** (+1.6R). |

### What the backtest found

Testing the settings on 6 months of 5-minute bars for about 1,800 liquid stocks (with the checks in "Does it
hold up?" above) found that the **minimum stop distance matters most**: stops closer than about 1.5 ATR were
shaken out by ordinary noise far too often. Most other settings made less difference than luck did: which
stocks happened to be picked, and which weeks were tested. The edge found was small (about +0.1R a trade), so
run it on a paper account and compare with the backtest (the evening check does this) before risking real
money. Past results don't guarantee anything.

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
python -m swingbot fetch --data data    # last week of bars for your watchlist into data/ (indexes, more days: download)
python -m swingbot backtest --days 120  # backtest your settings on 120 days of Alpaca history (see below)
python -m swingbot backtest --data data # backtest on the bars you've collected
python -m swingbot scan                 # show this morning's ranking
python -m swingbot run                  # dry run for today's session: real data, simulated fills, NO orders
python -m swingbot run --live           # real orders (asks you to type LIVE)
python -m swingbot site --install       # the always-on results website (see "Results website" below)
```

**Shorter bars or a stock you picked yourself.** If a move catches your eye during the day:

```bash
python -m swingbot run --bars 1 --symbol NVDA          # dry run: watch only NVDA, 1-minute bars, from now
python -m swingbot run --bars 1 --symbol NVDA --live   # same, with orders
```

`--symbol` skips the morning scan and starts on that symbol right away (for futures, use the contract code, e.g. `MCLZ6`). `--bars` sets the bar size for that run; `bar_minutes` in `config.yaml` sets the default. The rules are unchanged; they're counted in bars, so on 1-minute bars everything happens five times faster: swing points are confirmed after 5 minutes instead of 25, ATR-based stops are tighter, and trend lines only reach back about a day (`max_line_age_bars: 400`). Expect more signals, more noise and more stop-outs, and slippage matters more relative to the smaller stop. Try it in dry run or paper first. Bars under 5 minutes need Alpaca's or NinjaTrader's own data: Public's data (`broker: public` or `price_data: public`) has no 1-, 2- or 3-minute history.

Backtest runs are zipped into `backtests/Results/`, daily logs go to `logs/`, and the bot's trades to `trades.csv`.

**Live dashboard:** while `run` is going, the bot serves a page at http://127.0.0.1:8765 and opens it in your browser. The page shows:
- today's 5-minute bars
- the support and resistance lines the bot is watching
- your entry, the trailing stop as it steps up, and the initial stop
- the last price (refreshed every 15 seconds) with open and day P&L
- today's trades, the morning scan, and recent activity

It is reachable from this PC only.

**Close a winning trade by hand:** while a trade is ahead, the Open P&L tile has a **Close now** button. After you confirm, the bot sells (or buys back a short) at the market and records it as "closed by you". It checks a fresh price first and only closes if the trade is still ahead; if it has slipped back, the trade stays open with its stop. A market order can fill a little worse than the last price, so a trade that is only just ahead can still close a few cents behind. The button is greyed out while the trade is behind, and only shows in a browser on the PC running the bot (with `dashboard_lan_control: true`, also for network viewers with the key). After closing, the bot carries on looking for new trades as usual.

**Move the stop yourself:** the Stop tile has a **Move** button while a trade is open. Type a price, press Set, and confirm (it shows what the trade would lose or keep if the stop is hit). The bot checks the price again and reprices the stop order at the broker in one step, so a stop is always in place. The stop can go anywhere from the **initial stop** (never more risk than the trade started with) to **0.25% from the price** (`min_stop_pct`, so it can't fill straight away); the button's tooltip shows the range. Tighter or looser within that range are both fine. By default, once you've set it the bot **stops moving that trade's stop itself** (no breakeven or trailing moves), so it won't undo your choice; the Stop tile says "set by you". If you'd rather the bot carry on, tick **"then let the bot keep tightening it"** in the Move form (or set `auto_stop_after_manual: true` in config.yaml to have it ticked every time): breakeven and trailing then keep working from your level, and as always they only tighten the stop (up for a long, down for a short), never loosen it; the tile says "set by you, bot tightening". You can move it again as often as you like. Only on the PC running the bot, like Close now.

**Pause new trades:** the button next to Rescan stops the bot opening new trades for the rest of the day; a trade already open is still managed as usual (stop, breakeven, target, end of day). Press **Resume** to start looking again. A pause survives a restart the same day and is gone the next morning.

**New version:** each morning the bot checks GitHub, and if a newer swingbot is out the dashboard says so ("v1.23.0 is out - after the close: git pull, then start the bot again"). Turn off with `update_check: false`.

Apart from Close now, Move and Pause, the page's controls never place orders; the profit target setting can move the open trade's target order.

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
- **Which stocks:** `--symbol NVDA` (or `NVDA,AMD`) trades just those, every day, with no scan (like `run --symbol`); otherwise `--universe sp500` (the default), or any of `sp500, nasdaq100, dow, russell2000, nasdaq` joined with commas, or `watchlist` for your own list and `watchlist_indexes`. At most 800 stocks (`--max-symbols`): months of bars for thousands of stocks need a lot of memory.
- **Download ahead of time:** `py -m swingbot download --days 180 --universe sp500,dow,nasdaq100,nasdaq,russell2000` fills the history cache (`data/alpaca-5min/`) for whole indexes without running a backtest, 100 stocks at a time, so memory stays low and there's no 800-stock limit. Stocks already up to date are skipped, so it can be stopped with Ctrl+C and run again to carry on, or run every evening to add the new day. Use the most days you'll backtest: a later backtest of up to that many days on those stocks starts right away. Each stock takes about 100 KB for 180 days.
- **Time:** the first run downloads the bars (a few minutes for 500 stocks) into `data/`; later runs only fetch new days. The test itself takes about a minute or two per run for 500 stocks and 3 months.
- **Results** (zipped into `backtests/Results/`): `report.txt` with the totals (win rate, average R, profit factor, return, largest drawdown) and the same split **by setup** (long/short bounce and breakout/breakdown), **by scan** (morning picks or midday rescan), **by entry hour**, **by exit** and **by month**; every trade in `trades.csv`; the equity curve in `equity.csv`.
- **Excel copy with charts:** each run also has `backtest.xlsx`: a **Summary** tab with the totals, a line chart of the account value day by day and the breakdown tables; an **Equity** tab (value, change and drawdown each day); and a **Trades** tab with filters. A `--try` comparison has `compare.xlsx` instead: the side-by-side table with the best net shaded and a line chart of every combination's account value (the 10 best by net when there are more), an **Equity** tab with one column per combination, and a **Trades** tab with every combination's trades and the settings that made them. The CSV files are still there for anyone without Excel.
- **Interactive chart:** each run also has `backtest-chart.html` (or `compare-chart.html` for `--try`), which opens in any browser, even straight from the zip. It fills the window (sized for a large screen) and has a point at every trade's exit. Hover anywhere and a box at the cursor shows the account value, the date and time (ET), and the trade that closed there: symbol, setup, exit price and reason, P&L and R. Drag across the chart to zoom in on some days; double-click or **Reset zoom** to see them all. In a comparison, click names in the legend to show or hide lines (the 10 best by net are shown at first; **Show all** and **Best 10** switch). No internet is needed; the page has no outside scripts.
- **Every run is kept:** when a `backtest` finishes, its files are zipped and moved into `backtests/Results/`, named by date, time and what was tested (e.g. `backtests/Results/2026-11-03_193005-STGW-180d-swing_n=4-8-breakeven_at_r=1-2-step0.5.zip`), so nothing is overwritten. The zip has that run's reports and trades (and `compare.txt`/`compare.csv` for `--try`), plus `run.txt` (the exact command and version) and the `config.yaml` it used, so you can rerun it later. Change the folder with `backtest_results_dir` in `config.yaml`; leave it blank to leave the files loose in `backtests/` as before.

**Comparing settings side by side:**

```
py -m swingbot backtest --days 120 --try swing_n=4,5 --try breakeven_at_r=1,1.5
```

Values can be a list (`1,1.5,2`), a range of whole numbers (`swing_n=4-8` = 4, 5, 6, 7, 8), a range with a step (`breakeven_at_r=1-2:0.25` = 1, 1.25, 1.5, 1.75, 2), or a mix (`2,4-6`). Or set the gap with a separate switch: `--try breakeven_at_r=1-2 --step 0.5` tries 1, 1.5 and 2; `--step breakeven_at_r=0.5` sets it for one setting only (whole-number settings like `swing_n` keep steps of 1 unless you give them a whole step, e.g. `--step swing_n=2`). The values each setting will try are printed first. Every combination (here 4, up to 64) runs on the same data and prints one line each: trades, win rate, average R, profit factor, net, return and drawdown. The table and each run's full report and trades go into that run's zip (see below). Any setting in `config.yaml` can be tried this way.

**Does it hold up?** Each combination is also checked four ways: the first and second half of the period, and
two random halves of the stocks (the same halves for every combination). The **holds up** column says in how
many of those it made money. Only settings at **4/4** made money every way; a big net without that is usually
luck: the weeks or stocks it happened to get. The halves triple the run time; `--quick` skips them (then it's
the two periods only, x/2). With `--symbol` or fewer than 40 stocks there are no stock halves.

**Dates:** `--from 2026-09-01` and `--to 2026-10-15` trade only on those days (earlier days still give the
trend lines their history). `--from` without `--days` gets enough history by itself. Example:
`py -m swingbot backtest --universe sp500 --from 2026-09-01 --try min_stop_atr=1-2:0.5`

**Reading the results:** average R (the average trade's result in multiples of its initial risk) is the number to watch. Above 0 means the winners pay for the losers; a few hundred trades are needed before the difference between two settings means much. A setting that only wins in one month, or only with one setup, is more likely luck than an edge. And a backtest is the best case: real fills (CELC filled 28 cents worse than its signal price on 2026-10-27) and spreads cost more than `slippage_pct`.

Other data: `py -m swingbot backtest --data data` still tests on one `SYMBOL.csv` per stock in `data/` (columns `timestamp,open,high,low,close,volume`; timestamps in UTC or with an offset, marking the bar's start), for example built with `fetch` each evening.

## Running it daily

Start the bot any time before 9:35 ET on trading days; 9:00 is a good choice. It runs the morning scan straight away (the scan only uses data up to yesterday's close), shows the pick and the earlier days on the dashboard, waits for 9:35, trades, flattens at 15:50, and exits. Weekends exit immediately, and so do holidays once no bars show up by 10:15. On a Mac or Linux machine, a cron entry works. Cron uses the computer's local time, so for San Diego (Pacific) 6:00 AM is 9:00 ET:

```
0 6 * * 1-5  cd /path/to/swingbot && .venv/bin/python -m swingbot run >> logs/cron.log 2>&1
```

On Windows, use Task Scheduler. Only add `--live --yes` once you trust the dry runs. The computer must stay awake and online all session.

## Live vs backtest, every evening

After each session the bot backtests the day on the same stocks it watched, with the settings it used and
the same starting account value, and lines the trades up with what it really did: the same trades in both
(and how much worse or better the real fills were), trades only the bot made, and trades only the backtest
made. It goes in the day report, the results website ("Live vs backtest") and `logs/<day>-check.json`. If real
fills keep coming in worse than `slippage_pct` assumes, raise it so backtests stay honest. By hand, for a day
in the last week: `py -m swingbot.daycheck 2026-11-06`. It can't copy a midday rescan, a bar-size change
during the day, a trade you closed with Close now, or partial fills exactly.

## Settings over time

Each trading day's settings are recorded in `logs/settings-history.jsonl` (the trading settings from
config.yaml, plus anything changed on the dashboard for that day only). The results website groups the days
into periods with the same settings and shows how each did and what changed, so you can tell a change that
helped from a good week.

## Alerts

The bot can tell you when something goes wrong, so you don't have to be watching:
- anything it logs as an error: an order it couldn't confirm, a stop it couldn't move, an exit that failed
- an entry order refused, a stop it had to place on its own, the day report not made or not sent
- from the results website (when it's running): the bot **hasn't started by 9:31 ET** on a trading day,
  or has **stopped responding** during the session (market holidays are known)

and, if you want, a push for **every entry and exit**.

```yaml
ntfy_topic: swingbot-7f3k9q2x8m   # phone: the free ntfy app, subscribed to this topic (long and random: anyone with the name can read it)
alert_email: true                 # also by email, to email_to (uses the SMTP login in .env)
notify_trades: true               # a phone push for each entry and exit too
```

Test it with `py -m swingbot.notify`. The same alert isn't repeated within 15 minutes, there are at most 30 a
day, and sending happens in the background, so it never holds up trading. Your own ntfy server: `ntfy_server`,
with a token in `.env` as `NTFY_TOKEN`.

## Results website (always on)

A second website, separate from the daily dashboard, shows every trading day's results as they build up:
- the account value at the end of each day, and the bot's P&L each day (hover for the numbers; 1M/3M/6M/1Y/All)
- totals: change since the start, bot P&L, trades and win rate, days up and down, the largest drop
- every day, week and month, with links to that day's report and dashboard picture
- every trade, what's working (by setup, by exit, by entry hour) and the best and worst stocks
- downloads (results spreadsheet, `daily-results.csv`, `trades.csv`) and your backtest zips
- a status line: whether the bot is running (or finished, or hasn't started), the last day report and whether
  it was emailed, any file names OneDrive can't sync (like `CON.csv`), and whether a newer version is out
- the activity log for today or any earlier day, as on the PC (keys and passwords hidden)

It reads the files the bot already writes each time a page is opened, so a new day appears after each
session with nothing else to run; an open page refreshes itself every 15 minutes. It never connects to a
broker and nothing on it can place orders.

```bash
py -m swingbot site               # run it in this window (Ctrl+C stops it)
py -m swingbot site --install     # run it in the background, now and every time you log on to Windows
py -m swingbot site --stop        # stop the background copy
py -m swingbot site --uninstall   # stop it, and don't start it at logon any more
```

Settings in `config.yaml`:

```yaml
site_port: 4334
site_cert: C:\Users\you\certs\both.txt   # one PEM file with the certificate AND its private key
site_lan: true                            # other devices can view it (needs site_cert and a key)
site_key:                                 # access key (blank = same as dashboard_key)
```

- **https:** with `site_cert` set it serves https on all network connections. A private key with a password: put it in `.env` as `SITE_CERT_PASSWORD`. A renewed certificate (the file replaced) is picked up within 5 minutes, without a restart. Open it by a name your certificate covers (a wildcard `*.example.com` certificate: e.g. `https://swingbot.example.com:4334/`), with that name pointing to this PC in your DNS or router; an IP address works but the browser will warn that the name doesn't match.
- **Key:** open `https://<name>:4334/?key=<your key>` once on each device; the browser remembers it for 30 days and the key is taken out of the address. 10 wrong keys from one address lock it out for 10 minutes.
- **Without `site_cert`** it's plain http at http://127.0.0.1:4334, on this PC only, with no key.
- **Always on:** `--install` adds a small launcher to your Windows Startup folder (no window, no admin needed), so it starts when you log on. Problems are retried rather than ending it: for example, if OneDrive hasn't synced the certificate yet at logon, it starts once it has. The PC has to be on, awake and logged in for the site to be reachable. Windows may ask once whether Python may use the network: allow private networks. After a `git pull`, run `--install` again to restart it on the new version. Its log is `logs\site.log`.

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
