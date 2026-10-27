"""NinjaTrader (futures) layer against a fake that speaks the documented Trade API: REST auth with
apiHosts and p-ticket penalties, OSO entries with a stop leg, modifyOrder, cancel, positions, fills,
and the market-data WebSocket framing (o / a[...] frames, authorize, client heartbeats, chart events)."""
import json
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import live, ninja_live
from swingbot.config import Config
from swingbot.data import synthetic
from swingbot.ninja_live import NinjaBot, NinjaClient, NinjaError, bars_to_df

ET = ZoneInfo("America/New_York")
ninja_live.MD_IDLE_S = 0.0                                  # the fake socket delivers everything at once
PRODUCTS = {"MGC": (10.0, 0.1), "MCL": (100.0, 0.01), "SIL": (1000.0, 0.005)}


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeNinjaHTTP:
    """REST side of the fake NinjaTrader."""

    def __init__(self, price=2500.0, held=0, penalty=False, first_notice_days=40):
        self.price, self.penalty, self.fnd = price, penalty, first_notice_days
        self.pos = held                                   # net contracts held in the account
        self.orders, self.fills, self.calls = {}, {}, []
        self.next_id = 1000
        self.stops_seen = []

    def _id(self):
        self.next_id += 1
        return self.next_id

    def open_stops(self):
        return [o for o in self.orders.values() if o["type"] == "Stop" and o["ordStatus"] == "Working"]

    def _note(self):
        if self.pos:
            self.stops_seen.append(len(self.open_stops()))

    def _fill(self, oid, qty, price, side):
        self.orders[oid]["ordStatus"] = "Filled"
        self.fills.setdefault(oid, []).append({"orderId": oid, "qty": qty, "price": price, "action": side})
        self.pos += qty if side == "Buy" else -qty

    def trigger_stop(self, at):
        for o in self.open_stops():
            self._fill(o["id"], o["qty"], at, o["action"])
            for sib in self.orders.values():              # OCO sibling (target leg) goes away
                if sib.get("parent") == o.get("parent") and sib is not o and sib["ordStatus"] == "Working":
                    sib["ordStatus"] = "Canceled"

    # --- HTTP verbs
    def post(self, url, json=None, headers=None, timeout=None):
        path = url.split("/v1", 1)[1]
        self.calls.append(("POST", path, json))
        if path == "/auth/accesstokenrequest":
            if self.penalty and "p-ticket" not in json:
                return Resp({"p-ticket": "T1", "p-time": 0})
            return Resp({"accessToken": "tok", "mdAccessToken": "mdtok", "hasMarketData": True,
                         "expirationTime": (datetime.now(ZoneInfo("UTC")) + timedelta(minutes=90)).isoformat(),
                         "apiHosts": {"demo": "demo-x.example", "mdDemo": "md-x.example"}})
        assert headers and headers["Authorization"] == "Bearer tok"
        if path == "/cashBalance/getcashbalancesnapshot":
            return Resp({"netLiq": 25000.0, "totalCashValue": 25000.0, "initialMargin": 0.0})
        if path == "/order/placeoso":
            assert json["isAutomated"] is True and json["orderType"] == "Market"
            assert json["bracket1"]["action"] != json["action"]
            oid, sid = self._id(), self._id()
            self.orders[oid] = {"id": oid, "action": json["action"], "type": "Market", "qty": json["orderQty"],
                                "ordStatus": "Working"}
            self.orders[sid] = {"id": sid, "action": json["bracket1"]["action"], "type": json["bracket1"]["orderType"],
                                "qty": json["orderQty"], "stopPrice": json["bracket1"]["stopPrice"],
                                "ordStatus": "Working", "parent": oid}
            self._fill(oid, json["orderQty"], self.price, json["action"])
            self._note()
            return Resp({"orderId": oid, "oso1Id": sid, "failureReason": "Success"})
        if path == "/order/placeorder":
            oid = self._id()
            self.orders[oid] = {"id": oid, "action": json["action"], "type": json["orderType"],
                                "qty": json["orderQty"], "stopPrice": json.get("stopPrice"), "ordStatus": "Working"}
            if json["orderType"] == "Market":
                self._fill(oid, json["orderQty"], self.price, json["action"])
            self._note()
            return Resp({"orderId": oid})
        if path == "/order/modifyorder":
            o = self.orders[json["orderId"]]
            assert o["ordStatus"] == "Working" and json["orderType"] == "Stop"
            o["stopPrice"] = json["stopPrice"]               # same order, new price: never unprotected
            self._note()
            return Resp({"commandId": 1, "failureReason": "Success"})
        if path == "/order/cancelorder":
            self.orders[json["orderId"]]["ordStatus"] = "Canceled"
            return Resp({"commandId": 2})
        raise AssertionError("unexpected POST " + path)

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("/v1", 1)[1]
        self.calls.append(("GET", path, params))
        if path == "/account/list":
            return Resp([{"id": 7, "name": "DEMO123", "active": True}])
        if path == "/contract/find":
            root = params["name"][:3]
            return Resp({"id": 500 + list(PRODUCTS).index(root), "name": params["name"],
                         "contractMaturityId": 900 + list(PRODUCTS).index(root)})
        if path == "/contractMaturity/item":
            i = params["id"] - 900
            fnd = (datetime.now(ET) + timedelta(days=self.fnd)).date().isoformat()
            return Resp({"id": params["id"], "productId": 300 + i, "firstIntentDate": fnd,
                         "expirationDate": (datetime.now(ET) + timedelta(days=self.fnd + 20)).date().isoformat()})
        if path == "/product/item":
            name = list(PRODUCTS)[params["id"] - 300]
            vpp, tick = PRODUCTS[name]
            return Resp({"id": params["id"], "name": name, "valuePerPoint": vpp, "tickSize": tick})
        if path == "/position/list":
            return Resp([{"accountId": 7, "contractId": 500, "netPos": self.pos}])
        if path == "/order/items":
            ids = [int(x) for x in params["ids"].split(",")]
            return Resp([{"id": i, "action": self.orders[i]["action"], "ordStatus": self.orders[i]["ordStatus"]}
                         for i in ids if i in self.orders])
        if path == "/order/item":
            o = self.orders[params["id"]]
            return Resp({"id": o["id"], "action": o["action"], "ordStatus": o["ordStatus"]})
        if path == "/fill/deps":
            return Resp(self.fills.get(params["masterid"], []))
        raise AssertionError("unexpected GET " + path)


