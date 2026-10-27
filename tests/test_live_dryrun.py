"""Dry-run bot against a fake Public client: checks it never places orders and paper-trades sensibly."""
import pytest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from swingbot import live
from swingbot.config import Config
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")


class FakeClient:
    def __init__(self, df):
        self.df = df
        self.orders_placed = 0
        self.now = None

    def get_bars(self, symbol, period, **kw):
        d = self.df[self.df.index < pd.Timestamp(self.now)]
        if period.name == "DAY":
            d = d[d.index.date == self.now.date()]
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low,
                                close=r.close, volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def place_order(self, *a, **k):
        self.orders_placed += 1
        raise AssertionError("dry run must not place orders")

    def get_portfolio(self):
        return SimpleNamespace(total_account_value=25_000, equity=[], positions=[], orders=[])


def test_dry_run_session(tmp_path, monkeypatch):
    cfg = Config(watchlist=["AAA"])
    df = synthetic("AAA", days=8)
    day = sorted(set(df.index.date))[-1]
    client = FakeClient(df)
    bot = live.Bot(client, cfg, live=False, workdir=tmp_path)

    clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    client.now = clock["t"]
    bot.morning_scan()
    if not bot.symbol:
        return  # no setup that day is a valid outcome
    t = clock["t"]
    while t.time() < live._t(cfg.flatten_time):
        clock["t"] = t
        client.now = t
        bot.step()
        t += pd.Timedelta(minutes=5)
    bot.flatten("end of day")
    assert client.orders_placed == 0
    assert bot.pos is None
    assert bot.trades_today <= cfg.max_trades_per_day


def test_cap_by_buying_power():
    assert live.cap_by_buying_power(100, 10_000, 50.0, 2.0) == 100      # $5,000 fits
    assert live.cap_by_buying_power(300, 10_000, 50.0, 2.0) == 196      # 98% of $10k / $50
    assert live.cap_by_buying_power(10, 0, 50.0, 2.0) == 0
    assert live.cap_by_buying_power(10, 40, 50.0, 2.0) == 0


class FakeBroker:
    """Minimal stand-in for Public: tracks positions and orders, fills market orders at ``price``,
    creates a bracket's stop leg on fill, and supports cancel-and-replace."""

    def __init__(self, cash_bp=100_000, withdrawable=100_000, price=50.0, held=0):
        from decimal import Decimal
        self.D = Decimal
        self.cash_bp, self.withdrawable, self.price = cash_bp, withdrawable, price
        self.held = {"AAPL": held}
        self.orders = {}            # id -> SimpleNamespace order
        self.sent = []              # every request placed
        self.min_stops_seen = []    # number of open bot stop orders after each operation (while holding)

    # -- helpers
    def _order(self, oid, side, typ, qty, stop=None, bracket=None, status="NEW", avg=None):
        o = SimpleNamespace(order_id=oid, bracket_id=bracket, instrument=SimpleNamespace(symbol="AAPL"),
                            side=side, type=typ, quantity=self.D(qty), stop_price=stop, status=status,
                            expiration=SimpleNamespace(time_in_force="DAY", expiration_time=None),
                            created_at=datetime.now(ZoneInfo("UTC")), average_price=avg)
        self.orders[oid] = o
        return o

    def open_stops(self):
        return [o for o in self.orders.values() if o.type == "STOP" and o.status == "NEW"]

    def trigger_stop(self, at):
        for o in self.open_stops():
            o.status, o.average_price = "FILLED", self.D(str(at))
            self.held["AAPL"] += int(o.quantity) if o.side == "BUY" else -int(o.quantity)

    # -- shorts
    shortable = "EASY_TO_BORROW"
    uptick = "NOT_TRIGGERED"

    def get_instrument(self, symbol, instrument_type):
        return SimpleNamespace(shorting_availability=self.shortable)

    def preflight_short_order(self, symbol, quantity):
        return SimpleNamespace(short_selling=SimpleNamespace(uptick_rule=self.uptick))

    def place_short_order(self, symbol, quantity, **kw):
        oid = str(__import__("uuid").uuid4())
        self.sent.append(SimpleNamespace(kind="short", qty=int(quantity), **kw))
        self._order(oid, "SELL", "MARKET", int(quantity), status="FILLED", avg=self.D(str(self.price)))
        self.held["AAPL"] -= int(quantity)
        broker = self

        class NewOrder:
            order_id = oid
            def wait_for_fill(self, timeout):
                o = broker.orders[oid]
                return SimpleNamespace(filled_quantity=o.quantity, average_price=o.average_price)
            def cancel(self):
                pass
        return NewOrder()

    def get_quotes(self, instruments):
        return [SimpleNamespace(last=self.D(str(self.price)))]

    # -- API surface used by the bot
    def get_portfolio(self):
        bp = SimpleNamespace(cash_only_buying_power=self.D(str(self.cash_bp)), buying_power=self.D("1e6"))
        atw = None if self.withdrawable is None else SimpleNamespace(
            cash_only_available_to_withdraw=self.D(str(self.withdrawable)))
        pos = [SimpleNamespace(instrument=SimpleNamespace(symbol=k), quantity=str(v))
               for k, v in self.held.items() if v]
        return SimpleNamespace(total_account_value=self.D("25000"), equity=[], positions=pos,
                               orders=list(self.orders.values()), buying_power=bp, available_to_withdraw=atw)

    def place_order(self, req):
        self.sent.append(req)
        side, typ, qty = req.order_side.value, req.order_type.value, int(req.quantity)
        broker = self
        if typ == "MARKET":
            self._order(req.order_id, side, typ, qty, status="FILLED", avg=self.D(str(self.price)))
            self.held["AAPL"] += qty if side == "BUY" else -qty
            if side == "BUY" and req.stop_loss is not None:
                self._order(str(__import__("uuid").uuid4()), "SELL", "STOP", qty, stop=req.stop_loss.stop_price,
                            bracket=req.order_id)
        else:
            self._order(req.order_id, side, typ, qty, stop=req.stop_price)

        class NewOrder:
            order_id = req.order_id
            def wait_for_fill(self, timeout):
                o = broker.orders[req.order_id]
                return SimpleNamespace(filled_quantity=o.quantity, average_price=o.average_price)
            def cancel(self):
                pass
        self._note()
        return NewOrder()

    def cancel_and_replace_order(self, req):
        old = self.orders[req.order_id]
        new_id = req.request_id
        self._order(new_id, old.side, "STOP", int(req.quantity), stop=req.stop_price, bracket=old.bracket_id)
        old.status = "REPLACED"          # atomic: new stop exists before the old one goes away
        self._note()
        return SimpleNamespace(order_id=new_id)

    def cancel_order(self, order_id, account_id=None):
        self.orders[order_id].status = "CANCELLED"

    def get_order(self, order_id, account_id=None):
        return self.orders[order_id]

    def _note(self):
        self.min_stops_seen.append(len(self.open_stops()))


