"""Restarting the bot during the day carries on from where it was."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from swingbot import alpaca_live, live
from swingbot.alpaca_live import AlpacaBot, AlpacaClients
from swingbot.config import Config
from swingbot.data import synthetic
from test_alpaca import FakeData, FakeTrading

ET = ZoneInfo("America/New_York")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(alpaca_live._time, "sleep", lambda s: None)
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    df = synthetic("AAPL", days=6)
    day = sorted(set(df.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 5, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    monkeypatch.setattr(alpaca_live, "now_et", lambda: clock["t"])
    t = FakeTrading(cash=100_000.0, price=50.0)
    data = FakeData(df)

    def new_bot(**cfg):
        b = AlpacaBot(AlpacaClients(t, data, "iex", True), Config(broker="alpaca", watchlist=["AAPL"], **cfg),
                      live=True, workdir=tmp_path)
        b.forced_symbol = "AAPL"
        return b
    return new_bot, t, clock


def test_restart_takes_over_the_open_position(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    settled = a.settled_start
    clock["t"] = clock["t"].replace(hour=10)
    a.enter(50.0, 49.0, "bounce")
    assert a.pos and a.trades_today == 1 and a.spent_today > 0
    spent = a.spent_today

    t.cash += 20_000                       # proceeds of a sale today: must NOT become spendable after a restart
    clock["t"] = clock["t"].replace(hour=11)
    b = new_bot()                          # the restarted program: a fresh object, same folder
    b.prepare()
    assert b.symbol == "AAPL" and "resuming" in b.pick_reason
    assert b.pos and b.pos["shares"] == a.pos["shares"] and b.pos["stop"] == 49.0
    assert b.trades_today == 1 and b.settled_start == settled and b.spent_today == spent
    assert b._my_stop_order() is not None  # it recognises its own stop again
    b.raise_stop(49.5)                     # and can trail it
    assert [float(o.stop_price) for o in t.open_stops()] == [49.5]
    b.flatten("end of day")                # and sells it at 15:50
    assert t.pos["AAPL"] == 0 and b.pos is None


def test_stop_hit_while_the_bot_was_off_is_recorded(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    clock["t"] = clock["t"].replace(hour=10)
    a.enter(50.0, 49.0, "bounce")
    t.trigger_stop(48.97)                   # the broker's stop fired while the PC was off
    clock["t"] = clock["t"].replace(hour=12)
    b = new_bot()
    b.prepare()
    b._check_live_exit()
    assert b.pos is None and b.trades_today == 1
    assert b.closed[-1]["exit"] == 48.97 and b.closed[-1]["reason"] == "stop"


def test_flat_restart_keeps_the_day_counts(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    a.trades_today, a.halted = 2, True
    a.save_state()
    b = new_bot()
    b.prepare()
    assert b.trades_today == 2 and b.halted and b.pos is None


def test_dry_run_state_is_not_used_by_a_live_run(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.live = False
    a.prepare()
    a.trades_today = 2
    a.save_state()
    b = new_bot()
    b.prepare()
    assert b.trades_today == 0


def test_yesterdays_state_is_ignored(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    a.trades_today = 2
    a.save_state()
    path = a._state_path()
    st = json.loads(path.read_text())
    st["date"] = "2000-01-01"
    path.write_text(json.dumps(st))
    b = new_bot()
    b.prepare()
    assert b.trades_today == 0


def test_paper_stop_in_missed_bars_counts(tmp_path, monkeypatch):
    """Dry run restarted at 11:00 after an entry at 10:00: a stop hit at 10:05 is still found."""
    df = synthetic("AAA", days=3)
    day = sorted(set(df.index.date))[-1]

    class C:
        def get_bars(self, symbol, period, aggregation=None, **kw):
            from types import SimpleNamespace
            d = df[df.index + pd.Timedelta(minutes=5) <= pd.Timestamp(clock["t"])]
            if period.name == "DAY":
                d = d[d.index.date == day]
            bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                    volume=r.volume) for ts, r in d.iterrows()]
            return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))
    clock = {"t": datetime(day.year, day.month, day.day, 10, 1, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    bot = live.Bot(C(), Config(watchlist=["AAA"]), live=False, workdir=tmp_path)
    bot.forced_symbol = "AAA"
    bot.prepare()
    bot.step()                                          # sees the 9:55 bar
    bot.pos = dict(kind="bounce", entry=1e9, init_stop=1e9, stop=1e9, target=None, shares=10, signal_ts=None,
                   entry_x=5.0, entry_clock="10:00", stop_steps=[[5.0, 1e9]])   # any bar's low is under this stop
    bot.trades_today = 1
    bot.save_state()
    clock["t"] = clock["t"].replace(hour=11)
    b2 = live.Bot(C(), Config(watchlist=["AAA"]), live=False, workdir=tmp_path)
    b2.forced_symbol = "AAA"
    b2.prepare()
    assert b2.pos is not None
    b2.step()
    assert b2.pos is None and b2.trades_today == 1
    exit_bar_x = b2.closed[-1]["exit_x"]
    assert exit_bar_x == live.x_of(pd.Timestamp(datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET)), 5)
