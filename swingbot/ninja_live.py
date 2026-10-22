"""NinjaTrader (futures) version of the live bot, using NinjaTrader's web Trade API
(the Tradovate-based REST + WebSocket API documented at docs.ninjatrader.com).

Same strategy, trailing stop, risk-per-trade rule and dashboard as the stock versions, with
futures adjustments:
  * Size is in CONTRACTS: risk per contract = stop distance x the contract's dollar value per point.
    A hard cap (futures_max_contracts) applies on top.
  * Prices are rounded to each contract's tick size (from the product data).
  * Entry is an OSO order: market buy with a Stop sell attached, live from the fill. The stop is
    trailed with modifyOrder (same order, new price), so it is never removed.
  * Futures are always on margin, so the cash-only / settled-cash rules don't apply here; the
    contract cap, risk per trade and daily loss limit do.
  * Trading hours are the stock session (09:35-15:50 ET) and the bot is flat overnight.

Modes:  run = dry run (real NinjaTrader prices, simulated fills, no orders)
        run --live = orders to the NinjaTrader DEMO environment if ninja_env is "demo" (default),
                     or to your LIVE account only if ninja_env is "live".
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import time as _time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config
from .data import COLS, empty_bars, normalize
from .live import Bot, _NOrder, now_et

log = logging.getLogger("swingbot")
UTC = ZoneInfo("UTC")
OPEN_NT = {"working", "pendingnew", "pendingreplace", "pendingcancel", "suspended"}
OK_RESULTS = (None, "", "Success")
MD_IDLE_S = 2.0          # chart history is complete once nothing new has arrived for this long


class NinjaError(RuntimeError):
    pass


# ============================================================================ API client
class NinjaClient:
    """Authenticated REST calls + one-shot market-data WebSocket requests."""

    def __init__(self, username: str, password: str, cid: str, sec: str, env: str = "demo",
                 device_id: str = "", app_id: str = "swingbot", http=None, ws_factory=None):
        self.username, self.password, self.cid, self.sec = username, password, cid, sec
        self.env = env
        self.device_id = device_id or str(uuid.uuid4())
        self.app_id = app_id
        self.hosts = {"demo": "demo.tradovateapi.com", "live": "live.tradovateapi.com",
                      "mdDemo": "md-demo.tradovateapi.com", "mdLive": "md.tradovateapi.com"}
        self.token = self.md_token = None
        self.expires: datetime | None = None
        if http is None:
            import requests
            http = requests.Session()
        self.http = http
        self.ws_factory = ws_factory
        self._req_id = 10

    # ------------------------------------------------------------ auth
    @property
    def rest_base(self) -> str:
        return f"https://{self.hosts['live' if self.env == 'live' else 'demo']}/v1"

    @property
    def md_url(self) -> str:
        return f"wss://{self.hosts['mdLive' if self.env == 'live' else 'mdDemo']}/v1/websocket"

    def login(self) -> None:
        body = {"name": self.username, "password": self.password, "appId": self.app_id, "appVersion": "1.0",
                "cid": self.cid, "sec": self.sec, "deviceId": self.device_id}
        for _ in range(3):
            r = self.http.post(self.rest_base + "/auth/accesstokenrequest", json=body, timeout=20)
            data = r.json()
            if data.get("p-ticket"):                       # rate-limit penalty: wait and retry with the ticket
                if data.get("p-captcha"):
                    raise NinjaError("NinjaTrader is asking for a captcha (usually after repeated failed logins). "
                                     "Wait about an hour, then try again.")
                wait = float(data.get("p-time", 10))
                log.warning("NinjaTrader asked to wait %.0fs before logging in again", wait)
                _time.sleep(wait)
                body["p-ticket"] = data["p-ticket"]
                continue
            if data.get("errorText") or not data.get("accessToken"):
                raise NinjaError(f"NinjaTrader login failed: {data.get('errorText') or 'no access token returned'}")
            self._take_token(data)
            hosts = data.get("apiHosts") or {}
            for k in ("demo", "live", "mdDemo", "mdLive"):  # the login response is authoritative for hosts
                if hosts.get(k):
                    self.hosts[k] = hosts[k]
            if self.env == "live" and data.get("hasLive") is False:
                raise NinjaError("This NinjaTrader login has no live access.")
            if data.get("hasMarketData") is False:
                log.warning("NinjaTrader reports no market data entitlement on this login - "
                            "price bars may not load until the API data subscription is active.")
            return
        raise NinjaError("NinjaTrader login kept being rate-limited.")

    def _take_token(self, data: dict) -> None:
        self.token = data["accessToken"]
        self.md_token = data.get("mdAccessToken") or self.md_token or self.token
        exp = data.get("expirationTime")
        self.expires = pd.Timestamp(exp).to_pydatetime() if exp else datetime.now(UTC) + timedelta(minutes=80)

    def ensure_token(self) -> None:
        if self.token is None:
            self.login()
            return
        if self.expires and datetime.now(UTC) > self.expires - timedelta(minutes=15):
            try:
                r = self.http.get(self.rest_base + "/auth/renewaccesstoken", headers=self._h(), timeout=20)
                data = r.json()
                if data.get("accessToken"):
                    self._take_token(data)
                    return
            except Exception as e:
                log.warning("token renewal failed (%s); logging in again", e)
            self.login()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    # ------------------------------------------------------------ REST
    def get(self, path: str, **params):
        return self._call("GET", path, params=params)

    def post(self, path: str, body: dict):
        return self._call("POST", path, json_body=body)

    def _call(self, method, path, params=None, json_body=None, _retry=True):
        self.ensure_token()
        url = self.rest_base + path
        if method == "GET":
            r = self.http.get(url, params=params, headers=self._h(), timeout=20)
        else:
            r = self.http.post(url, json=json_body, headers=self._h(), timeout=20)
        if r.status_code == 401 and _retry:                # token rejected: log in once more
            self.token = None
            return self._call(method, path, params, json_body, _retry=False)
        if r.status_code in (423, 429):
            raise NinjaError(f"NinjaTrader rate limit hit on {path} (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise NinjaError(f"NinjaTrader {path} failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json() if r.content else None
        if isinstance(data, dict) and data.get("errorText"):
            raise NinjaError(f"NinjaTrader {path}: {data['errorText']}")
        return data

    # ------------------------------------------------------------ market data (WebSocket)
    def _ws(self):
        if self.ws_factory:
            return self.ws_factory(self.md_url)
        import websocket
        return websocket.create_connection(self.md_url, timeout=1)   # short reads so heartbeats keep flowing

    def md_request(self, endpoint: str, body: dict, collect, timeout: float = 20.0, idle_done=None):
        """Open a market-data socket, authorize, send one request and feed every event to
        ``collect(event) -> bool`` until it returns True (done), ``idle_done()`` returns True while
        nothing is arriving, or the timeout passes."""
        self.ensure_token()
        ws = self._ws()
        try:
            first = ws.recv()
            if not str(first).startswith("o"):
                raise NinjaError(f"unexpected market-data greeting: {str(first)[:50]}")
            ws.send(f"authorize\n1\n\n{self.md_token}")
            self._req_id += 1
            rid = self._req_id
            ws.send(f"{endpoint}\n{rid}\n\n{json.dumps(body)}")
            end = _time.monotonic() + timeout
            last_beat = _time.monotonic()
            authorized = False
            while _time.monotonic() < end:
                if _time.monotonic() - last_beat > 2.0:
                    ws.send("[]")                           # client heartbeat (required every 2.5 s)
                    last_beat = _time.monotonic()
                try:
                    frame = ws.recv()
                except Exception as e:
                    if "timed out" in str(e).lower():       # nothing waiting: only now can we call it quiet
                        if idle_done and idle_done():
                            return
                        continue
                    raise
                frame = str(frame)
                if not frame or frame[0] in "oh":
                    continue
                if frame[0] == "c":
                    raise NinjaError(f"market data connection closed: {frame[1:200]}")
                if frame[0] != "a":
                    continue
                for msg in json.loads(frame[1:]):
                    if msg.get("i") == 1:
                        if msg.get("s") != 200:
                            raise NinjaError(f"market data authorization failed: {msg.get('d')}")
                        authorized = True
                    elif msg.get("i") == rid and msg.get("s") not in (None, 200):
                        raise NinjaError(f"{endpoint} failed: {msg.get('d')}")
                    elif msg.get("i") == rid:
                        if collect({"response": msg.get("d")}):
                            return
                    elif "e" in msg:
                        if collect(msg):
                            return
            if not authorized:
                raise NinjaError("market data authorization timed out")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def chart_bars(self, symbol: str, minutes: int, count: int) -> list[dict]:
        """Historical + current bars of ``minutes`` (up to ``count``) for a contract."""
        bars, ids, state = [], set(), {"last": None}

        def collect(ev):
            if "response" in ev:
                d = ev["response"] or {}
                ids.update(x for x in (d.get("historicalId"), d.get("realtimeId")) if x is not None)
                state["last"] = state["last"] or _time.monotonic()
                return False
            if ev.get("e") != "chart":
                return False
            done = False
            for ch in (ev.get("d") or {}).get("charts", []):
                if ids and ch.get("id") not in ids:
                    continue
                if ch.get("bars"):
                    bars.extend(ch["bars"])
                    state["last"] = _time.monotonic()
                if ch.get("eoh"):                            # end-of-history marker, if the server sends one
                    done = True
            return done

        def idle_done():          # otherwise: done once the reply (and any bars) have gone quiet
            return state["last"] is not None and _time.monotonic() - state["last"] > MD_IDLE_S

        self.md_request("md/getChart", {
            "symbol": symbol,
            "chartDescription": {"underlyingType": "MinuteBar", "elementSize": minutes,
                                 "elementSizeUnit": "UnderlyingUnits", "withHistogram": False},
            "timeRange": {"asMuchAsElements": count},
        }, collect, timeout=25, idle_done=idle_done)
        return bars

    def last_trade(self, symbol: str) -> float | None:
        out = {}

        def collect(ev):
            if ev.get("e") != "md":
                return False
            for q in (ev.get("d") or {}).get("quotes", []):
                t = (q.get("entries") or {}).get("Trade") or {}
                if t.get("price") is not None:
                    out["p"] = float(t["price"])
                    return True
            return False

        self.md_request("md/subscribeQuote", {"symbol": symbol}, collect, timeout=10)
        return out.get("p")

    def close(self) -> None:
        pass


def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return empty_bars()
    rows = [{"timestamp": pd.Timestamp(b["timestamp"]), "open": b["open"], "high": b["high"], "low": b["low"],
             "close": b["close"], "volume": (b.get("upVolume") or 0) + (b.get("downVolume") or 0)} for b in bars]
    return normalize(pd.DataFrame(rows).set_index("timestamp"))


def make_ninja_client(cfg: Config, workdir: Path) -> NinjaClient:
    need = ["NINJA_USERNAME", "NINJA_PASSWORD", "NINJA_CID", "NINJA_SECRET"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"Set {', '.join(missing)} in .env (see .env.example).")
    dev_file = workdir / ".ninja_device_id"                 # a stable id for this PC (NinjaTrader requires one)
    if dev_file.exists():
        device = dev_file.read_text(encoding="utf-8").strip()
    else:
        device = str(uuid.uuid4())
        dev_file.write_text(device, encoding="utf-8")
    c = NinjaClient(os.environ["NINJA_USERNAME"], os.environ["NINJA_PASSWORD"], os.environ["NINJA_CID"],
                    os.environ["NINJA_SECRET"], env=cfg.ninja_env, device_id=device)
    c.login()
    return c


# ============================================================================ bot
class NinjaBot(Bot):
    broker_name = "NinjaTrader"

    def __init__(self, client, cfg: Config, live: bool, workdir: Path):
        super().__init__(client, cfg, live, workdir)
        self.account: dict | None = None
        self.contract: dict | None = None                    # today's contract (id, name)
        self.tick = Decimal("0.01")
        self._meta: dict[str, dict] = {}                     # order id -> {type, side, qty}
        self._quote_cache: tuple[float, float] | None = None  # (monotonic time, price)
        self.unit_label = "ct"

    # ------------------------------------------------------------ setup
    def _acct(self) -> dict:
        if self.account is None:
            accts = [a for a in (self.client.get("/account/list") or []) if a.get("active", True)]
            if not accts:
                raise NinjaError("No active NinjaTrader account found for this login.")
            if self.cfg.ninja_account:
                accts = [a for a in accts if a.get("name") == self.cfg.ninja_account]
                if not accts:
                    raise NinjaError(f"Account {self.cfg.ninja_account!r} not found on this login.")
            self.account = accts[0]
        return self.account

    def scan_cfg(self) -> Config:
        # futures: the contract list replaces the stock watchlist, and stock-only filters are skipped
        # (contract prices and volumes aren't comparable to share prices and dollar volumes)
        return dataclasses.replace(self.cfg, watchlist=self._tradable_contracts(), min_price=0.0,
                                   min_avg_dollar_volume=0.0)

    def _tradable_contracts(self) -> list[str]:
        """Contracts from futures_watchlist that aren't too close to expiry / first notice."""
        keep = []
        today = now_et().date()
        for name in self.cfg.futures_watchlist:
            try:
                c = self.client.get("/contract/find", name=name)
                m = self.client.get("/contractMaturity/item", id=c["contractMaturityId"])
                dates = [pd.Timestamp(m[k]).date() for k in ("firstIntentDate", "expirationDate") if m.get(k)]
                limit = min(dates) if dates else None
                if limit and (limit - today).days <= self.cfg.futures_roll_days:
                    log.warning("%s: first notice/expiry %s is within %d days - skipped. Switch to the next "
                                "contract month in futures_watchlist.", name, limit, self.cfg.futures_roll_days)
                    continue
                keep.append(name)
            except Exception as e:
                log.warning("%s: could not look up the contract (%s) - skipped", name, e)
        return keep

    def _on_activate(self, sym: str) -> None:
        # contract size and tick for the symbol the bot is about to chart / trade
        if getattr(self, "contract", None) is None or self.contract.get("name") != sym:
            self._load_contract(sym)

    def _load_contract(self, name: str) -> None:
        c = self.client.get("/contract/find", name=name)
        m = self.client.get("/contractMaturity/item", id=c["contractMaturityId"])
        prod = self.client.get("/product/item", id=m["productId"])
        self.contract = {"id": c["id"], "name": c["name"]}
        self.point_value = float(prod["valuePerPoint"])
        self.tick = Decimal(str(prod["tickSize"]))
        log.info("%s: $%.2f per 1.00 move, tick %s ($%.2f per tick)", name, self.point_value, self.tick,
                 float(self.tick) * self.point_value)

    def session_checks(self) -> None:
        a = self._acct()
        bal = self.client.post("/cashBalance/getcashbalancesnapshot", {"accountId": a["id"]})
        log.info("NinjaTrader %s account %s | net liquidation %.2f | margin in use %.2f",
                 "DEMO" if self.cfg.ninja_env != "live" else "LIVE (real money)", a.get("name"),
                 float(bal.get("netLiq") or 0), float(bal.get("initialMargin") or 0))

    def snapshot_settled_cash(self) -> None:
        self.settled_start, self.spent_today = None, 0.0
        log.info("Futures trade on margin; the settled-cash rule doesn't apply. Contract cap: %d per trade.",
                 self.cfg.futures_max_contracts)

    # ------------------------------------------------------------ sizing & prices
    def size_position(self, equity: float, entry: float, stop: float) -> int:
        risk_per_contract = (entry - stop) * self.point_value
        if risk_per_contract <= 0:
            return 0
        n = int(equity * self.cfg.risk_per_trade_pct / 100 // risk_per_contract)
        if n < 1:
            log.info("One contract would risk $%.2f, more than %.2f%% of $%.0f - trade skipped.",
                     risk_per_contract, self.cfg.risk_per_trade_pct, equity)
        return max(0, min(n, self.cfg.futures_max_contracts))

    def _cap_shares(self, shares: int, entry_ref: float) -> int:
        return min(shares, self.cfg.futures_max_contracts)

    def _round_price(self, x: float, rounding) -> Decimal:
        ticks = (Decimal(str(x)) / self.tick).quantize(Decimal("1"), rounding=rounding)
        return (ticks * self.tick).normalize()

    # ------------------------------------------------------------ data
    def _extra_state(self):
        return {"meta": self._meta}

    def _restore_extra(self, extra):
        self._meta.update(extra.get("meta") or {})

    def _stop_price_of(self, order):
        return None          # NinjaTrader: not read back yet (a hand-raised stop there isn't followed)

    def _tick(self):
        return self.tick

    def fetch_many(self, symbols, period):
        # one market-data connection at a time: NinjaTrader allows only two sessions per user
        out = {}
        for sym in dict.fromkeys(symbols):
            try:
                out[sym] = self.fetch_bars(sym, period)
            except Exception as e:
                log.warning("bars for %s failed: %s", sym, e)
        return out

    def fetch_bars(self, symbol: str, period: str) -> pd.DataFrame:
        # futures trade ~23 h/day: 2000 5-min bars ~ 7 sessions; scale the count to the bar size
        base = 2000 if period != "DAY" else 320
        count = int(min(10000, base * 5 / self.bm))
        return bars_to_df(self.client.chart_bars(symbol, self.bm, count))

    def liquidity(self, symbols):
        return None                                     # volume filter is off for futures (scan_cfg)

    def _last_trade_price(self) -> float | None:
        # one market-data connection at most every 60 s (bars already give a price every 5 minutes)
        now = _time.monotonic()
        if self._quote_cache and now - self._quote_cache[0] < 60:
            return self._quote_cache[1]
        p = self.client.last_trade(self.contract["name"] if self.contract else self.symbol)
        if p is not None:
            self._quote_cache = (now, p)
        return p

    # ------------------------------------------------------------ account
    def account_equity(self) -> float:
        bal = self.client.post("/cashBalance/getcashbalancesnapshot", {"accountId": self._acct()["id"]})
        return float(bal.get("netLiq") or bal.get("totalCashValue") or 0)

    def _cash_now(self):
        return None, None

    def held_quantity(self) -> int:
        cid, aid = self.contract["id"], self._acct()["id"]
        for p in self.client.get("/position/list") or []:
            if p.get("contractId") == cid and p.get("accountId") == aid:
                return max(0, int(p.get("netPos") or 0))      # long-only bot: shorts never count as ours
        return 0

    # ------------------------------------------------------------ orders
    def _order_body(self, **kw) -> dict:
        a = self._acct()
        return {"accountSpec": a["name"], "accountId": a["id"], "symbol": self.contract["name"],
                "timeInForce": "Day", "isAutomated": True, **kw}

    @staticmethod
    def _check(res: dict, what: str) -> dict:
        if not isinstance(res, dict) or res.get("failureReason") not in OK_RESULTS:
            reason = (res or {}).get("failureReason") if isinstance(res, dict) else res
            text = (res or {}).get("failureText", "") if isinstance(res, dict) else ""
            raise NinjaError(f"{what} rejected: {reason} {text}".strip())
        return res

    def _orders(self) -> list:
        ids = [i for i in self._meta]
        if not ids:
            return []
        items = self.client.get("/order/items", ids=",".join(ids)) or []
        out = []
        for o in items:
            oid = str(o["id"])
            meta = self._meta.get(oid, {})
            out.append(_NOrder(id=oid, parent=meta.get("parent"), side=str(o.get("action", "")).upper(),
                               type=meta.get("type", ""), open=str(o.get("ordStatus", "")).lower() in OPEN_NT,
                               qty=meta.get("qty"), created_at=None, raw=o))
        return out

    def _wait_fill(self, order_id: str, timeout: int = 60) -> dict:
        end = _time.monotonic() + timeout
        o = self.client.get("/order/item", id=int(order_id))
        while str(o.get("ordStatus")) not in ("Filled", "Canceled", "Rejected", "Expired", "Completed"):
            if _time.monotonic() > end:
                break
            _time.sleep(1)
            o = self.client.get("/order/item", id=int(order_id))
        return o

    def _avg_fill(self, order_id: str) -> float | None:
        fills = self.client.get("/fill/deps", masterid=int(order_id)) or []
        qty = sum(int(f["qty"]) for f in fills)
        return sum(float(f["price"]) * int(f["qty"]) for f in fills) / qty if qty else None

    def _submit_entry(self, shares: int, stop_d: Decimal, tgt_d: Decimal | None, entry_ref: float):
        body = self._order_body(action="Buy", orderQty=int(shares), orderType="Market",
                                bracket1={"action": "Sell", "orderType": "Stop", "stopPrice": float(stop_d),
                                          "timeInForce": "Day"})
        if tgt_d is not None:
            body["bracket2"] = {"action": "Sell", "orderType": "Limit", "price": float(tgt_d), "timeInForce": "Day"}
        res = self._check(self.client.post("/order/placeoso", body), "Entry order")
        oid = str(res["orderId"])
        self.my_order_ids.add(oid)
        self._meta[oid] = {"type": "MARKET", "side": "BUY", "qty": shares}
        if res.get("oso1Id"):
            sid = str(res["oso1Id"])
            self.my_order_ids.add(sid)
            self._meta[sid] = {"type": "STOP", "side": "SELL", "qty": shares, "parent": oid}
        if res.get("oso2Id"):
            tid = str(res["oso2Id"])
            self.my_order_ids.add(tid)
            self._meta[tid] = {"type": "LIMIT", "side": "SELL", "qty": shares, "parent": oid}
        log.info("OSO entry submitted: %s (stop leg %s at %s)", oid, res.get("oso1Id"), stop_d)
        o = self._wait_fill(oid)
        if str(o.get("ordStatus")) == "Filled":
            price = self._avg_fill(oid)
            log.info("filled %d %s @ %s", shares, self.contract["name"], price)
            return oid, price
        log.warning("entry not filled within 60s (status %s); cancelling", o.get("ordStatus"))
        try:
            self._cancel(oid)
        except Exception as e:
            log.error("cancel failed: %s", e)
        return oid, None

    def _place_stop(self, qty: int, stop_d: Decimal) -> str:
        res = self._check(self.client.post("/order/placeorder", self._order_body(
            action="Sell", orderQty=int(qty), orderType="Stop", stopPrice=float(stop_d))), "Stop order")
        oid = str(res["orderId"])
        self._meta[oid] = {"type": "STOP", "side": "SELL", "qty": qty}
        return oid

    def _replace_stop(self, so, new_d: Decimal) -> str:
        self._check(self.client.post("/order/modifyorder", {
            "orderId": int(so.id), "orderQty": int(so.qty), "orderType": "Stop", "stopPrice": float(new_d),
            "timeInForce": "Day", "isAutomated": True}), "Stop change")
        return so.id                                    # modifyOrder keeps the same order

    def _cancel(self, order_id: str) -> None:
        self._check(self.client.post("/order/cancelorder", {"orderId": int(order_id), "isAutomated": True}),
                    "Cancel")

    def _market_sell(self, qty: int):
        res = self._check(self.client.post("/order/placeorder", self._order_body(
            action="Sell", orderQty=int(qty), orderType="Market")), "Sell order")
        oid = str(res["orderId"])
        self._meta[oid] = {"type": "MARKET", "side": "SELL", "qty": qty}
        o = self._wait_fill(oid)
        if str(o.get("ordStatus")) == "Filled":
            return oid, self._avg_fill(oid)
        log.error("sell not confirmed (status %s) - CHECK THE ACCOUNT", o.get("ordStatus"))
        return oid, None

    def _filled_sell_price(self, order_ids) -> float | None:
        price = None
        for oid in order_ids:
            if self._meta.get(str(oid), {}).get("side") != "SELL":
                continue
            try:
                o = self.client.get("/order/item", id=int(oid))
            except Exception:
                continue
            if str(o.get("ordStatus")) == "Filled":
                price = self._avg_fill(str(oid)) or price
        return price
