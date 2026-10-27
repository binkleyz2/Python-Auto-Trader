"""Shorts: the long rules on the chart turned upside down (direction: short / both)."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from swingbot import backtest as bt, live
from swingbot.config import Config
from swingbot.data import synthetic
from swingbot.scanner import scan
from swingbot.strategy import (add_indicators, find_swings, mirror, short_signal_at, sided_breakeven_stop,
                               sided_trailing_stop, signal_at, signals_at, trailing_stop)

ET = ZoneInfo("America/New_York")
C = 300.0


def _flipped(df):
    """The same chart upside down as real (positive) prices: C - price, highs and lows swapped."""
    f = df.copy()
    f["open"], f["close"], f["high"], f["low"] = C - df["open"], C - df["close"], C - df["low"], C - df["high"]
    return f


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_short_signals_are_long_signals_on_the_flipped_chart(seed):
    cfg = Config(min_stop_pct=0, direction="short")       # % floor is relative to price, so it can't mirror exactly
    d = add_indicators(synthetic("TEST", days=8, seed=seed), cfg)
    f = add_indicators(_flipped(synthetic("TEST", days=8, seed=seed)), cfg)
    lo, hi = find_swings(d, cfg.swing_n)
    flo, fhi = find_swings(f, cfg.swing_n)
    assert (lo == fhi).all() and (hi == flo).all()            # swing highs become swing lows
    md, found = mirror(d), 0
    for t in range(30, len(d)):
        s, ref = short_signal_at(md, t, cfg, lo, hi), signal_at(f, t, cfg, flo, fhi)
        assert (s is None) == (ref is None), t
        if s:
            found += 1
            assert s.side == -1 and s.kind == {"bounce": "short bounce", "breakout": "short breakdown"}[ref.kind]
            assert s.stop == pytest.approx(C - ref.stop) and s.ref_price == pytest.approx(C - ref.ref_price)
            assert s.stop > s.ref_price                         # stop above the entry
            assert s.line.kind == ("resistance" if ref.line.kind == "support" else "support")
    assert found >= 3


def test_short_trailing_and_breakeven_mirror_the_long_ones():
    cfg = Config(min_stop_pct=0)
    df = synthetic("TEST", days=6, seed=5)
    d, f = add_indicators(df, cfg), add_indicators(_flipped(df), cfg)
    lo, hi = find_swings(d, cfg.swing_n)
    flo, _ = find_swings(f, cfg.swing_n)
    moved = 0
    for t in range(60, len(d), 7):
        long_f = trailing_stop(f, t, cfg, flo, 40, C - 1e6)
        short = sided_trailing_stop(d, None, t, cfg, lo, hi, 40, 1e6, -1)
        assert short == pytest.approx(C - long_f)
        moved += short < 1e6
    assert moved
    # breakeven: price 1R in favour -> stop to entry; never loosens
    assert sided_breakeven_stop(50.0, 51.0, 48.9, 51.0, cfg, -1) == 50.0
    assert sided_breakeven_stop(50.0, 51.0, 49.5, 51.0, cfg, -1) == 51.0
    assert sided_breakeven_stop(50.0, 51.0, 48.0, 49.5, cfg, -1) == 49.5


def test_direction_long_never_shorts_and_both_finds_both():
    df = synthetic("TEST", days=10, seed=7)
    d = add_indicators(df, Config())
    lo, hi = find_swings(d, 5)
    md = mirror(d)
    sides = {c: set() for c in ("long", "short", "both")}
    for direction in sides:
        cfg = Config(direction=direction)
        for t in range(30, len(d)):
            s = signals_at(d, md, t, cfg, lo, hi)
            if s:
                sides[direction].add(s.side)
    assert sides["long"] == {1} and sides["short"] == {-1} and sides["both"] == {1, -1}


def test_scan_ranks_short_setups_only_when_asked():
    hist = {f"S{i}": synthetic(f"S{i}", days=6, seed=i)[:-78] for i in range(30)}
    longs = scan(hist, Config(min_avg_dollar_volume=0))
    shorts = scan(hist, Config(min_avg_dollar_volume=0, direction="short"))
    both = scan(hist, Config(min_avg_dollar_volume=0, direction="both"))
    assert all(r.side == 1 and not r.setup.startswith("short") for r in longs)
    good = [r for r in shorts if r.score > 0]
    assert good and all(r.side == -1 and r.setup.startswith("short: ") for r in good)
    assert {r.side for r in both if r.score > 0} == {1, -1}
    best = {r.symbol: r.score for r in both}
    for r in longs + shorts:                                   # "both" keeps each symbol's better setup
        assert best[r.symbol] >= r.score - 1e-12


def test_backtest_with_shorts():
    hist = {s: synthetic(s, days=25, seed=k) for k, s in enumerate(["AAA", "BBB", "CCC", "DDD"])}
    cfg = Config(direction="both", min_avg_dollar_volume=0)
    trades, curve, _ = bt.run(hist, cfg)
    assert set(trades.side) == {"long", "short"}
    for _, tr in trades.iterrows():
        sign = 1 if tr.side == "long" else -1
        assert tr.pnl == pytest.approx((tr.exit - tr.entry) * tr.shares * sign, abs=0.02)
        assert sign * (tr.entry - tr.stop) > 0                 # stop on the losing side of the entry
        if tr.reason == "trail":
            assert sign * (tr.final_stop - tr.stop) > 0       # the trailing stop moved toward the price
    assert bt.summarize(trades, curve, cfg)["short_trades"] > 0
    long_only = bt.run(hist, Config(min_avg_dollar_volume=0))[0]
    assert set(long_only.side) == {"long"}


def test_dry_run_short_round_trip(tmp_path, monkeypatch):
    clock = {"t": datetime(2026, 10, 27, 10, 0, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    bot = live.Bot(None, Config(direction="short"), live=False, workdir=tmp_path)
    bot.symbol, bot.prepared = "AAA", True
    monkeypatch.setattr(bot, "short_check", lambda sym, qty: None)
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    p = bot.pos
    assert p["side"] == -1 and p["shares"] == 125 and p["stop"] == 51.0
    st = bot.status()["pos"]
    bot.last_price = 49.0
    assert bot.status()["pos"]["unreal"] == pytest.approx(125.0) and st["risk_dollars"] == pytest.approx(125.0)
    bar = pd.Series(dict(open=49.5, high=50.2, low=49.0, close=49.2))
    bot._paper_check(bar)                                        # high under the stop: still open
    assert bot.pos
    bot.raise_stop(50.1)
    bot._paper_check(pd.Series(dict(open=49.8, high=50.3, low=49.7, close=50.2)))
    c = bot.closed[-1]
    assert bot.pos is None and c["exit"] == 50.1 and c["reason"] == "trail" and c["pnl"] == pytest.approx(-0.1 * 125)
    assert bot.paper_pnl == pytest.approx(-12.5)


def test_dry_run_short_skips_what_the_broker_would_refuse(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    bot = live.Bot(None, Config(direction="short"), live=False, workdir=tmp_path)
    bot.symbol = "AAA"
    monkeypatch.setattr(bot, "short_check", lambda sym, qty: "hard to borrow")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    assert bot.pos is None and "hard to borrow" in caplog.text


def test_direction_setting_is_checked():
    Config(direction="both").validate()
    with pytest.raises(ValueError, match="direction"):
        Config(direction="sideways").validate()
