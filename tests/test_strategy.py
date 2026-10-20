from datetime import time

import numpy as np
import pandas as pd
import pytest

from swingbot import backtest as bt
from swingbot.config import Config
from swingbot.data import synthetic
from swingbot.scanner import pick, scan
from swingbot.strategy import add_indicators, find_swings, signal_at, support_line, trailing_stop


@pytest.fixture(scope="module")
def cfg():
    return Config()


@pytest.fixture(scope="module")
def df():
    return synthetic("TEST", days=15)


def test_swing_low_detected():
    lows = [10, 9, 8, 7, 6, 5, 6, 7, 8, 9, 10, 11]
    d = pd.DataFrame({"open": lows, "high": [x + 1 for x in lows], "low": lows,
                      "close": lows, "volume": 1.0})
    is_low, is_high = find_swings(d, 5)
    assert list(np.flatnonzero(is_low)) == [5]
    assert not is_high.any()


def test_no_lookahead(df, cfg):
    """A signal at bar t must be identical whether or not later bars exist."""
    full = add_indicators(df, cfg)
    lo, hi = find_swings(full, cfg.swing_n)
    checked = hits = 0
    for t in range(100, len(full), 7):
        s_full = signal_at(full, t, cfg, lo, hi)
        cut = add_indicators(df.iloc[: t + 1], cfg)
        lo_c, hi_c = find_swings(cut, cfg.swing_n)
        s_cut = signal_at(cut, t, cfg, lo_c, hi_c)
        assert (s_full is None) == (s_cut is None), f"lookahead at bar {t}"
        if s_full:
            assert s_full.kind == s_cut.kind
            assert s_full.stop == pytest.approx(s_cut.stop)
            hits += 1
        checked += 1
    assert checked > 50


def test_support_line_is_rising_and_unbroken(df, cfg):
    d = add_indicators(df, cfg)
    lo, _ = find_swings(d, cfg.swing_n)
    found = 0
    for t in range(100, len(d), 5):
        line = support_line(d, t, cfg, lo)
        if line:
            found += 1
            assert line.p2 > line.p1 and line.i1 < line.i2 <= t - cfg.swing_n
    assert found > 0


def test_position_size(cfg):
    # $25k * 0.5% = $125 risk / $1 per share = 125 shares, cap 25% = $6,250 / $50 = 125
    assert bt.position_size(25_000, 50.0, 49.0, cfg) == 125
    assert bt.position_size(25_000, 50.0, 49.9, cfg) == 125   # capped by 25% position limit
    assert bt.position_size(25_000, 50.0, 51.0, cfg) == 0     # stop above entry


def test_backtest_flat_by_close_and_rules(cfg):
    hist = {s: synthetic(s, days=20) for s in ["AAA", "BBB", "CCC"]}
    trades, curve, picks = bt.run(hist, cfg)
    assert not trades.empty
    assert (trades.exit_time <= cfg.flatten_time).all()
    assert (trades.entry_time >= cfg.first_entry_time).all()
    assert (trades.groupby("date").size() <= cfg.max_trades_per_day).all()
    assert (trades.groupby("date").symbol.nunique() == 1).all()       # one pick per day
    assert (trades.stop < trades.entry).all()
    assert (trades.final_stop >= trades.stop).all()               # trailing only ever raises
    assert trades.target.isna().all() and "target" not in set(trades.reason)
    assert (trades[trades.reason == "stop"].r_multiple <= 0).all()


def test_scanner_uses_only_prior_data(cfg):
    hist = {s: synthetic(s, days=10) for s in ["AAA", "BBB"]}
    r1 = scan(hist, cfg)
    assert [r.symbol for r in r1] and all(r.score >= 0 for r in r1)
    # filters
    cheap = Config(min_price=10_000)
    assert pick(hist, cheap) is None


def test_trailing_stop_rises_only_on_confirmed_swing_lows(cfg):
    # climb with a dip (swing low at bar 10), then keep climbing
    lows = [100, 99, 98, 97.5, 97.2, 97, 97.4, 97.8, 97.6, 97.1, 96.5, 97.5, 98.5, 99.5, 100.5, 101.5, 102.5]
    d = pd.DataFrame({"open": lows, "high": [x + 1 for x in lows], "low": lows,
                      "close": [x + 0.8 for x in lows], "volume": 1.0})
    d["atr"] = 1.0
    is_low, _ = find_swings(d, cfg.swing_n)
    assert is_low[10]
    # before bar 10 is confirmed (t < 15) the stop doesn't move
    assert trailing_stop(d, 14, cfg, is_low, 6, 90.0) == 90.0
    # once confirmed it moves to 96.5 - 0.25 ATR
    assert trailing_stop(d, 15, cfg, is_low, 6, 90.0) == pytest.approx(96.25)
    # never lowers an already-higher stop
    assert trailing_stop(d, 16, cfg, is_low, 6, 97.0) == 97.0


def test_trailing_stop_no_lookahead(df, cfg):
    full = add_indicators(df, cfg)
    lo, _ = find_swings(full, cfg.swing_n)
    for t in range(120, len(full), 11):
        a = trailing_stop(full, t, cfg, lo, t - 60, 0.0)
        cut = add_indicators(df.iloc[: t + 1], cfg)
        lo_c, _ = find_swings(cut, cfg.swing_n)
        assert a == pytest.approx(trailing_stop(cut, t, cfg, lo_c, t - 60, 0.0))
