"""Run the strategy against Public.com during one trading session.

DRY RUN IS THE DEFAULT. In dry run the bot reads real market data, makes the
same decisions, and logs the trades it *would* have made with simulated fills.
No orders are sent. Pass --live (and confirm) to send real orders.

Kill switch: create a file named STOP in the working folder. The bot cancels
its open orders, sells its position and exits within one loop.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import logging
import math
import os
import re
import sys
import threading
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import __version__
from .backtest import position_size
from .config import Config
from .data import fetch_public, normalize
from .scanner import avg_dollar_volume, one_per_company, scan
from .strategy import (add_indicators, better_stop, find_swings, mirror, resistance_line, sided_breakeven_stop,
                       sided_trailing_stop, signals_at, support_line, target_for)

ET = ZoneInfo("America/New_York")
log = logging.getLogger("swingbot")


@dataclass
class _NOrder:
    """A broker order in one common shape."""
    id: str
    parent: str | None
    side: str          # "BUY" / "SELL"
    type: str          # "MARKET", "LIMIT", "STOP", "STOP_LIMIT", ...
    open: bool
    qty: object
    created_at: object
    raw: object = None


class MarketClosed(Exception):
    pass


OPEN_STATES = {"NEW", "PARTIALLY_FILLED", "PENDING_REPLACE", "PENDING_CANCEL"}


def _val(x) -> str:
    return str(getattr(x, "value", x))


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _price(x: float, rounding) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=rounding)


def cap_by_buying_power(shares: int, buying_power: float, price: float, buffer_pct: float) -> int:
    """Largest share count <= ``shares`` whose cost fits inside buying power, keeping a
    small buffer because a market order can fill above the last price."""
    if price <= 0 or buying_power <= 0:
        return 0
    usable = buying_power * (1 - buffer_pct / 100)
    return max(0, min(shares, math.floor(usable / price)))


def now_et() -> datetime:
    return datetime.now(ET)


def x_of(ts, minutes: int = 5) -> float:
    """Position of a time on today's chart, in bars of ``minutes`` since 09:30 ET."""
    ts = pd.Timestamp(ts).tz_convert(ET) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).tz_localize(ET)
    return (ts - ts.normalize() - pd.Timedelta(hours=9, minutes=30)).total_seconds() / (60.0 * minutes)


def _bar_start(ts, minutes: int):
    """Start of the ``minutes`` bar (counted from 9:30) that contains ``ts``."""
    m = ts.hour * 60 + ts.minute - 570
    return ts.replace(second=0, microsecond=0) - timedelta(minutes=m % minutes)


def complete_bars(df: pd.DataFrame, now: datetime, minutes: int = 5) -> pd.DataFrame:
    """Drop the bar that is still forming (bar start + 5 min is in the future)."""
    if df.empty:
        return df
    return df[df.index + timedelta(minutes=minutes) <= pd.Timestamp(now)]


