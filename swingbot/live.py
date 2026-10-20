"""Run the strategy against Public.com during one trading session.

DRY RUN IS THE DEFAULT. In dry run the bot reads real market data, makes the
same decisions, and logs the trades it *would* have made with simulated fills.
No orders are sent. Pass --live (and confirm) to send real orders.

Kill switch: create a file named STOP in the working folder. The bot cancels
its open orders, sells its position and exits within one loop.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import sys
import threading
import time as _time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .backtest import position_size
from .config import Config
from .data import fetch_public, normalize
from .scanner import scan
from .strategy import (add_indicators, find_swings, resistance_line, signal_at, support_line, target_for,
                       trailing_stop)

ET = ZoneInfo("America/New_York")
log = logging.getLogger("swingbot")


@dataclass
class _NOrder:
    """A broker order in one common shape."""
    id: str
    parent: str | None
    side: str          # "BUY" / "SELL"
    type: str          # "MARKET", "LIMIT", "STOP", "STOP_LIMIT", ...
    open: bool
    qty: object
    created_at: object
    raw: object = None


class MarketClosed(Exception):
    pass


OPEN_STATES = {"NEW", "PARTIALLY_FILLED", "PENDING_REPLACE", "PENDING_CANCEL"}


def _val(x) -> str:
    return str(getattr(x, "value", x))


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _price(x: float, rounding) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=rounding)


def cap_by_buying_power(shares: int, buying_power: float, price: float, buffer_pct: float) -> int:
    """Largest share count <= ``shares`` whose cost fits inside buying power, keeping a
    small buffer because a market order can fill above the last price."""
    if price <= 0 or buying_power <= 0:
        return 0
    usable = buying_power * (1 - buffer_pct / 100)
    return max(0, min(shares, math.floor(usable / price)))


def now_et() -> datetime:
    return datetime.now(ET)


def x_of(ts, minutes: int = 5) -> float:
    """Position of a time on today's chart, in bars of ``minutes`` since 09:30 ET."""
    ts = pd.Timestamp(ts).tz_convert(ET) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).tz_localize(ET)
    return (ts - ts.normalize() - pd.Timedelta(hours=9, minutes=30)).total_seconds() / (60.0 * minutes)


def _bar_start(ts, minutes: int):
    """Start of the ``minutes`` bar (counted from 9:30) that contains ``ts``."""
    m = ts.hour * 60 + ts.minute - 570
    return ts.replace(second=0, microsecond=0) - timedelta(minutes=m % minutes)


def complete_bars(df: pd.DataFrame, now: datetime, minutes: int = 5) -> pd.DataFrame:
    """Drop the bar that is still forming (bar start + 5 min is in the future)."""
    if df.empty:
        return df
    return df[df.index + timedelta(minutes=minutes) <= pd.Timestamp(now)]


