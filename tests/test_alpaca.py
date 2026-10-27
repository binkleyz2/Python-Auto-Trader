"""Alpaca broker layer against a fake Alpaca that behaves like the real API: brackets create a
held stop leg + target leg, cancelling one leg cancels its sibling, replacing an order gives it
a new id, and 'no position' is a 404 APIError. Requests are real alpaca-py objects, so their
shape is validated by the SDK itself."""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from alpaca.common.exceptions import APIError

from swingbot import alpaca_live, live
from swingbot.alpaca_live import AlpacaBot, AlpacaClients
from swingbot.config import Config
from swingbot.data import synthetic

UTC = ZoneInfo("UTC")


def _v(x):
    return str(getattr(x, "value", x)).lower()


class FakeTrading:
    def __init__(self, cash=100_000.0, price=50.0, held=0):
        self.cash, self.price = cash, price
        self.pos = {"AAPL": held}
        self.orders = {}            # id -> order (top level)
        self.all = {}               # id -> order (incl. legs)
        self.sent = []
        self.stops_seen = []        # open stop orders after each change while holding
        self.fail_position = False
        self.shorting_enabled, self.shortable, self.easy_to_borrow = True, True, True

    def _mk(self, side, typ, qty, status="new", stop=None, limit=None, top=True):
        o = SimpleNamespace(id=uuid.uuid4(), symbol="AAPL", side=side, order_type=typ, type=typ, qty=str(qty),
                            status=status, stop_price=stop, limit_price=limit, filled_qty="0",
                            filled_avg_price=None, created_at=datetime.now(UTC), legs=[], parent=None)
        self.all[str(o.id)] = o
        if top:
            self.orders[str(o.id)] = o
        return o

    def open_stops(self):
        return [o for o in self.all.values() if o.order_type == "stop" and o.status in ("new", "held")]

    def _note(self):
        if self.pos["AAPL"]:
            self.stops_seen.append(len(self.open_stops()))

    # --- API used by the bot
    def submit_order(self, req):
        self.sent.append(req)
        side, qty = _v(req.side), int(req.qty)
        typ = _v(req.type)
        if typ == "market":
            o = self._mk(side, "market", qty, status="filled")
            o.filled_qty, o.filled_avg_price = str(qty), str(self.price)
            self.pos["AAPL"] += qty if side == "buy" else -qty
            if req.order_class is not None and _v(req.order_class) == "bracket":
                ex = "sell" if side == "buy" else "buy"         # a short's legs buy back
                tp = self._mk(ex, "limit", qty, status="new", limit=req.take_profit.limit_price, top=False)
                sl = self._mk(ex, "stop", qty, status="held", stop=req.stop_loss.stop_price, top=False)
                tp.parent = sl.parent = o
                o.legs = [tp, sl]
        else:
            o = self._mk(side, typ, qty, stop=getattr(req, "stop_price", None))
        self._note()
        return o

    def get_asset(self, sym):
        return SimpleNamespace(symbol=sym, shortable=self.shortable, easy_to_borrow=self.easy_to_borrow)

    def get_order_by_id(self, oid):
        return self.all[str(oid)]

    def get_orders(self, req):
        assert req.nested and req.symbols == ["AAPL"]
        return list(self.orders.values())

    def cancel_order_by_id(self, oid):
        o = self.all[str(oid)]
        o.status = "canceled"
        if o.parent:                                   # bracket legs are one-cancels-other
            for leg in o.parent.legs:
                if leg.status in ("new", "held"):
                    leg.status = "canceled"

    def replace_order_by_id(self, oid, req):
        old = self.all[str(oid)]
        new = self._mk(old.side, old.order_type, int(old.qty), status=old.status,
                       stop=getattr(req, "stop_price", None) or old.stop_price,
                       limit=getattr(req, "limit_price", None) or old.limit_price, top=False)
        new.parent = old.parent
        if old.parent:
            old.parent.legs = [new if leg is old else leg for leg in old.parent.legs]
        else:
            self.orders[str(new.id)] = new
        old.status = "replaced"                        # new order exists before the old one is gone
        self._note()
        return new

    def get_open_position(self, sym):
        if self.fail_position:
            raise APIError(json.dumps({"code": 50010000, "message": "internal error"}))
        if not self.pos.get(sym):
            raise APIError(json.dumps({"code": 40410000, "message": "position does not exist"}))
        return SimpleNamespace(qty=str(self.pos[sym]))

    def get_account(self):
        return SimpleNamespace(equity="25000", cash=str(self.cash), non_marginable_buying_power=str(self.cash),
                               buying_power=str(self.cash * 4), multiplier="4", account_number="PA123",
                               shorting_enabled=self.shorting_enabled,
                               trading_blocked=False, account_blocked=False, trade_suspended_by_user=False)

    def trigger_stop(self, at):
        for o in self.open_stops():
            o.status, o.filled_qty, o.filled_avg_price = "filled", o.qty, str(at)
            self.pos["AAPL"] += int(o.qty) if o.side == "buy" else -int(o.qty)
            for leg in (o.parent.legs if o.parent else []):
                if leg is not o and leg.status in ("new", "held"):
                    leg.status = "canceled"


