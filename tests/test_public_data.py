"""broker: alpaca with price_data: public - Public's bars and quotes, Alpaca's orders."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import alpaca_live, live
from swingbot.alpaca_live import AlpacaBot, AlpacaClients
from swingbot.config import Config
from swingbot.data import resample_bars, synthetic
from test_alpaca import FakeTrading

ET = ZoneInfo("America/New_York")


AGG = {"FIVE_MINUTES": 5, "TEN_MINUTES": 10, "FIFTEEN_MINUTES": 15, "THIRTY_MINUTES": 30}


class FakePublic:
    """Public-style client that behaves like the real one on 2026-10-22: no 1-minute bars for WEEK,
    and 401 Unauthorized for a request when asked too fast (here: every 7th request, once)."""

    def __init__(self, df1, clock, burst_401=False):
        self.df1, self.clock, self.asked, self.closed = df1, clock, [], False
        self.burst_401, self.n = burst_401, 0
        self.auth_manager = SimpleNamespace(revoke_current_token=lambda: None, refresh_token_if_needed=lambda: None)

    def get_bars(self, symbol, period, aggregation=None, **kw):
        self.n += 1
        if self.burst_401 and self.n % 7 == 0:
            err = RuntimeError("API Error 401: Unauthorized")
            err.status_code = 401
            raise err
        self.asked.append((symbol, period.name, aggregation.name))
        if aggregation.name == "ONE_MINUTE" and period.name != "DAY":
            err = RuntimeError("API Error 400: Period WEEK is not compatible with aggregation ONE_MINUTE")
            err.status_code = 400
            raise err
        m = AGG[aggregation.name]
        d = self.df1[self.df1.index < pd.Timestamp(self.clock["t"])]
        if period.name == "DAY":
            d = d[d.index.date == self.clock["t"].date()]
        d = resample_bars(d, m)
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def get_quotes(self, instruments):
        return [SimpleNamespace(last=123.45)]

    def close(self):
        self.closed = True


class NoAlpacaData:
    def get_stock_bars(self, req):
        raise AssertionError("Alpaca's data feed must not be used with price_data: public")

    def get_stock_latest_trade(self, req):
        raise AssertionError("Alpaca's data feed must not be used with price_data: public")


def test_resample_one_minute_bars_into_three():
    df1 = synthetic("AAA", days=2, minutes=1)
    df3 = resample_bars(df1, 3)
    day = df3[df3.index.date == df3.index[0].date()]
    assert len(day) == 130 and day.index[0].strftime("%H:%M") == "09:30" and day.index[1].strftime("%H:%M") == "09:33"
    first = df1.iloc[:3]
    assert day.iloc[0].open == first.open.iat[0] and day.iloc[0].close == first.close.iat[-1]
    assert day.iloc[0].high == first.high.max() and day.iloc[0].volume == first.volume.sum()


def test_alpaca_orders_with_public_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(alpaca_live._time, "sleep", lambda s: None)
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    df1 = synthetic("AAPL", days=4, minutes=1)
    day = sorted(set(df1.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 10, 31, 5, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    monkeypatch.setattr(alpaca_live, "now_et", lambda: clock["t"])
    pub, t = FakePublic(df1, clock), FakeTrading()
    clients = AlpacaClients(t, NoAlpacaData(), "iex", True, public=pub)
    bot = AlpacaBot(clients, Config(broker="alpaca", price_data="public", bar_minutes=5, watchlist=["AAPL"]),
                    live=True, workdir=tmp_path)
    today = bot.fetch_bars("AAPL", "DAY")
    assert pub.asked[-1] == ("AAPL", "DAY", "FIVE_MINUTES")
    assert today.index[-1].strftime("%H:%M") == "10:30"
    done = live.complete_bars(today, clock["t"], 5)
    assert done.index[-1].strftime("%H:%M") == "10:25"           # the 10:30 bar is still forming
    assert bot.bar_sizes() == [5, 10, 15, 30]
    assert bot.liquidity(["AAPL"]) is None                       # Public's bars carry full-market volume
    got = bot.fetch_many(["AAPL", "AAPL"], "WEEK")
    assert list(got) == ["AAPL"] and len(got["AAPL"]) > 100
    bot.symbol = "AAPL"
    assert bot._last_trade_price() == 123.45
    assert "Public" in bot.data_feed_note
    bot.snapshot_settled_cash()
    bot.enter(50.0, 49.0, "bounce")                              # the order still goes to Alpaca
    assert t.sent and bot.pos
    clients.close()
    assert pub.closed


def test_price_data_setting_is_checked():
    Config(broker="alpaca", price_data="public").validate()
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "c.yaml").write_text("broker: public\nprice_data: public\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(d / "c.yaml")
    (d / "c.yaml").write_text("broker: alpaca\nprice_data: yahoo\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(d / "c.yaml")


def test_public_bursts_are_paced_and_retried(tmp_path, monkeypatch):
    """2026-10-22: 300 parallel requests got '401 Unauthorized' from Public after ~25 in a second.
    Requests are now paced and a 401/429 is retried after a pause."""
    from swingbot import data
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    data.set_public_rate(1000)
    df1 = synthetic("AAPL", days=3, minutes=1)
    day = sorted(set(df1.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 11, 0, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    pub = FakePublic(df1, clock, burst_401=True)
    bot = live.Bot(pub, Config(watchlist=["AAPL"], scan_workers=15), live=False, workdir=tmp_path)
    syms = [f"S{i}" for i in range(40)]
    pub.df1 = df1
    got = bot.fetch_many(syms, "WEEK")
    assert len(got) == 40                                         # every 401 was retried successfully
    assert any(s >= 1.0 for s in sleeps)


def test_rate_limiter_spaces_calls():
    import time
    from swingbot.data import RateLimiter
    rl = RateLimiter(50)
    t0 = time.monotonic()
    for _ in range(11):
        rl.wait()
    assert time.monotonic() - t0 >= 0.19                          # 10 gaps of 20 ms