class Bot:
    def __init__(self, client, cfg: Config, live: bool, workdir: Path):
        self.client = client
        self.cfg = cfg
        self.live = live
        self.workdir = workdir
        self.symbol: str | None = None
        self.history: pd.DataFrame | None = None     # prior-days bars for the active symbol
        self.watch: list[str] = []                     # symbols watched for entries, best-ranked first
        self.histories: dict[str, pd.DataFrame] = {}   # prior-days bars for each watched symbol
        self.views: dict[str, dict] = {}               # chart data per watched symbol (dashboard)
        self.watch_info: dict[str, dict] = {}          # latest price / nearest line per watched symbol
        self.pending_rescan = False                    # "Rescan" pressed on the dashboard
        self.pending_close = False                     # "Close now" pressed on the dashboard (trade ahead only)
        self.pending_stop = None                       # a stop price you set on the dashboard, to apply
        self.paused = False                            # "Pause new trades" on the dashboard: manage, don't enter
        self.watched_today: list[str] = []             # every stock watched today (morning, rescans), for the check
        self.update = None                             # a newer swingbot release, if the morning check found one
        self._beat_at = 0.0
        self.midday_done = False                       # today's automatic midday rescan has run (or was dropped)
        self.midday_waiting = False                    # midday rescan is due but waits for the open trade to close
        self.cfg0 = dataclasses.replace(cfg)           # config.yaml as loaded (today's changes are compared to it)
        self.overrides: dict[str, float] = {}          # settings changed on the dashboard, for the rest of today
        self.pending_settings: dict[str, float] = {}
        self.bar_time: dict[str, pd.Timestamp] = {}    # start of each symbol's newest bar today
        self.not_flat: dict | None = None              # end of day: shares the bot could not sell
        self.leftover: dict | None = None              # a previous day's position that is still open
        self._stale_logged: set[str] = set()
        self.bm = int(cfg.bar_minutes)                 # bar size in minutes (1, 5, ...)
        self.forced_symbol: str | None = None          # set by `run --symbol X`: skip the scan, trade X
        self.last_bar_seen: pd.Timestamp | None = None
        self.trades_today = 0
        self.start_equity: float | None = None
        self.pos: dict | None = None                  # the bot's open position (paper or live)
        self.my_order_ids: set[str] = set()           # orders the bot created; it touches no others
        self.paper_pnl = 0.0
        self.halted = False
        self.trade_log = workdir / "trades.csv"
        self.settled_start: float | None = None       # settled cash when the session began
        self.spent_today = 0.0                         # cash committed to buys this session
        # --- for the live dashboard (display only; never used for trading decisions)
        self.dashboard = None
        self.state = "Starting"
        self.scan_results: list = []
        self.pick_reason = ""
        self.view: dict = {"bars": [], "lines": [], "swings": [], "days": []}
        self.closed: list[dict] = []
        self.last_price: float | None = None
        self.last_price_time: str | None = None
        self.final = False
        self.prepared = False                          # account checks + morning scan done
        self.point_value = 1.0                         # $ per 1.00 price move per unit (futures: contract multiplier)
        # --- bar size changes asked for from the dashboard (applied by the main loop, never mid-step)
        self._bm_lock = threading.Lock()
        self.pending_bm: int | None = None
        self._resync = False                           # first check after a change: rebuild only, no new orders
        self.unit_label = "sh"                         # shown on the dashboard ("ct" for futures contracts)

    # ------------------------------------------------------------------ setup
    def account_equity(self) -> float:
        pf = self.client.get_portfolio()
        eq = getattr(pf, "total_account_value", None)
        if eq is None and pf.equity:
            eq = sum(Decimal(str(e.value)) for e in pf.equity if getattr(e, "value", None) is not None)
        return float(eq or 0)

    def morning_scan(self) -> None:
        with self._bm_lock:                            # a change asked for before the scan just applies
            if self.pending_bm:
                self.bm, self.pending_bm = self.pending_bm, None
        today = now_et().date()
        if self.forced_symbol:                         # `run --symbol X`: you picked it - no scan
            sym = self.forced_symbol
            df = self.fetch_bars(sym, "WEEK")
            self.watch = [sym]
            self.watched_today = list(dict.fromkeys(self.watched_today + self.watch))
            self.histories = {sym: df[df.index.date < today] if not df.empty else df}
            self._activate(sym)
            self.pick_reason = f"chosen by you ({self.bm}-minute bars)"
            log.info("TODAY'S PICK: %s - chosen by you, %d-minute bars, scan skipped", sym, self.bm)
            return
        scfg = self.scan_cfg()
        symbols = self.scan_symbols(scfg)
        public_data = self.broker_name == "Public" or self.cfg.price_data == "public"
        log.info("Scanning %d symbols%s%s", len(symbols),
                 f" from {self.cfg.watchlist_file}" if self.cfg.watchlist_file and not self.cfg.watchlist_indexes
                 and scfg.watchlist is self.cfg.watchlist else "",
                 f" (about {len(symbols) / max(1.0, self.cfg.public_rate_limit) / 60:.0f} minutes at "
                 f"{self.cfg.public_rate_limit:g} a second)" if public_data and len(symbols) > 600 else "")
        t0 = _time.monotonic()
        raw = self.fetch_many(symbols, "WEEK")
        hist = {sym: df[df.index.date < today] if not df.empty else df for sym, df in raw.items()}
        log.info("Scan data: %d of %d symbols in %.0f s", len(hist), len(symbols), _time.monotonic() - t0)
        try:
            liq = self.liquidity(list(hist))
        except Exception as e:
            log.warning("liquidity check failed (%s); using bar volume", e)
            liq = None
        self.scan_results = scan(hist, scfg, dollar_volume=liq)
        self._take_scan(hist, "TODAY'S PICK")

    def scan_symbols(self, scfg: Config) -> list[str]:
        """Today's list to scan: the indexes in watchlist_indexes (downloaded daily) plus your own watchlist,
        each stock once, keeping only stocks the broker can trade. Worked out once a day."""
        today = now_et().date()
        if getattr(self, "_scan_list", None) and self._scan_list[0] == today:
            return self._scan_list[1]
        syms = list(dict.fromkeys(s.upper() for s in scfg.watchlist))
        if self.cfg.watchlist_indexes and scfg.watchlist is self.cfg.watchlist:     # stocks, not futures
            from .indexes import daily_watchlist
            syms, counts = daily_watchlist(list(self.cfg.watchlist_indexes), syms, self.workdir / "logs" / "indexes",
                                           self.cfg.min_price, fallback_dir=self.workdir)
            log.info("Watchlist today: %s = %d different stocks",
                     " + ".join(f"{k} {v}" for k, v in counts.items()), len(syms))
        if scfg.watchlist is self.cfg.watchlist:
            try:
                ok = self.tradable_symbols()
            except Exception as e:
                ok = None
                log.warning("Couldn't get the list of tradable stocks (%s) - scanning all %d; an order in a stock "
                            "the broker doesn't trade would be refused and not counted", e, len(syms))
            if ok is not None:
                key = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())
                okk = {key(s) for s in ok}
                out = [s for s in syms if key(s) in okk]
                if len(out) < len(syms):
                    gone = [s for s in syms if key(s) not in okk]
                    log.info("%d of %d not tradable %s - left out%s", len(gone), len(syms), self.tradable_note,
                             f": {', '.join(gone[:15])}{' ...' if len(gone) > 15 else ''}")
                syms = out
        self._scan_list = (today, syms)
        return syms

    tradable_note = "at Public"

    def tradable_symbols(self) -> set[str] | None:
        """Every stock symbol the broker lets you buy and sell now (None = no check)."""
        return public_tradable(self.client)

    def _take_scan(self, hist: dict, label: str) -> bool:
        """Watch the best-ranked symbols from self.scan_results. False if nothing has a setup."""
        for r in self.scan_results[:25]:                             # best 25 (the log stays readable)
            log.info("scan %-6s score=%.2f %-16s %s", r.symbol, r.score, r.setup, r.detail)
        skipped: list = []
        good = one_per_company([r for r in self.scan_results if r.score > 0], max(1, int(self.cfg.watch_count)),
                               key=lambda r: r.symbol, extra=self.cfg.same_company, skipped=skipped)
        for sym, kept in skipped:
            log.info("Not watching %s: same company as %s, which ranked higher - the next stock is watched instead",
                     sym, kept)
        if not good:
            log.info("No symbol has a setup. Nothing to trade.")
            return False
        today = now_et().date()
        self.watch = [r.symbol for r in good]
        self.watched_today = list(dict.fromkeys(self.watched_today + self.watch))
        self.histories = {r.symbol: hist[r.symbol][hist[r.symbol].index.date < today] for r in good}
        self.watch_info = {}
        self.views = {}
        top = good[0]
        if not self.pos:
            self._activate(top.symbol)
        self.pick_reason = f"{top.setup} - {top.detail}"
        log.info("%s: %s (%s) - %s", label, top.symbol, top.setup, top.detail)
        if len(self.watch) > 1:
            log.info("Watching %d for entries, best first: %s", len(self.watch), ", ".join(self.watch))
        return True

    # ------------------------------------------------- several symbols at once
    def _activate(self, sym: str) -> None:
        """Make ``sym`` the symbol the bot charts, quotes and (on a signal) trades."""
        self.symbol = sym
        if sym in self.histories:
            self.history = self.histories[sym]
        self._on_activate(sym)

    def _on_activate(self, sym: str) -> None:
        pass                                           # NinjaTrader: load the contract's size and tick

    def _watched(self) -> list[str]:
        if self.pos:
            return [self.symbol]                       # one position at a time: only it matters now
        if self.watch:
            return list(self.watch)
        return [self.symbol] if self.symbol else []

    def _hist(self, sym: str):
        if sym in self.histories:
            return self.histories[sym]
        return self.history if sym == self.symbol else None

    # ------------------------------------------------- settings: shown on the page, some adjustable today
    ADJUSTABLE = {
        "trail_buffer_atr": dict(label="Trail distance from swing points", unit="ATR", min=0.05, max=3.0, step=0.05),
        "breakeven_at_r": dict(label="Move stop to entry once ahead", unit="R (0 = off)", min=0.0, max=5.0, step=0.25),
        "target_r": dict(label="Profit target", unit="R (0 = off)", min=0.0, max=10.0, step=0.25),
    }

    def setting_value(self, key: str, cfg: Config | None = None) -> float:
        cfg = cfg or self.cfg
        if key == "trail_buffer_atr":
            return float(cfg.trail_buffer_atr if cfg.trail_buffer_atr is not None else cfg.stop_buffer_atr)
        if key == "target_r":
            return float(cfg.reward_risk) if cfg.use_target else 0.0
        return float(getattr(cfg, key))

    def request_setting(self, key: str, value) -> str:
        """Dashboard (its own thread): check the value and queue it; the main loop applies it between
        checks. Lasts for the rest of today (kept across restarts); tomorrow starts from config.yaml."""
        spec = self.ADJUSTABLE.get(key)
        if spec is None:
            raise ValueError("that setting can't be changed from the page")
        if self.final:
            raise ValueError("the session is over")
        try:
            v = round(float(value), 4)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} must be a number")
        if not spec["min"] <= v <= spec["max"]:
            raise ValueError(f"{spec['label']} must be {spec['min']:g} to {spec['max']:g} {spec['unit']}")
        if key == "trail_buffer_atr" and not self.cfg.trail_stop:
            raise ValueError("the trailing stop is off (trail_stop: false in config.yaml), so this has no effect")
        if key in ("breakeven_at_r", "target_r") and 0 < v < 0.5:
            raise ValueError(f"{spec['label']}: 0 (off) or at least 0.5 R")
        with self._bm_lock:
            self.pending_settings[key] = v
        log.info("Dashboard: %s -> %g requested", spec["label"], v)
        return f"{spec['label']}: {v:g} {spec['unit'].split(' (')[0]} for the rest of today"

    def apply_pending_settings(self) -> bool:
        with self._bm_lock:
            pend, self.pending_settings = self.pending_settings, {}
        for key, v in pend.items():
            self._apply_setting(key, v)
        return bool(pend)

    def _apply_setting(self, key: str, v: float, announce: bool = True) -> None:
        spec, old = self.ADJUSTABLE[key], self.setting_value(key)
        if key == "trail_buffer_atr":
            self.cfg.trail_buffer_atr = v
        elif key == "breakeven_at_r":
            self.cfg.breakeven_at_r = v
        elif key == "target_r":
            self.cfg.use_target = v > 0
            if v > 0:
                self.cfg.reward_risk = v
        self.overrides[key] = v
        if announce:
            log.info("Setting for the rest of today: %s %g -> %g %s (config.yaml: %g)", spec["label"], old, v,
                     spec["unit"].split(" (")[0], self.setting_value(key, self.cfg0))
            if key == "target_r" and self.pos:
                self._retarget(v)
            if key == "breakeven_at_r" and self.pos and \
                    (self.pos["stop"] - self.pos["entry"]) * self.pos.get("side", 1) >= 0:
                log.info("(the open trade's stop is already at or past the entry; it never moves back)")
        self.save_state()

    def _retarget(self, r: float) -> None:
        """Put a profit target on the open trade at entry + r x its risk (r = 0 removes it)."""
        p = self.pos
        side = p.get("side", 1)
        risk = abs(p["entry"] - p["init_stop"])
        new = p["entry"] + side * r * risk if r > 0 and risk > 0 else None
        if new is not None:
            try:
                px = self._last_trade_price()
            except Exception:
                px = None
            px = px if px is not None else self.last_price
            if px is not None and side * (new - px) <= float(self._tick()):
                log.warning("Target %.2f is at or %s the price %.2f - not set for the open trade (it would "
                            "fill at once); it applies to the next trade", new, "below" if side > 0 else "above", px)
                return
            new = float(self._round_price(new, ROUND_UP if side > 0 else ROUND_DOWN))
        if self.live:
            try:
                self._set_target(new)
            except NotImplementedError:
                log.warning("%s: a profit target can't be added to an open trade here - it applies from the "
                            "next trade", self.broker_name)
                return
            except Exception as e:
                log.error("Could not change the open trade's target (it stays as it was): %s", e)
                return
        p["target"] = new
        log.info("Open trade %s: %s", self.symbol, f"profit target {new:.2f}" if new is not None else "no profit target")

    def _set_target(self, price: float | None) -> None:
        raise NotImplementedError                      # Public / NinjaTrader: not for an open trade

    def _settings_rows(self) -> list[dict]:
        c, c0 = self.cfg, self.cfg0
        def adj(key):
            spec = self.ADJUSTABLE[key]
            return dict(key=key, label=spec["label"], value=self.setting_value(key), unit=spec["unit"],
                        min=spec["min"], max=spec["max"], step=spec["step"], default=self.setting_value(key, c0),
                        changed=key in self.overrides and self.setting_value(key) != self.setting_value(key, c0))
        cash = ("settled cash only" if c.settled_cash_only else "cash") + (", margin allowed" if c.use_margin else ", no margin")
        if c.direction != "long":
            cash += "; shorts on margin, up to the account value"
        direction = {"long": "long only", "short": "short only", "both": "long and short (one trade at a time)"}[c.direction]
        profit = [adj("target_r")]
        if self.pos:
            t = self.pos.get("target")
            ride = "rides the trailing stop" if c.trail_stop else f"held until the stop or {c.flatten_time}"
            profit.append(dict(label="This trade", text=f"target {t:.2f}" if t else f"no target - {ride}"))
        else:
            profit.append(dict(label="With no target", text=f"winners run until the {'trailing ' if c.trail_stop else ''}"
                                                            f"stop or {c.flatten_time}"))
        trail_off = {} if c.trail_stop else dict(off=True, note="not used - trail_stop is off in config.yaml")
        return [
            dict(group="Risk and limits", items=[
                dict(label="Risk per trade", text=f"{c.risk_per_trade_pct:g}% of the account"),
                dict(label="Daily loss limit", text=f"{c.daily_loss_limit_pct:g}% (no new entries after)"),
                dict(label="Trades per day", text=f"{c.max_trades_per_day}"),
                dict(label="Direction", text=direction),
                dict(label="Largest position", text=f"{c.max_position_pct:g}% of the account"),
                dict(label="Cash", text=cash)]),
            dict(group="Stops", items=[
                dict(label="Initial stop", text=f"{c.stop_buffer_atr:g} ATR beyond the line; at least {c.min_stop_atr:g} ATR "
                                                f"and {c.min_stop_pct:g}% from entry; skip if over {c.max_stop_atr:g} ATR"),
                dict(adj("trail_buffer_atr"), **trail_off),
                dict(label="Trail step", text=f"moved only by {c.trail_min_step_atr:g} ATR or more", **trail_off),
                adj("breakeven_at_r")]),
            dict(group="Profit", items=profit),
            dict(group="Timing", items=[
                dict(label="New entries", text=f"{c.first_entry_time}-{c.last_entry_time} ET"),
                dict(label="Sell everything", text=f"{c.flatten_time} ET"),
                dict(label="Bars", text=f"{self.bm} minutes"),
                dict(label="Watching", text=f"top {c.watch_count} from the scan" if c.watch_count > 1 else "the top pick")]),
        ]

    # ------------------------------------------------- rescan on demand
    def request_rescan(self) -> str:
        """Dashboard "Rescan" button (called from the dashboard thread): only queues it."""
        if self.final:
            raise ValueError("the session is over")
        if self.pos:
            raise ValueError("not while in a trade - the bot rescans only when flat")
        self.pending_rescan = True
        log.info("Dashboard: rescan requested")
        return "Rescanning with today's prices..."

    # ------------------------------------------------- close a winning trade by hand
    def _open_pnl(self, price: float | None) -> float | None:
        p = self.pos
        if not p or price is None or price != price:
            return None
        return (price - p["entry"]) * p["shares"] * self.point_value * p.get("side", 1)

    def request_pause(self, paused: bool) -> str:
        """Dashboard "Pause new trades" / "Resume": no new entries while paused; an open trade is still
        managed (stop, breakeven, target, end of day). Lasts until resumed or the end of the day."""
        if self.final:
            raise ValueError("the session is over")
        self.paused = bool(paused)
        log.info("Dashboard: %s", "PAUSED - no new trades until resumed (an open trade is still managed)"
                 if self.paused else "RESUMED - looking for new trades again")
        self.save_state()
        return "Paused: no new trades" if self.paused else "Resumed: looking for trades"

    def request_close(self) -> str:
        """Dashboard "Close now" button (dashboard thread): only queues it, and only for a trade that's ahead.
        The main loop checks again at a fresh price before it sells."""
        if self.final:
            raise ValueError("the session is over")
        if not self.pos:
            raise ValueError("there's no open trade")
        pnl = self._open_pnl(self.last_price)
        if pnl is None or pnl <= 0:
            raise ValueError("only when the trade is ahead" + (f" - it's at {pnl:+,.2f} now" if pnl is not None else ""))
        self.pending_close = True
        log.info("Dashboard: close %s requested (open P&L %+.2f)", self.symbol, pnl)
        return f"Closing {self.symbol} (about {pnl:+,.2f})..."

    def close_by_hand(self) -> None:
        """Close the open trade at the market if it is still ahead at a fresh price; otherwise leave it."""
        self.pending_close = False
        if not self.pos or self.final:
            return
        self.refresh_quote()
        pnl = self._open_pnl(self.last_price)
        if pnl is None or pnl <= 0:
            log.warning("Close by hand skipped: %s is no longer ahead (%s) - the trade stays open with its stop",
                        self.symbol, "no price" if pnl is None else f"{pnl:+.2f}")
            return
        log.info("CLOSE BY HAND %s at about %.2f (open P&L %+.2f)", self.symbol, self.last_price, pnl)
        if self.live:
            self.flatten("closed by you")
        else:
            self._close_pos(self.last_price, "closed by you")

    def check_midday_rescan(self, now: datetime | None = None) -> None:
        """Automatic rescan at midday_rescan_time. In a trade it waits until the trade closes, and is
        dropped if that is not before midday_rescan_by. Runs at most once a day."""
        c = self.cfg
        if self.midday_done or not str(c.midday_rescan_time or "").strip():
            return
        t = (now or now_et()).time()
        if t < _t(c.midday_rescan_time):
            return
        why = None
        if t >= _t(c.midday_rescan_by):
            why = f"the trade was still open at {c.midday_rescan_by}" if self.midday_waiting else f"it is past {c.midday_rescan_by}"
        elif self.forced_symbol:
            why = "trading only the symbol given with --symbol"
        elif self.halted or self.trades_today >= c.max_trades_per_day:
            why = "no more entries today"
        elif self.pos:
            if not self.midday_waiting:
                self.midday_waiting = True
                log.info("Midday rescan due - waiting until the current trade closes (if before %s)", c.midday_rescan_by)
            return
        self.midday_done, self.midday_waiting = True, False
        if why:
            log.info("Midday rescan skipped: %s", why)
            return
        log.info("Midday rescan starting")
        self.pending_rescan = True
        self.save_state()

    def rescan(self) -> bool:
        """Rank the watchlist again, this time including today's completed bars, and watch the new
        best symbols. Only when flat. Returns True if the watch list changed."""
        self.pending_rescan = False
        if self.pos:
            log.info("Rescan skipped: in a trade")
            return False
        now = now_et()
        scfg = self.scan_cfg()
        symbols = self.scan_symbols(scfg)
        self.publish(f"Rescanning {len(symbols)} symbols")
        t0 = _time.monotonic()
        raw = self.fetch_many(symbols, "WEEK")
        hist, combined = {}, {}
        for sym, df in raw.items():
            if df.empty:
                continue
            hist[sym] = df[df.index.date < now.date()]
            combined[sym] = complete_bars(df, now, self.bm)       # yesterday and before + today so far
        log.info("Rescan data: %d of %d symbols in %.0f s", len(combined), len(symbols),
                 _time.monotonic() - t0)
        try:
            liq = self.liquidity(list(hist))
        except Exception as e:
            log.warning("liquidity check failed (%s); using bar volume", e)
            liq = None
        if not liq:                                    # judge volume on full days, not today's partial one
            liq = {s: avg_dollar_volume(h) for s, h in hist.items() if len(h)}
        old = list(self.watch)
        results = scan(combined, scfg, dollar_volume=liq)
        if not any(r.score > 0 for r in results):
            log.info("Rescan found no setups - keeping the current watch list")
            return False
        self.scan_results = results
        self.forced_symbol = None
        self._take_scan(hist, f"RESCAN {now:%H:%M}")
        self.pick_reason = f"rescan {now:%H:%M}: " + self.pick_reason
        self.last_bar_seen = None
        self._resync = True                            # next check redraws; entries resume from the next bar
        return self.watch != old

    def _line_for_report(self, d: pd.DataFrame, sig) -> dict | None:
        """The trend line behind a signal, as two (time, price) points on today's chart (for the report)."""
        try:
            L, t = sig.line, sig.bar
            today = np.flatnonzero(d.index.date == d.index[t].date())
            a = max(L.i1, int(today[0])) if len(today) else L.i1
            return dict(kind=L.kind, pts=[[d.index[a].isoformat(), float(L.value_at(a))],
                                          [d.index[t].isoformat(), float(L.value_at(t))]])
        except Exception:
            return None

    def _is_short(self) -> bool:
        return bool(self.pos) and self.pos.get("side", 1) < 0

    def _x(self, ts) -> float:
        return x_of(ts, self.bm)

    def fetch_many(self, symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
        """Bars for many symbols, ``scan_workers`` downloads at a time (at most 8: Public paces requests
        anyway). A symbol that fails is left out; the rest carry on. Results keep the watchlist order."""
        symbols = list(dict.fromkeys(symbols))
        got: dict[str, pd.DataFrame] = {}
        failed: list[tuple[str, str]] = []
        lock = threading.Lock()
        if not symbols:
            return got

        def one(sym):
            try:
                got[sym] = self.fetch_bars(sym, period)
            except Exception as e:
                with lock:
                    failed.append((sym, str(e)))
                    if len(failed) <= 5:
                        log.warning("bars for %s failed: %s", sym, e)

        one(symbols[0])                       # first one alone: signs in / refreshes the token once
        rest = symbols[1:]
        workers = max(1, min(int(self.cfg.scan_workers), 8, len(rest) or 1))
        with ThreadPoolExecutor(workers, thread_name_prefix="scan") as ex:
            futs = [ex.submit(one, s) for s in rest]
            for i, _ in enumerate(as_completed(futs), 2):
                if i % 100 == 0:
                    log.info("scan: %d of %d symbols downloaded", i, len(symbols))
                    self.publish(f"Scanning: {i} of {len(symbols)}")
        if len(failed) > 5:
            kinds = {}
            for _, msg in failed:
                kinds[msg[:90]] = kinds.get(msg[:90], 0) + 1
            top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
            log.warning("...and %d more symbols failed. Most common: %s", len(failed) - 5,
                        "; ".join(f"{n}x {m}" for m, n in top))
        if symbols and not got:
            log.error("No price data for any of the %d symbols - the bot can't scan or trade. "
                      "See the errors above (keys in .env, data source, bar size).", len(symbols))
        return {s: got[s] for s in symbols if s in got}

    # --------------------------------------------------- bar size on demand
    def bar_sizes(self) -> list[int]:
        """Bar sizes this broker can supply."""
        if self.broker_name == "Public" or self.cfg.price_data == "public":
            return [5, 10, 15, 30]                     # Public has no 1-3 minute history
        return [1, 2, 3, 5, 10, 15, 30]

    def request_bar_size(self, minutes: int) -> str:
        """Ask for a new bar size (called from the dashboard thread). Returns a message for the page.

        Only records the request; the main loop applies it between checks, so a change can
        never land in the middle of placing or moving an order.
        """
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ValueError("bar size must be a whole number of minutes")
        if minutes not in self.bar_sizes():
            raise ValueError(f"{self.broker_name} offers {', '.join(map(str, self.bar_sizes()))}-minute bars")
        if self.final:
            raise ValueError("the session is over")
        with self._bm_lock:
            if minutes == self.bm and self.pending_bm is None:
                return f"Already on {minutes}-minute bars"
            self.pending_bm = None if minutes == self.bm else minutes
        log.info("Dashboard: switch to %d-minute bars requested", minutes)
        return f"Switching to {minutes}-minute bars"

    def _rescale(self, old: int, new: int) -> None:
        """Chart positions are counted in bars: convert the ones already drawn to a new bar size."""
        k = old / new
        if self.pos:
            p = self.pos
            p["entry_x"] *= k
            p["stop_steps"] = [[x * k, y] for x, y in p["stop_steps"]]
            if p.get("signal_ts") is not None:
                p["signal_ts"] = _bar_start(p["signal_ts"], new)
        for c in self.closed:
            c["entry_x"] *= k
            c["exit_x"] *= k
            if c.get("stop_steps"):
                c["stop_steps"] = [[x * k, y] for x, y in c["stop_steps"]]

    # ------------------------------------------------- saved day (restarts)
    # The bot writes its day to logs/<date>-state.json after every check, so a restart during the
    # day carries on: same trade count, loss-limit baseline and settled cash, and any open
    # position is taken over again (its stop keeps trailing, it is sold at 15:50).
    def _state_path(self) -> Path:
        return self.workdir / "logs" / f"{now_et().date()}-state.json"

    def _extra_state(self) -> dict:
        return {}                                      # broker-specific bits (NinjaTrader: order details)

    def _restore_extra(self, extra: dict) -> None:
        pass

    def save_state(self) -> None:
        if not self.prepared:
            return                                     # never overwrite a saved day before reading it
        try:
            p = dict(self.pos) if self.pos else None
            if p:
                for k in ("signal_ts", "entry_time"):
                    if p.get(k) is not None:
                        p[k] = pd.Timestamp(p[k]).isoformat()
            st = dict(version=1, date=str(now_et().date()), broker=self.broker_name, live=self.live,
                      bar_minutes=self.bm, symbol=self.symbol, start_equity=self.start_equity,
                      settled_start=self.settled_start, spent_today=self.spent_today,
                      trades_today=self.trades_today, halted=self.halted, paper_pnl=self.paper_pnl,
                      closed=self.closed, pos=p, my_order_ids=sorted(map(str, self.my_order_ids)),
                      overrides=self.overrides, midday_done=self.midday_done, paused=self.paused,
                      watched_today=self.watched_today,
                      last_bar_seen=self.last_bar_seen.isoformat() if self.last_bar_seen is not None else None,
                      extra=self._extra_state())
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
            os.replace(tmp, path)                      # all or nothing, even if the PC dies mid-write
        except Exception as e:
            log.warning("could not save the day's state: %s", e)

    def load_state(self) -> dict | None:
        path = self._state_path()
        if not path.exists():
            return None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("ignoring unreadable %s (%s)", path.name, e)
            return None
        if st.get("date") != str(now_et().date()):
            return None
        if st.get("broker") != self.broker_name or bool(st.get("live")) != self.live:
            log.info("Saved session in %s is from a %s %s run - not resuming it in this %s %s run.",
                     path.name, st.get("broker"), "LIVE" if st.get("live") else "dry", self.broker_name,
                     "LIVE" if self.live else "dry")
            return None
        return st

    def _check_leftover(self) -> None:
        """Warn loudly if a position from an earlier day (the bot failed to sell it) is still open.
        The bot won't trade it: shares it didn't buy today count as yours."""
        today = str(now_et().date())
        files = sorted((self.workdir / "logs").glob("*-state.json"), reverse=True)
        for f in files:
            if f.name[:10] >= today:
                continue
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                return
            p = st.get("pos")
            if not p or st.get("broker") != self.broker_name or not st.get("live"):
                return
            sym, saved = st.get("symbol"), self.symbol
            try:
                self.symbol = sym
                held = self.held_quantity()
            except Exception:
                held = 0
            finally:
                self.symbol = saved
            held *= int(p.get("side", 1))                  # a short shows as negative shares
            if held > 0:
                self.leftover = dict(symbol=sym, shares=min(held, int(p["shares"])), date=st.get("date"),
                                     side=int(p.get("side", 1)))
                log.error("%s: %d shares from %s are STILL OPEN - the bot did not close them that day. It "
                          "won't trade them; close the position in your broker's app.", sym, self.leftover["shares"],
                          st.get("date"))
            return                                          # only the most recent earlier day matters

    def _restore_day(self, st: dict) -> None:
        self.start_equity = st.get("start_equity", self.start_equity)
        self.settled_start = st.get("settled_start")
        self.spent_today = float(st.get("spent_today") or 0)
        self.trades_today = int(st.get("trades_today") or 0)
        self.halted = bool(st.get("halted"))
        self.paper_pnl = float(st.get("paper_pnl") or 0)
        self.closed = list(st.get("closed") or [])
        self.midday_done = bool(st.get("midday_done"))
        self.paused = bool(st.get("paused"))
        self.watched_today = list(dict.fromkeys(list(st.get("watched_today") or []) + self.watched_today))
        if self.paused:
            log.info("Still PAUSED from earlier today - no new trades until you press Resume on the dashboard")
        for key, v in (st.get("overrides") or {}).items():       # today's dashboard changes still apply
            if key in self.ADJUSTABLE:
                self._apply_setting(key, float(v), announce=False)
                log.info("Today's setting kept from earlier: %s %g", self.ADJUSTABLE[key]["label"], float(v))
        log.info("Resuming today's session: %d trade(s) so far, day starts from equity %.2f%s",
                 self.trades_today, self.start_equity or 0,
                 ", loss limit already hit" if self.halted else "")

    def _restore_position(self, st: dict) -> None:
        p = dict(st["pos"])
        for k in ("signal_ts", "entry_time"):
            if p.get(k):
                ts = pd.Timestamp(p[k])
                p[k] = ts.tz_convert(ET) if k == "signal_ts" else ts.to_pydatetime()
        self.pos = p
        self.my_order_ids |= set(st.get("my_order_ids") or [])
        self._restore_extra(st.get("extra") or {})
        old = int(st.get("bar_minutes") or self.bm)
        if old != self.bm:
            self._rescale(old, self.bm)
        elif st.get("last_bar_seen"):
            self.last_bar_seen = pd.Timestamp(st["last_bar_seen"]).tz_convert(ET)
        self.pick_reason = "resuming the open position"
        log.info("Taking over the open position: %d %s %s from %.2f, stop %.2f",
                 p["shares"], self.unit_label, self.symbol, p["entry"], p["stop"])

    def apply_pending_bar_size(self) -> bool:
        """Switch to a requested bar size. Keeps the open position and its stop as they are."""
        with self._bm_lock:
            new, self.pending_bm = self.pending_bm, None
        if not new or new == self.bm:
            return False
        old = self.bm
        if not self.symbol:
            self.bm = new
            log.info("Bar size now %d minutes", new)
            return True
        self.bm = new
        syms = list(dict.fromkeys([self.symbol] + self.watch))
        try:
            got = {self.symbol: self.fetch_bars(self.symbol, "WEEK")}
            if len(syms) > 1:
                got.update(self.fetch_many(syms[1:], "WEEK"))
        except Exception as e:
            self.bm = old
            log.warning("Could not load %d-minute bars (%s) - staying on %d-minute bars", new, e, old)
            return False
        today = now_et().date()
        self.histories = {s: df[df.index.date < today] if not df.empty else df for s, df in got.items()}
        self.history = self.histories[self.symbol]
        self.views = {}
        self._rescale(old, new)
        self.last_bar_seen = None
        self._resync = True
        if " bars)" in self.pick_reason:
            self.pick_reason = f"chosen by you ({new}-minute bars)"
        log.info("Bar size changed from %d to %d minutes%s", old, new,
                 " - open position and its stop are unchanged" if self.pos else "")
        return True

    # ======================================================================
    # Broker hooks. Everything between here and the shared logic below is the
    # ONLY broker-specific code; these defaults talk to Public. AlpacaBot
    # (alpaca_live.py) overrides the same hooks.
    # ======================================================================
    broker_name = "Public"

    def scan_cfg(self) -> Config:
        """Settings the morning scan uses (futures swap in their contract list and skip stock filters)."""
        return self.cfg

    def size_position(self, equity: float, entry: float, stop: float, side: int = 1) -> int:
        """Units to trade from the risk rules (shares for stocks; contracts for futures)."""
        return position_size(equity, entry, stop, self.cfg, side)

    def _tick(self) -> Decimal:
        """Smallest price step (stocks: one cent)."""
        return Decimal("0.01")

    def _round_price(self, x: float, rounding) -> Decimal:
        """Round an order price to what the market accepts (cents for stocks, tick size for futures)."""
        return _price(x, rounding)

    def _cap_shares(self, shares: int, entry_ref: float, side: int = 1) -> int:
        """Live: trim the size to what the account may spend (cash-only and settled-cash rules).
        Shorts use no cash: they are limited by the broker's short buying power, and never more than
        the account value (no leverage)."""
        if side < 0:
            bp = self.short_buying_power()
            capped = cap_by_buying_power(shares, bp, entry_ref, self.cfg.buying_power_buffer_pct)
            if capped < shares:
                log.info("Size cut from %d to %d shares to fit short buying power $%.2f", shares, capped, bp)
            return capped
        bp = self.available_buying_power()
        capped = cap_by_buying_power(shares, bp, entry_ref, self.cfg.buying_power_buffer_pct)
        if capped < shares:
            log.info("Size cut from %d to %d shares to fit spendable cash $%.2f", shares, capped, bp)
        # count the money as spent at submission (with the slippage buffer), even if the
        # order later fails, so the bot can never double-spend settled cash
        self.spent_today += capped * entry_ref * (1 + self.cfg.buying_power_buffer_pct / 100)
        return capped

    def session_checks(self) -> None:
        """Broker-specific checks logged at the start of a live session."""
        return None

    def fetch_bars(self, symbol: str, period: str) -> pd.DataFrame:
        """Regular-hours bars of ``bar_minutes``. period: "WEEK" (scan history) or "DAY" (today)."""
        return fetch_public(self.client, symbol, period, self.bm)

    def liquidity(self, symbols: list[str]) -> dict | None:
        """Average daily dollar volume per symbol, if the bar feed can't be trusted for it."""
        return None

    def short_buying_power(self) -> float:
        """Margin available for a short, capped at the account value (no leverage). 0 = can't short."""
        pf = self.client.get_portfolio()
        bp = getattr(getattr(pf, "buying_power", None), "buying_power", None)
        return max(0.0, min(float(bp or 0), self.account_equity()))

    def short_check(self, symbol: str, qty: int) -> str | None:
        """Why ``symbol`` can't be shorted now (None = it can). Easy-to-borrow only; and not while the
        short-sale restriction (uptick rule) is on, because market orders are refused then."""
        from public_api_sdk import InstrumentType
        inst = self.client.get_instrument(symbol, InstrumentType.EQUITY)
        avail = _val(getattr(inst, "shorting_availability", None) or "NOT_SHORTABLE")
        if avail != "EASY_TO_BORROW":
            return "hard to borrow" if avail == "HARD_TO_BORROW" else "not shortable at Public"
        if qty > 0:
            pre = self.client.preflight_short_order(symbol, Decimal(qty))
            ss = getattr(pre, "short_selling", None)
            if ss is not None and _val(getattr(ss, "uptick_rule", "")) == "TRIGGERED":
                return "short-sale restriction (down 10% from yesterday) is on"
        return None

    def _instrument(self):
        from public_api_sdk import InstrumentType, OrderInstrument
        return OrderInstrument(symbol=self.symbol, type=InstrumentType.EQUITY)

    def _last_trade_price(self) -> float | None:
        q = self.client.get_quotes([self._instrument()])
        return float(q[0].last) if q and getattr(q[0], "last", None) is not None else None

    def _cash_now(self) -> tuple[float | None, float | None]:
        """(buying power the bot may use, settled cash right now - None if the broker doesn't say)."""
        pf = self.client.get_portfolio()
        bp = getattr(pf, "buying_power", None)
        field = "buying_power" if self.cfg.use_margin else "cash_only_buying_power"
        val = getattr(bp, field, None) if bp is not None else None
        # Withdrawable cash excludes unsettled sale proceeds: Public's closest figure to settled cash.
        atw = getattr(pf, "available_to_withdraw", None)
        settled = getattr(atw, "cash_only_available_to_withdraw", None) if atw is not None else None
        return (float(val) if val is not None else None, float(settled) if settled is not None else None)

    def held_quantity(self) -> int:
        """Total shares of today's symbol in the account (may include shares you owned before)."""
        pf = self.client.get_portfolio()
        for p in pf.positions or []:
            if p.instrument.symbol == self.symbol:
                return int(Decimal(str(p.quantity)))
        return 0

    def _orders(self) -> list:
        """Orders on today's symbol, normalized: id, parent (bracket entry id), side, type, open, qty, created_at."""
        pf = self.client.get_portfolio()
        out = []
        for o in pf.orders or []:
            if o.instrument.symbol != self.symbol:
                continue
            out.append(_NOrder(id=o.order_id, parent=o.bracket_id, side=_val(o.side).upper(),
                               type=_val(o.type).upper(), open=_val(o.status) in OPEN_STATES,
                               qty=o.quantity, created_at=o.created_at, raw=o))
        return out

    def _submit_entry(self, shares: int, stop_d: Decimal, tgt_d: Decimal | None, entry_ref: float, side: int = 1):
        """Market buy with the stop attached. Returns (order_id, fill_price or None).
        Short: market sell to open, then a buy stop to close (Public has no short brackets; no target)."""
        from public_api_sdk import (EquityMarketSession, OrderClass, OrderExpirationRequest, OrderRequest,
                                    OrderSide, OrderType, StopLoss, TakeProfit, TimeInForce)
        if side < 0:
            return self._submit_short(shares, stop_d, tgt_d)
        req = OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(shares),
            equity_market_session=EquityMarketSession.CORE,
            use_margin=self.cfg.use_margin,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLoss(stop_price=stop_d),
            take_profit=TakeProfit(limit_price=tgt_d) if tgt_d is not None else None,
        )
        order = self.client.place_order(req)
        self.my_order_ids.add(order.order_id)
        log.info("bracket order submitted: %s", order.order_id)
        try:
            filled = order.wait_for_fill(timeout=60)
            log.info("filled %s @ %s", filled.filled_quantity, filled.average_price)
            return order.order_id, float(filled.average_price)
        except Exception as e:
            log.warning("entry not fully confirmed within 60s (%s); cancelling any remainder", e)
            try:
                order.cancel()
            except Exception as ce:
                log.error("cancel failed: %s", ce)
            return order.order_id, None

    def _submit_short(self, shares: int, stop_d: Decimal, tgt_d: Decimal | None):
        from public_api_sdk import EquityMarketSession
        if tgt_d is not None:
            log.info("Public: no profit target on a short (it would be a second, unlinked order) - the stop "
                     "and the 15:50 exit manage it")
        order = self.client.place_short_order(self.symbol, Decimal(shares),
                                              equity_market_session=EquityMarketSession.CORE)
        self.my_order_ids.add(order.order_id)
        log.info("short sale submitted: %s", order.order_id)
        try:
            filled = order.wait_for_fill(timeout=60)
            price = float(filled.average_price)
            log.info("filled %s @ %s", filled.filled_quantity, filled.average_price)
        except Exception as e:
            log.warning("short sale not fully confirmed within 60s (%s); cancelling any remainder", e)
            try:
                order.cancel()
            except Exception as ce:
                log.error("cancel failed: %s", ce)
            price = None
        qty = max(0, -self.held_quantity())
        if qty:                                   # protect it at once (no bracket to do it for us)
            self.my_order_ids.add(self._place_stop(qty, stop_d))
            log.info("buy stop placed at %s for %d shares", stop_d, qty)
        return order.order_id, price

    def _exit_side(self) -> str:
        """The side that closes the open position: SELL for a long, BUY (to cover) for a short."""
        return "BUY" if self.pos and self.pos.get("side", 1) < 0 else "SELL"

    def _place_stop(self, qty: int, stop_d: Decimal) -> str:
        from public_api_sdk import (EquityMarketSession, OpenCloseIndicator, OrderExpirationRequest,
                                    OrderRequest, OrderSide, OrderType, TimeInForce)
        order = self.client.place_order(OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.BUY if self._exit_side() == "BUY" else OrderSide.SELL,
            order_type=OrderType.STOP,
            stop_price=stop_d,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(qty),
            open_close_indicator=OpenCloseIndicator.CLOSE,
            equity_market_session=EquityMarketSession.CORE,
        ))
        return order.order_id

    def _replace_stop(self, so, new_d: Decimal) -> str:
        """Reprice the stop in one step (cancel-and-replace). Returns the new order id; raises on refusal."""
        from public_api_sdk import CancelAndReplaceRequest, OrderExpirationRequest, OrderType, TimeInForce
        exp = getattr(so.raw, "expiration", None)
        tif = getattr(exp, "time_in_force", None) or TimeInForce.DAY
        repl = self.client.cancel_and_replace_order(CancelAndReplaceRequest(
            order_id=so.id,
            request_id=str(uuid.uuid4()),
            order_type=OrderType.STOP,
            expiration=OrderExpirationRequest(time_in_force=tif,
                                              expiration_time=getattr(exp, "expiration_time", None)),
            quantity=so.qty,
            stop_price=new_d,
        ))
        return repl.order_id

    def _cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id=order_id)

    def _market_sell(self, qty: int):
        """Close the bot's position at market (sell a long, buy back a short). Returns (order_id, fill or None)."""
        from public_api_sdk import (EquityMarketSession, OpenCloseIndicator, OrderExpirationRequest,
                                    OrderRequest, OrderSide, OrderType, TimeInForce)
        order = self.client.place_order(OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.BUY if self._exit_side() == "BUY" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(qty),
            open_close_indicator=OpenCloseIndicator.CLOSE,
            equity_market_session=EquityMarketSession.CORE,
        ))
        try:
            return order.order_id, float(order.wait_for_fill(timeout=60).average_price)
        except Exception as e:
            log.error("%s not confirmed: %s - CHECK THE ACCOUNT", "cover" if self._exit_side() == "BUY" else "sell", e)
            return order.order_id, None

    def _filled_sell_price(self, order_ids) -> float | None:
        """Average price of a filled exit (SELL for a long, BUY for a short) among the bot's orders."""
        price, want = None, self._exit_side()
        for oid in order_ids:
            try:
                o = self.client.get_order(order_id=oid)
            except Exception:
                continue
            if (_val(getattr(o, "side", "")).upper() == want and _val(getattr(o, "status", "")).upper() == "FILLED"
                    and getattr(o, "average_price", None) is not None):
                price = float(o.average_price)
        return price

    # ======================================================================
    # Shared logic (all brokers)
    # ======================================================================
    def snapshot_settled_cash(self) -> None:
        """Record settled cash at the start of the session (before the bot trades)."""
        _, self.settled_start = self._cash_now()
        self.spent_today = 0.0
        if self.cfg.settled_cash_only:
            if self.settled_start is None:
                log.warning("%s did not report settled cash; the settled-cash check will block all buys today.",
                            self.broker_name)
            else:
                log.info("Settled cash at start of session: $%.2f", self.settled_start)

    def available_buying_power(self) -> float:
        """Money the bot may spend on the next buy. 0 if anything needed is unknown (fail safe).

        Without margin, this is the smallest of:
          * cash-only buying power right now
          * settled cash right now (excludes unsettled proceeds)            [settled_cash_only]
          * settled cash at the start of the day minus what the bot has
            already spent today; today's sale proceeds are never added back [settled_cash_only]
        """
        bp, settled_now = self._cash_now()
        if bp is None:
            return 0.0
        limits = [bp]
        if self.cfg.settled_cash_only:
            if settled_now is None or self.settled_start is None:
                return 0.0
            limits += [settled_now, self.settled_start - self.spent_today]
        return max(0.0, min(limits))

    def bot_shares(self) -> int:
        """Shares the BOT bought (or sold short) and still has open. Never counts shares you already owned.
        held_quantity is negative for a short position."""
        if not self.pos:
            return 0
        moved = self.held_quantity() - self.pos.get("base_qty", 0)
        return max(0, min(self.pos["shares"], moved * self.pos.get("side", 1)))

    def _my_open_orders(self) -> list:
        """Open orders the bot itself created (its entries and their stop/target legs) - never yours."""
        return [o for o in self._orders()
                if o.open and (o.id in self.my_order_ids or (o.parent and o.parent in self.my_order_ids))]

    def _my_stop_order(self):
        exit_side = self._exit_side()
        for o in self._my_open_orders():
            if o.side == exit_side and o.type in ("STOP", "STOP_LIMIT"):
                return o
        # Fallback in case the broker doesn't tag bracket legs with the entry id: a sell stop on this
        # symbol for exactly the bot's share count, created after the bot's entry.
        if self.pos and self.pos.get("entry_time"):
            for o in self._orders():
                if (o.open and o.side == exit_side and o.type in ("STOP", "STOP_LIMIT")
                        and o.qty is not None and int(Decimal(str(o.qty))) == self.pos["shares"]
                        and o.created_at is not None and o.created_at >= self.pos["entry_time"]):
                    self.my_order_ids.add(o.id)
                    return o
        return None

    def enter(self, entry_ref: float, stop: float, kind: str, signal_ts=None, side: int = 1,
              line: dict | None = None) -> None:
        cfg = self.cfg
        if side < 0:
            why = self._why_no_short(0)
            if why:
                log.info("Signal %s on %s skipped: %s", kind, self.symbol, why)
                return
        equity = self.account_equity() if self.live else cfg.starting_equity + self.paper_pnl
        shares = self.size_position(equity, entry_ref, stop, side)
        if self.live and shares >= 1:
            shares = self._cap_shares(shares, entry_ref, side)
        if shares < 1:
            log.info("Signal %s skipped: position size rounds to 0 shares", kind)
            return
        # stops are rounded away from the price, targets toward it... from the far side
        stop_d = self._round_price(stop, ROUND_DOWN if side > 0 else ROUND_UP)
        tgt_d = (self._round_price(target_for(entry_ref, stop, cfg), ROUND_UP if side > 0 else ROUND_DOWN)
                 if cfg.use_target else None)
        if self.live:
            if side < 0:
                why = self._why_no_short(shares)
                if why:
                    log.info("Signal %s on %s skipped: %s", kind, self.symbol, why)
                    return
            # The signal used the bar's close; by the time the order goes out the price may already
            # have reached the stop. A broker refuses a stop on the wrong side of the price, so check first.
            try:
                now_px = self._last_trade_price()
            except Exception as e:
                now_px = None
                log.debug("price check failed: %s", e)
            if now_px is not None and side * (now_px - float(stop_d)) <= float(self._tick()):
                log.info("Signal %s skipped: price %.2f is already at the stop %s (it moved after the bar closed)",
                         kind, now_px, stop_d)
                return
        log.info("ENTER %s %s x%d ~%.2f stop %s target %s", kind, self.symbol, shares, entry_ref, stop_d,
                 tgt_d if tgt_d is not None else "none (trailing stop)", extra={"notify": "trade"})
        ex = self._x(signal_ts) + 1 if signal_ts is not None else self._x(now_et())
        pos = dict(kind=kind, side=side, entry=entry_ref, init_stop=float(stop_d), stop=float(stop_d),
                   target=float(tgt_d) if tgt_d is not None else None, shares=shares, signal_ts=signal_ts,
                   entry_x=ex, entry_clock=now_et().strftime("%H:%M"), stop_steps=[[ex, float(stop_d)]],
                   line=line)

        if not self.live:
            self.trades_today += 1
            self.pos = pos
            return

        base_qty = self.held_quantity()          # shares you already owned - never touched
        if side < 0 and base_qty != 0:
            log.info("Signal %s on %s skipped: the account already holds %d shares of it (a short needs a flat "
                     "position)", kind, self.symbol, base_qty)
            return
        entry_time = datetime.now(ZoneInfo("UTC"))
        pos["ids_before"] = sorted(map(str, self.my_order_ids))   # orders from earlier trades: not this one's
        self.pos = pos                            # (the broker hooks read the side from it)
        try:
            _, fill_price = self._submit_entry(shares, stop_d, tgt_d, entry_ref, side)
        except Exception as e:
            # A refused order changes nothing. But a timeout can hide an order that did go through,
            # so look at the account before giving up: shares that arrived are adopted and protected.
            log.warning("Entry order failed: %s", e, extra={"notify": "alert"})
            fill_price = None
        self.pos = None
        got = max(0, (self.held_quantity() - base_qty) * side)
        if got <= 0:
            log.warning("No shares were %s - not counted as a trade; still watching for setups.",
                        "bought" if side > 0 else "sold short")
            return
        self.trades_today += 1
        pos.update(entry=fill_price if fill_price is not None else entry_ref, shares=min(got, shares),
                   base_qty=base_qty, entry_time=entry_time)
        self.pos = pos
        self._record("entry", 0.0)
        self.save_state()                         # a crash from here on still knows about these shares
        self.ensure_stop(wait_seconds=15)

    def _why_no_short(self, qty: int) -> str | None:
        """Reason a short can't be opened in the active symbol now (None = go ahead). In a dry run the
        broker is still asked when it can be, so the simulation skips what the real account would."""
        try:
            return self.short_check(self.symbol, qty)
        except Exception as e:
            if self.live:
                return f"couldn't check whether it can be shorted ({e})"
            log.debug("short check failed in dry run: %s", e)
            return None

    def _stop_price_of(self, order) -> float | None:
        """The trigger price of a stop order as the broker has it now (Public and Alpaca)."""
        v = getattr(order.raw, "stop_price", None) if order is not None else None
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def sync_stop_from_broker(self) -> None:
        """Live: if the stop order at the broker is higher than the bot's stop (you raised it by hand in
        the broker's app), follow it, so the bot never lowers a stop you set and trails from there."""
        if not (self.live and self.pos):
            return
        try:
            so = self._my_stop_order()
            px = self._stop_price_of(so)
        except Exception as e:
            log.debug("stop check failed: %s", e)
            return
        side = self.pos.get("side", 1)
        if px is None or not better_stop(px, self.pos["stop"], side):
            return
        log.info("Stop for %s is %.2f at the broker (moved by hand?) - the bot now uses %.2f instead of %.2f",
                 self.symbol, px, px, self.pos["stop"])
        self.pos["stop"] = px
        step_x = (self._x(self.last_bar_seen) + 1) if self.last_bar_seen is not None else self._x(now_et())
        self.pos["stop_steps"].append([step_x, px])
        self.save_state()

    def ensure_stop(self, wait_seconds: int = 0) -> None:
        """Live: make sure the bot's shares are protected by a stop at the broker. The broker creates
        the bracket's stop leg itself when the entry fills; if it hasn't appeared after
        ``wait_seconds`` (e.g. a partial fill), place a standalone stop for the bot's shares."""
        if not (self.live and self.pos):
            return
        for _ in range(max(1, wait_seconds // 3)):
            if self._my_stop_order() is not None:
                return
            if wait_seconds:
                _time.sleep(3)
        qty = self.bot_shares()
        if qty <= 0:
            return
        oid = self._place_stop(qty, self._round_price(self.pos["stop"], ROUND_DOWN if self.pos.get("side", 1) > 0
                                                      else ROUND_UP))
        self.my_order_ids.add(oid)
        log.warning("No stop found for %d %s - placed standalone stop at %.2f (order %s)",
                    qty, self.symbol, self.pos["stop"], oid, extra={"notify": "alert"})

    def _stop_price(self, x: float) -> Decimal:
        """A stop price rounded to what the market accepts, on the safe side (down for a long, up for a short)."""
        return self._round_price(x, ROUND_DOWN if self.pos.get("side", 1) > 0 else ROUND_UP)

    def raise_stop(self, new_stop: float, why: str = "TRAIL", force: bool = False) -> bool:
        """Tighten the stop (up for a long, down for a short); ``force`` (a stop you set by hand) may also
        loosen it. Live: reprice the existing stop order in one step, so a stop is always in place. If the
        broker refuses, the old stop stays and we retry next bar. True if the stop moved."""
        side = self.pos.get("side", 1)
        new_d = self._stop_price(new_stop)
        if float(new_d) == self.pos["stop"] or (not force and not better_stop(float(new_d), self.pos["stop"], side)):
            return False
        log.info("%s stop %s %.2f -> %s", why, self.symbol, self.pos["stop"], new_d)
        step_x = (self._x(self.last_bar_seen) + 1) if self.last_bar_seen is not None else self._x(now_et())
        if not self.live:
            self.pos["stop"] = float(new_d)
            self.pos["stop_steps"].append([step_x, float(new_d)])
            return True
        so = self._my_stop_order()
        if so is None:
            self.pos["stop"] = float(new_d)
            self.ensure_stop()
            return True
        try:
            self.my_order_ids.add(self._replace_stop(so, new_d))
            self.pos["stop"] = float(new_d)
            self.pos["stop_steps"].append([step_x, float(new_d)])
            return True
        except Exception as e:
            log.error("Could not move the stop (keeping %.2f%s): %s", self.pos["stop"],
                      "" if force else ", will retry next bar", e)
            return False

    # ------------------------------------------------- move the stop by hand
    def stop_range(self, price: float | None = None) -> tuple[float, float] | None:
        """Where a stop you set may go: from the initial stop (never more risk than planned) to one tick
        (a cent for stocks) from the price - your call, so the bot's 0.25% buffer (min_stop_pct, for its own
        moves) doesn't apply. (low, high) in price; None if there's no room."""
        p = self.pos
        price = self.last_price if price is None else price
        if not p or price is None or price != price:
            return None
        gap = float(getattr(self, "tick", None) or 0.01)
        if p.get("side", 1) > 0:
            lo, hi = float(self._stop_price(p["init_stop"])), float(self._stop_price(price - gap))
        else:
            lo, hi = float(self._stop_price(price + gap)), float(self._stop_price(p["init_stop"]))
        return (lo, hi) if lo <= hi else None

    def _check_stop(self, x: float, price: float | None = None) -> float:
        rng = self.stop_range(price)
        if rng is None:
            raise ValueError("no room to move it: the price is at the initial stop")
        lo, hi = rng
        x = float(self._stop_price(x))
        if not lo <= x <= hi:
            far, near = (lo, hi) if self.pos.get("side", 1) > 0 else (hi, lo)
            raise ValueError(f"the stop has to be between {far:.2f} (the initial stop) and {near:.2f} "
                             f"(just {'under' if self.pos.get('side', 1) > 0 else 'over'} the price)")
        if x == self.pos["stop"]:
            raise ValueError(f"the stop is already at {x:.2f}")
        return x

    def request_stop(self, price, auto=None) -> str:
        """Dashboard "Move" on the Stop tile (dashboard thread): checked, then queued for the main loop."""
        if self.final:
            raise ValueError("the session is over")
        if not self.pos:
            raise ValueError("there's no open trade")
        try:
            x = float(price)
        except (TypeError, ValueError):
            raise ValueError("enter a price, like 175.50")
        if not x > 0:
            raise ValueError("enter a price, like 175.50")
        x = self._check_stop(x)
        keep = self.cfg.auto_stop_after_manual if auto is None else bool(auto)
        self.pending_stop = x
        self.pending_stop_auto = keep
        log.info("Dashboard: move the %s stop from %.2f to %.2f requested (%s)", self.symbol, self.pos["stop"], x,
                 "the bot keeps tightening it" if keep else "left to you")
        return f"Moving the stop to {x:.2f}..."

    def move_stop_by_hand(self) -> None:
        """Main loop: check again at a fresh price, then move the stop order. Automatic moves (breakeven,
        trailing) then stop for this trade, so they don't undo it."""
        x, self.pending_stop = self.pending_stop, None
        keep = getattr(self, "pending_stop_auto", self.cfg.auto_stop_after_manual)
        if x is None or not self.pos or self.final:
            return
        self.refresh_quote()
        try:
            x = self._check_stop(x)
        except ValueError as e:
            log.warning("Stop not moved: %s (the price is %.2f now)", e, self.last_price or float("nan"))
            return
        old = self.pos["stop"]
        if self.raise_stop(x, why="YOUR", force=True):
            self.pos["set_by_you"] = True
            self.pos["manual_stop"] = not keep                 # True = no automatic moves for this trade
            if keep:
                log.info("Stop for %s moved by you: %.2f -> %.2f. The bot keeps tightening it from there "
                         "(breakeven%s; it only moves %s).", self.symbol, old, self.pos["stop"],
                         ", trailing" if self.cfg.trail_stop else "", "up" if self.pos.get("side", 1) > 0 else "down")
            else:
                log.info("Stop for %s moved by you: %.2f -> %.2f. Automatic stop moves (breakeven%s) are off for "
                         "this trade.", self.symbol, old, self.pos["stop"], ", trailing" if self.cfg.trail_stop else "")
            self.save_state()

    def breakeven(self, close: float) -> None:
        """Move the stop to the entry price once the trade is breakeven_at_r ahead (once, not every bar)."""
        p, side = self.pos, self.pos.get("side", 1)
        be = sided_breakeven_stop(p["entry"], p["init_stop"], close, p["stop"], self.cfg, side)
        # compare at the price the order would really get: a short filled at 175.8725 gets a breakeven
        # stop of 175.88, and compared unrounded it looked "not at the entry yet" on every bar
        if better_stop(float(self._stop_price(be)), p["stop"], side):
            log.info("BREAKEVEN %s: price %.2f is %.1fR %s entry - moving the stop to the entry price",
                     self.symbol, close, side * (close - p["entry"]) / abs(p["entry"] - p["init_stop"]),
                     "above" if side > 0 else "below")
            self.raise_stop(be, why="BREAKEVEN")

    def flatten(self, reason: str) -> None:
        if not self.symbol:
            return
        if not self.live:
            if self.pos:
                self._close_pos(self.last_close, reason)
            return
        try:
            self._flatten_live(reason)
        except Exception as e:                             # never let a refusal end the session silently
            log.exception("FLATTEN FAILED for %s (%s): %s", self.symbol, reason, e)

    def _cancel_mine(self) -> None:
        """Cancel the bot's open orders (stop / target legs, an unfilled sell) and wait until they're gone.
        Cancelling one leg of a bracket cancels the other, so 'pending cancel' errors are expected."""
        for o in self._my_open_orders():
            try:
                self._cancel(o.id)
                log.info("cancel requested for %s", o.id)
            except Exception as e:
                if "pending cancel" in str(e).lower() or "not cancelable" in str(e).lower():
                    log.info("cancel %s: already being cancelled", o.id)
                else:
                    log.error("cancel %s failed: %s", o.id, e)
        for _ in range(30):                                # paper accounts can take a while
            if not self._my_open_orders():
                return
            _time.sleep(1)
        log.warning("some of the bot's orders were still open after 30 s")

    def _flatten_live(self, reason: str) -> None:
        last = None
        for attempt in range(6):
            self._cancel_mine()                            # also cancels an unfilled sell from a try before
            qty = self.bot_shares()                        # only what the bot bought
            if qty <= 0:
                log.info("flatten (%s): bot holds no shares", reason)
                if self.pos:                               # sold (by the stop or an earlier try): record it
                    self._check_live_exit()
                self.pos = None
                return
            try:
                oid, price = self._market_sell(qty)
            except Exception as e:                         # e.g. shares still held by a pending cancel
                last = e
                log.warning("%s %d %s refused (%s) - retrying in 5 s", self._exit_verb(), qty, self.symbol, e, extra={"notify": "alert"})
                _time.sleep(5)
                continue
            self.my_order_ids.add(oid)
            log.info("FLATTEN (%s): %s %d %s, order %s", reason, self._exit_verb(), qty, self.symbol, oid)
            if price is not None:
                self._close_pos(price, reason)
            return
        raise RuntimeError(f"could not {self._exit_verb()} {self.symbol} after 6 tries: {last}")

    def _exit_verb(self) -> str:
        return "buy back" if self.pos and self.pos.get("side", 1) < 0 else "sell"

    def still_holding(self) -> int:
        try:
            return self.bot_shares() if (self.live and self.pos) else 0
        except Exception:
            return self.pos["shares"] if self.pos else 0

    def _check_live_exit(self) -> None:
        """Live: notice when the broker's stop (or target) has sold the bot's shares."""
        if not (self.live and self.pos) or self.bot_shares() > 0:
            return
        ids = self._position_order_ids()
        try:                                             # include bracket legs the broker created
            ids |= {o.id for o in self._orders() if o.parent and o.parent in ids}
        except Exception:
            pass
        price = self._filled_sell_price(sorted(ids))
        entry = self.pos["entry"]
        if price is not None and not (0.5 * entry <= price <= 2 * entry):
            log.warning("%s: sale price %.2f found for an entry at %.2f doesn't fit - using the last price %.2f "
                        "instead; check the fill in your broker's app.", self.symbol, price, entry, self.last_close)
            price = None
        if price is None:
            price = self.last_close
        side, tgt = self.pos.get("side", 1), self.pos["target"]
        reason = "target" if (tgt and side * (price - tgt) >= -1e-9) else "stop"
        self._close_pos(price, reason)

    def _position_order_ids(self) -> set[str]:
        """The open position's own orders: entry, stop/target legs, replaced stops, its sale.
        2026-10-26: all of the day's orders were used, so BUSE's exit was read from EVRG's earlier
        sale (84.32 instead of about 30.67) and the day showed +$41,606."""
        ids = set(map(str, self.my_order_ids))
        before = (self.pos or {}).get("ids_before")
        return ids - set(before) if before is not None else ids

    # ----------------------------------------------------------- exits & logging
    last_close: float = float("nan")

    def _paper_check(self, bar: pd.Series) -> None:
        p = self.pos
        if not p:
            return
        if p.get("side", 1) < 0:                       # short: stop above, target below
            if bar["high"] >= p["stop"]:
                self._close_pos(max(bar["open"], p["stop"]), "stop")
            elif p["target"] is not None and bar["low"] <= p["target"]:
                self._close_pos(min(bar["open"], p["target"]), "target")
            return
        if bar["low"] <= p["stop"]:
            self._close_pos(min(bar["open"], p["stop"]), "stop")
        elif p["target"] is not None and bar["high"] >= p["target"]:
            self._close_pos(max(bar["open"], p["target"]), "target")

    def _close_pos(self, price: float, reason: str) -> None:
        p = self.pos
        side = p.get("side", 1)
        if reason == "stop" and better_stop(p["stop"], p["init_stop"], side):
            reason = "trail"
        pnl = (price - p["entry"]) * p["shares"] * self.point_value * side
        if not self.live:
            self.paper_pnl += pnl
        log.info("%sEXIT %s @ %.2f (%s) P&L %.2f", "" if self.live else "[paper] ", self.symbol, price, reason, pnl, extra={"notify": "trade"})
        self._record(reason, pnl, exit_price=price)
        exit_x = self._x(self.last_bar_seen) if (self.last_bar_seen is not None and reason not in
                                                 ("end of day", "kill switch", "closed by you")) else min(390 / self.bm - 1, self._x(now_et()))
        self.closed.append(dict(symbol=self.symbol, kind=p["kind"], side=side, shares=p["shares"], entry=p["entry"], exit=price, pnl=pnl,
                                reason=reason, entry_time=p["entry_clock"], exit_time=now_et().strftime("%H:%M"),
                                entry_x=p["entry_x"], exit_x=max(exit_x, p["entry_x"]),
                                init_stop=p["init_stop"], final_stop=p["stop"], target=p.get("target"),
                                stop_by_you=bool(p.get("set_by_you")),
                                stop_steps=[list(x) for x in p.get("stop_steps") or []], line=p.get("line")))
        self.pos = None
        self.save_state()

    def _record(self, event, pnl, exit_price=None) -> None:
        p = self.pos
        new = not self.trade_log.exists()
        with self.trade_log.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "mode", "symbol", "kind", "event", "shares", "entry", "initial_stop",
                            "stop", "target", "exit", "pnl"])
            w.writerow([now_et().isoformat(timespec="seconds"), "live" if self.live else "paper", self.symbol,
                        p["kind"], event, p["shares"], round(p["entry"], 4), p["init_stop"], p["stop"],
                        "" if p["target"] is None else p["target"],
                        "" if exit_price is None else round(exit_price, 4), round(pnl, 2)])

    # ------------------------------------------------------------- main step
    def day_pnl(self) -> float:
        if not self.live:
            return self.paper_pnl
        return self.account_equity() - (self.start_equity or 0)

    def _today_bars(self, syms: list[str], now: datetime) -> dict[str, pd.DataFrame]:
        """Today's completed bars for each symbol (one request, or several at once)."""
        if len(syms) == 1:
            raw = {syms[0]: self.fetch_bars(syms[0], "DAY")}
        else:
            raw = self.fetch_many(syms, "DAY")
        out = {}
        for sym, df in raw.items():
            tb = complete_bars(df, now, self.bm)
            if not tb.empty:
                tb = tb[tb.index.date == now.date()]
            if not tb.empty:
                out[sym] = tb
        return out

    def step(self) -> None:
        cfg = self.cfg
        now = now_et()
        syms = self._watched()
        if not syms:
            return
        todays = self._today_bars(syms, now)
        for sym, tb in todays.items():
            self.bar_time[sym] = tb.index[-1]
        self._note_stale(syms, now)
        if not todays:
            if now.time() >= time(10, 15):
                raise MarketClosed("no bars today by 10:15 ET - market holiday?")
            return
        newest = max(tb.index[-1] for tb in todays.values())
        if self.last_bar_seen is not None and newest <= self.last_bar_seen:
            return                                               # no new bar yet
        mine = todays.get(self.symbol)
        if not self.live and self.pos and self.last_bar_seen is not None and mine is not None:
            # paper: bars missed in between (e.g. the bot was restarted) - a stop hit then still counts
            for ts, b in mine[(mine.index > self.last_bar_seen) & (mine.index < newest)].iterrows():
                if self.pos:
                    self.last_bar_seen = ts
                    self._paper_check(b)
        self.last_bar_seen = newest
        self.last_price_time = (newest + timedelta(minutes=self.bm)).strftime("%H:%M")

        frames = {}
        for sym in syms:
            tb = todays.get(sym)
            if tb is None:
                continue
            h = self._hist(sym)
            df = normalize(pd.concat([h, tb])) if h is not None and len(h) else tb
            d = add_indicators(df, cfg)
            lo, hi = find_swings(d, cfg.swing_n)
            frames[sym] = (d, lo, hi, mirror(d) if cfg.direction != "long" or self._is_short() else None)
            try:
                self.views[sym] = self._make_view(d, lo, hi)
                self.watch_info[sym] = self._line_note(d, self.views[sym])
            except Exception as e:                                # display must never block trading
                log.debug("view failed for %s: %s", sym, e)
        if self.symbol in frames:
            d = frames[self.symbol][0]
            self.last_close = float(d["close"].iat[-1])
            self.last_price = self.last_close
            if self.symbol in self.views:
                self.view = self.views[self.symbol]

        if self._resync:
            # first check after a bar-size change or rescan: the newest bar may have closed a while
            # ago, so only redraw; exits, trailing and entries resume from the next new bar
            self._resync = False
            if self.live:
                self._check_live_exit()
            if self.pos:
                self.ensure_stop()
                self.sync_stop_from_broker()
            return

        # 1) exits that happened during this bar
        if self.live:
            self._check_live_exit()
        elif self.pos and mine is not None:
            self._paper_check(mine.iloc[-1])

        # 2) manage an open position: keep a stop in place and trail it up
        if self.pos:
            self.ensure_stop()
            self.sync_stop_from_broker()
            side = self.pos.get("side", 1)
            manual = bool(self.pos.get("manual_stop"))         # you set the stop: no automatic moves
            if cfg.trail_stop and not manual and self.symbol in frames:
                d, lo, hi, md = frames[self.symbol]
                t = len(d) - 1
                ts = self.pos.get("signal_ts")
                from_bar = d.index.get_loc(ts) if ts is not None and ts in d.index else t
                new_stop = sided_trailing_stop(d, md, t, cfg, lo, hi, from_bar, self.pos["stop"], side)
                if better_stop(new_stop, self.pos["stop"], side):
                    self.raise_stop(new_stop)
            if self.pos and not manual and self.symbol in frames:
                self.breakeven(float(frames[self.symbol][0]["close"].iat[-1]))
            return

        # 3) look for a new entry on any watched symbol (best-ranked first)
        bt = newest.time()
        loss_limit = (self.start_equity or cfg.starting_equity) * cfg.daily_loss_limit_pct / 100
        if self.day_pnl() <= -loss_limit and not self.halted:
            log.warning("Daily loss limit reached (%.2f). No more entries today.", self.day_pnl())
            self.halted = True
        if (self.halted or self.paused or self.trades_today >= cfg.max_trades_per_day
                or not (_t(cfg.first_entry_time) <= bt <= _t(cfg.last_entry_time))):
            return
        before = self.symbol
        for sym in syms:
            if sym not in frames:
                continue
            d, lo, hi, md = frames[sym]
            if d.index[-1] != newest:
                continue                                         # no trade in this symbol this bar
            sig = signals_at(d, md, len(d) - 1, cfg, lo, hi)
            if not sig:
                continue
            log.info("SIGNAL %s on %s bar %s: line %.2f, stop %.2f",
                     sig.kind, sym, newest.strftime("%H:%M"), sig.line_value, sig.stop)
            self._activate(sym)
            self.last_close = float(d["close"].iat[-1])
            self.last_price = self.last_close
            if sym in self.views:
                self.view = self.views[sym]
            self.enter(sig.ref_price, sig.stop, sig.kind, signal_ts=newest, side=sig.side,
                       line=self._line_for_report(d, sig))
            if self.pos:
                return
        if self.symbol != before and not self.pos and before:
            self._activate(before)                               # no entry made: back to the chart you had
            if before in self.views:
                self.view = self.views[before]

    # ------------------------------------------------- data that stops arriving
    data_feed_note = "the data feed"                   # Alpaca on the free plan: "Alpaca's free IEX feed"

    def stale_minutes(self, sym: str, now: datetime | None = None) -> float | None:
        """Minutes since the symbol's newest bar ended, when that is long enough to mean the data feed
        has had no trades (bars only exist for intervals with trades). None = data is current."""
        now = now or now_et()
        ts = self.bar_time.get(sym)
        if ts is None or ts.date() != now.date() or self.final:
            return None
        if not (time(9, 35) <= now.time() < time(16, 0)):
            return None
        age = (pd.Timestamp(now) - (ts + timedelta(minutes=self.bm))).total_seconds() / 60
        return age if age >= max(2 * self.bm, 6) else None

    def _note_stale(self, syms: list[str], now: datetime) -> None:
        for sym in syms:
            age = self.stale_minutes(sym, now)
            if age is not None and sym not in self._stale_logged:
                self._stale_logged.add(sym)
                log.warning("No new %s bar for %.0f min - %s has had no trades in it%s", sym, age,
                            self.data_feed_note,
                            "; the broker's stop is still active" if self.pos and sym == self.symbol else "")
            elif age is None and sym in self._stale_logged:
                self._stale_logged.discard(sym)
                log.info("%s bars are arriving again", sym)

    def _line_note(self, d: pd.DataFrame, view: dict) -> dict:
        """Where price sits against the nearest line, for the dashboard's watch table."""
        c, atr = float(d["close"].iat[-1]), float(d["atr"].iat[-1])
        notes = []
        for L in view.get("lines", []):
            v = L["pts"][1][1]
            if atr and atr == atr:
                gap = (c - v) / atr                       # + above the line, - below it
                where = "above" if gap >= 0 else "below"
                notes.append(f"{abs(gap):.2f} ATR {where} {L['kind']}")
        return dict(last=c, note="; ".join(notes) or "no valid line")

    # ------------------------------------------------------------- dashboard
    VIEW_DAYS = 5                                   # sessions sent to the page (it shows 1, 2 or 5)

    def _build_view(self, d: pd.DataFrame, lo, hi, n_today: int = 0) -> None:
        self.view = self._make_view(d, lo, hi)

    def _make_view(self, d: pd.DataFrame, lo, hi) -> dict:
        """Chart data for the page. x is in bars from today's 9:30 open; earlier sessions are
        negative (yesterday's 9:30 bar is -390/bar_minutes), so today's positions never change."""
        cfg, t = self.cfg, len(d) - 1
        per_day = 390 / self.bm
        dates = sorted(set(d.index.date) | {now_et().date()})[-self.VIEW_DAYS:]   # today, even before its first bar
        back = {day: len(dates) - 1 - k for k, day in enumerate(dates)}     # today 0, yesterday 1, ...
        i0 = int(np.searchsorted(d.index.date, dates[0])) if dates else len(d)
        xs = [self._x(ts) - back[ts.date()] * per_day for ts in d.index[i0:]]

        def xi(i):
            return xs[i - i0] if i >= i0 else xs[0] - (i0 - i)

        bars = [dict(x=xs[k], t=ts.strftime("%H:%M"), o=r.open, h=r.high, l=r.low, c=r.close)
                for k, (ts, r) in enumerate(d.iloc[i0:].iterrows())]
        days = [dict(x=-b * per_day, label=f"{day:%a} {day.month}/{day.day}", back=b)
                for day, b in back.items()]
        lines = []
        for kind, line in (("support", support_line(d, t, cfg, lo) if cfg.enable_bounce else None),
                           ("resistance", resistance_line(d, t, cfg, hi) if cfg.enable_breakout else None)):
            if line is None:
                continue
            a = max(line.i1, i0)
            lines.append(dict(kind=kind, pts=[[xi(a), line.value_at(a)], [xi(t), line.value_at(t)]],
                              anchors=[[xi(line.i1), line.p1], [xi(line.i2), line.p2]]))
        swings = []
        for i in range(i0, max(i0, t - cfg.swing_n + 1)):
            if lo[i]:
                swings.append(dict(x=xi(i), y=float(d["low"].iat[i]), kind="low"))
            if hi[i]:
                swings.append(dict(x=xi(i), y=float(d["high"].iat[i]), kind="high"))
        return dict(bars=bars, lines=lines, swings=swings, days=days)

    def refresh_quote(self) -> None:
        """Latest trade price between bars (display only)."""
        if not self.symbol:
            return
        try:
            price = self._last_trade_price()
            if price is not None:
                self.last_price = price
                self.last_price_time = now_et().strftime("%H:%M:%S")
        except Exception as e:
            log.debug("quote failed: %s", e)

    def status(self) -> dict:
        cfg, p, last = self.cfg, self.pos, self.last_price
        realized = sum(c["pnl"] for c in self.closed)
        pos = None
        unreal = 0.0
        if p:
            side = p.get("side", 1)
            unreal = (last - p["entry"]) * p["shares"] * self.point_value * side if last is not None else 0.0
            risk = abs(p["entry"] - p["init_stop"]) * p["shares"] * self.point_value
            pos = dict(kind=p["kind"], side=side, shares=p["shares"], entry=p["entry"], stop=p["stop"], init_stop=p["init_stop"],
                       entry_x=p["entry_x"], entry_time=p["entry_clock"], stop_steps=p["stop_steps"],
                       unreal=unreal, risk_dollars=risk, r=(unreal / risk) if risk else None)
        spendable = None
        if self.live and cfg.settled_cash_only and self.settled_start is not None:
            spendable = max(0.0, self.settled_start - self.spent_today)
        now = now_et()
        return dict(
            version=__version__, mode="LIVE" if self.live else "DRY RUN", state=self.state, final=self.final, unit=self.unit_label,
            bar_minutes=self.bm, bar_sizes=self.bar_sizes(), pending_bars=self.pending_bm,
            symbol=self.symbol, pick_reason=self.pick_reason, date=now.strftime("%Y-%m-%d"),
            updated=now.strftime("%H:%M:%S"), epoch=now.timestamp(),
            last=last, last_time=self.last_price_time, pos=pos, trades=self.closed,
            day_pnl=realized + unreal, trades_used=self.trades_today, max_trades=cfg.max_trades_per_day,
            halted=self.halted, spendable=spendable,
            scan=[dict(symbol=r.symbol, score=r.score, setup=r.setup, detail=r.detail) for r in self.scan_results[:50]],
            not_flat=self.not_flat, leftover=self.leftover,
            settings=self._settings_rows(), settings_pending=sorted(self.pending_settings),
            stale=self._stale_status(), watch=self._watch_rows(), can_rescan=not self.pos and not self.final, rescan_pending=self.pending_rescan,
            can_close=bool(p) and not self.final and unreal > 0, close_pending=self.pending_close,
            stop_range=self.stop_range() if p and not self.final else None, stop_pending=self.pending_stop,
            stop_manual=bool(p and p.get("set_by_you")), stop_auto=not bool(p and p.get("manual_stop")),
            stop_auto_default=bool(self.cfg.auto_stop_after_manual), min_stop_pct=self.cfg.min_stop_pct,
            paused=self.paused, update=self.update,
            views={s: v for s, v in self.views.items() if s in self.watch or s == self.symbol},
            **self.view,
        )

    def _stale_text(self, sym: str) -> str:
        age = self.stale_minutes(sym)
        return f" - no trades on the feed for {age:.0f} min" if age is not None else ""

    def _stale_status(self) -> dict | None:
        if not self.symbol:
            return None
        age = self.stale_minutes(self.symbol)
        if age is None:
            return None
        ts = self.bar_time[self.symbol] + timedelta(minutes=self.bm)
        return dict(symbol=self.symbol, minutes=round(age), since=ts.strftime("%H:%M"), feed=self.data_feed_note,
                    in_trade=bool(self.pos))

    def _watch_rows(self) -> list[dict]:
        score = {r.symbol: r for r in self.scan_results}
        rows = []
        for sym in dict.fromkeys(self.watch + ([self.symbol] if self.symbol else [])):
            r, info = score.get(sym), self.watch_info.get(sym, {})
            rows.append(dict(symbol=sym, score=r.score if r else None, setup=r.setup if r else "",
                             last=info.get("last"),
                             note=info.get("note", "") + (" (paused while in a trade)" if self.pos and sym != self.symbol
                                                          else self._stale_text(sym)),
                             active=sym == self.symbol,
                             in_trade=bool(self.pos) and sym == self.symbol))
        return rows

    def beat(self, force: bool = False) -> None:
        """logs/heartbeat.json: the results website reads it to tell whether the bot is running."""
        now = _time.time()
        if not force and now - self._beat_at < 30:
            return
        self._beat_at = now
        try:
            from . import __version__
            hb = dict(time=now_et().isoformat(timespec="seconds"), date=str(now_et().date()), pid=os.getpid(),
                      version=__version__, live=self.live, broker=self.broker_name, state=self.state,
                      final=bool(self.final), symbol=self.symbol, in_trade=bool(self.pos), paused=self.paused,
                      bar_minutes=self.bm, update=self.update)
            path = self.workdir / "logs" / "heartbeat.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(hb), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            log.debug("heartbeat not written: %s", e)

    def publish(self, state: str | None = None) -> None:
        if state:
            self.state = state
        self.save_state()
        self.beat(force=bool(self.final))
        if self.dashboard is None:
            return
        try:
            self.dashboard.update(self.status())
        except Exception as e:                                    # display must never block trading
            log.debug("dashboard update failed: %s", e)

    def _auto_state(self) -> str:
        if self.pos:
            return (f"In a {'short ' if self._is_short() else ''}trade - stop {self.pos['stop']:.2f}"
                    + (" (rescan after it closes)" if self.midday_waiting else ""))
        if self.halted:
            return "Loss limit hit - watching only"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return "Trade limit reached - watching only"
        if self.paused:
            return "Paused - no new trades (press Resume)"
        if len(self.watch) > 1:
            return f"Watching {len(self.watch)} symbols for a setup"
        return f"Watching {self.symbol} for a setup"

    def prepare(self) -> None:
        """Account checks and the morning scan. Uses data up to yesterday's close only, so it can
        run before the open: start the bot early and the pick is ready when trading starts."""
        cfg = self.cfg
        st = self.load_state()                     # earlier run today? carry on from it
        self.start_equity = self.account_equity() if self.live else cfg.starting_equity
        if self.live:
            try:
                self.session_checks()
            except Exception as e:
                log.warning("account check failed: %s", e)
            if not st:
                self.snapshot_settled_cash()
        if self.live:
            self._check_leftover()
        if st:
            self._restore_day(st)
            if self.live and self.settled_start is not None:
                log.info("Settled cash for today (from the start of the day): $%.2f, $%.2f already spent",
                         self.settled_start, self.spent_today)
        log.info("swingbot v%s | Mode: %s | start equity %.2f", __version__,
                 "LIVE" if self.live else "DRY RUN (no orders)", self.start_equity)
        self.publish("Running morning scan")
        self.morning_scan()
        if st and st.get("pos") and st.get("symbol"):
            sym = st["symbol"]                     # an open position decides the active symbol
            if sym not in self.histories:
                df = self.fetch_bars(sym, "WEEK")
                self.histories[sym] = df[df.index.date < now_et().date()] if not df.empty else df
            self._activate(sym)
            self._restore_position(st)
        elif st and int(st.get("bar_minutes") or self.bm) != self.bm:
            self._rescale(int(st["bar_minutes"]), self.bm)      # earlier trades on the chart
        self.prepared = True
        self.save_state()
        if not self.symbol:
            self.final = True
            self.publish("No setup today - nothing to trade")
            return
        self._premarket_views()
        self.publish(f"Picked {self.symbol}" + (f" (+{len(self.watch) - 1} more watched)" if len(self.watch) > 1 else ""))

    def _premarket_views(self) -> None:
        """Charts of the earlier days for every watched symbol, while waiting for the open."""
        for sym in dict.fromkeys([self.symbol] + self.watch):
            h = self._hist(sym)
            if h is None or not len(h):
                continue
            try:
                d = add_indicators(h.copy(), self.cfg)
                lo, hi = find_swings(d, self.cfg.swing_n)
                self.views[sym] = self._make_view(d, lo, hi)
                self.watch_info[sym] = self._line_note(d, self.views[sym])
            except Exception as e:
                log.debug("view failed for %s: %s", sym, e)
        if self.symbol in self.views:
            self.view = self.views[self.symbol]

    def run_session(self) -> None:
        cfg = self.cfg
        if not self.prepared:
            self.prepare()
        if not self.symbol:
            return
        flatten_at = _t(cfg.flatten_time)
        stop_file = self.workdir / "STOP"
        while True:
            now = now_et()
            if stop_file.exists():
                log.warning("STOP file found - flattening and exiting")
                self.flatten("kill switch")
                self.final = True
                self.publish("Stopped by kill switch")
                return
            if now.time() >= flatten_at:
                self.flatten("end of day")
                while self.still_holding() > 0 and now_et().time() < time(15, 59, 30):
                    log.error("%s: %d shares still open - trying to %s again", self.symbol, self.still_holding(),
                              self._exit_verb())
                    self.publish(f"NOT FLAT - still trying to {self._exit_verb()} {self.symbol}")
                    _time.sleep(20)
                    self.flatten("end of day")
                left = self.still_holding()
                if left > 0:
                    log.critical("COULD NOT %s %d %s BEFORE THE CLOSE. It is still open with no stop - "
                                 "close it in your broker's app.", self._exit_verb().upper(), left, self.symbol)
                    self.not_flat = dict(symbol=self.symbol, shares=left, side=self.pos.get("side", 1) if self.pos else 1)
                log.info("Session done. Trades: %d, day P&L: %.2f", self.trades_today, self.day_pnl())
                self.final = True
                self.publish("Session finished")
                return
            try:
                if self.apply_pending_bar_size():
                    self.publish(f"Loading {self.bm}-minute bars")
                if self.apply_pending_settings():
                    self.publish()
                self.check_midday_rescan(now)
                if self.pending_close:
                    self.close_by_hand()
                if self.pending_stop is not None:
                    self.move_stop_by_hand()
                if self.pending_rescan:
                    self.rescan()
                self.step()
            except MarketClosed as e:
                log.info("%s Exiting.", e)
                self.final = True
                self.publish("Market closed today")
                return
            except Exception as e:
                log.exception("step failed: %s", e)
            self.publish(self._auto_state())
            # until 15s after the next bar boundary: refresh the price for the page every 15s
            nxt = (now.replace(second=0, microsecond=0)
                   + timedelta(minutes=self.bm - now.minute % self.bm)) + timedelta(seconds=15)
            # (wakes every 2 s so a bar-size change from the page is picked up quickly)
            last_quote = now_et()
            while True:
                remaining = (nxt - now_et()).total_seconds()
                if self.pending_settings:
                    self.apply_pending_settings()          # takes effect now, between checks
                    self.publish()
                if remaining <= 0 or stop_file.exists() or self.pending_bm or self.pending_rescan or self.pending_close \
                        or self.pending_stop is not None:
                    break
                _time.sleep(min(2.0 if self.dashboard is not None else 15.0, max(1.0, remaining)))
                self.beat()
                if self.dashboard is not None and not (self.pending_bm or self.pending_rescan) \
                        and (now_et() - last_quote).total_seconds() >= 14:
                    last_quote = now_et()
                    self.refresh_quote()
                    self.publish()


