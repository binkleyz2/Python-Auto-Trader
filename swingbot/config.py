"""All tunable settings in one place. Edit config.yaml rather than this file."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class Config:
    # --- Broker ----------------------------------------------------------------
    broker: str = "public"                # "public", "alpaca" or "ninjatrader"
    alpaca_paper: bool = True             # alpaca: True = paper (fake money) account, False = real money
    alpaca_feed: str = "iex"              # alpaca: "iex" (free plan) or "sip" (paid full-market data)
    price_data: str = "broker"            # alpaca: "broker" = Alpaca's own feed, "public" = Public.com's price data
    alpaca_far_target_pct: float = 50.0   # alpaca: with no target, park the bracket's target this far above
    ninja_env: str = "demo"               # ninjatrader: "demo" (simulated money) or "live" (real money)
    ninja_account: str = ""               # ninjatrader: account name to trade (blank = first active account)
    futures_watchlist: list[str] = field(default_factory=lambda: ["MGCZ6", "MCLZ6"])   # contract codes
    futures_max_contracts: int = 1        # never more than this many contracts per trade
    futures_roll_days: int = 5            # skip a contract this many days before first notice / expiry

    # --- Watchlist / scanner -------------------------------------------------
    watchlist: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "TSLA"])
    watchlist_file: str = ""              # read the watchlist from this file instead (e.g. watchlist_russell2000.yaml)
    watchlist_indexes: list[str] = field(default_factory=list)   # also scan these indexes' members, downloaded daily:
                                          # sp500, dow, nasdaq100, nasdaq, russell2000
    min_price: float = 10.0               # skip stocks under this price
    min_avg_dollar_volume: float = 50e6   # skip thin stocks (avg daily $ volume)
    watch_count: int = 5                  # watch this many top-ranked symbols for entries (1 = only the top pick)
    public_rate_limit: float = 8.0        # Public price data: at most this many requests per second
    scan_workers: int = 10                # morning scan: download this many symbols' bars at once (1-32)

    # --- Bars ---------------------------------------------------------------------
    bar_minutes: int = 5                  # bar size: 1, 2, 3, 5, 10, 15 or 30 (Public data: 5, 10, 15, 30)

    # --- Swing / trend line rules ---------------------------------------------
    swing_n: int = 5                      # bars on EACH side that define a swing point (of bar_minutes each)
    max_line_age_bars: int = 400          # ignore lines whose first anchor is older than this (~5 trading days)
    min_anchor_gap_bars: int = 6          # anchors must be at least this far apart
    touch_tolerance_atr: float = 0.25     # "touching" the line = within this many ATRs
    break_tolerance_atr: float = 0.10     # breakout close must clear the line by this many ATRs
    atr_period: int = 14
    breakout_volume_mult: float = 1.2     # breakout bar volume vs 20-bar average (0 disables)

    # --- Trade management ------------------------------------------------------
    direction: str = "long"               # "long", "short" or "both" (shorts: margin account, borrowable stocks)
    enable_bounce: bool = True
    enable_breakout: bool = True
    trail_stop: bool = True               # raise the stop below each new confirmed swing low
    breakeven_at_r: float = 1.0           # once price is this many R above entry, stop goes to entry (0 = off)
    trail_buffer_atr: float | None = None # trailing stop sits this far below each swing low (blank = stop_buffer_atr)
    trail_min_step_atr: float = 0.1       # only move the stop if it rises at least this many ATRs
    auto_stop_after_manual: bool = False  # after you move the stop on the dashboard: False = leave it to you,
                                          # True = breakeven and trailing keep tightening it from there
    use_target: bool = False              # fixed take-profit at reward_risk x risk (off = let winners run)
    reward_risk: float = 2.0              # take-profit multiple, only used when use_target is true
    stop_buffer_atr: float = 0.25         # stop sits this far below the line / swing low
    min_stop_pct: float = 0.25            # stop is at least this % below the price (short bars: tiny ATR); 0 = off
    min_stop_atr: float = 0.5             # stop is at least this far below entry (avoids noise stop-outs)
    max_stop_atr: float = 3.0             # skip trades whose stop is wider than this
    first_entry_time: str = "09:45"       # no entries in the opening 15 minutes
    last_entry_time: str = "15:30"
    flatten_time: str = "15:50"           # sell everything at/after this time (ET)
    max_trades_per_day: int = 2
    midday_rescan_time: str = "12:45"     # rescan with today's bars at this time ET ("" = off); in a trade: after it closes
    midday_rescan_by: str = "15:00"       # ...but only before this time ET

    # --- Risk --------------------------------------------------------------------
    risk_per_trade_pct: float = 0.5       # % of equity risked per trade (entry-to-stop)
    max_position_pct: float = 25.0        # cap any single position at this % of equity
    daily_loss_limit_pct: float = 1.5     # stop trading for the day after losing this %
    use_margin: bool = False              # False = cash-only buying power
    settled_cash_only: bool = True        # live: only spend cash that was settled before today; never sale proceeds
    buying_power_buffer_pct: float = 2.0  # live: keep this % of buying power unused (market-order slippage)

    # --- Live dashboard (a page on this computer only) -------------------------
    dashboard: bool = True
    dashboard_port: int = 8765
    dashboard_open_browser: bool = True   # open the page automatically when `run` starts
    dashboard_lan: bool = False           # True = other devices on your home network can view it too
    dashboard_key: str = ""               # access key for network viewers (random each run if left blank)
    dashboard_bar_control: bool = True    # show the bar-size dropdown on the page (on this PC only)
    dashboard_lan_control: bool = False   # True = network viewers (with the key) can use the dropdown too
    dashboard_jpg: bool = True            # also save logs/<date>-dashboard.jpg at the end of the day
    dashboard_jpg_browser: str = ""       # Edge/Chrome to draw it with (blank = find it automatically)
    results_spreadsheet: bool = True      # keep logs/results-<account>.xlsx up to date at the end of each day
    email_report: bool = False            # mail the day's report (trades + charts) after the session
    email_to: str = ""                    # where to send it (several: separate with commas)
    email_from: str = ""                  # sender shown on the mail (blank = SMTP_USER from .env)
    smtp_host: str = "smtp.gmail.com"     # mail server (Gmail by default; login in .env: SMTP_USER, SMTP_PASSWORD)
    smtp_port: int = 587                  # 587 = STARTTLS, 465 = SSL

    # --- Alerts (py -m swingbot.notify sends a test)
    ntfy_topic: str = ""                  # phone push via the free ntfy app: a long random topic name (blank = off)
    ntfy_server: str = "https://ntfy.sh"  # your own ntfy server, if you run one
    alert_email: bool = False             # alerts by email too (to email_to, with the SMTP login in .env)
    notify_trades: bool = False           # also a phone push for every entry and exit
    update_check: bool = True             # check GitHub each morning for a newer swingbot
    update_repo: str = "binkleyz2/Python-Auto-Trader"

    # --- Results website (py -m swingbot site): every day's results, always on, separate from the dashboard
    site_port: int = 4334
    site_cert: str = ""                   # PEM file with the certificate AND its private key -> https
                                          # (blank = plain http, on this PC only)
    site_lan: bool = True                 # other devices can view it (needs site_cert and a key)
    site_key: str = ""                    # access key for the site (blank = use dashboard_key)

    # --- Backtest only -------------------------------------------------------------
    starting_equity: float = 25_000.0
    slippage_pct: float = 0.02            # % per side
    backtest_results_dir: str = "backtests/Results"  # each backtest run is moved here as its own zip (blank = off)

    def validate(self) -> None:
        if float(self.breakeven_at_r) < 0:
            raise ValueError(f"breakeven_at_r must be 0 (off) or more, not {self.breakeven_at_r!r}")
        if not 1 <= int(self.watch_count) <= 20:
            raise ValueError(f"watch_count must be 1 to 20, not {self.watch_count!r}")
        if not 0 <= float(self.min_stop_pct) < 20:
            raise ValueError(f"min_stop_pct must be 0 to 20, not {self.min_stop_pct!r}")
        for key in ("midday_rescan_time", "midday_rescan_by"):
            v = str(getattr(self, key) or "").strip()
            if key == "midday_rescan_time" and not v:
                continue
            try:
                hh, mm = v.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
            except ValueError:
                raise ValueError(f"{key} must be a time like \"12:45\", not {getattr(self, key)!r}") from None
        if not 1 <= int(self.scan_workers) <= 32:
            raise ValueError(f"scan_workers must be 1 to 32, not {self.scan_workers!r}")
        if not 1 <= int(self.site_port) <= 65535:
            raise ValueError(f"site_port must be 1 to 65535, not {self.site_port!r}")
        if self.email_report and not str(self.email_to).strip():
            raise ValueError("email_report is on but email_to is empty - say where to send the report")
        from .indexes import INDEXES
        bad = [i for i in (self.watchlist_indexes or []) if i not in INDEXES]
        if bad:
            raise ValueError(f"watchlist_indexes: unknown {', '.join(map(str, bad))} (choose from {', '.join(INDEXES)})")
        if self.direction not in ("long", "short", "both"):
            raise ValueError(f"direction must be 'long', 'short' or 'both', not {self.direction!r}")
        if self.bar_minutes not in (1, 2, 3, 5, 10, 15, 30):
            raise ValueError(f"bar_minutes must be 1, 2, 3, 5, 10, 15 or 30, not {self.bar_minutes!r}")
        if self.broker == "public" or self.price_data == "public":
            if self.bar_minutes not in (5, 10, 15, 30):
                raise ValueError(f"bar_minutes {self.bar_minutes} isn't available with Public's price data: Public gives "
                                 "1-minute bars only for the current day, so there is no 1-, 2- or 3-minute history "
                                 "for the scan and the trend lines. Use bar_minutes 5, 10, 15 or 30.")

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown settings in {path}: {sorted(unknown)}")
        cfg = cls(**data)
        if cfg.watchlist_file:
            cfg.watchlist = read_watchlist(Path(path).parent / cfg.watchlist_file)
        if cfg.broker not in ("public", "alpaca", "ninjatrader"):
            raise ValueError(f"broker must be 'public', 'alpaca' or 'ninjatrader', not {cfg.broker!r}")
        cfg.validate()
        if cfg.ninja_env not in ("demo", "live"):
            raise ValueError(f"ninja_env must be 'demo' or 'live', not {cfg.ninja_env!r}")
        if cfg.price_data not in ("broker", "public"):
            raise ValueError(f"price_data must be 'broker' or 'public', not {cfg.price_data!r}")
        if cfg.price_data == "public" and cfg.broker != "alpaca":
            raise ValueError("price_data: public is for broker: alpaca (Public and NinjaTrader use their own data)")
        if cfg.alpaca_feed not in ("iex", "sip"):
            raise ValueError(f"alpaca_feed must be 'iex' or 'sip', not {cfg.alpaca_feed!r}")
        return cfg


def read_watchlist(path: str | Path) -> list[str]:
    """Symbols from a watchlist file: a `watchlist: [...]` YAML file (as made by
    tools/russell2000_watchlist.py), a YAML list, or plain text with symbols separated by
    commas, spaces or new lines. Lines starting with # are ignored."""
    import re
    p = Path(path)
    if not p.exists():
        raise ValueError(f"watchlist_file not found: {p.resolve()}")
    # read as plain text, not YAML: YAML would turn tickers like ON or NO into true/false
    text = "\n".join(line.split("#", 1)[0] for line in p.read_text(encoding="utf-8-sig").splitlines())
    words = re.split(r"[\s,\[\]'\"]+", re.sub(r"\bwatchlist\s*:", " ", text))
    out = list(dict.fromkeys(w.upper() for w in words if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./]*", w)))
    if not out:
        raise ValueError(f"watchlist_file {p} has no symbols in it")
    return out
