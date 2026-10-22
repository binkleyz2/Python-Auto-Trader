# Changelog

Newest first. Each version is also a tagged release on GitHub. The running bot shows its version on the
first line of the log ("swingbot v1.4.0 | Mode: ...") and next to the symbol on the dashboard;
`py -m swingbot --version` prints it too.

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