class FakeData:
    def __init__(self, df):
        self.df = df
        self.now = None

    def get_stock_bars(self, req):
        ts = lambda x: pd.Timestamp(x).tz_localize("UTC") if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)
        d = self.df[self.df.index < ts(req.end)]
        d = d[d.index >= ts(req.start)]
        if str(req.timeframe) == "1Day":
            d = d.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                      "volume": "sum"}).dropna()
            d["volume"] *= 40                         # full-market volume >> one-exchange volume
        syms = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
        frames = {s: d.tz_convert("UTC") for s in syms}
        return SimpleNamespace(df=pd.concat(frames, names=["symbol", "timestamp"]) if len(d) else pd.DataFrame())

    def get_stock_latest_trade(self, req):
        return {req.symbol_or_symbols: SimpleNamespace(price=float(self.df.close.iat[-1]))}


def _bot(tmp_path, monkeypatch, trading=None, live_mode=True, **cfg):
    monkeypatch.setattr(alpaca_live._time, "sleep", lambda s: None)
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    t = trading or FakeTrading()
    clients = AlpacaClients(t, FakeData(synthetic("AAPL", days=6)), "iex", True)
    bot = AlpacaBot(clients, Config(broker="alpaca", **cfg), live=live_mode, workdir=tmp_path)
    bot.symbol = "AAPL"
    if live_mode:
        bot.snapshot_settled_cash()
    return bot, t