class Bot:
    def __init__(self, client, cfg: Config, live: bool, workdir: Path):
        self.client = client
        self.cfg = cfg
        self.live = live
        self.workdir = workdir
        self.symbol: str | None = None
        self.history: pd.DataFrame | None = None     # prior-days bars for the picked symbol
        self.bm = int(cfg.bar_minutes)                 # bar size in minutes (1, 5, ...)
        self.forced_symbol: str | None = None          # set by `run --symbol X`: skip the scan, trade X
        self.last_bar_seen: pd.Timestamp | None = None
        self.trades_today = 0
        self.start_equity: float | None = None
        self.pos: dict | None = None                  # the bot's open position (paper or live)
        self.my_order_ids: set[str] = set()           # orders the bot created; it touches no others
        self.paper_pnl = 0.0
        self.halted = False
        self.trade_log = workdir / "trades.csv"
        self.settled_start: float | None = None       # settled cash when the session began
        self.spent_today = 0.0                         # cash committed to buys this session
        # --- for the live dashboard (display only; never used for trading decisions)
        self.dashboard = None
        self.state = "Starting"
        self.scan_results: list = []
        self.pick_reason = ""
        self.view: dict = {"bars": [], "lines": [], "swings": [], "days": []}
        self.closed: list[dict] = []
        self.last_price: float | None = None
        self.last_price_time: str | None = None
        self.final = False
        self.prepared = False                          # account checks + morning scan done
        self.point_value = 1.0                         # $ per 1.00 price move per unit (futures: contract multiplier)
        # --- bar size changes asked for from the dashboard (applied by the main loop, never mid-step)
        self._bm_lock = threading.Lock()
        self.pending_bm: int | None = None
        self._resync = False                           # first check after a change: rebuild only, no new orders
        self.unit_label = "sh"                         # shown on the dashboard ("ct" for futures contracts)

    # ------------------------------------------------------------------ setup
    def account_equity(self) -> float:
        pf = self.client.get_portfolio()
        eq = getattr(pf, "total_account_value", None)
        if eq is None and pf.equity:
            eq = sum(Decimal(str(e.value)) for e in pf.equity if getattr(e, "value", None) is not None)
        return float(eq or 0)

    def morning_scan(self) -> None:
        with self._bm_lock:                            # a change asked for before the scan just applies
            if self.pending_bm:
                self.bm, self.pending_bm = self.pending_bm, None
        today = now_et().date()
        if self.forced_symbol:                         # `run --symbol X`: you picked it - no scan
            sym = self.forced_symbol
            df = self.fetch_bars(sym, "WEEK")
            self.symbol = sym
            self.history = df[df.index.date < today] if not df.empty else df
            self.pick_reason = f"chosen by you ({self.bm}-minute bars)"
            log.info("TODAY'S PICK: %s - chosen by you, %d-minute bars, scan skipped", sym, self.bm)
            return
        scfg = self.scan_cfg()
        log.info("Scanning %d symbols%s", len(scfg.watchlist),
                 f" from {self.cfg.watchlist_file}" if self.cfg.watchlist_file and scfg.watchlist is self.cfg.watchlist else "")
        t0 = _time.monotonic()
        raw = self.fetch_many(scfg.watchlist, "WEEK")
        hist = {sym: df[df.index.date < today] if not df.empty else df for sym, df in raw.items()}
        log.info("Scan data: %d of %d symbols in %.0f s", len(hist), len(scfg.watchlist), _time.monotonic() - t0)
        try:
            liq = self.liquidity(list(hist))
        except Exception as e:
            log.warning("liquidity check failed (%s); using bar volume", e)
            liq = None
        self.scan_results = scan(hist, scfg, dollar_volume=liq)
        for r in self.scan_results[:25]:                             # best 25 (the log stays readable)
            log.info("scan %-6s score=%.2f %-16s %s", r.symbol, r.score, r.setup, r.detail)
        r0 = self.scan_results[0] if self.scan_results else None      # same as pick(), without scoring twice
        choice = r0 if r0 is not None and r0.score > 0 else None
        if not choice:
            log.info("No symbol has a setup today. Nothing to trade.")
            return
        self.symbol = choice.symbol
        self.history = hist[choice.symbol]
        self.pick_reason = f"{choice.setup} - {choice.detail}"
        log.info("TODAY'S PICK: %s (%s) - %s", choice.symbol, choice.setup, choice.detail)

    def _x(self, ts) -> float:
        return x_of(ts, self.bm)

    def fetch_many(self, symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
        """Bars for many symbols, ``scan_workers`` downloads at a time. A symbol that fails is
        logged and left out; the rest carry on. Results keep the watchlist order."""
        symbols = list(dict.fromkeys(symbols))
        got: dict[str, pd.DataFrame] = {}
        if not symbols:
            return got

        def one(sym):
            try:
                got[sym] = self.fetch_bars(sym, period)
            except Exception as e:
                log.warning("bars for %s failed: %s", sym, e)

        one(symbols[0])                       # first one alone: signs in / refreshes the token once
        rest = symbols[1:]
        workers = max(1, min(int(self.cfg.scan_workers), len(rest) or 1))
        with ThreadPoolExecutor(workers, thread_name_prefix="scan") as ex:
            futs = [ex.submit(one, s) for s in rest]
            for i, _ in enumerate(as_completed(futs), 2):
                if i % 100 == 0:
                    log.info("scan: %d of %d symbols downloaded", i, len(symbols))
                    self.publish(f"Scanning: {i} of {len(symbols)}")
        return {s: got[s] for s in symbols if s in got}

    # --------------------------------------------------- bar size on demand
    def bar_sizes(self) -> list[int]:
        """Bar sizes this broker can supply."""
        return [1, 5, 10, 15, 30] if self.broker_name == "Public" else [1, 2, 3, 5, 10, 15, 30]

    def request_bar_size(self, minutes: int) -> str:
        """Ask for a new bar size (called from the dashboard thread). Returns a message for the page.

        Only records the request; the main loop applies it between checks, so a change can
        never land in the middle of placing or moving an order.
        """
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ValueError("bar size must be a whole number of minutes")
        if minutes not in self.bar_sizes():
            raise ValueError(f"{self.broker_name} offers {', '.join(map(str, self.bar_sizes()))}-minute bars")
        if self.final:
            raise ValueError("the session is over")
        with self._bm_lock:
            if minutes == self.bm and self.pending_bm is None:
                return f"Already on {minutes}-minute bars"
            self.pending_bm = None if minutes == self.bm else minutes
        log.info("Dashboard: switch to %d-minute bars requested", minutes)
        return f"Switching to {minutes}-minute bars"

    def apply_pending_bar_size(self) -> bool:
        """Switch to a requested bar size. Keeps the open position and its stop as they are."""
        with self._bm_lock:
            new, self.pending_bm = self.pending_bm, None
        if not new or new == self.bm:
            return False
        old = self.bm
        if not self.symbol:
            self.bm = new
            log.info("Bar size now %d minutes", new)
            return True
        self.bm = new
        try:
            df = self.fetch_bars(self.symbol, "WEEK")
        except Exception as e:
            self.bm = old
            log.warning("Could not load %d-minute bars (%s) - staying on %d-minute bars", new, e, old)
            return False
        today = now_et().date()
        self.history = df[df.index.date < today] if not df.empty else df
        # chart positions are counted in bars: rescale the ones already drawn
        k = old / new
        if self.pos:
            p = self.pos
            p["entry_x"] *= k
            p["stop_steps"] = [[x * k, y] for x, y in p["stop_steps"]]
            if p.get("signal_ts") is not None:
                p["signal_ts"] = _bar_start(p["signal_ts"], new)
        for c in self.closed:
            c["entry_x"] *= k
            c["exit_x"] *= k
        self.last_bar_seen = None
        self._resync = True
        if " bars)" in self.pick_reason:
            self.pick_reason = f"chosen by you ({new}-minute bars)"
        log.info("Bar size changed from %d to %d minutes%s", old, new,
                 " - open position and its stop are unchanged" if self.pos else "")
        return True

    # ======================================================================
    # Broker hooks. Everything between here and the shared logic below is the
    # ONLY broker-specific code; these defaults talk to Public. AlpacaBot
    # (alpaca_live.py) overrides the same hooks.
    # ======================================================================
    broker_name = "Public"

    def scan_cfg(self) -> Config:
        """Settings the morning scan uses (futures swap in their contract list and skip stock filters)."""
        return self.cfg

    def size_position(self, equity: float, entry: float, stop: float) -> int:
        """Units to buy from the risk rules (shares for stocks; contracts for futures)."""
        return position_size(equity, entry, stop, self.cfg)

    def _round_price(self, x: float, rounding) -> Decimal:
        """Round an order price to what the market accepts (cents for stocks, tick size for futures)."""
        return _price(x, rounding)

    def _cap_shares(self, shares: int, entry_ref: float) -> int:
        """Live: trim the size to what the account may spend (cash-only and settled-cash rules)."""
        bp = self.available_buying_power()
        capped = cap_by_buying_power(shares, bp, entry_ref, self.cfg.buying_power_buffer_pct)
        if capped < shares:
            log.info("Size cut from %d to %d shares to fit spendable cash $%.2f", shares, capped, bp)
        # count the money as spent at submission (with the slippage buffer), even if the
        # order later fails, so the bot can never double-spend settled cash
        self.spent_today += capped * entry_ref * (1 + self.cfg.buying_power_buffer_pct / 100)
        return capped

    def session_checks(self) -> None:
        """Broker-specific checks logged at the start of a live session."""
        return None

    def fetch_bars(self, symbol: str, period: str) -> pd.DataFrame:
        """Regular-hours bars of ``bar_minutes``. period: "WEEK" (scan history) or "DAY" (today)."""
        return fetch_public(self.client, symbol, period, self.bm)

    def liquidity(self, symbols: list[str]) -> dict | None:
        """Average daily dollar volume per symbol, if the bar feed can't be trusted for it."""
        return None

    def _instrument(self):
        from public_api_sdk import InstrumentType, OrderInstrument
        return OrderInstrument(symbol=self.symbol, type=InstrumentType.EQUITY)

    def _last_trade_price(self) -> float | None:
        q = self.client.get_quotes([self._instrument()])
        return float(q[0].last) if q and getattr(q[0], "last", None) is not None else None

    def _cash_now(self) -> tuple[float | None, float | None]:
        """(buying power the bot may use, settled cash right now - None if the broker doesn't say)."""
        pf = self.client.get_portfolio()
        bp = getattr(pf, "buying_power", None)
        field = "buying_power" if self.cfg.use_margin else "cash_only_buying_power"
        val = getattr(bp, field, None) if bp is not None else None
        # Withdrawable cash excludes unsettled sale proceeds: Public's closest figure to settled cash.
        atw = getattr(pf, "available_to_withdraw", None)
        settled = getattr(atw, "cash_only_available_to_withdraw", None) if atw is not None else None
        return (float(val) if val is not None else None, float(settled) if settled is not None else None)

    def held_quantity(self) -> int:
        """Total shares of today's symbol in the account (may include shares you owned before)."""
        pf = self.client.get_portfolio()
        for p in pf.positions or []:
            if p.instrument.symbol == self.symbol:
                return int(Decimal(str(p.quantity)))
        return 0

    def _orders(self) -> list:
        """Orders on today's symbol, normalized: id, parent (bracket entry id), side, type, open, qty, created_at."""
        pf = self.client.get_portfolio()
        out = []
        for o in pf.orders or []:
            if o.instrument.symbol != self.symbol:
                continue
            out.append(_NOrder(id=o.order_id, parent=o.bracket_id, side=_val(o.side).upper(),
                               type=_val(o.type).upper(), open=_val(o.status) in OPEN_STATES,
                               qty=o.quantity, created_at=o.created_at, raw=o))
        return out

    def _submit_entry(self, shares: int, stop_d: Decimal, tgt_d: Decimal | None, entry_ref: float):
        """Market buy with the stop attached. Returns (order_id, fill_price or None)."""
        from public_api_sdk import (EquityMarketSession, OrderClass, OrderExpirationRequest, OrderRequest,
                                    OrderSide, OrderType, StopLoss, TakeProfit, TimeInForce)
        req = OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(shares),
            equity_market_session=EquityMarketSession.CORE,
            use_margin=self.cfg.use_margin,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLoss(stop_price=stop_d),
            take_profit=TakeProfit(limit_price=tgt_d) if tgt_d is not None else None,
        )
        order = self.client.place_order(req)
        self.my_order_ids.add(order.order_id)
        log.info("bracket order submitted: %s", order.order_id)
        try:
            filled = order.wait_for_fill(timeout=60)
            log.info("filled %s @ %s", filled.filled_quantity, filled.average_price)
            return order.order_id, float(filled.average_price)
        except Exception as e:
            log.warning("entry not fully confirmed within 60s (%s); cancelling any remainder", e)
            try:
                order.cancel()
            except Exception as ce:
                log.error("cancel failed: %s", ce)
            return order.order_id, None

    def _place_stop(self, qty: int, stop_d: Decimal) -> str:
        from public_api_sdk import (EquityMarketSession, OpenCloseIndicator, OrderExpirationRequest,
                                    OrderRequest, OrderSide, OrderType, TimeInForce)
        order = self.client.place_order(OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.SELL,
            order_type=OrderType.STOP,
            stop_price=stop_d,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(qty),
            open_close_indicator=OpenCloseIndicator.CLOSE,
            equity_market_session=EquityMarketSession.CORE,
        ))
        return order.order_id

    def _replace_stop(self, so, new_d: Decimal) -> str:
        """Reprice the stop in one step (cancel-and-replace). Returns the new order id; raises on refusal."""
        from public_api_sdk import CancelAndReplaceRequest, OrderExpirationRequest, OrderType, TimeInForce
        exp = getattr(so.raw, "expiration", None)
        tif = getattr(exp, "time_in_force", None) or TimeInForce.DAY
        repl = self.client.cancel_and_replace_order(CancelAndReplaceRequest(
            order_id=so.id,
            request_id=str(uuid.uuid4()),
            order_type=OrderType.STOP,
            expiration=OrderExpirationRequest(time_in_force=tif,
                                              expiration_time=getattr(exp, "expiration_time", None)),
            quantity=so.qty,
            stop_price=new_d,
        ))
        return repl.order_id

    def _cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id=order_id)

    def _market_sell(self, qty: int):
        """Sell the bot's shares at market. Returns (order_id, fill_price or None)."""
        from public_api_sdk import (EquityMarketSession, OpenCloseIndicator, OrderExpirationRequest,
                                    OrderRequest, OrderSide, OrderType, TimeInForce)
        order = self.client.place_order(OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=self._instrument(),
            order_side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(qty),
            open_close_indicator=OpenCloseIndicator.CLOSE,
            equity_market_session=EquityMarketSession.CORE,
        ))
        try:
            return order.order_id, float(order.wait_for_fill(timeout=60).average_price)
        except Exception as e:
            log.error("sell not confirmed: %s - CHECK THE ACCOUNT", e)
            return order.order_id, None

    def _filled_sell_price(self, order_ids) -> float | None:
        """Average price of a filled SELL among the bot's orders (to record a stop-out)."""
        price = None
        for oid in order_ids:
            try:
                o = self.client.get_order(order_id=oid)
            except Exception:
                continue
            if (_val(getattr(o, "side", "")).upper() == "SELL" and _val(getattr(o, "status", "")).upper() == "FILLED"
                    and getattr(o, "average_price", None) is not None):
                price = float(o.average_price)
        return price

    # ======================================================================
    # Shared logic (all brokers)
    # ======================================================================
    def snapshot_settled_cash(self) -> None:
        """Record settled cash at the start of the session (before the bot trades)."""
        _, self.settled_start = self._cash_now()
        self.spent_today = 0.0
        if self.cfg.settled_cash_only:
            if self.settled_start is None:
                log.warning("%s did not report settled cash; the settled-cash check will block all buys today.",
                            self.broker_name)
            else:
                log.info("Settled cash at start of session: $%.2f", self.settled_start)

    def available_buying_power(self) -> float:
        """Money the bot may spend on the next buy. 0 if anything needed is unknown (fail safe).

        Without margin, this is the smallest of:
          * cash-only buying power right now
          * settled cash right now (excludes unsettled proceeds)            [settled_cash_only]
          * settled cash at the start of the day minus what the bot has
            already spent today; today's sale proceeds are never added back [settled_cash_only]
        """
        bp, settled_now = self._cash_now()
        if bp is None:
            return 0.0
        limits = [bp]
        if self.cfg.settled_cash_only:
            if settled_now is None or self.settled_start is None:
                return 0.0
            limits += [settled_now, self.settled_start - self.spent_today]
        return max(0.0, min(limits))

    def bot_shares(self) -> int:
        """Shares the BOT bought and still holds. Never counts shares you already owned."""
        if not self.pos:
            return 0
        return max(0, min(self.pos["shares"], self.held_quantity() - self.pos.get("base_qty", 0)))

    def _my_open_orders(self) -> list:
        """Open orders the bot itself created (its entries and their stop/target legs) - never yours."""
        return [o for o in self._orders()
                if o.open and (o.id in self.my_order_ids or (o.parent and o.parent in self.my_order_ids))]

    def _my_stop_order(self):
        for o in self._my_open_orders():
            if o.side == "SELL" and o.type in ("STOP", "STOP_LIMIT"):
                return o
        # Fallback in case the broker doesn't tag bracket legs with the entry id: a sell stop on this
        # symbol for exactly the bot's share count, created after the bot's entry.
        if self.pos and self.pos.get("entry_time"):
            for o in self._orders():
                if (o.open and o.side == "SELL" and o.type in ("STOP", "STOP_LIMIT")
                        and o.qty is not None and int(Decimal(str(o.qty))) == self.pos["shares"]
                        and o.created_at is not None and o.created_at >= self.pos["entry_time"]):
                    self.my_order_ids.add(o.id)
                    return o
        return None

    def enter(self, entry_ref: float, stop: float, kind: str, signal_ts=None) -> None:
        cfg = self.cfg
        equity = self.account_equity() if self.live else cfg.starting_equity + self.paper_pnl
        shares = self.size_position(equity, entry_ref, stop)
        if self.live and shares >= 1:
            shares = self._cap_shares(shares, entry_ref)
        if shares < 1:
            log.info("Signal %s skipped: position size rounds to 0 shares", kind)
            return
        stop_d = self._round_price(stop, ROUND_DOWN)
        tgt_d = self._round_price(target_for(entry_ref, stop, cfg), ROUND_UP) if cfg.use_target else None
        log.info("ENTER %s %s x%d ~%.2f stop %s target %s", kind, self.symbol, shares, entry_ref, stop_d,
                 tgt_d if tgt_d is not None else "none (trailing stop)")
        self.trades_today += 1
        ex = self._x(signal_ts) + 1 if signal_ts is not None else self._x(now_et())
        pos = dict(kind=kind, entry=entry_ref, init_stop=float(stop_d), stop=float(stop_d),
                   target=float(tgt_d) if tgt_d is not None else None, shares=shares, signal_ts=signal_ts,
                   entry_x=ex, entry_clock=now_et().strftime("%H:%M"), stop_steps=[[ex, float(stop_d)]])

        if not self.live:
            self.pos = pos
            return

        base_qty = self.held_quantity()          # shares you already owned - never touched
        entry_time = datetime.now(ZoneInfo("UTC"))
        _, fill_price = self._submit_entry(shares, stop_d, tgt_d, entry_ref)
        got = max(0, self.held_quantity() - base_qty)
        if got <= 0:
            log.warning("No shares were bought.")
            return
        pos.update(entry=fill_price if fill_price is not None else entry_ref, shares=min(got, shares),
                   base_qty=base_qty, entry_time=entry_time)
        self.pos = pos
        self._record("entry", 0.0)
        self.ensure_stop(wait_seconds=15)

    def ensure_stop(self, wait_seconds: int = 0) -> None:
        """Live: make sure the bot's shares are protected by a stop at the broker. The broker creates
        the bracket's stop leg itself when the entry fills; if it hasn't appeared after
        ``wait_seconds`` (e.g. a partial fill), place a standalone stop for the bot's shares."""
        if not (self.live and self.pos):
            return
        for _ in range(max(1, wait_seconds // 3)):
            if self._my_stop_order() is not None:
                return
            if wait_seconds:
                _time.sleep(3)
        qty = self.bot_shares()
        if qty <= 0:
            return
        oid = self._place_stop(qty, self._round_price(self.pos["stop"], ROUND_DOWN))
        self.my_order_ids.add(oid)
        log.warning("No stop found for %d %s - placed standalone stop at %.2f (order %s)",
                    qty, self.symbol, self.pos["stop"], oid)

    def raise_stop(self, new_stop: float) -> None:
        """Move the stop up. Live: reprice the existing stop order in one step, so a stop is
        always in place. If the broker refuses, the old stop stays and we retry next bar."""
        new_d = self._round_price(new_stop, ROUND_DOWN)
        if float(new_d) <= self.pos["stop"]:
            return
        log.info("TRAIL stop %s %.2f -> %s", self.symbol, self.pos["stop"], new_d)
        step_x = (self._x(self.last_bar_seen) + 1) if self.last_bar_seen is not None else self._x(now_et())
        if not self.live:
            self.pos["stop"] = float(new_d)
            self.pos["stop_steps"].append([step_x, float(new_d)])
            return
        so = self._my_stop_order()
        if so is None:
            self.pos["stop"] = float(new_d)
            self.ensure_stop()
            return
        try:
            self.my_order_ids.add(self._replace_stop(so, new_d))
            self.pos["stop"] = float(new_d)
            self.pos["stop_steps"].append([step_x, float(new_d)])
        except Exception as e:
            log.error("Could not raise stop (keeping %.2f, will retry next bar): %s", self.pos["stop"], e)

    def flatten(self, reason: str) -> None:
        if not self.symbol:
            return
        if not self.live:
            if self.pos:
                self._close_pos(self.last_close, reason)
            return
        for o in self._my_open_orders():                   # the bot's stop/target legs only
            try:
                self._cancel(o.id)
                log.info("cancel requested for %s", o.id)
            except Exception as e:
                log.error("cancel %s failed: %s", o.id, e)
        for _ in range(10):                                # wait until they're gone
            if not self._my_open_orders():
                break
            _time.sleep(1)
        qty = self.bot_shares()                            # only what the bot bought
        if qty <= 0:
            log.info("flatten (%s): bot holds no shares", reason)
            self.pos = None
            return
        oid, price = self._market_sell(qty)
        self.my_order_ids.add(oid)
        log.info("FLATTEN (%s): sell %d %s, order %s", reason, qty, self.symbol, oid)
        if price is not None:
            self._close_pos(price, reason)

    def _check_live_exit(self) -> None:
        """Live: notice when the broker's stop (or target) has sold the bot's shares."""
        if not (self.live and self.pos) or self.bot_shares() > 0:
            return
        ids = set(self.my_order_ids)
        try:                                             # include bracket legs the broker created
            ids |= {o.id for o in self._orders() if o.parent and o.parent in self.my_order_ids}
        except Exception:
            pass
        price = self._filled_sell_price(list(ids))
        if price is None:
            price = self.last_close
        reason = "target" if (self.pos["target"] and price >= self.pos["target"]) else "stop"
        self._close_pos(price, reason)

    # ----------------------------------------------------------- exits & logging
    last_close: float = float("nan")

    def _paper_check(self, bar: pd.Series) -> None:
        p = self.pos
        if not p:
            return
        if bar["low"] <= p["stop"]:
            self._close_pos(min(bar["open"], p["stop"]), "stop")
        elif p["target"] is not None and bar["high"] >= p["target"]:
            self._close_pos(max(bar["open"], p["target"]), "target")

    def _close_pos(self, price: float, reason: str) -> None:
        p = self.pos
        if reason == "stop" and p["stop"] > p["init_stop"]:
            reason = "trail"
        pnl = (price - p["entry"]) * p["shares"] * self.point_value
        if not self.live:
            self.paper_pnl += pnl
        log.info("%sEXIT %s @ %.2f (%s) P&L %.2f", "" if self.live else "[paper] ", self.symbol, price, reason, pnl)
        self._record(reason, pnl, exit_price=price)
        exit_x = self._x(self.last_bar_seen) if (self.last_bar_seen is not None and reason not in
                                                 ("end of day", "kill switch")) else min(390 / self.bm - 1, self._x(now_et()))
        self.closed.append(dict(kind=p["kind"], shares=p["shares"], entry=p["entry"], exit=price, pnl=pnl,
                                reason=reason, entry_time=p["entry_clock"], exit_time=now_et().strftime("%H:%M"),
                                entry_x=p["entry_x"], exit_x=max(exit_x, p["entry_x"])))
        self.pos = None

    def _record(self, event, pnl, exit_price=None) -> None:
        p = self.pos
        new = not self.trade_log.exists()
        with self.trade_log.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "mode", "symbol", "kind", "event", "shares", "entry", "initial_stop",
                            "stop", "target", "exit", "pnl"])
            w.writerow([now_et().isoformat(timespec="seconds"), "live" if self.live else "paper", self.symbol,
                        p["kind"], event, p["shares"], round(p["entry"], 4), p["init_stop"], p["stop"],
                        "" if p["target"] is None else p["target"],
                        "" if exit_price is None else round(exit_price, 4), round(pnl, 2)])

    # ------------------------------------------------------------- main step
    def day_pnl(self) -> float:
        if not self.live:
            return self.paper_pnl
        return self.account_equity() - (self.start_equity or 0)

    def step(self) -> None:
        cfg = self.cfg
        now = now_et()
        today_bars = complete_bars(self.fetch_bars(self.symbol, "DAY"), now, self.bm)
        if not today_bars.empty:
            today_bars = today_bars[today_bars.index.date == now.date()]
        if today_bars.empty:
            if now.time() >= time(10, 15):
                raise MarketClosed("no bars today by 10:15 ET - market holiday?")
            return
        newest = today_bars.index[-1]
        if self.last_bar_seen is not None and newest <= self.last_bar_seen:
            return                                               # no new bar yet
        self.last_bar_seen = newest
        df = normalize(pd.concat([self.history, today_bars]))
        bar = df.iloc[-1]
        self.last_close = float(bar["close"])
        d = add_indicators(df, cfg)
        lo, hi = find_swings(d, cfg.swing_n)
        t = len(d) - 1
        self.last_price = self.last_close
        self.last_price_time = (newest + timedelta(minutes=self.bm)).strftime("%H:%M")
        try:
            self._build_view(d, lo, hi, len(today_bars))
        except Exception as e:                                    # display must never block trading
            log.debug("view failed: %s", e)

        if self._resync:
            # first check after a bar-size change: the newest bar may have closed a while ago,
            # so only redraw; exits, trailing and entries resume from the next new bar
            self._resync = False
            if self.live:
                self._check_live_exit()
            if self.pos:
                self.ensure_stop()
            return

        # 1) exits that happened during this bar
        if self.live:
            self._check_live_exit()
        else:
            self._paper_check(bar)

        # 2) manage an open position: keep a stop in place and trail it up
        if self.pos:
            self.ensure_stop()
            if cfg.trail_stop:
                ts = self.pos.get("signal_ts")
                from_bar = d.index.get_loc(ts) if ts is not None and ts in d.index else t
                new_stop = trailing_stop(d, t, cfg, lo, from_bar, self.pos["stop"])
                if new_stop > self.pos["stop"]:
                    self.raise_stop(new_stop)
            return

        # 3) look for a new entry
        bt = newest.time()
        loss_limit = (self.start_equity or cfg.starting_equity) * cfg.daily_loss_limit_pct / 100
        if self.day_pnl() <= -loss_limit and not self.halted:
            log.warning("Daily loss limit reached (%.2f). No more entries today.", self.day_pnl())
            self.halted = True
        if (self.halted or self.trades_today >= cfg.max_trades_per_day
                or not (_t(cfg.first_entry_time) <= bt <= _t(cfg.last_entry_time))):
            return
        sig = signal_at(d, t, cfg, lo, hi)
        if sig:
            log.info("SIGNAL %s on %s bar %s: line %.2f, stop %.2f",
                     sig.kind, self.symbol, newest.strftime("%H:%M"), sig.line_value, sig.stop)
            self.enter(sig.ref_price, sig.stop, sig.kind, signal_ts=newest)

    # ------------------------------------------------------------- dashboard
    VIEW_DAYS = 5                                   # sessions sent to the page (it shows 1, 2 or 5)

    def _build_view(self, d: pd.DataFrame, lo, hi, n_today: int) -> None:
        """Chart data for the page. x is in bars from today's 9:30 open; earlier sessions are
        negative (yesterday's 9:30 bar is -390/bar_minutes), so today's positions never change."""
        cfg, t = self.cfg, len(d) - 1
        per_day = 390 / self.bm
        dates = sorted(set(d.index.date) | {now_et().date()})[-self.VIEW_DAYS:]   # today, even before its first bar
        back = {day: len(dates) - 1 - k for k, day in enumerate(dates)}     # today 0, yesterday 1, ...
        i0 = int(np.searchsorted(d.index.date, dates[0])) if dates else len(d)
        xs = [self._x(ts) - back[ts.date()] * per_day for ts in d.index[i0:]]

        def xi(i):
            return xs[i - i0] if i >= i0 else xs[0] - (i0 - i)

        bars = [dict(x=xs[k], t=ts.strftime("%H:%M"), o=r.open, h=r.high, l=r.low, c=r.close)
                for k, (ts, r) in enumerate(d.iloc[i0:].iterrows())]
        days = [dict(x=-b * per_day, label=f"{day:%a} {day.month}/{day.day}", back=b)
                for day, b in back.items()]
        lines = []
        for kind, line in (("support", support_line(d, t, cfg, lo) if cfg.enable_bounce else None),
                           ("resistance", resistance_line(d, t, cfg, hi) if cfg.enable_breakout else None)):
            if line is None:
                continue
            a = max(line.i1, i0)
            lines.append(dict(kind=kind, pts=[[xi(a), line.value_at(a)], [xi(t), line.value_at(t)]],
                              anchors=[[xi(line.i1), line.p1], [xi(line.i2), line.p2]]))
        swings = []
        for i in range(i0, max(i0, t - cfg.swing_n + 1)):
            if lo[i]:
                swings.append(dict(x=xi(i), y=float(d["low"].iat[i]), kind="low"))
            if hi[i]:
                swings.append(dict(x=xi(i), y=float(d["high"].iat[i]), kind="high"))
        self.view = dict(bars=bars, lines=lines, swings=swings, days=days)

    def refresh_quote(self) -> None:
        """Latest trade price between bars (display only)."""
        if not self.symbol:
            return
        try:
            price = self._last_trade_price()
            if price is not None:
                self.last_price = price
                self.last_price_time = now_et().strftime("%H:%M:%S")
        except Exception as e:
            log.debug("quote failed: %s", e)

    def status(self) -> dict:
        cfg, p, last = self.cfg, self.pos, self.last_price
        realized = sum(c["pnl"] for c in self.closed)
        pos = None
        unreal = 0.0
        if p:
            unreal = (last - p["entry"]) * p["shares"] * self.point_value if last is not None else 0.0
            risk = (p["entry"] - p["init_stop"]) * p["shares"] * self.point_value
            pos = dict(kind=p["kind"], shares=p["shares"], entry=p["entry"], stop=p["stop"], init_stop=p["init_stop"],
                       entry_x=p["entry_x"], entry_time=p["entry_clock"], stop_steps=p["stop_steps"],
                       unreal=unreal, risk_dollars=risk, r=(unreal / risk) if risk else None)
        spendable = None
        if self.live and cfg.settled_cash_only and self.settled_start is not None:
            spendable = max(0.0, self.settled_start - self.spent_today)
        now = now_et()
        return dict(
            mode="LIVE" if self.live else "DRY RUN", state=self.state, final=self.final, unit=self.unit_label,
            bar_minutes=self.bm, bar_sizes=self.bar_sizes(), pending_bars=self.pending_bm,
            symbol=self.symbol, pick_reason=self.pick_reason, date=now.strftime("%Y-%m-%d"),
            updated=now.strftime("%H:%M:%S"), epoch=now.timestamp(),
            last=last, last_time=self.last_price_time, pos=pos, trades=self.closed,
            day_pnl=realized + unreal, trades_used=self.trades_today, max_trades=cfg.max_trades_per_day,
            halted=self.halted, spendable=spendable,
            scan=[dict(symbol=r.symbol, score=r.score, setup=r.setup, detail=r.detail) for r in self.scan_results[:50]],
            **self.view,
        )

    def publish(self, state: str | None = None) -> None:
        if state:
            self.state = state
        if self.dashboard is None:
            return
        try:
            self.dashboard.update(self.status())
        except Exception as e:                                    # display must never block trading
            log.debug("dashboard update failed: %s", e)

    def _auto_state(self) -> str:
        if self.pos:
            return f"In a trade - stop {self.pos['stop']:.2f}"
        if self.halted:
            return "Loss limit hit - watching only"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return "Trade limit reached - watching only"
        return f"Watching {self.symbol} for a setup"

    def prepare(self) -> None:
        """Account checks and the morning scan. Uses data up to yesterday's close only, so it can
        run before the open: start the bot early and the pick is ready when trading starts."""
        cfg = self.cfg
        self.start_equity = self.account_equity() if self.live else cfg.starting_equity
        if self.live:
            try:
                self.session_checks()
            except Exception as e:
                log.warning("account check failed: %s", e)
            self.snapshot_settled_cash()
        log.info("Mode: %s | start equity %.2f", "LIVE" if self.live else "DRY RUN (no orders)", self.start_equity)
        self.publish("Running morning scan")
        self.morning_scan()
        self.prepared = True
        if not self.symbol:
            self.final = True
            self.publish("No setup today - nothing to trade")
            return
        try:                                       # chart of the earlier days while waiting for the open
            if self.history is not None and len(self.history):
                d = add_indicators(self.history.copy(), cfg)
                lo, hi = find_swings(d, cfg.swing_n)
                self._build_view(d, lo, hi, 0)
        except Exception as e:
            log.debug("view failed: %s", e)
        self.publish(f"Picked {self.symbol}")

    def run_session(self) -> None:
        cfg = self.cfg
        if not self.prepared:
            self.prepare()
        if not self.symbol:
            return
        flatten_at = _t(cfg.flatten_time)
        stop_file = self.workdir / "STOP"
        while True:
            now = now_et()
            if stop_file.exists():
                log.warning("STOP file found - flattening and exiting")
                self.flatten("kill switch")
                self.final = True
                self.publish("Stopped by kill switch")
                return
            if now.time() >= flatten_at:
                self.flatten("end of day")
                log.info("Session done. Trades: %d, day P&L: %.2f", self.trades_today, self.day_pnl())
                self.final = True
                self.publish("Session finished")
                return
            try:
                if self.apply_pending_bar_size():
                    self.publish(f"Loading {self.bm}-minute bars")
                self.step()
            except MarketClosed as e:
                log.info("%s Exiting.", e)
                self.final = True
                self.publish("Market closed today")
                return
            except Exception as e:
                log.exception("step failed: %s", e)
            self.publish(self._auto_state())
            # until 15s after the next bar boundary: refresh the price for the page every 15s
            nxt = (now.replace(second=0, microsecond=0)
                   + timedelta(minutes=self.bm - now.minute % self.bm)) + timedelta(seconds=15)
            # (wakes every 2 s so a bar-size change from the page is picked up quickly)
            last_quote = now_et()
            while True:
                remaining = (nxt - now_et()).total_seconds()
                if remaining <= 0 or stop_file.exists() or self.pending_bm:
                    break
                _time.sleep(min(2.0 if self.dashboard is not None else 15.0, max(1.0, remaining)))
                if self.dashboard is not None and not self.pending_bm \
                        and (now_et() - last_quote).total_seconds() >= 14:
                    last_quote = now_et()
                    self.refresh_quote()
                    self.publish()


