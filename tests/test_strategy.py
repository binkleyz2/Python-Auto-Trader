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
    import dataclasses
    hist = {s: synthetic(s, days=20) for s in ["AAA", "BBB", "CCC"]}
    cfg = dataclasses.replace(cfg, watch_count=1, midday_rescan_time="")   # the top pick only, all day
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


def test_min_stop_pct_floor_on_entries_and_trailing():
    """2026-10-20: on 2-minute bars a tiny ATR put the stop 2 cents under a $27.52 entry.
    min_stop_pct keeps it at least 0.25% (about 7 cents) away."""
    import numpy as np
    from swingbot.strategy import min_stop_price, trailing_stop
    cfg = Config(min_stop_pct=0.25)
    assert min_stop_price(27.52, cfg) == pytest.approx(27.4512)
    assert min_stop_price(27.52, Config(min_stop_pct=0)) == 27.52
    # trailing: a swing low right under the price can't pull the stop inside the floor
    n = 30
    df = pd.DataFrame({"open": 100.0, "high": 100.1, "low": 99.99, "close": 100.0, "volume": 1.0,
                       "atr": 0.01}, index=range(n))
    is_low = np.zeros(n, bool)
    is_low[10] = True
    new = trailing_stop(df, 20, cfg, is_low, 0, current_stop=99.0)
    assert new == pytest.approx(99.75)                     # 0.25% under 100, not 99.9875
    with pytest.raises(ValueError):
        Config(min_stop_pct=-1).validate()


def test_breakeven_stop():
    """2026-10-21 BOOT: in at 114.23, stop 112.59 (1R = 1.64). Past 115.87 the stop goes to the entry."""
    from swingbot.strategy import breakeven_stop
    cfg = Config(breakeven_at_r=1.0)
    assert breakeven_stop(114.23, 112.59, 115.80, 112.59, cfg) == 112.59     # not yet 1R up
    assert breakeven_stop(114.23, 112.59, 116.03, 112.59, cfg) == 114.23     # 1.1R up: to the entry
    assert breakeven_stop(114.23, 112.59, 116.03, 115.00, cfg) == 115.00     # never lowers a trailed stop
    assert breakeven_stop(114.23, 112.59, 116.03, 112.59, Config(breakeven_at_r=0)) == 112.59
    # a tiny risk: entry would sit inside min_stop_pct of the price - keep that distance instead
    assert breakeven_stop(100.0, 99.5, 100.6, 99.5, Config(breakeven_at_r=1.0, min_stop_pct=1.0)) == pytest.approx(100.6 * 0.99)


def test_breakeven_in_the_live_loop(tmp_path, monkeypatch):
    from swingbot import live
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    df = synthetic("AAA", days=3)
    day = sorted(set(df.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 11, 1, tzinfo=ZoneInfo("America/New_York"))}

    class C:
        def get_bars(self, symbol, period, aggregation=None, **kw):
            d = df[df.index < pd.Timestamp(clock["t"])]
            if period.name == "DAY":
                d = d[d.index.date == day]
            return SimpleNamespace(regular_market=SimpleNamespace(bars=[SimpleNamespace(
                timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
                for ts, r in d.iterrows()]))
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    bot = live.Bot(C(), Config(trail_stop=False), live=False, workdir=tmp_path)
    bot.symbol, bot.history = "AAA", df[df.index.date < day]
    close = float(df[df.index < pd.Timestamp(clock["t"])].close.iat[-1])
    entry = close - 1.5                                   # price is 1.5R above an entry with 1.00 of risk
    bot.pos = dict(kind="bounce", entry=entry, init_stop=entry - 1.0, stop=entry - 1.0, target=None, shares=10,
                   signal_ts=None, entry_x=0.0, entry_clock="10:00", stop_steps=[[0.0, entry - 1.0]])
    bot.step()
    assert bot.pos["stop"] == pytest.approx(entry, abs=0.01) and len(bot.pos["stop_steps"]) == 2


def test_one_stock_per_company():
    from swingbot.scanner import company_key, one_per_company
    assert company_key("GOOG") == company_key("GOOGL") != company_key("AAPL")
    assert company_key("BRK.A") == company_key("BRK.B") == company_key("BRK/B")
    assert company_key("LEN") == company_key("LEN.B") and company_key("FOX") == company_key("FOXA")
    assert company_key("ABC") != company_key("ABCD") and company_key("ABC", [["ABC", "ABCD"]]) == company_key("ABCD", [["abcd", "abc"]])
    ranked = ["NVDA", "GOOGL", "MSFT", "GOOG", "AMZN", "BRK.B", "BRK.A", "META"]
    skipped = []
    got = one_per_company(ranked, 5, skipped=skipped)
    assert got == ["NVDA", "GOOGL", "MSFT", "AMZN", "BRK.B"] and skipped == [("GOOG", "GOOGL")]
    assert one_per_company(ranked, 10) == ["NVDA", "GOOGL", "MSFT", "AMZN", "BRK.B", "META"]
    rows = [("GOOG", 0.9), ("GOOGL", 0.8), ("AAPL", 0.7)]
    assert one_per_company(rows, 2, key=lambda r: r[0]) == [("GOOG", 0.9), ("AAPL", 0.7)]