class FakeWS:
    """Market-data socket: o frame, authorize, request/response, chart events, needs heartbeats."""

    def __init__(self, df, quote=None):
        self.df, self.quote = df, quote
        self.out, self.sent = ["o"], []

    def recv(self):
        if self.out:
            return self.out.pop(0)
        raise Exception("timed out")

    def send(self, msg):
        self.sent.append(msg)
        if msg == "[]":
            return
        endpoint, rid, _, body = msg.split("\n", 3)
        if endpoint == "authorize":
            assert body == "mdtok"
            self.out.append('a[{"s":200,"i":%s}]' % rid)
        elif endpoint == "md/getChart":
            req = json.loads(body)
            cd = req["chartDescription"]
            assert cd["underlyingType"] == "MinuteBar" and cd["elementSizeUnit"] == "UnderlyingUnits"
            self.chart_requests = getattr(self, "chart_requests", []) + [req]
            self.out.append('a[{"s":200,"i":%s,"d":{"historicalId":11,"realtimeId":12}}]' % rid)
            bars = [{"timestamp": ts.tz_convert("UTC").isoformat().replace("+00:00", "Z"), "open": r.open,
                     "high": r.high, "low": r.low, "close": r.close, "upVolume": int(r.volume // 2),
                     "downVolume": int(r.volume - r.volume // 2)} for ts, r in self.df.iterrows()]
            for i in range(0, len(bars), 500):              # history in packets, no end-of-history marker
                self.out.append("a" + json.dumps([{"e": "chart", "d": {"charts": [{"id": 11, "bars": bars[i:i + 500]}]}}]))
        elif endpoint == "md/subscribeQuote":
            self.out.append('a[{"s":200,"i":%s}]' % rid)
            self.out.append("a" + json.dumps([{"e": "md", "d": {"quotes": [
                {"contractId": 500, "entries": {"Trade": {"price": self.quote, "size": 1}}}]}}]))

    def close(self):
        pass


def _client(http, df=None, quote=2501.0):
    sockets = []

    def factory(url):
        ws = FakeWS(df if df is not None else synthetic("MGC", days=3, price=2500), quote)
        sockets.append((url, ws))
        return ws
    c = NinjaClient("me", "pw", "cid", "sec", env="demo", device_id="dev1", http=http, ws_factory=factory)
    c.sockets = sockets
    return c


def _bot(tmp_path, monkeypatch, http=None, live_mode=True, **cfg):
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    monkeypatch.setattr(ninja_live._time, "sleep", lambda s: None)
    http = http or FakeNinjaHTTP()
    client = _client(http)
    client.login()
    bot = NinjaBot(client, Config(broker="ninjatrader", futures_watchlist=["MGCZ6"], **cfg),
                   live=live_mode, workdir=tmp_path)
    bot.symbol = "MGCZ6"
    bot._load_contract("MGCZ6")
    return bot, http


# ---------------------------------------------------------------- connection
def test_login_adopts_returned_hosts_and_honors_rate_limit_ticket():
    http = FakeNinjaHTTP(penalty=True)
    c = _client(http)
    c.login()
    logins = [b for m, p, b in http.calls if p == "/auth/accesstokenrequest"]
    assert len(logins) == 2 and logins[1]["p-ticket"] == "T1" and logins[0]["deviceId"] == "dev1"
    assert c.rest_base == "https://demo-x.example/v1" and c.md_url == "wss://md-x.example/v1/websocket"


def test_market_data_socket_protocol_and_bars():
    df = synthetic("MGC", days=3, price=2500)
    c = _client(FakeNinjaHTTP(), df)
    c.login()
    bars = c.chart_bars("MGCZ6", 5, 2000)
    url, ws = c.sockets[-1]
    assert url == "wss://md-x.example/v1/websocket"
    assert ws.sent[0] == "authorize\n1\n\nmdtok" and ws.sent[1].startswith("md/getChart\n")
    got = bars_to_df(bars)
    assert len(got) == len(df) and got["volume"].sum() == df["volume"].sum()
    assert c.last_trade("MGCZ6") == 2501.0


def test_heartbeats_sent_while_waiting(monkeypatch):
    """On a slow connection the client must send '[]' at least every 2.5 seconds."""
    clock = {"t": 0.0}

    class SlowWS(FakeWS):
        def recv(self):
            clock["t"] += 0.9                                   # each read takes ~1 s of real time
            if self.out and clock["t"] > 6:                     # data shows up only after ~6 s
                return self.out.pop(0)
            if self.out and self.out[0] == "o":
                return self.out.pop(0)
            raise Exception("timed out")

    monkeypatch.setattr(ninja_live._time, "monotonic", lambda: clock["t"])
    made = []
    c = NinjaClient("me", "pw", "cid", "sec", device_id="d", http=FakeNinjaHTTP(),
                    ws_factory=lambda url: made.append(SlowWS(synthetic("MGC", days=2, price=2500))) or made[-1])
    c.login()
    assert c.chart_bars("MGCZ6", 5, 500)
    beats = [i for i, m in enumerate(made[0].sent) if m == "[]"]
    assert len(beats) >= 2                                      # kept the connection alive while waiting


# ---------------------------------------------------------------- futures math
def test_contract_sizing_uses_dollars_per_point_and_cap(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch)
    assert bot.point_value == 10.0 and bot.tick == Decimal("0.1")
    # $125 risk budget (0.5% of $25k); $10 stop x $10/pt = $100 per contract -> 1
    assert bot.size_position(25_000, 2500.0, 2490.0) == 1
    assert bot.size_position(25_000, 2500.0, 2497.0) == 1    # 4 contracts by risk, capped at 1
    assert bot.size_position(25_000, 2500.0, 2480.0) == 0    # $200 per contract > $125 budget: skip
    bot.cfg.futures_max_contracts = 3
    assert bot.size_position(25_000, 2500.0, 2497.0) == 3


def test_prices_round_to_tick_size(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch)
    assert bot._round_price(2489.97, ROUND_DOWN) == Decimal("2489.9")
    bot.tick = Decimal("0.005")
    assert bot._round_price(30.1234, ROUND_DOWN) == Decimal("30.12")
    bot.tick = Decimal("0.01")
    assert bot._round_price(71.239, ROUND_DOWN) == Decimal("71.23")


def test_contract_near_first_notice_is_skipped(tmp_path, monkeypatch):
    bot, _ = _bot(tmp_path, monkeypatch, http=FakeNinjaHTTP(first_notice_days=3))
    assert bot.scan_cfg().watchlist == []
    bot2, _ = _bot(tmp_path, monkeypatch, http=FakeNinjaHTTP(first_notice_days=30))
    cfg = bot2.scan_cfg()
    assert cfg.watchlist == ["MGCZ6"] and cfg.min_price == 0 and cfg.min_avg_dollar_volume == 0


# ---------------------------------------------------------------- orders
def test_entry_is_oso_with_tick_rounded_stop(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch)
    bot.enter(2500.0, 2490.03, "bounce")
    oso = [b for m, p, b in http.calls if p == "/order/placeoso"][0]
    assert oso["accountSpec"] == "DEMO123" and oso["accountId"] == 7 and oso["symbol"] == "MGCZ6"
    assert oso["orderQty"] == 1 and oso["bracket1"] == {"action": "Sell", "orderType": "Stop",
                                                         "stopPrice": 2490.0, "timeInForce": "Day"}
    assert "bracket2" not in oso                            # no profit target
    assert bot.pos["shares"] == 1 and bot.pos["entry"] == 2500.0
    assert len([1 for m, p, b in http.calls if p == "/order/placeorder"]) == 0   # stop leg found, none extra


def test_trailing_modifies_same_stop_and_pnl_uses_multiplier(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch)
    bot.enter(2500.0, 2490.0, "bounce")
    bot.raise_stop(2495.04)
    bot.raise_stop(2503.0)
    stops = http.open_stops()
    assert len(stops) == 1 and stops[0]["stopPrice"] == 2503.0 and min(http.stops_seen) >= 1
    http.trigger_stop(at=2503.0)
    bot._check_live_exit()
    assert bot.pos is None
    c = bot.closed[-1]
    assert c["reason"] == "trail" and c["pnl"] == pytest.approx(30.0)   # 3.0 points x $10 x 1 contract


def test_flatten_sells_only_bot_contracts(tmp_path, monkeypatch):
    http = FakeNinjaHTTP(held=2)                            # you already hold 2 contracts yourself
    bot, http = _bot(tmp_path, monkeypatch, http=http)
    bot.enter(2500.0, 2490.0, "bounce")
    bot.flatten("end of day")
    sells = [b for m, p, b in http.calls if p == "/order/placeorder" and b["action"] == "Sell"]
    assert len(sells) == 1 and sells[0]["orderQty"] == 1 and sells[0]["orderType"] == "Market"
    assert http.pos == 2 and not http.open_stops()


def test_rejected_order_raises_clearly(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch)
    with pytest.raises(NinjaError, match="InvalidPrice"):
        bot._check({"failureReason": "InvalidPrice", "failureText": "bad stop"}, "Entry order")


def test_full_live_session_against_fake_ninjatrader(tmp_path, monkeypatch):
    monkeypatch.setattr(live._time, "sleep", lambda s: None)
    monkeypatch.setattr(ninja_live._time, "sleep", lambda s: None)
    df = synthetic("AAA", days=8)                           # the known day with a loss, a trail and a win
    day = sorted(set(df.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    monkeypatch.setattr(ninja_live, "now_et", lambda: clock["t"])
    http = FakeNinjaHTTP(price=float(df.close.iat[0]))
    seen = {"df": df.iloc[:0]}
    c = NinjaClient("me", "pw", "cid", "sec", device_id="d", http=http,
                    ws_factory=lambda url: FakeWS(seen["df"], quote=None))
    c.login()
    bot = NinjaBot(c, Config(broker="ninjatrader", futures_watchlist=["MGCZ6"], futures_max_contracts=5),
                   live=True, workdir=tmp_path)
    bot.symbol = "MGCZ6"
    bot._load_contract("MGCZ6")
    bot.point_value, bot.tick = 1.0, Decimal("0.01")        # price scale of the test data
    bot.history = df[df.index.date < day]
    now = clock["t"]
    while now.time() < live._t(bot.cfg.flatten_time):
        clock["t"] = now
        seen["df"] = df[(df.index.date == day) & (df.index + pd.Timedelta(minutes=5) <= pd.Timestamp(now))]
        if len(seen["df"]):
            bar = seen["df"].iloc[-1]
            http.price = float(bar.close)
            for o in http.open_stops():
                if bar.low <= o["stopPrice"]:
                    http.trigger_stop(at=o["stopPrice"])
        bot.step()
        now += pd.Timedelta(minutes=5)
    clock["t"] = now
    bot.flatten("end of day")
    assert http.pos == 0 and bot.pos is None
    assert all(n >= 1 for n in http.stops_seen)
    assert bot.trades_today >= 1 and any(c["reason"] == "trail" for c in bot.closed)
    assert any(p == "/order/modifyorder" for m, p, b in http.calls)


def test_ninja_requests_one_minute_chart(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch, bar_minutes=1)
    bot.fetch_bars("MGCZ6", "DAY")
    url, ws = bot.client.sockets[-1]
    req = ws.chart_requests[-1]
    assert req["chartDescription"]["elementSize"] == 1 and req["timeRange"]["asMuchAsElements"] == 1600



def test_short_futures_entry_trail_and_exit(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch, direction="short")
    bot.enter(2500.0, 2510.03, "short bounce", side=-1)
    oso = [b for m, p, b in http.calls if p == "/order/placeoso"][0]
    assert oso["action"] == "Sell" and oso["bracket1"] == {"action": "Buy", "orderType": "Stop",
                                                            "stopPrice": 2510.1, "timeInForce": "Day"}
    assert http.pos == -1 and bot.bot_shares() == 1
    bot.raise_stop(2497.0)
    assert http.open_stops()[0]["stopPrice"] == 2497.0
    http.trigger_stop(at=2497.0)
    bot._check_live_exit()
    c = bot.closed[-1]
    assert bot.pos is None and c["reason"] == "trail" and c["pnl"] == pytest.approx(30.0)   # 3 points x $10


def test_short_futures_flatten_buys_back(tmp_path, monkeypatch):
    bot, http = _bot(tmp_path, monkeypatch, direction="short")
    bot.enter(2500.0, 2510.0, "short bounce", side=-1)
    bot.flatten("end of day")
    buys = [b for m, p, b in http.calls if p == "/order/placeorder" and b["orderType"] == "Market"]
    assert len(buys) == 1 and buys[0]["action"] == "Buy" and http.pos == 0 and bot.pos is None