def make_client():
    from public_api_sdk import ApiKeyAuthConfig, PublicApiClient, PublicApiClientConfiguration
    key = os.environ.get("PUBLIC_API_SECRET_KEY")
    acct = os.environ.get("PUBLIC_ACCOUNT_ID")
    if not key or not acct:
        sys.exit("Set PUBLIC_API_SECRET_KEY and PUBLIC_ACCOUNT_ID (see .env.example).")
    return PublicApiClient(ApiKeyAuthConfig(api_secret_key=key, validity_minutes=480),
                           config=PublicApiClientConfiguration(default_account_number=acct))


def is_weekday(bot: "Bot | None" = None) -> bool:
    if now_et().weekday() < 5:
        return True
    log.info("Weekend - market closed.")
    if bot:
        bot.final = True
        bot.publish("Weekend - market closed")
    return False


def wait_for_open(bot: "Bot | None" = None) -> bool:
    """Sleep until 09:35 ET on a weekday. Returns False on weekends."""
    if not is_weekday(bot):
        return False
    now = now_et()
    start = now.replace(hour=9, minute=35, second=0, microsecond=0)
    if now < start:
        log.info("Waiting until %s ET", start.strftime("%H:%M"))
        while (left := (start - now_et()).total_seconds()) > 0:
            if bot:
                bot.publish(f"Picked {bot.symbol} - trading starts 9:35 ET ({int(left // 60)} min)"
                            if bot.symbol else f"Waiting for 9:35 ET ({int(left // 60)} min)")
            _time.sleep(min(30.0, left))
    return True
