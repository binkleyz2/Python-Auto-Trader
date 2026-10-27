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
    st = json.loads(path.read_text(encoding="utf-8"))
    st["date"] = "2000-01-01"
    path.write_text(json.dumps(st), encoding="utf-8")
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


def test_yesterdays_unsold_position_is_reported(tmp_path, monkeypatch):
    """2026-10-22: LIVN was left open overnight. Next morning the bot must say so loudly."""
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    clock["t"] = clock["t"].replace(hour=10)
    a.enter(50.0, 49.0, "bounce")                       # bought, then never sold
    clock["t"] = clock["t"] + pd.Timedelta(days=1)
    b = new_bot()
    b.prepare()
    assert b.leftover and b.leftover["symbol"] == "AAPL" and b.leftover["shares"] == a.pos["shares"]
    assert b.status()["leftover"]["date"] == str((clock["t"] - pd.Timedelta(days=1)).date())
    assert b.pos is None                                # the bot doesn't adopt it


def test_exit_price_comes_from_this_trades_own_orders(tmp_path, monkeypatch):
    """2026-10-26: BUSE's exit was read from EVRG's earlier sale (84.32), showing +$41,741 on one trade."""
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot(max_trades_per_day=4)
    a.prepare()
    clock["t"] = clock["t"].replace(hour=11)
    t.price = 84.30
    a.enter(84.30, 83.90, "bounce")
    t.trigger_stop(84.32)                                   # first trade's stop fills at 84.32
    a._check_live_exit()
    assert a.closed[-1]["exit"] == 84.32
    clock["t"] = clock["t"].replace(hour=14)
    t.price = 30.46
    a.enter(30.47, 30.39, "bounce")
    assert set(a.pos["ids_before"]) and set(a.pos["ids_before"]) <= set(map(str, a.my_order_ids))
    b = new_bot(max_trades_per_day=4)                      # survives a restart too
    b.prepare()
    t.trigger_stop(30.40)
    b._check_live_exit()
    assert b.closed[-1]["exit"] == 30.40
    assert abs(b.closed[-1]["pnl"] - (30.40 - 30.46) * b.closed[-1]["shares"]) < 0.01


def test_a_sale_price_that_cannot_be_right_is_not_used(tmp_path, monkeypatch, caplog):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    clock["t"] = clock["t"].replace(hour=11)
    t.price = 30.46
    a.enter(30.47, 30.39, "bounce")
    t.trigger_stop(30.40)
    a.last_close = 30.41
    monkeypatch.setattr(a, "_filled_sell_price", lambda ids: 84.32)
    a._check_live_exit()
    assert a.closed[-1]["exit"] == 30.41 and "doesn't fit" in caplog.text


def test_restart_takes_over_an_open_short(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot(direction="short")
    monkeypatch.setattr(a, "_last_trade_price", lambda: t.price)
    a.prepare()
    clock["t"] = clock["t"].replace(hour=10)
    a.enter(50.0, 51.0, "short bounce", side=-1)
    assert t.pos["AAPL"] < 0
    b = new_bot(direction="short")
    b.prepare()
    assert b.pos["side"] == -1 and b.bot_shares() == a.pos["shares"]
    b.raise_stop(50.5)
    assert [float(o.stop_price) for o in t.open_stops()] == [50.5]
    b.flatten("end of day")
    assert t.pos["AAPL"] == 0 and b.closed[-1]["side"] == -1


def test_yesterdays_unclosed_short_is_reported(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot(direction="short")
    monkeypatch.setattr(a, "_last_trade_price", lambda: t.price)
    a.prepare()
    clock["t"] = clock["t"].replace(hour=10)
    a.enter(50.0, 51.0, "short bounce", side=-1)
    clock["t"] = clock["t"] + pd.Timedelta(days=1)
    b = new_bot()
    b.prepare()
    assert b.leftover and b.leftover["shares"] == a.pos["shares"] and b.leftover["side"] == -1
