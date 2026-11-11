"""Alpaca version of the live bot. Same strategy, risk rules, trailing stop and dashboard as the
Public version; only the broker hooks differ.

Modes (set by `run` flags and `alpaca_paper` in config.yaml):
  run                  dry run: real Alpaca data, simulated fills, no orders at all
  run --live           orders go to Alpaca: your PAPER account if alpaca_paper is true (default),
                       your REAL-MONEY account only if alpaca_paper is false

Alpaca specifics handled here:
  * Entry is a bracket order (market buy + stop + profit target). Alpaca lets the stop leg of a
    bracket be repriced in place but not the stop of a stop-only (OTO) order, so when no target
    is wanted the target is parked far above the price (alpaca_far_target_pct) where it won't fill.
  * Alpaca's account data has no separate "settled cash" figure. The bot uses the cash it had
    when the session started as its limit for the day and never reuses money from that day's sales.
  * Free data plan: live 5-minute bars come from one exchange (IEX). The morning volume screen
    uses full-market daily volume instead (allowed on the free plan because it's from past days).
"""
from __future__ import annotations

import logging
import os
import sys
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config
from .data import COLS, empty_bars, normalize
from .live import Bot, _NOrder, _price, now_et

log = logging.getLogger("swingbot")
UTC = ZoneInfo("UTC")
OPEN_ALPACA = {"new", "accepted", "pending_new", "partially_filled", "held", "pending_replace",
               "accepted_for_bidding", "calculated", "pending_cancel"}


def _v(x) -> str:
    return str(getattr(x, "value", x))


class AlpacaClients:
    """The two Alpaca connections the bot needs: trading (orders, account) and market data."""

    def __init__(self, trading, data, feed: str = "iex", paper: bool = True, public=None):
        self.trading, self.data, self.feed, self.paper = trading, data, feed.lower(), paper
        self.public = public                      # a Public.com client when price_data: public

    def close(self) -> None:
        if self.public is not None:
            try:
                self.public.close()
            except Exception:
                pass


def make_alpaca_client(cfg: Config) -> AlpacaClients:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env (see .env.example).")
    public = None
    if cfg.price_data == "public":
        from .live import make_client
        public = make_client()                    # needs PUBLIC_API_SECRET_KEY and PUBLIC_ACCOUNT_ID in .env
    return AlpacaClients(TradingClient(key, secret, paper=cfg.alpaca_paper),
                         StockHistoricalDataClient(key, secret), cfg.alpaca_feed, cfg.alpaca_paper, public)


