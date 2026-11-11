"""The daily trend filter (``trend_filter``): trade only in the direction of the stock's and/or the market's
daily trend.

The trend of a symbol on a day is worked out from its daily closes BEFORE that day (nothing from the day
itself, so there's no look-ahead): up when the last close is above the average of the last ``trend_days``
closes, down when below. Too little history = no trend known, and then that part of the filter doesn't
restrict anything.

    off     every signal is traded
    stock   long only when the stock's trend is up, short only when it's down
    market  the same, by the market's trend (trend_market_symbol, SPY by default)
    both    the stock and the market must agree; when they don't, the stock isn't traded that day
"""
from __future__ import annotations

from datetime import date

import pandas as pd

UP, DOWN = 1, -1
WORD = {UP: "up", DOWN: "down", None: "unknown"}


def daily_closes(df: pd.DataFrame) -> pd.Series:
    """Daily closes (indexed by date) from intraday bars: each day's last close."""
    if df is None or not len(df):
        return pd.Series(dtype=float)
    return df["close"].groupby(df.index.date).last()


def trend_on(closes: pd.Series | None, day: date, days: int) -> int | None:
    """UP / DOWN / None (not enough history) for ``day``, from the closes of the days before it."""
    if closes is None or not len(closes):
        return None
    prior = closes[[d < day for d in closes.index]] if not isinstance(closes.index, pd.DatetimeIndex) \
        else closes[closes.index.date < day]
    prior = prior.dropna()
    if len(prior) < days:
        return None
    last, avg = float(prior.iat[-1]), float(prior.tail(days).mean())
    return UP if last > avg else DOWN if last < avg else None


def allowed(mode: str, stock: int | None, market: int | None) -> set[int] | None:
    """The sides that may be traded (None = all, the filter is off)."""
    if mode in (None, "", "off"):
        return None
    sides = {UP, DOWN}
    if mode in ("stock", "both") and stock is not None:
        sides &= {stock}
    if mode in ("market", "both") and market is not None:
        sides &= {market}
    return sides


def needs_market(mode: str) -> bool:
    return mode in ("market", "both")


def describe(mode: str, stock: int | None, market: int | None) -> str:
    """For the log: "stock up, market down: no trades"."""
    bits = []
    if mode in ("stock", "both"):
        bits.append(f"stock {WORD[stock]}")
    if mode in ("market", "both"):
        bits.append(f"market {WORD[market]}")
    sides = allowed(mode, stock, market)
    what = ("no trades" if not sides else "long only" if sides == {UP} else "short only" if sides == {DOWN}
            else "long or short")
    return f"{', '.join(bits)}: {what}"


class DayTrend:
    """The filter for one day: which sides each symbol may trade. ``closes`` = {symbol: daily closes};
    symbols without closes are worked out from ``bars`` (intraday) when given, or fetched with
    ``fetch(symbols) -> {symbol: closes}`` (live)."""

    def __init__(self, cfg, day: date, closes: dict[str, pd.Series] | None, bars: dict | None = None, fetch=None):
        self.mode, self.days, self.day = str(cfg.trend_filter), int(cfg.trend_days), day
        self.closes, self.bars, self.fetch = dict(closes or {}), bars or {}, fetch
        self.market_symbol = str(cfg.trend_market_symbol or "SPY").upper()
        self._cache: dict[str, int | None] = {}
        self.market = self.trend(self.market_symbol) if needs_market(self.mode) else None

    @property
    def on(self) -> bool:
        return self.mode not in ("", "off")

    def prefetch(self, symbols) -> None:
        """Fetch the closes of these symbols in one go (fewer requests than one at a time)."""
        need = [x for x in dict.fromkeys(symbols) if x not in self.closes and x not in self._cache]
        if self.fetch is not None and need and self.on:
            got = self.fetch(need) or {}
            self.closes.update(got)
            self.closes.update({x: None for x in need if x not in got})      # asked: don't ask again

    def trend(self, sym: str) -> int | None:
        if sym not in self._cache:
            if sym not in self.closes and self.fetch is not None:
                self.prefetch([sym])
            c = self.closes.get(sym)
            if c is None and sym in self.bars:
                b = self.bars[sym]
                c = daily_closes(b[0] if isinstance(b, tuple) else b)
            self._cache[sym] = trend_on(c, self.day, self.days)
        return self._cache[sym]

    def sides(self, sym: str) -> set[int] | None:
        if not self.on:
            return None
        return allowed(self.mode, self.trend(sym) if self.mode in ("stock", "both") else None, self.market)

    def ok(self, sym: str, side: int) -> bool:
        s = self.sides(sym)
        return s is None or side in s

    def note(self, sym: str) -> str:
        return describe(self.mode, self.trend(sym) if self.mode in ("stock", "both") else None, self.market)


def setup_side(setup: str) -> int:
    """The side of a scan result's setup text ("short: near support" = short)."""
    return DOWN if str(setup).startswith("short") else UP
