"""Swing-point trend lines and bounce / breakout signals, long and (optionally) short.

Shorts use exactly the same rules, mirrored: the price chart is turned upside down (every price
negated, highs and lows swapped), the long rules are applied, and the result is turned back. So a
short bounce off falling resistance is a long bounce off rising support on the flipped chart, a
short breakdown through rising support is a long breakout, and a stop above swing highs that only
moves down is a stop under swing lows that only moves up.

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
_NP: dict = {}


def cols(df: pd.DataFrame) -> dict:
    """The frame's columns as numpy arrays, made once per frame (frames aren't changed after they're built)."""
    import weakref
    k = id(df)
    hit = _NP.get(k)
    if hit is not None and hit[0]() is df:
        return hit[1]
    arrs = {c: df[c].to_numpy(float) for c in ("open", "high", "low", "close", "volume", "atr", "vol_avg") if c in df}
    _NP[k] = (weakref.ref(df, lambda _, k=k: _NP.pop(k, None)), arrs)
    return arrs


_IDX: dict = {}


def _swing_idx(mask: np.ndarray) -> np.ndarray:
    """Positions of the True bars in a swing mask, made once per mask (masks aren't changed after
    find_swings builds them). Keyed by id, checked against the array's buffer so a reused id is caught."""
    k = id(mask)
    hit = _IDX.get(k)
    if hit is not None and hit[0] is mask:
        return hit[1]
    if len(_IDX) > 4096:
        _IDX.clear()
    idx = np.flatnonzero(mask)
    _IDX[k] = (mask, idx)
    return idx