class AlpacaBot(Bot):
    broker_name = "Alpaca"

    @property
    def data_feed_note(self) -> str:
        if self.client.public is not None:
            return "Public's price data"
        return "Alpaca's free IEX feed (one exchange)" if self.client.feed != "sip" else "Alpaca's data feed"

    # price_data: public - bars and quotes come from Public.com, orders still go to Alpaca
    def _public_bars(self, symbol: str, period: str) -> pd.DataFrame:
        from .data import fetch_public_bars
        return fetch_public_bars(self.client.public, symbol, period, self.bm)

    # ------------------------------------------------------------------ data
    def _feed(self, sip: bool = False):
        from alpaca.data.enums import DataFeed
        return DataFeed.SIP if (sip or self.client.feed == "sip") else DataFeed.IEX

    def _public_data_client(self):
        return self.client.public

    def momentum_bars(self, symbol: str, days: int) -> pd.DataFrame:
        """spy_momentum's history: enough earlier days of bars. With Public's price data, from Public if it
        has enough; otherwise (or without it) from Alpaca, at the bot's bar size."""
        if self.client.public is not None:
            try:
                df = Bot.momentum_bars(self, symbol, days)
                if df is not None and len(set(df.index.date)) >= days:
                    return df
            except Exception as e:
                log.warning("%s history from Public failed (%s) - using Alpaca's", symbol, e)
        df = self._one(self._bars_df(symbol, "DAYS", back=int(days * 1.6) + 10), symbol)
        return df[df.index.date < now_et().date()] if len(df) else df

    def _bars_df(self, symbols, period: str, back: int | None = None) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        now = now_et()
        back = back or (10 if self.bm >= 5 else 5)        # 1-minute bars: a shorter history is plenty
        start = (now - timedelta(days=back)) if period != "DAY" else now.replace(hour=4, minute=0, second=0, microsecond=0)
        return self.client.data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame(self.bm, TimeFrameUnit.Minute),
            start=start.astimezone(UTC), end=now.astimezone(UTC), feed=self._feed(),
            adjustment=Adjustment.SPLIT)).df

    @staticmethod
    def _one(df, symbol) -> pd.DataFrame:
        if df is None or df.empty:
            return empty_bars()
        if isinstance(df.index, pd.MultiIndex):
            if symbol not in df.index.get_level_values(0):
                return empty_bars()
            df = df.xs(symbol, level=0)
        return normalize(df)

    def fetch_bars(self, symbol: str, period: str) -> pd.DataFrame:
        if self.client.public is not None:
            return self._public_bars(symbol, period)
        return self._one(self._bars_df(symbol, period), symbol)

    BATCH = 100                                           # symbols per bars request

    def fetch_many(self, symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
        """Alpaca returns many symbols per request: 100 per request, scan_workers requests at once."""
        if self.client.public is not None:
            return Bot.fetch_many(self, symbols, period)      # Public: one symbol per request, in parallel
        symbols = list(dict.fromkeys(symbols))
        chunks = [symbols[i:i + self.BATCH] for i in range(0, len(symbols), self.BATCH)]
        got: dict[str, pd.DataFrame] = {}

        def run(chunk):
            try:
                df = self._bars_df(chunk, period)
            except Exception as e:
                if len(chunk) == 1:
                    log.warning("bars for %s failed: %s", chunk[0], e)
                    return
                log.warning("bars for %s..%s failed (%s) - retrying one by one", chunk[0], chunk[-1], e)
                for s in chunk:                          # one bad symbol can sink a whole request
                    run([s])
                return
            for s in chunk:
                got[s] = self._one(df, s)

        with ThreadPoolExecutor(max(1, min(int(self.cfg.scan_workers), len(chunks) or 1))) as ex:
            list(ex.map(run, chunks))
        return {s: got[s] for s in symbols if s in got}

    def daily_closes(self, symbols: list[str], days: int) -> dict:
        """Daily closes for the trend filter: many symbols per request (Public's data: one at a time)."""
        if self.client.public is not None:
            from .data import fetch_public_daily
            return {s: c for s in symbols if len(c := fetch_public_daily(self.client.public, s, days))}
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        now = now_et()
        out = {}
        for i in range(0, len(symbols), 200):
            df = self.client.data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbols[i:i + 200], timeframe=TimeFrame.Day,
                start=(now - timedelta(days=int(days * 1.6) + 15)).astimezone(UTC),
                end=(now - timedelta(minutes=16)).astimezone(UTC),   # free plan: full-market data must be 15+ min old
                feed=self._feed(sip=True), adjustment=Adjustment.SPLIT)).df
            if df is None or df.empty:
                continue
            for sym in symbols[i:i + 200]:
                try:
                    d = df.xs(sym, level=0)
                except KeyError:
                    continue
                idx = pd.DatetimeIndex(d.index).tz_convert("America/New_York")
                out[sym] = pd.Series(d["close"].to_numpy(dtype=float), index=idx.date).sort_index()
        return out

    def liquidity(self, symbols: list[str]) -> dict | None:
        """Full-market average daily dollar volume (last 5 sessions). Only needed on the free IEX feed,
        whose volume is a small slice of the real total."""
        if self.client.feed == "sip" or self.client.public is not None or not symbols:
            return None                                       # the bars already carry full-market volume
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        now = now_et()
        frames = []
        for i in range(0, len(symbols), 200):            # keep each request's address a sane length
            part = self.client.data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbols[i:i + 200], timeframe=TimeFrame.Day,
                start=(now - timedelta(days=14)).astimezone(UTC),
                end=(now - timedelta(minutes=16)).astimezone(UTC),  # free plan: full-market data must be 15+ min old
                feed=self._feed(sip=True))).df
            if part is not None and not part.empty:
                frames.append(part)
        out = {}
        if not frames:
            return out
        df = pd.concat(frames)
        for sym in symbols:
            try:
                d = df.xs(sym, level=0)
            except KeyError:
                continue
            idx = pd.DatetimeIndex(d.index).tz_convert("America/New_York")
            d = d[idx.date < now.date()]
            if len(d):
                out[sym] = float((d["close"] * d["volume"]).tail(5).mean())
        return out

    def _last_trade_price(self) -> float | None:
        if self.client.public is not None:
            from .data import public_last_price
            return public_last_price(self.client.public, self.symbol)
        from alpaca.data.requests import StockLatestTradeRequest
        res = self.client.data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=self.symbol, feed=self._feed()))
        t = res.get(self.symbol) if isinstance(res, dict) else res
        return float(t.price) if t is not None else None

    # --------------------------------------------------------------- account
    def account_equity(self) -> float:
        return float(self.client.trading.get_account().equity)

    def _cash_now(self) -> tuple[float | None, float | None]:
        a = self.client.trading.get_account()
        cash_bp = min(float(a.cash), float(a.non_marginable_buying_power))
        bp = float(a.buying_power) if self.cfg.use_margin else cash_bp
        # Alpaca reports no separate settled-cash figure. Cash at the start of the session is treated
        # as settled (the previous day's sales settle that day under T+1), and the bot's own ledger
        # (start cash minus today's buys, never adding today's sale proceeds) does the rest.
        return bp, cash_bp

    def session_checks(self) -> None:
        a = self.client.trading.get_account()
        log.info("Alpaca %s account %s | equity %.2f | cash %.2f | margin multiplier %s",
                 "PAPER" if self.client.paper else "LIVE (real money)", a.account_number,
                 float(a.equity), float(a.cash), a.multiplier)
        if a.trading_blocked or a.account_blocked or a.trade_suspended_by_user:
            log.error("Alpaca reports this account cannot trade right now - no orders will be placed.")
            self.halted = True
        if not self.cfg.use_margin and str(a.multiplier) not in ("1", "1.0"):
            log.info("This is a margin account; the bot only spends cash. You can also make it cash-only "
                     "at Alpaca (account configuration: max margin multiplier = 1).")
        if self.cfg.settled_cash_only:
            log.info("Alpaca doesn't report settled cash separately: today's limit is the cash at the start "
                     "of the session, and proceeds from today's sales are never reused.")

    def held_quantity(self) -> int:
        """Shares of the active symbol in the account: negative for a short position."""
        from alpaca.common.exceptions import APIError
        try:
            p = self.client.trading.get_open_position(self.symbol)
        except APIError as e:
            if _is_not_found(e):
                return 0
            raise                                   # a real error must never look like "no shares"
        q = int(Decimal(str(p.qty)))
        if _v(getattr(p, "side", "long")).lower() == "short":
            q = -abs(q)
        return q

    @property
    def tradable_note(self) -> str:
        return "at Alpaca and on Public's price data" if self.client.public is not None else "at Alpaca"

    def tradable_symbols(self) -> set[str] | None:
        """Active, tradable US stocks at Alpaca; with Public's price data, also tradable (so quoted) on Public."""
        assets = self._raw_assets()
        ok = {str(_field(a, "symbol")).upper() for a in assets or [] if _field(a, "tradable")}
        if not ok:
            raise RuntimeError("Alpaca returned no tradable stocks")
        if self.client.public is not None:
            from .live import public_tradable
            pub = public_tradable(self.client.public)
            key = lambda s: s.replace(".", "").replace("/", "").replace("-", "").replace(" ", "")
            pk = {key(s) for s in pub}
            ok = {s for s in ok if key(s) in pk}
        return ok

    def short_buying_power(self) -> float:
        a = self.client.trading.get_account()
        if not getattr(a, "shorting_enabled", False):
            return 0.0
        return max(0.0, min(float(a.buying_power), float(a.equity)))      # never more than the account value

    def short_check(self, symbol: str, qty: int) -> str | None:
        a = self.client.trading.get_account()
        if not getattr(a, "shorting_enabled", False):
            return "shorting isn't enabled on this Alpaca account (needs margin and $2,000 equity)"
        asset = self._raw_asset(symbol)
        if not _field(asset, "shortable"):
            return "not shortable at Alpaca"
        if not _field(asset, "easy_to_borrow"):
            return "hard to borrow"
        return None

    # Alpaca's asset list is read raw: alpaca-py rejects the whole list when one stock is on an exchange it
    # doesn't know yet (2026-10-27: 'TXSE', the Texas Stock Exchange).
    def _raw_assets(self):
        t = self.client.trading
        if hasattr(t, "get") and callable(getattr(t, "get")):
            return t.get("/assets", {"status": "active", "asset_class": "us_equity"})
        from alpaca.trading.requests import GetAssetsRequest
        return t.get_all_assets(GetAssetsRequest())

    def _raw_asset(self, symbol: str):
        t = self.client.trading
        if hasattr(t, "get") and callable(getattr(t, "get")):
            return t.get(f"/assets/{symbol}")
        return t.get_asset(symbol)

    # ---------------------------------------------------------------- orders
    def _orders(self) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        since = now_et().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        raw = self.client.trading.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, symbols=[self.symbol], nested=True, after=since, limit=200))
        out = []

        def add(o, parent=None):
            typ = _v(getattr(o, "order_type", None) or getattr(o, "type", "")).upper()
            out.append(_NOrder(id=str(o.id), parent=parent, side=_v(o.side).upper(), type=typ,
                               open=_v(o.status).lower() in OPEN_ALPACA, qty=o.qty, created_at=o.created_at, raw=o))

        for o in raw or []:
            add(o)
            for leg in (getattr(o, "legs", None) or []):
                add(leg, parent=str(o.id))
        return out

    def _wait_fill(self, order_id: str, timeout: int = 60):
        """Poll until filled or finished. Returns the last order state."""
        end = _time.monotonic() + timeout
        o = self.client.trading.get_order_by_id(order_id)
        while _v(o.status).lower() not in ("filled", "canceled", "rejected", "expired", "done_for_day"):
            if _time.monotonic() > end:
                break
            _time.sleep(1)
            o = self.client.trading.get_order_by_id(order_id)
        return o

    def _parked_target(self, entry: float, side: int) -> Decimal:
        """Where the take-profit leg waits when no target is wanted: far above (long) or below (short)."""
        if side > 0:
            return _price(entry * (1 + self.cfg.alpaca_far_target_pct / 100), ROUND_UP)
        return _price(max(0.01, entry * (1 - min(self.cfg.alpaca_far_target_pct, 90) / 100)), ROUND_DOWN)

    def _submit_entry(self, shares: int, stop_d: Decimal, tgt_d: Decimal | None, entry_ref: float, side: int = 1):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        # Alpaca can only reprice the stop of a full bracket, so with no target wanted the target is
        # parked far away from the price where it won't fill during the day.
        target = tgt_d if tgt_d is not None else self._parked_target(entry_ref, side)
        o = self.client.trading.submit_order(MarketOrderRequest(
            symbol=self.symbol, qty=shares, side=OrderSide.BUY if side > 0 else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET, client_order_id=str(uuid.uuid4()),
            stop_loss=StopLossRequest(stop_price=float(stop_d)),
            take_profit=TakeProfitRequest(limit_price=float(target))))
        oid = str(o.id)
        self.my_order_ids.add(oid)
        log.info("%sbracket order submitted: %s (stop %s, %s %s)", "short " if side < 0 else "", oid, stop_d,
                 "target" if tgt_d is not None else "parked target", target)
        o = self._wait_fill(oid)
        if _v(o.status).lower() == "filled":
            log.info("filled %s @ %s", o.filled_qty, o.filled_avg_price)
            return oid, float(o.filled_avg_price)
        log.warning("entry not fully filled within 60s (status %s); cancelling any remainder", _v(o.status))
        try:
            self.client.trading.cancel_order_by_id(oid)
        except Exception as e:
            log.error("cancel failed: %s", e)
        return oid, float(o.filled_avg_price) if getattr(o, "filled_avg_price", None) else None

    def _place_stop(self, qty: int, stop_d: Decimal) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        o = self.client.trading.submit_order(StopOrderRequest(
            symbol=self.symbol, qty=qty, side=OrderSide.BUY if self._exit_side() == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, stop_price=float(stop_d), client_order_id=str(uuid.uuid4())))
        return str(o.id)

    def _replace_stop(self, so, new_d: Decimal) -> str:
        from alpaca.trading.requests import ReplaceOrderRequest
        o = self.client.trading.replace_order_by_id(so.id, ReplaceOrderRequest(stop_price=float(new_d)))
        return str(o.id)

    def _set_target(self, price: float | None) -> None:
        """Move the take-profit leg of the open bracket (None = park it far away, i.e. no target)."""
        from alpaca.trading.requests import ReplaceOrderRequest
        exit_side = self._exit_side()
        leg = next((o for o in self._my_open_orders() if o.side == exit_side and o.type == "LIMIT"), None)
        if leg is None:
            raise RuntimeError("no take-profit leg found on the open bracket order")
        px = price if price is not None else float(self._parked_target(self.pos["entry"], self.pos.get("side", 1)))
        o = self.client.trading.replace_order_by_id(leg.id, ReplaceOrderRequest(limit_price=float(px)))
        self.my_order_ids.add(str(o.id))

    def _cancel(self, order_id: str) -> None:
        self.client.trading.cancel_order_by_id(order_id)

    def _market_sell(self, qty: int):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        o = self.client.trading.submit_order(MarketOrderRequest(
            symbol=self.symbol, qty=qty, side=OrderSide.BUY if self._exit_side() == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=str(uuid.uuid4())))
        oid = str(o.id)
        o = self._wait_fill(oid)
        if _v(o.status).lower() == "filled":
            return oid, float(o.filled_avg_price)
        log.error("%s not confirmed (status %s) - CHECK THE ACCOUNT", self._exit_verb(), _v(o.status))
        return oid, None

    def _filled_sell_price(self, order_ids) -> float | None:
        """Average price of this symbol's filled exits among `order_ids` (sells for a long, buys for a
        short; weighted by shares)."""
        qty = value = 0.0
        want = self._exit_side().lower()
        for oid in order_ids:
            try:
                o = self.client.trading.get_order_by_id(oid)
            except Exception:
                continue
            sym = getattr(o, "symbol", None)
            if sym and self.symbol and str(sym).upper() != self.symbol.upper():
                continue
            if _v(o.side).lower() == want and getattr(o, "filled_avg_price", None) is not None \
                    and float(getattr(o, "filled_qty", 0) or 0) > 0:
                q = float(o.filled_qty)
                qty, value = qty + q, value + q * float(o.filled_avg_price)
        return round(value / qty, 4) if qty else None


def _field(obj, name: str):
    """A field of an Alpaca object or of its raw JSON."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _is_not_found(e) -> bool:
    try:
        if e.status_code == 404:
            return True
    except Exception:
        pass
    try:
        return int(e.code) == 40410000            # "position does not exist"
    except Exception:
        return False