def test_entry_is_bracket_with_stop_and_parked_target(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    req = t.sent[0]
    assert _v(req.order_class) == "bracket" and _v(req.side) == "buy" and int(req.qty) == 125
    assert req.stop_loss.stop_price == 49.0
    assert req.take_profit.limit_price == 75.0                   # parked 50% above: effectively no target
    assert bot.pos["target"] is None and bot.bot_shares() == 125
    assert len(t.sent) == 1                                      # stop leg found; no extra stop placed


def test_trailing_reprices_bracket_stop_leg_without_gap(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    bot.raise_stop(49.60)
    bot.raise_stop(50.25)
    stops = t.open_stops()
    assert len(stops) == 1 and stops[0].stop_price == 50.25
    assert min(t.stops_seen) >= 1                                # never unprotected while holding
    t.trigger_stop(at=50.25)
    bot._check_live_exit()
    assert bot.pos is None and bot.closed[-1]["reason"] == "trail" and bot.closed[-1]["exit"] == 50.25


def test_flatten_sells_only_bot_shares_and_cancels_only_bot_orders(tmp_path, monkeypatch):
    t = FakeTrading(held=100)
    yours = t._mk("sell", "limit", 100, limit=80.0)               # your own open order on the same stock
    bot, t = _bot(tmp_path, monkeypatch, trading=t)
    bot.enter(50.0, 49.0, "bounce")
    bot.raise_stop(49.5)
    bot.flatten("end of day")
    sells = [r for r in t.sent if _v(r.side) == "sell" and _v(r.type) == "market"]
    assert len(sells) == 1 and int(sells[0].qty) == 125
    assert t.pos["AAPL"] == 100 and yours.status == "new"
    assert not t.open_stops()


def test_settled_cash_limit_and_no_reuse_of_sale_proceeds(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch, trading=FakeTrading(cash=1000))
    bot.enter(50.0, 49.0, "bounce")
    assert int(t.sent[0].qty) == 19                              # 98% of $1,000 cash, margin ignored
    t.trigger_stop(at=52.0)
    bot._check_live_exit()
    t.cash = 1050                                                # sale proceeds show up as cash
    bot.enter(50.0, 49.0, "bounce")
    assert sum(1 for r in t.sent if _v(r.side) == "buy") == 1


def test_position_lookup_error_is_not_mistaken_for_a_stop_out(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    t.fail_position = True
    with pytest.raises(APIError):
        bot._check_live_exit()
    assert bot.pos is not None                                   # still managing the trade


def test_bars_and_free_plan_volume_screen(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch, live_mode=False)
    df = bot.client.data.df
    last_day = sorted(set(df.index.date))[-1]
    monkeypatch.setattr(alpaca_live, "now_et", lambda: datetime(last_day.year, last_day.month, last_day.day,
                                                                14, 0, tzinfo=ZoneInfo("America/New_York")))
    week = bot.fetch_bars("AAPL", "WEEK")
    assert list(week.columns) == ["open", "high", "low", "close", "volume"] and len(week) > 200
    assert str(week.index.tz) == "America/New_York"
    liq = bot.liquidity(["AAPL"])
    assert liq["AAPL"] > 30 * (week["close"] * week["volume"]).groupby(week.index.date).sum().mean()


def test_dry_run_sends_no_orders(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch, live_mode=False)
    bot.enter(50.0, 49.0, "bounce")
    bot.raise_stop(49.5)
    bot.flatten("end of day")
    assert t.sent == [] and bot.pos is None


def test_full_live_session_against_fake_alpaca(tmp_path, monkeypatch):
    """A whole day in live mode: fills follow the bars, the fake triggers stops when a bar trades
    through them, and the bot must end flat with a stop in place whenever it held shares."""
    monkeypatch.setattr(alpaca_live._time, "sleep", lambda s: None)
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    df = synthetic("AAA", days=8)                        # a day known to produce a loss, a trail and a win
    day = sorted(set(df.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ZoneInfo("America/New_York"))}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    monkeypatch.setattr(alpaca_live, "now_et", lambda: clock["t"])
    t = FakeTrading(cash=25_000)
    data = FakeData(df)
    bot = AlpacaBot(AlpacaClients(t, data, "iex", True), Config(broker="alpaca", watchlist=["AAPL"]),
                    live=True, workdir=tmp_path)
    bot.snapshot_settled_cash()
    bot.symbol = "AAPL"                                  # the scan itself is tested separately
    bot.history = df[df.index.date < day]
    now = clock["t"]
    while now.time() < live._t(bot.cfg.flatten_time):
        clock["t"] = now
        seen = df[df.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
        if len(seen):
            bar = seen.iloc[-1]
            t.price = float(bar.close)
            for o in t.open_stops():                     # broker-side stop execution
                if bar.low <= float(o.stop_price):
                    t.trigger_stop(at=float(o.stop_price))
        bot.step()
        now += pd.Timedelta(minutes=5)
    clock["t"] = now
    bot.flatten("end of day")
    assert t.pos["AAPL"] == 0 and bot.pos is None
    assert all(n >= 1 for n in t.stops_seen)             # never held shares without a stop
    buys = [r for r in t.sent if _v(r.side) == "buy"]
    assert len(buys) == bot.trades_today >= 1
    assert any(c["reason"] == "trail" for c in bot.closed)     # the trailing stop was repriced at the broker
    for c in bot.closed:                                       # exits recorded at the broker's actual fill
        if c["reason"] in ("stop", "trail"):
            assert any(o.status == "filled" and o.order_type == "stop" and float(o.filled_avg_price) == c["exit"]
                       for o in t.all.values())
    assert any(_v(o.status) == "replaced" for o in t.all.values())


def test_alpaca_requests_one_minute_bars(tmp_path, monkeypatch):
    seen = []
    bot, t = _bot(tmp_path, monkeypatch, live_mode=False, bar_minutes=1)
    orig = bot.client.data.get_stock_bars
    bot.client.data.get_stock_bars = lambda req: (seen.append(str(req.timeframe)), orig(req))[1]
    bot.client.data.df = synthetic("AAPL", days=3, minutes=1)
    bot.fetch_bars("AAPL", "WEEK")
    assert seen == ["1Min"]


def test_entry_skipped_when_price_already_fell_to_the_stop(tmp_path, monkeypatch):
    """2026-10-20: signal close 27.52, stop 27.50, but the price was 27.495 when the order went out;
    Alpaca refused the bracket. Now the bot checks the price first and skips the trade."""
    bot, t = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot, "_last_trade_price", lambda: 49.005)
    bot.enter(50.0, 49.0, "breakout")
    assert t.sent == [] and bot.pos is None and bot.trades_today == 0


def test_refused_entry_is_not_counted_and_bot_carries_on(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)

    def refuse(req):
        raise RuntimeError('{"code":42210000,"message":"stop_loss.stop_price must be <= base_price - 0.01"}')
    monkeypatch.setattr(t, "submit_order", refuse)
    bot.enter(50.0, 49.0, "breakout")                         # must not raise
    assert bot.pos is None and bot.trades_today == 0


def test_entry_error_after_the_order_went_through_adopts_the_shares(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    real = t.submit_order

    def timeout_after_send(req):
        real(req)
        raise TimeoutError("read timed out")
    monkeypatch.setattr(t, "submit_order", timeout_after_send)
    bot.enter(50.0, 49.0, "bounce")
    assert bot.pos and bot.pos["shares"] == 125 and bot.trades_today == 1
    assert len(t.open_stops()) == 1                           # still protected by the bracket's stop


def test_bot_follows_a_stop_raised_by_hand(tmp_path, monkeypatch):
    """2026-10-21: the stop was raised by hand in Alpaca's app. The bot must adopt it and never lower it."""
    bot, t = _bot(tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    leg = t.open_stops()[0]
    t.replace_order_by_id(leg.id, SimpleNamespace(stop_price=49.80))       # you, in Alpaca's app
    bot.sync_stop_from_broker()
    assert bot.pos["stop"] == 49.80 and bot.pos["stop_steps"][-1][1] == 49.80
    bot.raise_stop(49.50)                                                  # a lower swing-low stop: ignored
    assert [float(o.stop_price) for o in t.open_stops()] == [49.80]
    bot.raise_stop(49.90)                                                  # higher: the bot moves it
    assert [float(o.stop_price) for o in t.open_stops()] == [49.90] and bot.pos["stop"] == 49.90
    t.replace_order_by_id(t.open_stops()[0].id, SimpleNamespace(stop_price=49.70))   # lowered by hand
    bot.sync_stop_from_broker()
    assert bot.pos["stop"] == 49.90                                        # the bot's own level stands


class SlowCancelTrading(FakeTrading):
    """Like 2026-10-22 on the paper account: cancelled legs sit in 'pending_cancel' for a while, the second
    leg's cancel errors, and a sell is refused while shares are still held by those orders."""

    def __init__(self, *a, pending_polls=40, **k):
        super().__init__(*a, **k)
        self.pending_polls, self.sell_refusals = pending_polls, 0

    def cancel_order_by_id(self, oid):
        o = self.all[str(oid)]
        if o.status == "pending_cancel":
            raise APIError(json.dumps({"code": 42210000, "message": "order pending cancel"}))
        legs = o.parent.legs if o.parent else [o]
        for leg in legs:
            if leg.status in ("new", "held"):
                leg.status = "pending_cancel"

    def get_orders(self, req):
        pend = [o for o in self.all.values() if o.status == "pending_cancel"]
        if pend:
            self.pending_polls -= 1
            if self.pending_polls <= 0:
                for o in pend:
                    o.status = "canceled"
        return super().get_orders(req)

    def submit_order(self, req):
        if _v(req.side) == "sell" and any(o.status == "pending_cancel" for o in self.all.values()):
            self.sell_refusals += 1
            raise APIError(json.dumps({"code": 40310000, "message": "insufficient qty available for order"}))
        return super().submit_order(req)


def test_flatten_survives_slow_cancels_and_refused_sells(tmp_path, monkeypatch):
    """2026-10-22 LIVN: at 15:50 the sell was refused while the stop leg was still 'pending cancel'; the
    error ended the session and 310 shares stayed open overnight with no stop."""
    t = SlowCancelTrading(pending_polls=40)
    bot, _ = _bot(tmp_path, monkeypatch, trading=t)
    bot.enter(50.0, 49.0, "bounce")
    bot.flatten("end of day")
    assert t.pos["AAPL"] == 0 and bot.pos is None
    assert t.sell_refusals >= 1                         # it was refused at least once, and retried


def test_flatten_failure_never_raises_and_is_reported(tmp_path, monkeypatch):
    t = SlowCancelTrading(pending_polls=10 ** 9)        # the cancels never finish
    bot, _ = _bot(tmp_path, monkeypatch, trading=t)
    bot.enter(50.0, 49.0, "bounce")
    bot.flatten("end of day")                           # must not raise
    assert t.pos["AAPL"] == 125 and bot.pos and bot.still_holding() == 125



# ---------------------------------------------------------------- shorts (direction: short / both)
def _sbot(tmp_path, monkeypatch, **kw):
    bot, t = _bot(tmp_path, monkeypatch, **kw)
    monkeypatch.setattr(bot, "_last_trade_price", lambda: t.price)   # the fake's price, not the bars'
    return bot, t

def test_short_entry_is_a_sell_bracket_with_buy_stop_above(tmp_path, monkeypatch):
    bot, t = _sbot(tmp_path, monkeypatch, direction="both")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    req = t.sent[0]
    assert _v(req.order_class) == "bracket" and _v(req.side) == "sell" and int(req.qty) == 125
    assert req.stop_loss.stop_price == 51.0 and req.take_profit.limit_price == 25.0   # parked 50% below
    assert t.pos["AAPL"] == -125 and bot.held_quantity() == -125 and bot.bot_shares() == 125
    assert bot.pos["side"] == -1 and len(t.sent) == 1          # the bracket's buy stop was found
    stop = t.open_stops()[0]
    assert stop.side == "buy" and stop.stop_price == 51.0


def test_short_stop_trails_down_only_and_exit_pnl_is_right(tmp_path, monkeypatch):
    bot, t = _sbot(tmp_path, monkeypatch, direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    bot.raise_stop(50.40)
    bot.raise_stop(50.70)                                        # looser: ignored, a stop never moves back
    bot.raise_stop(49.80)
    stops = t.open_stops()
    assert len(stops) == 1 and stops[0].stop_price == 49.8 and min(t.stops_seen) >= 1
    t.trigger_stop(at=49.80)
    bot._check_live_exit()
    c = bot.closed[-1]
    assert bot.pos is None and c["reason"] == "trail" and c["exit"] == 49.8 and c["side"] == -1
    assert c["pnl"] == pytest.approx((50.0 - 49.8) * 125)       # profit when the price falls


def test_short_flatten_buys_back_only_the_bots_shares(tmp_path, monkeypatch):
    bot, t = _sbot(tmp_path, monkeypatch, direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    t.price = 50.5
    bot.flatten("end of day")
    covers = [r for r in t.sent if _v(r.side) == "buy" and _v(r.type) == "market"]
    assert len(covers) == 1 and int(covers[0].qty) == 125
    assert t.pos["AAPL"] == 0 and not t.open_stops() and bot.pos is None
    assert bot.closed[-1]["pnl"] == pytest.approx(-0.5 * 125)


def test_short_skipped_when_not_allowed(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    for attr, why in (("shorting_enabled", "isn't enabled"), ("shortable", "not shortable"),
                      ("easy_to_borrow", "hard to borrow")):
        t = FakeTrading()
        setattr(t, attr, False)
        bot, t = _sbot(tmp_path, monkeypatch, trading=t, direction="short")
        bot.enter(50.0, 51.0, "short bounce", side=-1)
        assert not t.sent and bot.pos is None and bot.trades_today == 0 and why in caplog.text


def test_short_needs_a_flat_position_and_is_limited_to_the_account_value(tmp_path, monkeypatch):
    bot, t = _sbot(tmp_path, monkeypatch, trading=FakeTrading(held=10), direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    assert not t.sent                                            # you hold shares: no short
    t2 = FakeTrading(cash=1000)
    bot2, t2 = _sbot(tmp_path, monkeypatch, trading=t2, direction="short")
    monkeypatch.setattr(t2, "get_account", lambda: SimpleNamespace(
        equity="1000", cash="1000", non_marginable_buying_power="1000", buying_power="4000", multiplier="4",
        account_number="PA1", shorting_enabled=True, trading_blocked=False, account_blocked=False,
        trade_suspended_by_user=False))
    bot2.enter(50.0, 51.0, "short bounce", side=-1)
    assert int(t2.sent[0].qty) == 5                              # 25% of $1,000 (max position): 5 shares


def test_short_entry_skipped_when_price_already_at_the_stop(tmp_path, monkeypatch):
    bot, t = _sbot(tmp_path, monkeypatch, direction="short")
    t.price = 51.0
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    assert not t.sent and bot.pos is None


@pytest.mark.parametrize("direction", ["short", "both"])
def test_full_live_session_with_shorts_against_fake_alpaca(tmp_path, monkeypatch, direction):
    """Whole days with shorts allowed: stops above the price fire when a bar trades up through them,
    the bot is never open without a stop, and it ends every day flat."""
    monkeypatch.setattr(alpaca_live._time, "sleep", lambda s: None)
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    shorts = 0
    for days in (6, 8, 10):
        df = synthetic("AAA", days=days)
        day = sorted(set(df.index.date))[-1]
        clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ZoneInfo("America/New_York"))}
        monkeypatch.setattr(live, "now_et", lambda: clock["t"])
        monkeypatch.setattr(alpaca_live, "now_et", lambda: clock["t"])
        t = FakeTrading(cash=25_000)
        bot = AlpacaBot(AlpacaClients(t, FakeData(df), "iex", True),
                        Config(broker="alpaca", watchlist=["AAPL"], direction=direction, max_trades_per_day=4),
                        live=True, workdir=tmp_path)
        monkeypatch.setattr(bot, "_last_trade_price", lambda: t.price)
        bot.snapshot_settled_cash()
        bot.symbol = "AAPL"
        bot.history = df[df.index.date < day]
        now = clock["t"]
        while now.time() < live._t(bot.cfg.flatten_time):
            clock["t"] = now
            seen = df[df.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
            if len(seen):
                bar = seen.iloc[-1]
                t.price = float(bar.close)
                for o in t.open_stops():
                    hit = bar.high >= float(o.stop_price) if o.side == "buy" else bar.low <= float(o.stop_price)
                    if hit:
                        t.trigger_stop(at=float(o.stop_price))
            bot.step()
            now += pd.Timedelta(minutes=5)
        clock["t"] = now
        bot.flatten("end of day")
        assert t.pos["AAPL"] == 0 and bot.pos is None, days
        assert all(n >= 1 for n in t.stops_seen), days
        for c in bot.closed:
            assert c["pnl"] == pytest.approx((c["exit"] - c["entry"]) * c["shares"] * c["side"])
            if c["side"] < 0 and c["reason"] in ("stop", "trail"):
                assert any(o.side == "buy" and o.order_type == "stop" and o.status == "filled"
                           and float(o.filled_avg_price) == c["exit"] for o in t.all.values())
        shorts += sum(1 for c in bot.closed if c["side"] < 0)
        if direction == "short":
            assert all(c["side"] < 0 for c in bot.closed)
    assert shorts >= 1
