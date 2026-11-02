"""Backtests on months of Alpaca history, the watch-list engine, and side-by-side comparisons."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from swingbot import backtest as bt
from swingbot.config import Config
from swingbot.data import synthetic
from swingbot.history_data import load_history
from swingbot.scanner import scan

ET = ZoneInfo("America/New_York")


class FakeAlpacaData:
    def __init__(self, frames):
        self.frames, self.requests = frames, []

    def get_stock_bars(self, req):
        syms = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
        utc = lambda x: pd.Timestamp(x).tz_localize("UTC") if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)
        start, end = utc(req.start), utc(req.end)
        self.requests.append((list(syms), start, end, str(req.feed)))
        parts = {}
        for s in syms:
            d = self.frames[s].tz_convert("UTC")
            d = d[(d.index >= start) & (d.index <= end)]
            if len(d):
                parts[s] = d
        if not parts:
            return SimpleNamespace(df=pd.DataFrame())
        return SimpleNamespace(df=pd.concat(parts, names=["symbol", "timestamp"]))


def test_history_is_downloaded_once_then_only_new_days(tmp_path):
    full = {s: synthetic(s, days=40, start="2026-08-03", seed=k) for k, s in enumerate(["AAA", "BBB", "CCC"])}
    upto = lambda day: {s: df[df.index.date <= pd.Timestamp(day).date()] for s, df in full.items()}
    client = FakeAlpacaData(upto("2026-09-11"))
    now = datetime(2026, 9, 12, 9, 0, tzinfo=ET)
    got = load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=now)
    assert set(got) == {"AAA", "BBB", "CCC"} and "sip" in client.requests[0][3].lower()
    assert got["AAA"].index.min().date() >= pd.Timestamp("2026-08-13").date()
    assert (tmp_path / "AAA.csv.gz").exists()
    first = len(client.requests)
    client.frames = upto("2026-09-18")                       # a week later: only the new days are asked for
    got2 = load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=datetime(2026, 9, 19, 9, 0, tzinfo=ET))
    new = client.requests[first:]
    assert new and all(r[1] >= pd.Timestamp("2026-09-11", tz="UTC") for r in new)
    assert got2["AAA"].index.max().date() == pd.Timestamp("2026-09-18").date()
    pd.testing.assert_frame_equal(got2["AAA"][got2["AAA"].index.date <= pd.Timestamp("2026-09-11").date()],
                                  got["AAA"][got["AAA"].index >= got2["AAA"].index.min()], check_freq=False)
    n = len(client.requests)
    load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=datetime(2026, 9, 19, 9, 5, tzinfo=ET))
    assert len(client.requests) == n                          # up to date: nothing downloaded


def test_fast_backtest_scan_ranks_exactly_like_the_live_scan():
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=16, seed=i) for i in range(25)}
    cfg = Config(direction="both", min_avg_dollar_volume=0)
    prep = bt.prepare(hist, cfg)
    days = sorted(set(next(iter(prep.values()))[3]))
    for k in range(3, len(days)):
        day, ws = days[k], days[max(0, k - 10)]
        live = {s: h[(np.array(h.index.date) >= ws) & (np.array(h.index.date) < day)] for s, h in hist.items()}
        pre, prior = {}, {}
        for s, (d, lo, hi, dates, md) in prep.items():
            a, b = np.searchsorted(dates, ws), np.searchsorted(dates, day)
            prior[s] = d.iloc[a:b]
            pre[s] = (prior[s], lo[a:b], hi[a:b], md.iloc[a:b])
        assert [(r.symbol, round(r.score, 9)) for r in scan(live, cfg)] == \
               [(r.symbol, round(r.score, 9)) for r in scan(prior, cfg, pre=pre)]


def test_watch_engine_follows_the_live_rules():
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=30, seed=i) for i in range(30)}
    cfg = Config(direction="both", min_avg_dollar_volume=0, watch_count=10, max_trades_per_day=3)
    trades, curve, picks = bt.run(hist, cfg)
    assert len(trades) > 20
    per_day = trades.groupby("date").size()
    assert per_day.max() <= 3                                # trades per day
    for _, g in trades.groupby("date"):                      # one position at a time
        spans = sorted(zip(g.entry_time, g.exit_time))
        assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    assert trades["rank"].max() > 1 and trades["rank"].max() <= 10       # not only the top pick trades
    assert all(len(p["watched"]) <= 10 for p in picks)
    assert (trades.entry_time >= "09:45").all() and (trades.exit_time <= "15:50").all()
    one = bt.run(hist, Config(direction="both", min_avg_dollar_volume=0, watch_count=1, max_trades_per_day=3))[0]
    assert (one["rank"] == 1).all()
    tables = bt.breakdown(trades)
    assert set(tables) == {"By setup", "By entry hour", "By exit", "By month"}
    assert set(tables["By setup"].index) <= {"long bounce", "long breakout", "short bounce", "short breakdown"}
    assert tables["By setup"].trades.sum() == len(trades)


def test_compare_runs_each_combination(tmp_path, monkeypatch, capsys):
    from swingbot import __main__ as m
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=14, seed=i) for i in range(12)}
    cfg = Config(min_avg_dollar_volume=0, direction="both")
    m._compare(hist, cfg, ["breakeven_at_r=1,1.5", "trail_buffer_atr=0.25,0.4"], tmp_path)
    table = pd.read_csv(tmp_path / "compare.csv", index_col=0)
    assert len(table) == 4 and "breakeven_at_r=1.5, trail_buffer_atr=0.4" in table.index
    assert (tmp_path / "report-breakeven_at_r1.5-trail_buffer_atr0.4.txt").exists()
    with pytest.raises(SystemExit):
        m._compare(hist, cfg, ["nonsense=1,2"], tmp_path)
