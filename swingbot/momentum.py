"""SPY intraday momentum (``strategy: spy_momentum``).

From Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF
(SPY)". One symbol (SPY), at most a trade or two a day, nothing held overnight.

The noise band. For each time of day (10:00, 10:30, ...), sigma = the average absolute move from the day's
open to that time over the last ``momentum_lookback_days`` (14) days. Today's band at that time:

    upper = max(today's open, yesterday's close) x (1 + band_mult x sigma)
    lower = min(today's open, yesterday's close) x (1 - band_mult x sigma)

Inside the band the market is just noise; outside it, it's moving. Every ``momentum_check_minutes`` (30, on
the clock: 10:00, 10:30, ...) from ``momentum_first_check``:

    flat, price above the upper band   -> buy        flat, price below the lower band -> sell short
    long, price below max(upper, VWAP) -> sell        short, price above min(lower, VWAP) -> buy back
    (and if the price is past the other band too, the position is turned around: long to short or back)

Everything is closed at ``flatten_time``. Size: the same number of shares all day, worked out at the open:
account x min(momentum_max_leverage, momentum_vol_target_pct / SPY's daily volatility over the lookback) / open.

The checks see a bar's close at the check time; the order fills at the next bar's open (with ``slippage_pct``),
the way the live bot trades. 1R in the results = 1% of the position (there's no fixed stop to measure from).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import asdict, fields
from datetime import date, time, timedelta

import numpy as np
import pandas as pd

from .backtest import Trade


def _t(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def _bar_minutes(df: pd.DataFrame) -> int:
    idx = df.index
    gaps = pd.Series(idx[1:] - idx[:-1])[pd.Series(idx[1:].date) == pd.Series(idx[:-1].date)]
    return max(1, int(round(gaps.mode().iloc[0].total_seconds() / 60))) if len(gaps) else 5


class Days:
    """A symbol's bars split into days, with what the strategy needs from each: the open, the close, the
    move from the open at every bar end, and the running VWAP."""

    def __init__(self, df: pd.DataFrame, bar_minutes: int | None = None):
        df = df.sort_index()
        self.bm = bar_minutes or _bar_minutes(df)
        self.days: list[date] = []
        self.bars: dict[date, pd.DataFrame] = {}
        moves = {}
        for d, g in df.groupby(df.index.date):
            if len(g) < 2:
                continue
            g = g.copy()
            gaps = pd.Series(g.index[1:] - g.index[:-1])
            bm = int(round(gaps.mode().iloc[0].total_seconds() / 60)) if len(gaps) else self.bm   # this day's bar size
            g["end"] = [(ts + pd.Timedelta(minutes=bm)).strftime("%H:%M") for ts in g.index]
            o = float(g["open"].iat[0])
            g["move"] = (g["close"] / o - 1).abs()
            g["vwap"] = vwap(g)
            self.days.append(d)
            self.bars[d] = g
            moves[d] = pd.Series(g["move"].to_numpy(), index=g["end"].to_numpy())
        self.moves = pd.DataFrame(moves).T.sort_index()        # rows = days, columns = bar end times
        self.open = {d: float(self.bars[d]["open"].iat[0]) for d in self.days}
        self.close = {d: float(self.bars[d]["close"].iat[-1]) for d in self.days}

    def prior(self, d: date, n: int) -> list[date]:
        """The n days with bars before d (d itself needn't be in the history: today, live)."""
        i = bisect.bisect_left(self.days, d)
        return self.days[max(0, i - n):i]

    def sigma(self, d: date, n: int) -> pd.Series | None:
        """Average move from the open, by bar end time, over the n days before d (None: not enough days)."""
        p = self.prior(d, n)
        if len(p) < n:
            return None
        return self.moves.loc[p].mean(skipna=True)

    def prev_close(self, d: date) -> float | None:
        p = self.prior(d, 1)
        return self.close[p[0]] if p else None

    def daily_vol(self, d: date, n: int) -> float | None:
        """Standard deviation of the daily returns (close to close) over the n days before d."""
        p = self.prior(d, n + 1)
        if len(p) < n + 1:
            return None
        c = np.array([self.close[x] for x in p])
        r = c[1:] / c[:-1] - 1
        return float(np.std(r, ddof=1)) if len(r) > 1 else None


def vwap(g: pd.DataFrame) -> np.ndarray:
    """The running volume-weighted average price over one day's bars (typical price (H+L+C)/3)."""
    tp = (g["high"] + g["low"] + g["close"]) / 3
    vol = g["volume"].astype(float).clip(lower=0)
    cum = vol.cumsum()
    return np.where(cum > 0, (tp * vol).cumsum() / cum.replace(0, np.nan), tp.expanding().mean())


def bands(days: Days, d: date, cfg, day_open: float | None = None) -> pd.DataFrame | None:
    """Day d's upper and lower band at every bar end time (None: not enough history yet). ``day_open``:
    the day's open when d isn't in the history (live: today)."""
    sig = days.sigma(d, int(cfg.momentum_lookback_days))
    pc = days.prev_close(d)
    if sig is None or pc is None:
        return None
    o = day_open if day_open is not None else days.open[d]
    m = float(cfg.momentum_band_mult)
    return pd.DataFrame({"upper": max(o, pc) * (1 + m * sig), "lower": min(o, pc) * (1 - m * sig), "sigma": sig})


def leverage(days: Days, d: date, cfg) -> float:
    """How many times the account to hold: momentum_vol_target_pct / SPY's recent daily volatility, at most
    momentum_max_leverage."""
    vol = days.daily_vol(d, int(cfg.momentum_lookback_days))
    lev = float(cfg.momentum_max_leverage)
    if vol and vol > 0:
        lev = min(lev, float(cfg.momentum_vol_target_pct) / 100 / vol)
    return lev


def shares_for(equity: float, days: Days, d: date, cfg, day_open: float | None = None) -> int:
    """Shares for the day: volatility-targeted, capped by momentum_max_leverage."""
    o = day_open if day_open is not None else days.open[d]
    return max(0, math.floor(equity * leverage(days, d, cfg) / o))


def is_check(end: str, cfg) -> bool:
    """A check time: on the momentum_check_minutes clock (10:00, 10:30, ...), from momentum_first_check."""
    t = _t(end)
    every = int(cfg.momentum_check_minutes)
    return (t.hour * 60 + t.minute) % every == 0 and t >= _t(cfg.momentum_first_check)


def decide(pos_side: int, price: float, upper: float, lower: float, vwap: float, cfg, can_enter: bool) -> tuple[bool, int]:
    """At a check: (exit the position?, side to enter: 1, -1 or 0). A turnaround = exit + enter the other way."""
    long_ok = cfg.direction in ("long", "both")
    short_ok = cfg.direction in ("short", "both")
    exit_now = False
    if pos_side > 0:
        exit_now = price < max(upper, vwap) if cfg.momentum_vwap_stop else price < lower
    elif pos_side < 0:
        exit_now = price > min(lower, vwap) if cfg.momentum_vwap_stop else price > upper
    enter = 0
    if can_enter and (pos_side == 0 or exit_now):
        if price > upper and long_ok and pos_side <= 0:
            enter = 1
        elif price < lower and short_ok and pos_side >= 0:
            enter = -1
    return exit_now, enter


def stop_level(side: int, upper: float, lower: float, vwap: float, cfg) -> float:
    """Where the position would be closed at the next check."""
    if side > 0:
        return max(upper, vwap) if cfg.momentum_vwap_stop else lower
    return min(lower, vwap) if cfg.momentum_vwap_stop else upper


def run(hist: dict, cfg, progress=None, start=None, end=None, **_) -> tuple[pd.DataFrame, pd.Series, list[dict]]:
    """Backtest: same outputs as backtest.run (trades, daily equity, picks)."""
    sym = str(cfg.momentum_symbol).upper()
    if sym not in hist or hist[sym] is None or not len(hist[sym]):
        raise SystemExit(f"spy_momentum needs {sym}'s bars, and the history has none")
    days = Days(hist[sym])
    slip = float(cfg.slippage_pct) / 100
    flat_t, last_t = _t(cfg.flatten_time), _t(cfg.last_entry_time)
    loss_limit = float(cfg.daily_loss_limit_pct) / 100
    equity = float(cfg.starting_equity)
    trades: list[Trade] = []
    curve: dict = {}
    picks: list[dict] = []
    todo = [d for d in days.days if (start is None or d >= start) and (end is None or d <= end)]
    for k, d in enumerate(todo):
        b = bands(days, d, cfg)
        g = days.bars[d]
        n = shares_for(equity, days, d, cfg) if b is not None else 0
        day_pnl, n_trades, pos = 0.0, 0, None
        picks.append(dict(date=str(d), watched=[sym], shares=n,
                          sigma_1000=round(float(b["sigma"].get("10:00", float("nan"))), 5) if b is not None else None))

        def close(i, price, reason):
            nonlocal pos, day_pnl
            fill = price * (1 - slip * pos["side"])
            pnl = (fill - pos["entry"]) * pos["shares"] * pos["side"]
            one_r = 0.01 * pos["entry"] * pos["shares"]
            trades.append(Trade(
                date=str(d), symbol=sym, kind="momentum" if pos["side"] > 0 else "short momentum",
                entry_time=pos["time"], entry=round(pos["entry"], 4), stop=round(pos["init_stop"], 4),
                final_stop=round(pos["stop"], 4), target=float("nan"), shares=pos["shares"],
                exit_time=g.index[i].strftime("%H:%M"), exit=round(fill, 4), reason=reason, pnl=round(pnl, 2),
                r_multiple=round(pnl / one_r, 2) if one_r else 0.0, side="long" if pos["side"] > 0 else "short"))
            day_pnl += pnl
            pos = None

        if b is not None and n > 0:
            for i in range(len(g) - 1):
                nxt = g.index[i + 1]
                if nxt.time() >= flat_t:
                    break
                end_s = g["end"].iat[i]
                if pos is not None:
                    pos["stop"] = stop_level(pos["side"], b["upper"].get(end_s, np.nan), b["lower"].get(end_s, np.nan),
                                             float(g["vwap"].iat[i]), cfg)
                if not is_check(end_s, cfg) or end_s not in b.index:
                    continue
                price, up, lo, vw = float(g["close"].iat[i]), float(b.at[end_s, "upper"]), float(b.at[end_s, "lower"]), \
                    float(g["vwap"].iat[i])
                can_enter = (_t(end_s) <= last_t and n_trades < int(cfg.max_trades_per_day)
                             and day_pnl > -loss_limit * equity)
                exit_now, enter = decide(pos["side"] if pos else 0, price, up, lo, vw, cfg, can_enter)
                fill_at = float(g["open"].iat[i + 1])
                if exit_now and pos is not None:
                    close(i + 1, fill_at, "stop")
                if enter:
                    pos = dict(side=enter, shares=n, entry=fill_at * (1 + slip * enter), time=nxt.strftime("%H:%M"),
                               init_stop=stop_level(enter, up, lo, vw, cfg), stop=stop_level(enter, up, lo, vw, cfg))
                    n_trades += 1
            if pos is not None:                           # the sell-off at flatten_time
                j = next((i for i in range(len(g)) if g.index[i].time() >= flat_t), len(g) - 1)
                price = float(g["open"].iat[j]) if g.index[j].time() >= flat_t else float(g["close"].iat[j])
                close(j, price, "flatten")
        equity += day_pnl
        curve[pd.Timestamp(d)] = equity
        if progress:
            progress(k + 1, len(todo))
    tdf = pd.DataFrame([asdict(t) for t in trades], columns=[f.name for f in fields(Trade)])
    return tdf, pd.Series(curve, name="equity", dtype=float), picks
