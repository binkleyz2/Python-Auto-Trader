"""The daily trend filter (trend_filter): stock and/or market trend decide which sides may be traded."""
from datetime import date, timedelta

import pandas as pd
import pytest

from swingbot import backtest as bt
from swingbot import trend
from swingbot.config import Config
from swingbot.data import synthetic


def _closes(values, last=date(2026, 11, 9)):
    days = [last - timedelta(days=len(values) - 1 - i) for i in range(len(values))]
    return pd.Series(values, index=days, dtype=float)


def test_trend_uses_only_the_days_before():
    up = _closes(list(range(100, 130)))
    assert trend.trend_on(up, date(2026, 11, 10), 20) == trend.UP
    assert trend.trend_on(_closes(list(range(130, 100, -1))), date(2026, 11, 10), 20) == trend.DOWN
    assert trend.trend_on(up, date(2026, 11, 10), 40) is None                   # not enough history
    # the day itself never counts: a crash on Nov 9 doesn't show in Nov 9's trend, only from Nov 10
    crash = _closes(list(range(100, 129)) + [50])
    assert trend.trend_on(crash, date(2026, 11, 9), 20) == trend.UP
    assert trend.trend_on(crash, date(2026, 11, 10), 20) == trend.DOWN


def test_allowed_sides():
    U, D = trend.UP, trend.DOWN
    assert trend.allowed("off", D, D) is None
    assert trend.allowed("stock", U, D) == {U}
    assert trend.allowed("market", U, D) == {D}
    assert trend.allowed("both", U, U) == {U} and trend.allowed("both", U, D) == set()
    assert trend.allowed("both", None, D) == {D} and trend.allowed("stock", None, D) == {U, D}   # unknown: no limit
    assert trend.describe("both", U, D) == "stock up, market down: no trades"


def test_config_checks_the_setting():
    with pytest.raises(ValueError, match="trend_filter"):
        Config(trend_filter="sideways").validate()
    Config(trend_filter="both").validate()


def _hist():
    return {s: synthetic(s, days=40, seed=i) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])}


def test_backtest_trades_only_with_the_trend():
    hist = _hist()
    base = Config(min_avg_dollar_volume=0, direction="both", watch_count=6)
    off, _, _ = bt.run(hist, base)
    on, _, picks = bt.run(hist, Config(**{**base.__dict__, "trend_filter": "stock", "trend_days": 5}))
    assert len(off) and len(on) < len(off)
    closes = {s: trend.daily_closes(df) for s, df in hist.items()}
    for t in on.itertuples():
        tr = trend.trend_on(closes[t.symbol], date.fromisoformat(t.date), 5)
        assert tr is None or tr == (1 if t.side == "long" else -1), t
    # the market: a market that only falls allows shorts only
    days = sorted(set().union(*[set(df.index.date) for df in hist.values()]))
    falling = pd.Series([200 - i for i in range(len(days) + 30)],
                        index=[days[0] - timedelta(days=30 - i) for i in range(30)] + days, dtype=float)
    mk, _, _ = bt.run(hist, Config(**{**base.__dict__, "trend_filter": "market", "trend_days": 5}),
                      daily={"SPY": falling})
    assert len(mk) and set(mk["side"]) == {"short"}
    # off is exactly as before
    again, _, _ = bt.run(hist, base, daily={"SPY": falling})
    pd.testing.assert_frame_equal(off, again)


def test_live_watch_list_and_signals_follow_the_trend(monkeypatch):
    from types import SimpleNamespace
    from swingbot import live
    from swingbot.scanner import ScanResult
    bot = live.Bot.__new__(live.Bot)
    bot.cfg = Config(trend_filter="both", trend_days=5)
    asked = []

    def daily(symbols, days):
        asked.append(list(symbols))
        up, down = _closes(list(range(10, 40))), _closes(list(range(40, 10, -1)))
        return {s: (down if s in ("BBB", "SPY_DOWN") else up) for s in symbols}
    bot.daily_closes = daily
    monkeypatch.setattr(live, "now_et", lambda: pd.Timestamp("2026-11-10 09:00", tz="America/New_York").to_pydatetime())
    res = [ScanResult("AAA", 2.0, "near support", 1, "", 1), ScanResult("BBB", 1.5, "near support", 1, "", 1),
           ScanResult("CCC", 1.2, "short: near support", 1, "", -1), ScanResult("DDD", 1.0, "near resistance", 1, "", 1)]
    kept = bot._with_trend(res, 10)
    assert [r.symbol for r in kept] == ["AAA", "DDD"]          # BBB: stock down; CCC: a short with both up
    assert asked[0] == ["SPY"] and set(asked[1]) == {"AAA", "BBB", "CCC", "DDD"}   # market once, then one batch
    tr = bot.day_trend()
    assert tr.ok("AAA", 1) and not tr.ok("AAA", -1) and not tr.ok("BBB", 1)
    assert len(asked) == 2                                      # cached for the day
    bot.cfg = Config(trend_filter="off")
    assert bot._with_trend(res, 10) == res


def test_yaml_off_means_off(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("trend_filter: off\n", encoding="utf-8")           # YAML reads a bare off as false
    assert Config.load(p).trend_filter == "off"
