"""Morning scan downloads several symbols at once."""
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from swingbot import live
from swingbot.alpaca_live import AlpacaBot, AlpacaClients
from swingbot.config import Config
from swingbot.data import synthetic


class SlowClient:
    """Public-style fake: each bars request takes 50 ms; records how many run at once."""

    def __init__(self, df, bad=()):
        self.df, self.bad, self.lock = df, set(bad), threading.Lock()
        self.active = self.peak = 0
        self.order = []

    def get_bars(self, symbol, period, aggregation=None, **kw):
        with self.lock:
            self.order.append(symbol)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.05)
            if symbol in self.bad:
                raise RuntimeError("unknown symbol")
            bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                    volume=r.volume) for ts, r in self.df.tail(20).iterrows()]
            return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))
        finally:
            with self.lock:
                self.active -= 1


def test_downloads_ten_at_a_time_and_skips_failures(tmp_path):
    syms = [f"S{i:03d}" for i in range(101)]
    c = SlowClient(synthetic("AAA", days=1), bad={"S050"})
    bot = live.Bot(c, Config(scan_workers=10), live=False, workdir=tmp_path)
    t0 = time.monotonic()
    got = bot.fetch_many(syms, "WEEK")
    took = time.monotonic() - t0
    assert c.order[0] == "S000"                          # first request alone (sign-in happens once)
    assert c.peak == 10                                  # never more than scan_workers at once
    assert took < 1.5                                    # one at a time would take 5 s
    assert list(got) == [s for s in syms if s != "S050"] # failure skipped, watchlist order kept
    assert all(len(df) == 20 for df in got.values())


def test_one_worker_is_sequential(tmp_path):
    c = SlowClient(synthetic("AAA", days=1))
    bot = live.Bot(c, Config(scan_workers=1), live=False, workdir=tmp_path)
    bot.fetch_many(["A", "B", "C", "D"], "WEEK")
    assert c.peak == 1


def test_scan_workers_setting_is_checked():
    Config(scan_workers=1).validate()
    with pytest.raises(ValueError):
        Config(scan_workers=0).validate()
    with pytest.raises(ValueError):
        Config(scan_workers=100).validate()


class BatchData:
    """Alpaca-style fake: multi-symbol requests; any request containing BAD fails."""

    def __init__(self, df):
        self.df, self.requests, self.lock = df, [], threading.Lock()

    def get_stock_bars(self, req):
        syms = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
        with self.lock:
            self.requests.append((str(req.timeframe), list(syms)))
        if "BAD" in syms:
            raise RuntimeError("invalid symbol: BAD")
        d = self.df.tail(30).tz_convert("UTC")
        if str(req.timeframe) == "1Day":
            d = d.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                      "volume": "sum"}).dropna()
        return SimpleNamespace(df=pd.concat({s: d for s in syms}, names=["symbol", "timestamp"]))


def test_alpaca_asks_for_100_symbols_per_request(tmp_path, monkeypatch):
    data = BatchData(synthetic("AAA", days=2))
    bot = AlpacaBot(AlpacaClients(None, data, "iex", True), Config(broker="alpaca"), live=False, workdir=tmp_path)
    syms = [f"S{i:03d}" for i in range(250)] + ["BAD"]
    got = bot.fetch_many(syms, "WEEK")
    batches = [r for r in data.requests if len(r[1]) > 1]
    assert sorted(len(r[1]) for r in batches) == [51, 100, 100]     # 3 requests for 251 symbols
    assert "BAD" not in got and len(got) == 250                      # the bad batch was retried one by one
    assert list(got)[:3] == ["S000", "S001", "S002"] and len(got["S123"]) == 30
    data.requests.clear()
    liq = bot.liquidity([f"S{i:03d}" for i in range(450)])
    assert [len(r[1]) for r in data.requests] == [200, 200, 50] and len(liq) == 450


def test_watchlist_file_replaces_the_watchlist_line(tmp_path):
    from swingbot.config import read_watchlist
    (tmp_path / "config.yaml").write_text("watchlist: [AAPL, MSFT]\nwatchlist_file: r2k.yaml\n")
    (tmp_path / "r2k.yaml").write_text("# Russell 2000 (test)\nwatchlist: [BTSG, MOG.A,\n            ON, NO]\n")
    cfg = Config.load(tmp_path / "config.yaml")
    assert cfg.watchlist == ["BTSG", "MOG.A", "ON", "NO"]             # ON / NO stay tickers, not true/false
    (tmp_path / "plain.txt").write_text("aapl msft\nnvda, AMD\n- TSLA  # a comment\n")
    assert read_watchlist(tmp_path / "plain.txt") == ["AAPL", "MSFT", "NVDA", "AMD", "TSLA"]
    (tmp_path / "config.yaml").write_text("watchlist_file: missing.yaml\n")
    with pytest.raises(ValueError, match="not found"):
        Config.load(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text('watchlist: [AAPL]\nwatchlist_file: ""\n')
    assert Config.load(tmp_path / "config.yaml").watchlist == ["AAPL"]


def test_shipped_lists_load():
    from pathlib import Path
    from swingbot.config import read_watchlist
    sp = read_watchlist(Path(__file__).parent.parent / "watchlist_sp100.yaml")
    assert len(sp) == 100 and "BRK.B" in sp and "GOOG" not in sp
    Config.load(Path(__file__).parent.parent / "config.yaml")
