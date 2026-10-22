"""Watching several symbols at once, and rescanning during the day."""
import json
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import live, strategy
from swingbot.config import Config
from swingbot.dashboard import Dashboard
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")
SYMS = ["AAA", "BBB", "CCC", "DDD"]


class MultiClient:
    """Public-style fake with several symbols; bars appear only once they exist at 'now'."""

    def __init__(self, dfs, clock):
        self.dfs, self.clock, self.requests = dfs, clock, []

    def get_bars(self, symbol, period, aggregation=None, **kw):
        self.requests.append((symbol, period.name))
        d = self.dfs[symbol]
        d = d[d.index < pd.Timestamp(self.clock["t"])]
        if period.name == "DAY":
            d = d[d.index.date == self.clock["t"].date()]
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def place_order(self, *a, **k):
        raise AssertionError("dry run must not place orders")


def _setup(tmp_path, monkeypatch, start=(9, 0), **cfg):
    dfs = {s: synthetic(s, days=8, seed=i) for i, s in enumerate(SYMS)}
    day = sorted(set(dfs["AAA"].index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, *start, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    c = MultiClient(dfs, clock)
    conf = dict(watchlist=SYMS, min_price=0, min_avg_dollar_volume=0, watch_count=3, max_trades_per_day=5)
    conf.update(cfg)
    bot = live.Bot(c, Config(**conf), live=False, workdir=tmp_path)
    return bot, c, clock, dfs


def _run_day(bot, clock, until="15:50"):
    t = clock["t"].replace(hour=9, minute=36)
    held = []
    while t.time() < live._t(until):
        clock["t"] = t
        bot.step()
        held.append(bot.symbol if bot.pos else None)
        t += pd.Timedelta(minutes=5)
    bot.flatten("end of day")
    return held


def test_watches_the_top_ranked_and_trades_whichever_signals(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch)
    bot.prepare()
    ranked = [r.symbol for r in bot.scan_results if r.score > 0]
    assert bot.watch == ranked[:3] and bot.symbol == bot.watch[0]
    assert set(bot.histories) == set(bot.watch)
    assert len(bot.status()["views"]) == len(bot.watch)                  # charts of earlier days, pre-open
    held = _run_day(bot, clock)
    assert bot.trades_today >= 1
    assert {t["symbol"] for t in bot.closed} <= set(bot.watch)
    # one position at a time: a new trade only after the last one closed
    for a, b in zip(held, held[1:]):
        assert not (a and b and a != b)


def test_best_ranked_signal_wins_a_tie(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch)
    bot.prepare()
    real = strategy.signal_at

    def everyone_signals(d, t, cfg, lo, hi):
        c_ = float(d["close"].iat[t])
        return strategy.Signal("bounce", t, None, c_ - 1, c_ * 0.99, c_)
    monkeypatch.setattr(live, "signal_at", everyone_signals)
    clock["t"] = clock["t"].replace(hour=10, minute=1)
    bot.step()
    assert bot.pos and bot.symbol == bot.watch[0]
    monkeypatch.setattr(live, "signal_at", real)


def test_while_in_a_trade_only_that_symbol_is_fetched(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch)
    bot.prepare()
    clock["t"] = clock["t"].replace(hour=10, minute=1)
    bot.step()
    bot._activate(bot.watch[1])
    bot.pos = dict(kind="bounce", entry=1.0, init_stop=0.001, stop=0.001, target=None, shares=1, signal_ts=None,
                   entry_x=0.0, entry_clock="10:00", stop_steps=[[0.0, 0.001]])
    c.requests.clear()
    clock["t"] = clock["t"].replace(minute=6)
    bot.step()
    assert {s for s, _ in c.requests} == {bot.watch[1]}


def test_rescan_uses_todays_bars_and_only_when_flat(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch, watch_count=2)
    bot.prepare()
    morning = list(bot.watch)
    clock["t"] = clock["t"].replace(hour=12, minute=1)
    bot.step()
    assert "rescan" in bot.request_rescan().lower() and bot.pending_rescan
    c.requests.clear()
    bot.rescan()
    assert not bot.pending_rescan and bot._resync and bot.last_bar_seen is None
    assert {s for s, p in c.requests if p == "WEEK"} == set(SYMS)           # the whole watchlist again
    assert bot.pick_reason.startswith("rescan 12:01")
    # the ranking now includes today's bars: compare with scoring the same data by hand
    from swingbot.scanner import scan
    today = clock["t"].date()
    combined = {s: live.complete_bars(df[df.index < pd.Timestamp(clock["t"])], clock["t"], 5) for s, df in dfs.items()}
    expect = [r.symbol for r in scan(combined, bot.cfg, {s: 1e12 for s in SYMS}) if r.score > 0][:2]
    assert bot.watch == expect
    assert all(h.index.max().date() < today for h in bot.histories.values())
    bot.step()                                                             # redraw only after a rescan
    assert not bot._resync and bot.pos is None
    bot.pos = {"stop": 1.0}
    with pytest.raises(ValueError, match="not while in a trade"):
        bot.request_rescan()
    assert morning                                                          # (morning list existed)


def test_dashboard_serves_another_symbols_chart_and_rescan(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch)
    bot.prepare()
    d = Dashboard(8871)
    d.on_bar_size, d.on_rescan = bot.request_bar_size, bot.request_rescan
    url = d.start()
    bot.dashboard = d
    try:
        bot.publish()
        base = f"http://127.0.0.1:{d.port}/"
        main = json.loads(urllib.request.urlopen(base + "status.json").read())
        other = bot.watch[1]
        alt = json.loads(urllib.request.urlopen(base + f"status.json?chart={other}").read())
        assert main["chart_symbol"] == bot.symbol and alt["chart_symbol"] == other
        assert alt["symbol"] == bot.symbol and alt["bars"] != main["bars"]   # same status, other chart
        assert "views" not in main and len(main["watch"]) == len(bot.watch) >= 2
        req = urllib.request.Request(base + "rescan", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)                                    # no page token
        assert e.value.code == 403 and not bot.pending_rescan
        req.add_header("X-Swingbot-Token", d.token)
        assert urllib.request.urlopen(req).status == 200 and bot.pending_rescan
    finally:
        d.stop()


def test_watch_count_one_is_the_old_single_pick(tmp_path, monkeypatch):
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch, watch_count=1)
    bot.prepare()
    assert len(bot.watch) == 1 and bot.symbol == bot.watch[0]
    with pytest.raises(ValueError):
        Config(watch_count=0).validate()


def test_warns_when_the_feed_stops_trading_a_symbol(tmp_path, monkeypatch, caplog):
    """2026-10-21: Alpaca's free IEX feed had no BOOT trades from 10:39, so no bars arrived and the page
    looked frozen. Now the log and the dashboard say so."""
    import logging
    bot, c, clock, dfs = _setup(tmp_path, monkeypatch, watch_count=1)
    bot.prepare()
    sym = bot.symbol
    cut = pd.Timestamp(clock["t"].replace(hour=10, minute=40))
    dfs[sym] = dfs[sym][(dfs[sym].index < cut) | (dfs[sym].index.date != cut.date())]   # no trades after 10:40
    clock["t"] = clock["t"].replace(hour=10, minute=46)
    bot.step()
    assert bot.status()["stale"] is None                         # last bar (10:35) ended 10:40: 6 min, still current
    with caplog.at_level(logging.WARNING, logger="swingbot"):
        clock["t"] = clock["t"].replace(hour=10, minute=58)
        bot.step()
    st = bot.status()["stale"]
    assert st and st["symbol"] == sym and st["since"] == "10:40" and st["minutes"] == 18
    assert any("No new" in r.getMessage() for r in caplog.records)
    assert "no trades on the feed" in bot.status()["watch"][0]["note"]
    clock["t"] = clock["t"].replace(hour=16, minute=30)
    assert bot.status()["stale"] is None                          # after the close: not a problem