def _bot(broker, tmp_path, monkeypatch, **cfg):
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    bot = live.Bot(broker, Config(**cfg), live=True, workdir=tmp_path)
    bot.symbol = "AAPL"
    bot.snapshot_settled_cash()
    return bot


def test_live_entry_trimmed_to_cash(tmp_path, monkeypatch):
    b = FakeBroker(cash_bp=1000, withdrawable=1000)
    _bot(b, tmp_path, monkeypatch).enter(50.0, 49.0, "bounce")   # risk sizing alone wants 125 shares
    assert int(b.sent[0].quantity) == 19                          # 98% of $1,000 / $50
    assert b.sent[0].use_margin is False


def test_unsettled_funds_not_used(tmp_path, monkeypatch):
    b = FakeBroker(cash_bp=5000, withdrawable=1000)
    _bot(b, tmp_path, monkeypatch).enter(50.0, 49.0, "bounce")
    assert int(b.sent[0].quantity) == 19                          # settled cash, not $5,000


def test_sale_proceeds_not_reused_same_day(tmp_path, monkeypatch):
    b = FakeBroker(cash_bp=1000, withdrawable=1000)
    bot = _bot(b, tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    b.trigger_stop(at=52.0)
    bot._check_live_exit()
    b.cash_bp = b.withdrawable = 1050                             # even if proceeds showed as withdrawable
    bot.enter(50.0, 49.0, "bounce")
    assert sum(1 for r in b.sent if r.order_side.value == "BUY") == 1


def test_blocks_when_settled_cash_unknown(tmp_path, monkeypatch):
    b = FakeBroker(cash_bp=10000, withdrawable=None)
    _bot(b, tmp_path, monkeypatch).enter(50.0, 49.0, "bounce")
    assert b.sent == []


def test_entry_is_stop_only_bracket(tmp_path, monkeypatch):
    b = FakeBroker()
    _bot(b, tmp_path, monkeypatch).enter(50.0, 49.0, "bounce")
    req = b.sent[0]
    assert req.order_class.value == "BRACKET"
    assert req.stop_loss.stop_price == Decimal("49.00")
    assert req.take_profit is None                                # 2x target dropped
    assert len(b.sent) == 1                                       # bracket leg found, no extra stop placed


def test_trail_reprices_stop_without_gap(tmp_path, monkeypatch):
    b = FakeBroker()
    bot = _bot(b, tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    bot.raise_stop(49.60)
    bot.raise_stop(49.40)                                         # lower: ignored
    bot.raise_stop(50.25)
    stops = b.open_stops()
    assert len(stops) == 1 and stops[0].stop_price == Decimal("50.25")
    assert bot.pos["stop"] == 50.25 and bot.pos["init_stop"] == 49.0
    assert min(b.min_stops_seen) >= 1                             # never without a stop while holding
    b.trigger_stop(at=50.25)
    bot._check_live_exit()
    assert bot.pos is None
    assert "trail" in (tmp_path / "trades.csv").read_text(encoding="utf-8")


def test_never_touches_your_own_shares_or_orders(tmp_path, monkeypatch):
    b = FakeBroker(held=100)                                      # you already own 100 AAPL
    b._order("yours", "SELL", "LIMIT", 100)                       # ...and have your own limit sell open
    bot = _bot(b, tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    assert bot.bot_shares() == 125
    bot.flatten("end of day")
    sells = [r for r in b.sent if r.order_side.value == "SELL" and r.order_type.value == "MARKET"]
    assert len(sells) == 1 and int(sells[0].quantity) == 125      # only the bot's shares
    assert b.held["AAPL"] == 100
    assert b.orders["yours"].status == "NEW"                      # your order untouched


def test_missing_stop_gets_replaced(tmp_path, monkeypatch):
    b = FakeBroker()
    bot = _bot(b, tmp_path, monkeypatch)
    bot.enter(50.0, 49.0, "bounce")
    for o in b.open_stops():                                      # stop leg vanishes somehow
        o.status = "CANCELLED"
    bot.ensure_stop()
    assert len(b.open_stops()) == 1 and int(b.open_stops()[0].quantity) == 125


def test_no_bars_yet_does_not_crash(tmp_path, monkeypatch):
    """A broker returning zero bars (e.g. right after the open) must mean 'wait', not an error."""
    from swingbot.data import empty_bars, normalize
    assert normalize(pd.DataFrame()).empty and str(empty_bars().index.tz) == "America/New_York"
    bot = live.Bot(client=None, cfg=Config(), live=False, workdir=tmp_path)
    bot.symbol = "AAA"
    bot.history = synthetic("AAA", days=3)
    monkeypatch.setattr(bot, "fetch_bars", lambda sym, period: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    monkeypatch.setattr(live, "now_et", lambda: datetime(2026, 6, 4, 9, 36, tzinfo=ET))
    bot.step()                                                # must simply return
    assert bot.pos is None



def test_public_short_sale_gets_a_buy_stop_at_once(tmp_path, monkeypatch):
    b = FakeBroker(price=50.0)
    bot = _bot(b, tmp_path, monkeypatch, direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    assert b.sent[0].kind == "short" and b.sent[0].qty == 125 and b.held["AAPL"] == -125
    stops = b.open_stops()
    assert len(stops) == 1 and stops[0].side == "BUY" and float(stops[0].stop_price) == 51.0
    assert bot.pos["side"] == -1 and bot.bot_shares() == 125
    bot.raise_stop(50.5)
    assert [float(o.stop_price) for o in b.open_stops()] == [50.5]
    b.trigger_stop(at=50.5)
    bot._check_live_exit()
    c = bot.closed[-1]
    assert bot.pos is None and c["exit"] == 50.5 and c["pnl"] == -0.5 * 125 and b.held["AAPL"] == 0


def test_public_short_flatten_buys_to_close(tmp_path, monkeypatch):
    b = FakeBroker(price=50.0)
    bot = _bot(b, tmp_path, monkeypatch, direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    b.price = 49.0
    bot.flatten("end of day")
    cover = [r for r in b.sent if getattr(r, "order_type", None) is not None and r.order_type.value == "MARKET"]
    assert len(cover) == 1 and cover[0].order_side.value == "BUY" and cover[0].open_close_indicator.value == "CLOSE"
    assert b.held["AAPL"] == 0 and bot.closed[-1]["pnl"] == 125.0


@pytest.mark.parametrize("attr,value,why", [("shortable", "HARD_TO_BORROW", "hard to borrow"),
                                            ("shortable", "NOT_SHORTABLE", "not shortable"),
                                            ("uptick", "TRIGGERED", "short-sale restriction")])
def test_public_short_skipped_when_the_broker_would_refuse(tmp_path, monkeypatch, caplog, attr, value, why):
    caplog.set_level("INFO")
    b = FakeBroker(price=50.0)
    setattr(b, attr, value)
    bot = _bot(b, tmp_path, monkeypatch, direction="short")
    bot.enter(50.0, 51.0, "short bounce", side=-1)
    assert not b.sent and bot.pos is None and why in caplog.text
