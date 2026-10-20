"""Shorter bars: same logic on 1-minute bars, the --symbol shortcut, and each broker asking for the right size."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import live
from swingbot.config import Config
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")


class OneMinClient:
    """Public-style fake serving bars of any size up to 'now'."""

    def __init__(self, dfs):
        self.dfs, self.now, self.requests, self.orders_placed = dfs, None, [], 0

    def get_bars(self, symbol, period, aggregation=None, **kw):
        self.requests.append((symbol, period.name, aggregation.name if aggregation else None))
        d = self.dfs[symbol]
        d = d[d.index < pd.Timestamp(self.now)]
        if period.name == "DAY":
            d = d[d.index.date == self.now.date()]
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def place_order(self, *a, **k):
        raise AssertionError("dry run must not place orders")


def test_config_bar_sizes():
    Config(bar_minutes=1).validate()
    with pytest.raises(ValueError):
        Config(bar_minutes=7).validate()
    with pytest.raises(ValueError):
        Config(bar_minutes=2, broker="public").validate()       # Public has no 2-minute bars
    Config(bar_minutes=2, broker="alpaca").validate()


def test_public_aggregation_mapping():
    from swingbot.data import _public_aggregation
    assert _public_aggregation(1).name == "ONE_MINUTE" and _public_aggregation(5).name == "FIVE_MINUTES"


def test_bar_completeness_uses_bar_size():
    df = synthetic("AAA", days=1, minutes=1)
    day = df.index[0].date()
    now = datetime(day.year, day.month, day.day, 9, 40, 30, tzinfo=ET)
    done = live.complete_bars(df, now, 1)
    assert done.index[-1].strftime("%H:%M") == "09:39"            # 9:39 bar closed at 9:40; 9:40 still forming
    assert live.x_of(done.index[-1], 1) == 9 and live.x_of(done.index[-1], 5) == pytest.approx(1.8)


def test_symbol_shortcut_skips_the_scan(tmp_path, monkeypatch):
    dfs = {"NVDA": synthetic("NVDA", days=4, minutes=1), "AAPL": synthetic("AAPL", days=4, minutes=1)}
    c = OneMinClient(dfs)
    day = sorted(set(dfs["NVDA"].index.date))[-1]
    now = datetime(day.year, day.month, day.day, 11, 2, tzinfo=ET)
    c.now = now
    monkeypatch.setattr(live, "now_et", lambda: now)
    bot = live.Bot(c, Config(watchlist=["AAPL"], bar_minutes=1), live=False, workdir=tmp_path)
    bot.forced_symbol = "NVDA"
    bot.morning_scan()
    assert bot.symbol == "NVDA" and "chosen by you" in bot.pick_reason
    assert [r[0] for r in c.requests] == ["NVDA"]                 # nothing else was scanned
    assert c.requests[0][2] == "ONE_MINUTE"
    assert bot.history.index.max().date() < day                   # history stops at yesterday's close


def test_full_dry_run_day_on_one_minute_bars(tmp_path, monkeypatch):
    cfg = Config(watchlist=["AAA"], bar_minutes=1)
    df = synthetic("AAA", days=4, minutes=1, seed=3)
    c = OneMinClient({"AAA": df})
    day = sorted(set(df.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    bot = live.Bot(c, cfg, live=False, workdir=tmp_path)
    bot.forced_symbol = "AAA"
    c.now = clock["t"]
    bot.morning_scan()
    t = clock["t"]
    while t.time() < live._t(cfg.flatten_time):
        clock["t"] = c.now = t
        bot.step()
        t += pd.Timedelta(minutes=1)
    clock["t"] = c.now = t
    bot.flatten("end of day")
    assert bot.pos is None and bot.trades_today >= 1             # it traded on 1-minute bars
    st = bot.status()
    today = [b for b in st["bars"] if b["x"] >= 0]
    assert st["bar_minutes"] == 1 and len(today) > 300            # ~380 one-minute bars today
    assert max(b["x"] for b in st["bars"]) < 390
    assert min(b["x"] for b in st["bars"]) >= -3 * 390            # plus up to 3 earlier sessions of history
    assert [dd["back"] for dd in st["days"]] == [3, 2, 1, 0]
    for c_ in bot.closed:
        assert 0 <= c_["entry_x"] <= c_["exit_x"] < 390
