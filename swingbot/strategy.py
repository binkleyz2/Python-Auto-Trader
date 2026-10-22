"""Swing-point trend lines and long-only bounce / breakout signals.

Everything here works on 5-minute bars (regular hours) and is written so a
decision at bar ``t`` only uses information that existed when bar ``t`` closed:

* A swing low at bar ``i`` needs ``n`` bars after it, so it is only *confirmed*
  at bar ``i + n``. Lines are built only from confirmed swings.
* Signals fire on the close of bar ``t``; the trade is entered at the open of
  bar ``t + 1`` (backtest) or immediately after the bar closes (live).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Return a copy of ``df`` with ATR and 20-bar average volume columns."""
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [out["high"] - out["low"], (out["high"] - prev_close).abs(), (out["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(cfg.atr_period, min_periods=cfg.atr_period).mean()
    # average of the 20 bars *before* this one, so the current bar can be compared to it
    out["vol_avg"] = out["volume"].shift(1).rolling(20, min_periods=20).mean()
    return out


def find_swings(df: pd.DataFrame, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Boolean arrays marking swing lows and swing highs.

    A swing low is a bar whose low is strictly below the ``n`` lows before it
    and no higher than the ``n`` lows after it (the mirror for swing highs).
    The last ``n`` bars can never be swings yet, because their right side
    hasn't happened.
    """
    lows = df["low"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    size = len(df)
    is_low = np.zeros(size, bool)
    is_high = np.zeros(size, bool)
    for i in range(n, size - n):
        left_l, right_l = lows[i - n:i], lows[i + 1:i + n + 1]
        if lows[i] < left_l.min() and lows[i] <= right_l.min():
            is_low[i] = True
        left_h, right_h = highs[i - n:i], highs[i + 1:i + n + 1]
        if highs[i] > left_h.max() and highs[i] >= right_h.max():
            is_high[i] = True
    return is_low, is_high


# --------------------------------------------------------------------------- #
# Trend lines
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendLine:
    kind: str          # "support" (rising, through swing lows) or "resistance" (falling, through swing highs)
    i1: int            # first anchor bar index
    i2: int            # second anchor bar index
    p1: float
    p2: float
    touches: int       # extra bars that touched the line between i2 and now

    @property
    def slope(self) -> float:
        return (self.p2 - self.p1) / (self.i2 - self.i1)

    def value_at(self, t: int) -> float:
        return self.p2 + self.slope * (t - self.i2)


def _build_line(df: pd.DataFrame, t: int, cfg: Config, swing_mask: np.ndarray, kind: str) -> TrendLine | None:
    """Most recent valid line as of the close of bar ``t``.

    support:    second swing low HIGHER than the first, and no close below the line since.
    resistance: second swing high LOWER than the first, and no close above the line since
                (checked through bar t-1, so bar t itself is free to break out).
    """
    n = cfg.swing_n
    last_confirmed = t - n                       # newest bar that can be a confirmed swing at time t
    if last_confirmed < 0:
        return None
    idx = np.flatnonzero(swing_mask[: last_confirmed + 1])
    if len(idx) < 2:
        return None

    price_col = "low" if kind == "support" else "high"
    prices = df[price_col].to_numpy(float)
    closes = df["close"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    atr = float(df["atr"].iat[t]) if not np.isnan(df["atr"].iat[t]) else np.nan
    if np.isnan(atr) or atr <= 0:
        return None

    i2 = idx[-1]
    for i1 in idx[-2::-1]:                       # walk back through older swings
        if t - i1 > cfg.max_line_age_bars:
            break
        if i2 - i1 < cfg.min_anchor_gap_bars:
            continue
        p1, p2 = prices[i1], prices[i2]
        if kind == "support" and not p2 > p1:
            continue
        if kind == "resistance" and not p2 < p1:
            continue

        slope = (p2 - p1) / (i2 - i1)
        check_end = t if kind == "support" else t - 1    # resistance: bar t may break
        span = np.arange(i1 + 1, check_end + 1)
        if len(span):
            line_vals = p1 + slope * (span - i1)
            if kind == "support":
                if np.any(closes[span] < line_vals - cfg.break_tolerance_atr * atr):
                    continue
            else:
                if np.any(closes[span] > line_vals + cfg.break_tolerance_atr * atr):
                    continue

        # touches after the second anchor (excluding the anchor itself and its neighbours)
        touch_span = np.arange(i2 + n + 1, t)
        touches = 0
        if len(touch_span):
            lv = p1 + slope * (touch_span - i1)
            tol = cfg.touch_tolerance_atr * atr
            if kind == "support":
                touches = int(np.sum(np.abs(lows[touch_span] - lv) <= tol))
            else:
                touches = int(np.sum(np.abs(highs[touch_span] - lv) <= tol))
        return TrendLine(kind, int(i1), int(i2), float(p1), float(p2), touches)
    return None


def support_line(df, t, cfg, is_low) -> TrendLine | None:
    return _build_line(df, t, cfg, is_low, "support")


def resistance_line(df, t, cfg, is_high) -> TrendLine | None:
    return _build_line(df, t, cfg, is_high, "resistance")


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Signal:
    kind: str           # "bounce" or "breakout"
    bar: int            # index of the bar whose close triggered it
    line: TrendLine
    line_value: float
    stop: float
    ref_price: float    # close of the signal bar (used for sizing in live mode)


def signal_at(df: pd.DataFrame, t: int, cfg: Config, is_low: np.ndarray, is_high: np.ndarray) -> Signal | None:
    """Long signal on the close of bar ``t``, or None. ``df`` must have indicator columns."""
    row = df.iloc[t]
    atr = row["atr"]
    if np.isnan(atr) or atr <= 0:
        return None
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]

    # --- Bounce off a rising support line ---------------------------------
    if cfg.enable_bounce:
        sup = support_line(df, t, cfg, is_low)
        if sup is not None and t > sup.i2 + cfg.swing_n:
            lv = sup.value_at(t)
            touched = l <= lv + cfg.touch_tolerance_atr * atr
            held = c > lv
            bullish = c > o
            if touched and held and bullish:
                stop = min(lv - cfg.stop_buffer_atr * atr, c - cfg.min_stop_atr * atr)
                if 0 < c - stop <= cfg.max_stop_atr * atr:
                    stop = min(stop, min_stop_price(c, cfg))
                    return Signal("bounce", t, sup, lv, stop, c)

    # --- Breakout through a falling resistance line -----------------------
    if cfg.enable_breakout:
        res = resistance_line(df, t, cfg, is_high)
        if res is not None:
            lv = res.value_at(t)
            broke = c > lv + cfg.break_tolerance_atr * atr
            prev_c = df["close"].iat[t - 1]
            prev_lv = res.value_at(t - 1)
            fresh = prev_c <= prev_lv + cfg.break_tolerance_atr * atr
            vol_ok = True
            if cfg.breakout_volume_mult > 0:
                va = row["vol_avg"]
                vol_ok = (not np.isnan(va)) and row["volume"] >= cfg.breakout_volume_mult * va
            if broke and fresh and vol_ok and c > o:
                stop = min(lv - cfg.stop_buffer_atr * atr, c - cfg.min_stop_atr * atr)
                if 0 < c - stop <= cfg.max_stop_atr * atr:
                    stop = min(stop, min_stop_price(c, cfg))
                    return Signal("breakout", t, res, lv, stop, c)
    return None


def min_stop_price(price: float, cfg: Config) -> float:
    """Highest stop allowed: at least min_stop_pct below the price. Keeps stops on short bars (tiny ATR)
    from sitting inside the bid-ask spread. The max_stop_atr limit is checked before this floor."""
    return price * (1 - cfg.min_stop_pct / 100)


def breakeven_stop(entry: float, init_stop: float, close: float, current_stop: float, cfg: Config) -> float:
    """Once a bar closes ``breakeven_at_r`` x the initial risk above the entry, lift the stop to the
    entry price, so the trade can no longer turn into a loss. Never lowers the stop, and never puts it
    closer than min_stop_pct under the price. 0 turns it off."""
    r = cfg.breakeven_at_r
    risk = entry - init_stop
    if r <= 0 or risk <= 0 or current_stop >= entry or close < entry + r * risk:
        return current_stop
    return max(current_stop, min(entry, min_stop_price(close, cfg)))


def target_for(entry: float, stop: float, cfg: Config) -> float:
    return entry + cfg.reward_risk * (entry - stop)


def trailing_stop(df: pd.DataFrame, t: int, cfg: Config, is_low: np.ndarray,
                  from_bar: int, current_stop: float) -> float:
    """Swing-low trailing stop as of the close of bar ``t``.

    Looks at swing lows from ``from_bar`` (the signal bar) onward that are
    confirmed by bar ``t`` (i.e. at index <= t - swing_n). The newest one sets a
    candidate stop just below its low. The stop only ever moves up, and never to
    a level at or above the current close, nor closer than ``min_stop_pct`` below it.
    """
    last = t - cfg.swing_n
    if last < from_bar:
        return current_stop
    idx = np.flatnonzero(is_low[from_bar:last + 1])
    if not len(idx):
        return current_stop
    atr = df["atr"].iat[t]
    if np.isnan(atr) or atr <= 0:
        return current_stop
    buf = cfg.trail_buffer_atr if cfg.trail_buffer_atr is not None else cfg.stop_buffer_atr
    candidate = df["low"].iat[from_bar + idx[-1]] - buf * atr
    if candidate >= df["close"].iat[t]:
        return current_stop
    candidate = min(candidate, min_stop_price(df["close"].iat[t], cfg))   # never trail closer than min_stop_pct
    if candidate - current_stop < cfg.trail_min_step_atr * atr:     # skip tiny moves (fewer order changes)
        return current_stop
    return candidate
