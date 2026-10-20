"""Morning watchlist scan: rank symbols by how close they are to a tradable setup.

Runs on 5-minute history up to the PRIOR close, so it never peeks at today.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .strategy import add_indicators, find_swings, resistance_line, support_line


@dataclass
class ScanResult:
    symbol: str
    score: float
    setup: str          # "near support", "near resistance", or "none"
    last_close: float
    detail: str


def daily_closes(df: pd.DataFrame) -> pd.Series:
    return df["close"].groupby(df.index.date).last()


def avg_dollar_volume(df: pd.DataFrame) -> float:
    dv = (df["close"] * df["volume"]).groupby(df.index.date).sum()
    return float(dv.mean()) if len(dv) else 0.0


def score_symbol(symbol: str, df: pd.DataFrame, cfg: Config, adv: float | None = None) -> ScanResult:
    if len(df) < 3 * cfg.swing_n + cfg.atr_period:
        return ScanResult(symbol, 0.0, "none", float("nan"), "not enough history")
    last_close = float(df["close"].iat[-1])
    if last_close < cfg.min_price:
        return ScanResult(symbol, 0.0, "none", last_close, f"price under ${cfg.min_price:g}")
    if adv is None:
        adv = avg_dollar_volume(df)
    if adv < cfg.min_avg_dollar_volume:
        return ScanResult(symbol, 0.0, "none", last_close, f"avg $ volume {adv/1e6:.0f}M too low")

    # Long-only bias: skip names below their multi-day average close
    dc = daily_closes(df)
    if len(dc) >= 3 and last_close < dc.tail(5).mean():
        trend_note = "below 5-day avg"
        trend_ok = False
    else:
        trend_note = "above 5-day avg"
        trend_ok = True

    d = add_indicators(df, cfg)
    is_low, is_high = find_swings(d, cfg.swing_n)
    t = len(d) - 1
    atr = float(d["atr"].iat[t])
    best = ScanResult(symbol, 0.0, "none", last_close, f"no valid line ({trend_note})")

    sup = support_line(d, t, cfg, is_low) if cfg.enable_bounce else None
    if sup is not None and trend_ok:
        dist = (last_close - sup.value_at(t)) / atr
        if 0 <= dist <= 2.0:
            s = 1.0 / (1.0 + dist) + 0.15 * min(sup.touches, 3)
            if s > best.score:
                best = ScanResult(symbol, s, "near support", last_close,
                                  f"{dist:.2f} ATR above rising support, {sup.touches} extra touches")

    res = resistance_line(d, t, cfg, is_high) if cfg.enable_breakout else None
    if res is not None:
        dist = (res.value_at(t) - last_close) / atr
        if 0 <= dist <= 1.5:
            s = 1.0 / (1.0 + dist) + 0.15 * min(res.touches, 3) + (0.1 if trend_ok else -0.2)
            if s > best.score:
                best = ScanResult(symbol, s, "near resistance", last_close,
                                  f"{dist:.2f} ATR below falling resistance, {res.touches} extra touches")
    return best


def scan(history: dict[str, pd.DataFrame], cfg: Config, dollar_volume: dict | None = None) -> list[ScanResult]:
    """Rank all symbols; best first. Symbols with score 0 are not tradable today.
    ``dollar_volume`` optionally supplies each symbol's average daily dollar volume from a
    better source than the bars (e.g. when the bars come from a single exchange)."""
    dv = dollar_volume or {}
    results = [score_symbol(sym, df, cfg, dv.get(sym)) for sym, df in history.items() if df is not None and len(df)]
    return sorted(results, key=lambda r: (-r.score, r.symbol))


def pick(history: dict[str, pd.DataFrame], cfg: Config, dollar_volume: dict | None = None) -> ScanResult | None:
    ranked = scan(history, cfg, dollar_volume)
    return ranked[0] if ranked and ranked[0].score > 0 else None
