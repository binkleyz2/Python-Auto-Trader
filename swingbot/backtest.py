"""Backtest that trades the way the live bot does.

Each day: rank the watchlist on the days before (the morning scan), watch the best ``watch_count``,
and on every bar take the first signal among them (best-ranked first), one position at a time, until
``max_trades_per_day`` or the daily loss limit. Entries fill at the next bar's open plus slippage;
stops and targets fill inside a bar (stop first, the cautious assumption); everything is sold at 15:50.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import time

import numpy as np
import pandas as pd

from .config import Config
from .scanner import avg_dollar_volume, one_per_company, scan
from .trend import DayTrend, setup_side
from .strategy import (add_indicators, better_stop, cols, find_swings, mirror, sided_breakeven_stop, sided_trailing_stop,
                       signals_at, target_for)


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
    reason: str          # "stop" (initial stop), "trail" (moved stop), "target", "flatten"
    pnl: float
    r_multiple: float
    side: str = "long"   # "long" or "short"
    rank: int = 1        # the stock's place in the scan it was picked from (1 = best)
    scan: str = "morning"  # "morning", or "midday" for a stock picked by the midday rescan


def position_size(equity: float, entry: float, stop: float, cfg: Config, side: int = 1) -> int:
    risk_per_share = (entry - stop) * side             # a stop on the wrong side of the entry: no trade
    if risk_per_share <= 0:
        return 0
    by_risk = equity * cfg.risk_per_trade_pct / 100 / risk_per_share
    by_cap = equity * cfg.max_position_pct / 100 / entry
    return max(0, math.floor(min(by_risk, by_cap)))


# --------------------------------------------------------------------------------- one session
def simulate_watch(sessions: list[tuple], equity: float, cfg: Config, rescan=None, allow=None) -> list[Trade]:
    """Trade one session over several watched symbols, best-ranked first.
    ``sessions`` = [(symbol, d, is_low, is_high, md, day_idx), ...] where ``day_idx`` are the positions of
    today's bars in ``d`` (d has the earlier days too, so lines and swings can reach back).
    ``rescan(ts)`` = the midday rescan: new sessions ranked on the bars before ``ts`` (None = keep the
    morning list). Like the live bot it runs once, at ``midday_rescan_time``, or after an open trade closes
    if that is before ``midday_rescan_by``; not after the trade or loss limit.
    ``allow(symbol, side)`` = False for a signal the trend filter rules out (None = no filter)."""
    first_entry, last_entry, flatten = _t(cfg.first_entry_time), _t(cfg.last_entry_time), _t(cfg.flatten_time)
    slip = cfg.slippage_pct / 100
    trades: list[Trade] = []
    loss_limit = equity * cfg.daily_loss_limit_pct / 100
    state = dict(pos=None, pending=None, day_pnl=0.0, scan="morning")
    mid = _t(cfg.midday_rescan_time) if rescan is not None and str(cfg.midday_rescan_time or "").strip() else None
    mid_by = _t(cfg.midday_rescan_by) if mid is not None else None
    mid_done = mid is None

    def load(sessions_):
        a = {s[0]: dict(zip(s[1].index[s[5]], map(int, s[5]))) for s in sessions_}   # symbol -> {timestamp: position}
        return a, {s[0]: s for s in sessions_}, {s[0]: k + 1 for k, s in enumerate(sessions_)}

    at, info, rank = load(sessions)
    timeline = sorted(set().union(*[set(m) for m in at.values()])) if at else []

    def close_pos(sym, t, price, reason):
        pos = state["pos"]
        d = info[sym][1]
        side = pos["side"]
        fill = price * (1 - slip * side)                 # slippage always costs: sell lower, buy back higher
        pnl = (fill - pos["entry"]) * pos["shares"] * side
        risk = abs(pos["entry"] - pos["init_stop"]) * pos["shares"]
        if reason == "stop" and better_stop(pos["stop"], pos["init_stop"], side):
            reason = "trail"
        trades.append(Trade(
            date=str(d.index[t].date()), symbol=sym, kind=pos["kind"],
            entry_time=pos["time"], entry=round(pos["entry"], 4), stop=round(pos["init_stop"], 4),
            final_stop=round(pos["stop"], 4),
            target=round(pos["target"], 4) if pos["target"] is not None else float("nan"),
            shares=pos["shares"], exit_time=d.index[t].strftime("%H:%M"), exit=round(fill, 4), reason=reason,
            pnl=round(pnl, 2), r_multiple=round(pnl / risk, 2) if risk else 0.0,
            side="long" if side > 0 else "short", rank=pos["rank"], scan=pos["scan"]))
        state["day_pnl"] += pnl
        state["pos"] = None

    i = 0
    while i < len(timeline):
        ts = timeline[i]
        i += 1
        bt = ts.time()
        # 0) the midday rescan, at the open of the first bar at or after its time (bars before it are complete)
        if not mid_done and bt >= mid:
            if bt >= mid_by or len(trades) >= cfg.max_trades_per_day or state["day_pnl"] <= -loss_limit:
                mid_done = True                           # too late (a trade ran past it), or no entries left
            elif state["pos"] is None and state["pending"] is None:
                mid_done = True
                new = rescan(ts)
                if new:
                    sessions = new
                    at, info, rank = load(sessions)
                    state["scan"] = "midday"
                    timeline = sorted(t_ for t_ in set().union(*[set(m) for m in at.values()]) if t_ >= ts)
                    i = 0
                    continue
        pos = state["pos"]
        # 1) the open of this bar: sell at 15:50, or fill a waiting entry
        if pos is not None and bt >= flatten and ts in at[pos["sym"]]:
            t = at[pos["sym"]][ts]
            close_pos(pos["sym"], t, info[pos["sym"]][1]["open"].iat[t], "flatten")
        pend = state["pending"]
        if pend is not None and state["pos"] is None and ts in at[pend[0]]:
            sym, sig = pend
            state["pending"] = None
            if bt < flatten:
                d = info[sym][1]
                t = at[sym][ts]
                side = sig.side
                entry = d["open"].iat[t] * (1 + slip * side)
                if side * (entry - sig.stop) > 0:        # the open hasn't already gone through the stop
                    shares = position_size(equity + state["day_pnl"], entry, sig.stop, cfg, side)
                    if shares > 0:
                        state["pos"] = dict(sym=sym, kind=sig.kind, side=side, entry=entry, stop=sig.stop,
                                            init_stop=sig.stop, shares=shares, from_bar=sig.bar,
                                            rank=rank[sym], scan=state["scan"],
                                            target=target_for(entry, sig.stop, cfg) if cfg.use_target else None,
                                            time=d.index[t].strftime("%H:%M"))
        # 2) inside this bar: stop first (cautious), then target
        pos = state["pos"]
        if pos is not None and ts in at[pos["sym"]]:
            sym = pos["sym"]
            d, lo, hi, md = info[sym][1], info[sym][2], info[sym][3], info[sym][4]
            t = at[sym][ts]
            a = cols(d)
            bar = {k: a[k][t] for k in ("open", "high", "low", "close")}
            if pos["side"] > 0:
                if bar["low"] <= pos["stop"]:
                    close_pos(sym, t, min(bar["open"], pos["stop"]), "stop")
                elif pos["target"] is not None and bar["high"] >= pos["target"]:
                    close_pos(sym, t, max(bar["open"], pos["target"]), "target")
            else:
                if bar["high"] >= pos["stop"]:
                    close_pos(sym, t, max(bar["open"], pos["stop"]), "stop")
                elif pos["target"] is not None and bar["low"] <= pos["target"]:
                    close_pos(sym, t, min(bar["open"], pos["target"]), "target")
            # 3) the close of this bar: move the stop (takes effect from the next bar)
            pos = state["pos"]
            if pos is not None and cfg.trail_stop:
                pos["stop"] = sided_trailing_stop(d, md, t, cfg, lo, hi, pos["from_bar"], pos["stop"], pos["side"])
            if pos is not None:
                pos["stop"] = sided_breakeven_stop(pos["entry"], pos["init_stop"], float(bar["close"]), pos["stop"],
                                                   cfg, pos["side"])
        # 4) the close of this bar: a new signal on any watched symbol, best-ranked first
        if (state["pos"] is None and state["pending"] is None and first_entry <= bt <= last_entry
                and len(trades) < cfg.max_trades_per_day and state["day_pnl"] > -loss_limit):
            for sym, d, lo, hi, md, _ in sessions:
                if ts not in at[sym]:
                    continue
                sig = signals_at(d, md, at[sym][ts], cfg, lo, hi)
                if sig and allow is not None and not allow(sym, sig.side):
                    continue                              # against the trend: not traded
                if sig:
                    state["pending"] = (sym, sig)
                    break

    pos = state["pos"]
    if pos is not None:                                   # safety net: the last bar of the day
        t = max(at[pos["sym"]].values())
        close_pos(pos["sym"], t, info[pos["sym"]][1]["close"].iat[t], "flatten")
    return trades


def simulate_day(sym: str, d: pd.DataFrame, is_low, is_high, day_idx: np.ndarray,
                 equity: float, cfg: Config, md: pd.DataFrame | None = None) -> list[Trade]:
    """One symbol for one session (the watch-list engine with a single symbol)."""
    if md is None and cfg.direction != "long":
        md = mirror(d)
    return simulate_watch([(sym, d, is_low, is_high, md, day_idx)], equity, cfg)


# --------------------------------------------------------------------------------- many days
SCAN_KEYS = ("swing_n", "direction", "min_price", "min_avg_dollar_volume", "enable_bounce", "enable_breakout",
             "touch_tolerance_atr", "break_tolerance_atr", "atr_period", "max_line_age_bars", "min_anchor_gap_bars")


def prepare(history: dict[str, pd.DataFrame], cfg: Config) -> dict:
    """Indicators and swing points for each symbol's whole history (no look-ahead: a swing at bar i is
    only used once bar i + swing_n has closed, as in live trading)."""
    out = {}
    for sym, df in history.items():
        if df is None or not len(df):
            continue
        d = add_indicators(df, cfg)
        lo, hi = find_swings(d, cfg.swing_n)
        out[sym] = (d, lo, hi, np.array(d.index.date), mirror(d) if cfg.direction != "long" else None)
    return out


def run(history: dict[str, pd.DataFrame], cfg: Config, lookback_days: int = 10, warmup_days: int = 3,
        progress=None, scan_cache: dict | None = None, prepared: dict | None = None,
        forced: list[str] | None = None, start=None, end=None,
        daily: dict | None = None) -> tuple[pd.DataFrame, pd.Series, list[dict]]:
    """Returns (trades, end-of-day equity curve, daily scan log). ``scan_cache`` lets several runs with
    settings that don't change the scan (stops, targets, trades per day...) share the daily rankings.
    ``forced`` = watch these symbols every day without a scan or a midday rescan (live: run --symbol).
    ``start``/``end`` (dates): trade only on those days; earlier days still give the lines their history.
    ``daily`` = {symbol: daily closes} for ``trend_filter`` (the market's too); symbols missing from it get
    their trend from their own bars."""
    prepared = prepared if prepared is not None else prepare(history, cfg)
    all_days = sorted(set().union(*[set(p[3]) for p in prepared.values()])) if prepared else []
    equity = cfg.starting_equity
    trades: list[Trade] = []
    curve, picks = {}, []
    key = tuple(getattr(cfg, k) for k in SCAN_KEYS)

    for k, day in enumerate(all_days):
        if k < warmup_days:
            continue
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            break
        window_start = all_days[max(0, k - lookback_days)]
        ck = (key, day)
        if forced:
            ranked = [(f, 0.0, "chosen by you", "") for f in forced if f in prepared]
        elif scan_cache is not None and ck in scan_cache:
            ranked = scan_cache[ck]
        else:
            prior, pre = {}, {}
            for sym, (d, lo, hi, dates, md) in prepared.items():
                a, b = np.searchsorted(dates, window_start), np.searchsorted(dates, day)
                if b > a and b < len(dates) and dates[b] == day:
                    # the same window the live scan sees; its indicators and swings come from the whole
                    # history, which is identical up to the window's end (a swing needs swing_n bars after
                    # it, so the last swing_n bars never count - see strategy._build_line)
                    w = d.iloc[a:b]
                    prior[sym] = w
                    pre[sym] = (w, lo[a:b], hi[a:b], md.iloc[a:b] if md is not None else None)
            ranked = [(r.symbol, r.score, r.setup, r.detail) for r in scan(prior, cfg, pre=pre) if r.score > 0]
            if scan_cache is not None:
                scan_cache[ck] = ranked
        trend = DayTrend(cfg, day, daily, bars=prepared)
        if trend.on and not forced:
            ranked = [w for w in ranked if trend.ok(w[0], setup_side(w[2]))]    # setups that go with the trend
        watched = one_per_company(ranked, max(1, int(cfg.watch_count)), key=lambda w: w[0], extra=cfg.same_company)
        picks.append({"date": str(day), "symbol": watched[0][0] if watched else None,
                      "setup": watched[0][2] if watched else "none", "detail": watched[0][3] if watched else "",
                      "watched": [w[0] for w in watched]})

        def sessions_for(syms, day=day):
            out = []
            for sym in syms:
                d, lo, hi, dates, md = prepared[sym]
                day_idx = np.flatnonzero(dates == day)
                if len(day_idx):
                    out.append((sym, d, lo, hi, md, day_idx))
            return out

        def midday(ts, day=day, window_start=window_start, pick=picks[-1], trend=trend):
            """The live midday rescan: the same window as the morning plus today's bars before ``ts``; the
            volume screen uses full days only, as live."""
            ck2 = (key, day, ts.time())
            if scan_cache is not None and ck2 in scan_cache:
                ranked2 = scan_cache[ck2]
            else:
                prior, pre, dv = {}, {}, {}
                for sym, (d, lo, hi, dates, md) in prepared.items():
                    a, b0 = np.searchsorted(dates, window_start), np.searchsorted(dates, day)
                    if not (b0 > a and b0 < len(dates) and dates[b0] == day):
                        continue
                    b = int(d.index.searchsorted(ts))
                    w = d.iloc[a:b]
                    prior[sym] = w
                    pre[sym] = (w, lo[a:b], hi[a:b], md.iloc[a:b] if md is not None else None)
                    dv[sym] = avg_dollar_volume(d.iloc[a:b0])
                ranked2 = [(r.symbol, r.score, r.setup, r.detail)
                           for r in scan(prior, cfg, dollar_volume=dv, pre=pre) if r.score > 0]
                if scan_cache is not None:
                    scan_cache[ck2] = ranked2
            if trend.on:
                ranked2 = [w for w in ranked2 if trend.ok(w[0], setup_side(w[2]))]
            if not ranked2:
                return None                               # no setups: keep the morning list (as live)
            new = [w[0] for w in one_per_company(ranked2, max(1, int(cfg.watch_count)), key=lambda w: w[0],
                                                  extra=cfg.same_company)]
            pick["midday"] = dict(time=ts.strftime("%H:%M"), watched=new)
            return sessions_for(new)

        for tr in simulate_watch(sessions_for([w[0] for w in watched]), equity, cfg,
                                 rescan=None if forced else midday, allow=trend.ok if trend.on else None):
            equity += tr.pnl
            trades.append(tr)
        curve[pd.Timestamp(day)] = equity
        if progress:
            progress(k + 1, len(all_days))

    tdf = pd.DataFrame([asdict(t) for t in trades], columns=[f.name for f in fields(Trade)])
    return tdf, pd.Series(curve, name="equity", dtype=float), picks


# --------------------------------------------------------------------------------- results
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
    daily = curve.pct_change().dropna() if len(curve) > 2 else pd.Series(dtype=float)
    if len(daily) > 20 and daily.std() > 0:
        out["sharpe"] = round(float(daily.mean() / daily.std() * np.sqrt(252)), 2)     # yearly, from daily returns
    years = len(curve) / 252
    if years >= 1 and curve.iloc[-1] > 0:
        out["annual_return_pct"] = round(100 * ((curve.iloc[-1] / cfg.starting_equity) ** (1 / years) - 1), 2)
    if "side" in trades and trades.side.nunique() > 1:
        for side, g in trades.groupby("side"):
            out[f"{side}_trades"] = int(len(g))
            out[f"{side}_net_pnl"] = round(g.pnl.sum(), 2)
    for kind, g in trades.groupby("kind"):
        out[f"{kind}_trades"] = int(len(g))
        out[f"{kind}_win_rate_pct"] = round(100 * (g.pnl > 0).mean(), 1)
        out[f"{kind}_avg_r"] = round(g.r_multiple.mean(), 2)
    return out


def _table(g) -> dict:
    return dict(trades=int(len(g)), win_pct=round(100 * (g.pnl > 0).mean(), 1), avg_r=round(g.r_multiple.mean(), 2),
                net=round(g.pnl.sum(), 2))


def breakdown(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Results by setup (side + kind), by entry hour, by exit reason and by month."""
    if trades.empty:
        return {}
    t = trades.copy()
    t["setup"] = [k if k.startswith("short") else f"long {k}" for k in t.kind]
    t["hour"] = [f"{h:02d}:00" for h in t.entry_time.str[:2].astype(int)]
    t["month"] = t.date.str[:7]
    out = {}
    groups = [("By setup", "setup"), ("By entry hour", "hour"), ("By exit", "reason"), ("By month", "month")]
    t["year"] = t.date.str[:4]
    if t.year.nunique() > 1:
        groups.append(("By year", "year"))
    if "scan" in t and t.scan.nunique() > 1:
        groups.insert(1, ("By scan (morning picks vs midday rescan)", "scan"))
    for name, col in groups:
        rows = {key: _table(g) for key, g in t.groupby(col)}
        out[name] = pd.DataFrame.from_dict(rows, orient="index")
    return out


def report_text(trades: pd.DataFrame, curve: pd.Series, cfg: Config, title: str = "") -> str:
    s = summarize(trades, curve, cfg)
    lines = [title] if title else []
    if s.get("trades", 0) == 0:
        return "\n".join(lines + ["No trades."])
    lines += [f"Days {s['days']}  Trades {s['trades']}  Win rate {s['win_rate_pct']}%  Avg R {s['avg_r']:+.2f}  "
              f"Profit factor {s['profit_factor']}  Net ${s['net_pnl']:,.2f} ({s['return_pct']:+.2f}%)  "
              f"Max drawdown {s['max_drawdown_pct']:.2f}%"
              + (f"  Sharpe {s['sharpe']:.2f}" if "sharpe" in s else "")
              + (f"  Yearly {s['annual_return_pct']:+.2f}%" if "annual_return_pct" in s else "")]
    if cfg.strategy == "spy_momentum":
        lines.append("(spy_momentum: 1R = 1% of the position; read the $, return, drawdown and Sharpe)")
    for name, df in breakdown(trades).items():
        lines += ["", name, df.to_string()]
    return "\n".join(lines)