def public_tradable(client) -> set[str]:
    """Symbols Public lets you buy and sell (one request for the whole list)."""
    from public_api_sdk import InstrumentType, InstrumentsRequest
    res = client.get_all_instruments(InstrumentsRequest(type_filter=[InstrumentType.EQUITY]))
    out = set()
    for inst in getattr(res, "instruments", None) or []:
        if _val(getattr(inst, "trading", "")) == "BUY_AND_SELL":
            out.add(str(inst.instrument.symbol).upper())
    if not out:
        raise RuntimeError("Public returned no tradable stocks")
    return out


def make_client():
    from public_api_sdk import ApiKeyAuthConfig, PublicApiClient, PublicApiClientConfiguration
    key = os.environ.get("PUBLIC_API_SECRET_KEY")
    acct = os.environ.get("PUBLIC_ACCOUNT_ID")
    if not key or not acct:
        sys.exit("Set PUBLIC_API_SECRET_KEY and PUBLIC_ACCOUNT_ID (see .env.example).")
    return PublicApiClient(ApiKeyAuthConfig(api_secret_key=key, validity_minutes=480),
                           config=PublicApiClientConfiguration(default_account_number=acct))


def is_weekday(bot: "Bot | None" = None) -> bool:
    if now_et().weekday() < 5:
        return True
    log.info("Weekend - market closed.")
    if bot:
        bot.final = True
        bot.publish("Weekend - market closed")
    return False


def wait_for_open(bot: "Bot | None" = None) -> bool:
    """Sleep until 09:35 ET on a weekday. Returns False on weekends."""
    if not is_weekday(bot):
        return False
    now = now_et()
    start = now.replace(hour=9, minute=35, second=0, microsecond=0)
    if now < start:
        log.info("Waiting until %s ET", start.strftime("%H:%M"))
        while (left := (start - now_et()).total_seconds()) > 0:
            if bot:
                bot.publish(f"Picked {bot.symbol} - trading starts 9:35 ET ({int(left // 60)} min)"
                            if bot.symbol else f"Waiting for 9:35 ET ({int(left // 60)} min)")
            _time.sleep(min(30.0, left))
    return True