def add_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Return a copy of ``df`` with ATR and 20-bar average volume columns."""
    out = df.copy()
    h, l, c = (out[k].to_numpy(float) for k in ("high", "low", "close"))
    prev = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev), np.abs(l - prev)))   # first bar: high - low
    out["atr"] = pd.Series(tr, index=out.index).rolling(cfg.atr_period, min_periods=cfg.atr_period).mean()
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
    if size < 2 * n + 1 or n < 1:
        return is_low, is_high
    from numpy.lib.stride_tricks import sliding_window_view as win
    # windows of n bars: left[k] = bars k..k+n-1, so for bar i the left side is left[i-n], the right left[i+1]
    lo_w, hi_w = win(lows, n).min(axis=1), win(highs, n).max(axis=1)
    i = np.arange(n, size - n)
    is_low[i] = (lows[i] < lo_w[i - n]) & (lows[i] <= lo_w[i + 1])
    is_high[i] = (highs[i] > hi_w[i - n]) & (highs[i] >= hi_w[i + 1])
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
    all_idx = _swing_idx(swing_mask)
    idx = all_idx[: np.searchsorted(all_idx, last_confirmed, side="right")]
    if len(idx) < 2:
        return None

    a = cols(df)
    closes, lows, highs = a["close"], a["low"], a["high"]
    prices = lows if kind == "support" else highs
    atr = float(a["atr"][t])
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
        if check_end > i1:
            line_vals = p1 + slope * np.arange(1, check_end - i1 + 1)
            seg = closes[i1 + 1: check_end + 1]
            if kind == "support":
                if (seg < line_vals - cfg.break_tolerance_atr * atr).any():
                    continue
            else:
                if (seg > line_vals + cfg.break_tolerance_atr * atr).any():
                    continue

        # touches after the second anchor (excluding the anchor itself and its neighbours)
        start = i2 + n + 1
        touches = 0
        if t > start:
            lv = p1 + slope * np.arange(start - i1, t - i1)
            tol = cfg.touch_tolerance_atr * atr
            touches = int(np.count_nonzero(np.abs(prices[start:t] - lv) <= tol))
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
    kind: str           # "bounce" or "breakout"; shorts: "short bounce" or "short breakdown"
    bar: int            # index of the bar whose close triggered it
    line: TrendLine
    line_value: float
    stop: float
    ref_price: float    # close of the signal bar (used for sizing in live mode)
    side: int = 1       # 1 = long, -1 = short


def signal_at(df: pd.DataFrame, t: int, cfg: Config, is_low: np.ndarray, is_high: np.ndarray) -> Signal | None:
    """Long signal on the close of bar ``t``, or None. ``df`` must have indicator columns."""
    a = cols(df)
    atr = a["atr"][t]
    if np.isnan(atr) or atr <= 0:
        return None
    o, h, l, c = a["open"][t], a["high"][t], a["low"][t], a["close"][t]

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
            prev_c = a["close"][t - 1]
            prev_lv = res.value_at(t - 1)
            fresh = prev_c <= prev_lv + cfg.break_tolerance_atr * atr
            vol_ok = True
            if cfg.breakout_volume_mult > 0:
                va = a["vol_avg"][t]
                vol_ok = (not np.isnan(va)) and a["volume"][t] >= cfg.breakout_volume_mult * va
            if broke and fresh and vol_ok and c > o:
                stop = min(lv - cfg.stop_buffer_atr * atr, c - cfg.min_stop_atr * atr)
                if 0 < c - stop <= cfg.max_stop_atr * atr:
                    stop = min(stop, min_stop_price(c, cfg))
                    return Signal("breakout", t, res, lv, stop, c)
    return None


def min_stop_price(price: float, cfg: Config) -> float:
    """Highest stop allowed: at least min_stop_pct below the price. Keeps stops on short bars (tiny ATR)
    from sitting inside the bid-ask spread. The max_stop_atr limit is checked before this floor.
    (Written as a distance, so it also holds on the flipped chart used for shorts.)"""
    return price - abs(price) * cfg.min_stop_pct / 100


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
    a = cols(df)
    atr = a["atr"][t]
    if np.isnan(atr) or atr <= 0:
        return current_stop
    buf = cfg.trail_buffer_atr if cfg.trail_buffer_atr is not None else cfg.stop_buffer_atr
    candidate = a["low"][from_bar + idx[-1]] - buf * atr
    if candidate >= a["close"][t]:
        return current_stop
    candidate = min(candidate, min_stop_price(a["close"][t], cfg))   # never trail closer than min_stop_pct
    if candidate - current_stop < cfg.trail_min_step_atr * atr:     # skip tiny moves (fewer order changes)
        return current_stop
    return candidate


# --------------------------------------------------------------------------- #
# Shorts: the same rules on the flipped chart
# --------------------------------------------------------------------------- #
def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """The chart upside down: prices negated, highs and lows swapped. ATR and volume are unchanged.
    Swing highs of ``df`` are the swing lows of ``mirror(df)`` and vice versa."""
    out = df.copy()
    out["open"], out["close"] = -df["open"], -df["close"]
    out["high"], out["low"] = -df["low"], -df["high"]
    return out


def _unmirror_line(L: TrendLine) -> TrendLine:
    return TrendLine("resistance" if L.kind == "support" else "support", L.i1, L.i2, -L.p1, -L.p2, L.touches)


def short_signal_at(md: pd.DataFrame, t: int, cfg: Config, is_low: np.ndarray, is_high: np.ndarray) -> Signal | None:
    """Short signal on the close of bar ``t``. ``md`` = mirror(df with indicators); ``is_low``/``is_high``
    are the swings of the real chart. Returns real prices: the stop is above the entry."""
    s = signal_at(md, t, cfg, is_high, is_low)
    if s is None:
        return None
    kind = "short bounce" if s.kind == "bounce" else "short breakdown"
    return Signal(kind, s.bar, _unmirror_line(s.line), -s.line_value, -s.stop, -s.ref_price, side=-1)


def signals_at(d: pd.DataFrame, md: pd.DataFrame | None, t: int, cfg: Config, is_low, is_high) -> Signal | None:
    """Long and/or short signal per ``cfg.direction``. A long signal needs a rising bar and a short one a
    falling bar, so both can't fire on the same bar."""
    if cfg.direction in ("long", "both"):
        s = signal_at(d, t, cfg, is_low, is_high)
        if s:
            return s
    if cfg.direction in ("short", "both"):
        return short_signal_at(md if md is not None else mirror(d), t, cfg, is_low, is_high)
    return None


def sided_trailing_stop(d: pd.DataFrame, md: pd.DataFrame | None, t: int, cfg: Config, is_low, is_high,
                        from_bar: int, current_stop: float, side: int) -> float:
    """Long: trails under swing lows, only up. Short: trails over swing highs, only down."""
    if side > 0:
        return trailing_stop(d, t, cfg, is_low, from_bar, current_stop)
    md = md if md is not None else mirror(d)
    return -trailing_stop(md, t, cfg, is_high, from_bar, -current_stop)


def sided_breakeven_stop(entry: float, init_stop: float, close: float, current_stop: float, cfg: Config,
                         side: int) -> float:
    if side > 0:
        return breakeven_stop(entry, init_stop, close, current_stop, cfg)
    return -breakeven_stop(-entry, -init_stop, -close, -current_stop, cfg)


def better_stop(new: float, old: float, side: int) -> bool:
    """True if ``new`` is tighter than ``old`` (higher for a long, lower for a short)."""
    return new > old + 1e-9 if side > 0 else new < old - 1e-9
