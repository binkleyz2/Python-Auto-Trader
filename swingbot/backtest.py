"""Day-by-day backtest: morning scan -> one symbol -> 5-minute trading -> flat at close."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import time

import numpy as np
import pandas as pd

from .config import Config
from .scanner import pick
from .strategy import add_indicators, breakeven_stop, find_swings, signal_at, target_for, trailing_stop


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class Trade:
    date: str
    symbol: str
    kind: str
    entry_time: str
    entry: float
    stop: float          # initial stop (defines 1R)
    final_stop: float    # stop at exit, after any trailing
    target: float        # NaN when use_target is off
    shares: int
    exit_time: str
    exit: float
    reason: str          # "stop" (initial stop), "trail" (raised stop), "target", "flatten"
    pnl: float
    r_multiple: float


def position_size(equity: float, entry: float, stop: float, cfg: Config) -> int:
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    by_risk = equity * cfg.risk_per_trade_pct / 100 / risk_per_share
    by_cap = equity * cfg.max_position_pct / 100 / entry
    return max(0, math.floor(min(by_risk, by_cap)))


def simulate_day(sym: str, d: pd.DataFrame, is_low, is_high, day_idx: np.ndarray,
                 equity: float, cfg: Config) -> list[Trade]:
    """Trade one symbol for one session. ``day_idx`` = positional indices of today's bars in ``d``."""
    first_entry, last_entry, flatten = _t(cfg.first_entry_time), _t(cfg.last_entry_time), _t(cfg.flatten_time)
    slip = cfg.slippage_pct / 100
    trades: list[Trade] = []
    pos = None            # dict while in a trade
    pending = None        # Signal waiting for next bar's open
    day_pnl = 0.0
    loss_limit = equity * cfg.daily_loss_limit_pct / 100

    def close_pos(t, price, reason):
        nonlocal pos, day_pnl
        fill = price * (1 - slip)
        pnl = (fill - pos["entry"]) * pos["shares"]
        risk = (pos["entry"] - pos["init_stop"]) * pos["shares"]
        if reason == "stop" and pos["stop"] > pos["init_stop"]:
            reason = "trail"
        trades.append(Trade(
            date=str(d.index[t].date()), symbol=sym, kind=pos["kind"],
            entry_time=pos["time"], entry=round(pos["entry"], 4), stop=round(pos["init_stop"], 4),
            final_stop=round(pos["stop"], 4),
            target=round(pos["target"], 4) if pos["target"] is not None else float("nan"),
            shares=pos["shares"],
            exit_time=d.index[t].strftime("%H:%M"), exit=round(fill, 4), reason=reason,
            pnl=round(pnl, 2), r_multiple=round(pnl / risk, 2) if risk else 0.0,
        ))
        day_pnl += pnl
        pos = None

    for t in day_idx:
        bar = d.iloc[t]
        bt = d.index[t].time()

        # 1) open of bar t: flatten, or fill a pending entry
        if pos is not None and bt >= flatten:
            close_pos(t, bar["open"], "flatten")
        if pending is not None and pos is None and bt < flatten:
            entry = bar["open"] * (1 + slip)
            if entry > pending.stop:
                shares = position_size(equity + day_pnl, entry, pending.stop, cfg)
                if shares > 0:
                    pos = dict(kind=pending.kind, entry=entry, stop=pending.stop, init_stop=pending.stop,
                               target=target_for(entry, pending.stop, cfg) if cfg.use_target else None,
                               shares=shares, from_bar=pending.bar, time=d.index[t].strftime("%H:%M"))
        pending = None

        # 2) intrabar: stop first (conservative), then target
        if pos is not None:
            if bar["low"] <= pos["stop"]:
                close_pos(t, min(bar["open"], pos["stop"]), "stop")
            elif pos["target"] is not None and bar["high"] >= pos["target"]:
                close_pos(t, max(bar["open"], pos["target"]), "target")

        # 3) close of bar t: raise the trailing stop (takes effect from the next bar)
        if pos is not None and cfg.trail_stop:
            pos["stop"] = trailing_stop(d, t, cfg, is_low, pos["from_bar"], pos["stop"])
        if pos is not None:
            pos["stop"] = breakeven_stop(pos["entry"], pos["init_stop"], float(bar["close"]), pos["stop"], cfg)

        # 4) close of bar t: look for a new signal
        if (pos is None and first_entry <= bt <= last_entry
                and len(trades) < cfg.max_trades_per_day and day_pnl > -loss_limit):
            pending = signal_at(d, t, cfg, is_low, is_high)

    if pos is not None:                       # safety net: last bar of the day
        close_pos(day_idx[-1], d.iloc[day_idx[-1]]["close"], "flatten")
    return trades


def run(history: dict[str, pd.DataFrame], cfg: Config, lookback_days: int = 10,
        warmup_days: int = 3) -> tuple[pd.DataFrame, pd.Series, list[dict]]:
    """Returns (trades, end-of-day equity curve, daily pick log)."""
    prepared = {}
    for sym, df in history.items():
        d = add_indicators(df, cfg)
        lo, hi = find_swings(d, cfg.swing_n)
        prepared[sym] = (d, lo, hi, np.array(d.index.date))

    all_days = sorted(set().union(*[set(p[3]) for p in prepared.values()]))
    equity = cfg.starting_equity
    trades: list[Trade] = []
    curve = {}
    picks = []

    for k, day in enumerate(all_days):
        if k < warmup_days:
            continue
        window_start = all_days[max(0, k - lookback_days)]
        prior = {}
        for sym, (d, _, _, dates) in prepared.items():
            mask = (dates >= window_start) & (dates < day)
            if mask.any() and (dates == day).any():
                prior[sym] = history[sym][mask]
        choice = pick(prior, cfg)
        picks.append({"date": str(day), "symbol": choice.symbol if choice else None,
                      "setup": choice.setup if choice else "none",
                      "detail": choice.detail if choice else ""})
        if choice:
            d, lo, hi, dates = prepared[choice.symbol]
            day_idx = np.flatnonzero(dates == day)
            day_trades = simulate_day(choice.symbol, d, lo, hi, day_idx, equity, cfg)
            for tr in day_trades:
                equity += tr.pnl
            trades.extend(day_trades)
        curve[pd.Timestamp(day)] = equity

    tdf = pd.DataFrame([asdict(t) for t in trades])
    return tdf, pd.Series(curve, name="equity"), picks


def summarize(trades: pd.DataFrame, curve: pd.Series, cfg: Config) -> dict:
    if trades.empty:
        return {"trades": 0, "note": "no trades triggered"}
    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    gross_win, gross_loss = wins.pnl.sum(), -losses.pnl.sum()
    peak = curve.cummax()
    dd = ((curve - peak) / peak).min() if len(curve) else 0.0
    out = {
        "days": int(len(curve)),
        "trades": int(len(trades)),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "avg_r": round(trades.r_multiple.mean(), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "net_pnl": round(trades.pnl.sum(), 2),
        "return_pct": round(100 * (curve.iloc[-1] / cfg.starting_equity - 1), 2),
        "max_drawdown_pct": round(100 * dd, 2),
        "exits": trades.reason.value_counts().to_dict(),
    }
    for kind, g in trades.groupby("kind"):
        out[f"{kind}_trades"] = int(len(g))
        out[f"{kind}_win_rate_pct"] = round(100 * (g.pnl > 0).mean(), 1)
        out[f"{kind}_avg_r"] = round(g.r_multiple.mean(), 2)
    return out
