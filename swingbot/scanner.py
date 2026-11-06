"""Morning watchlist scan: rank symbols by how close they are to a tradable setup.

Runs on 5-minute history up to the PRIOR close, so it never peeks at today.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .strategy import add_indicators, find_swings, mirror, resistance_line, support_line


@dataclass
class ScanResult:
    symbol: str
    score: float
    setup: str          # "near support", "near resistance", "short: near ...", or "none"
    last_close: float
    detail: str
    side: int = 1       # 1 = long setup, -1 = short setup


def daily_closes(df: pd.DataFrame) -> pd.Series:
    return df["close"].groupby(df.index.date).last()


def avg_dollar_volume(df: pd.DataFrame) -> float:
    dv = (df["close"] * df["volume"]).groupby(df.index.date).sum()
    return float(dv.mean()) if len(dv) else 0.0


def score_symbol(symbol: str, df: pd.DataFrame, cfg: Config, adv: float | None = None, pre=None) -> ScanResult:
    """``pre`` = (frame with indicators, swing lows, swing highs, mirrored frame or None) for this same window,
    when the caller has them already (the backtest); otherwise they're worked out here."""
    if len(df) < 3 * cfg.swing_n + cfg.atr_period:
        return ScanResult(symbol, 0.0, "none", float("nan"), "not enough history")
    last_close = float(df["close"].iat[-1])
    if last_close < cfg.min_price:
        return ScanResult(symbol, 0.0, "none", last_close, f"price under ${cfg.min_price:g}")
    if adv is None:
        adv = avg_dollar_volume(df)
    if adv < cfg.min_avg_dollar_volume:
        return ScanResult(symbol, 0.0, "none", last_close, f"avg $ volume {adv/1e6:.0f}M too low")

    # Trend bias: longs prefer names above their 5-day average close, shorts names below it
    dc = daily_closes(df)
    above = not (len(dc) >= 3 and last_close < dc.tail(5).mean())
    trend_note = "above 5-day avg" if above else "below 5-day avg"

    if pre is not None:
        d, is_low, is_high, md = pre
    else:
        d = add_indicators(df, cfg)
        is_low, is_high = find_swings(d, cfg.swing_n)
        md = None
    t = len(d) - 1
    best = ScanResult(symbol, 0.0, "none", last_close, f"no valid line ({trend_note})")
    if cfg.direction in ("long", "both"):
        for r in _side_scores(d, t, cfg, is_low, is_high, last_close, above, 1):
            if r[0] > best.score:
                best = ScanResult(symbol, r[0], r[1], last_close, r[2], 1)
    if cfg.direction in ("short", "both"):
        md = md if md is not None else mirror(d)
        for r in _side_scores(md, t, cfg, is_high, is_low, -last_close, not above, -1):
            if r[0] > best.score:
                best = ScanResult(symbol, r[0], r[1], last_close, r[2], -1)
    return best


def _side_scores(d, t, cfg, is_low, is_high, last_close, trend_ok, side):
    """(score, setup, detail) for each setup on one side. Shorts get the flipped chart, where the
    falling resistance above the price is a rising support below it, and so on."""
    atr = float(d["atr"].iat[t])
    out = []
    sup = support_line(d, t, cfg, is_low) if cfg.enable_bounce else None
    if sup is not None and trend_ok:
        dist = (last_close - sup.value_at(t)) / atr
        if 0 <= dist <= 2.0:
            s = 1.0 / (1.0 + dist) + 0.15 * min(sup.touches, 3)
            if side > 0:
                out.append((s, "near support", f"{dist:.2f} ATR above rising support, {sup.touches} extra touches"))
            else:
                out.append((s, "short: near resistance",
                            f"{dist:.2f} ATR below falling resistance, {sup.touches} extra touches"))
    res = resistance_line(d, t, cfg, is_high) if cfg.enable_breakout else None
    if res is not None:
        dist = (res.value_at(t) - last_close) / atr
        if 0 <= dist <= 1.5:
            s = 1.0 / (1.0 + dist) + 0.15 * min(res.touches, 3) + (0.1 if trend_ok else -0.2)
            if side > 0:
                out.append((s, "near resistance", f"{dist:.2f} ATR below falling resistance, {res.touches} extra touches"))
            else:
                out.append((s, "short: near support",
                            f"{dist:.2f} ATR above rising support, {res.touches} extra touches"))
    return out


def scan(history: dict[str, pd.DataFrame], cfg: Config, dollar_volume: dict | None = None,
         pre: dict | None = None) -> list[ScanResult]:
    """Rank all symbols; best first. Symbols with score 0 are not tradable today.
    ``dollar_volume`` optionally supplies each symbol's average daily dollar volume from a
    better source than the bars (e.g. when the bars come from a single exchange)."""
    dv, pre = dollar_volume or {}, pre or {}
    results = [score_symbol(sym, df, cfg, dv.get(sym), pre.get(sym)) for sym, df in history.items()
               if df is not None and len(df)]
    return sorted(results, key=lambda r: (-r.score, r.symbol))


def pick(history: dict[str, pd.DataFrame], cfg: Config, dollar_volume: dict | None = None) -> ScanResult | None:
    ranked = scan(history, cfg, dollar_volume)
    return ranked[0] if ranked and ranked[0].score > 0 else None


# ------------------------------------------------------------------ one stock per company
# Share classes of the same company move together, so watching both is watching one stock twice.
# Classes written with a dot (BRK.A / BRK.B, LEN / LEN.B) are matched by the part before the dot; these are
# the ones with different tickers. Add your own in config.yaml: same_company: [[ABC, ABCD]].
SHARE_CLASSES = [
    ("GOOG", "GOOGL"), ("FOX", "FOXA"), ("NWS", "NWSA"), ("UA", "UAA"), ("Z", "ZG"), ("LBRDA", "LBRDK"),
    ("FWONA", "FWONK"), ("LILA", "LILAK"), ("BATRA", "BATRK"), ("RUSHA", "RUSHB"), ("KELYA", "KELYB"),
    ("LSXMA", "LSXMK"), ("QRTEA", "QRTEB"), ("DISCA", "DISCK"), ("VIAC", "VIACA"), ("PARA", "PARAA"),
    ("BIO", "BIO.B"), ("MKC", "MKC.V"), ("GEF", "GEF.B"), ("HEI", "HEI.A"), ("TAP", "TAP.A"),
]


def company_key(symbol: str, extra=()) -> str:
    s = str(symbol).upper().replace("/", ".")
    for group in list(SHARE_CLASSES) + [tuple(g) for g in (extra or ())]:
        if s in {str(x).upper().replace("/", ".") for x in group}:
            return "|".join(sorted(str(x).upper() for x in group))
    return s.split(".", 1)[0] if "." in s else s


def one_per_company(symbols: list, n: int, key=lambda x: x, extra=(), skipped: list | None = None) -> list:
    """The first ``n`` of ``symbols`` (best first), skipping a second share class of a company already in."""
    out, seen = [], {}
    for item in symbols:
        sym = key(item)
        k = company_key(sym, extra)
        if k in seen:
            if skipped is not None:
                skipped.append((sym, seen[k]))
            continue
        seen[k] = sym
        out.append(item)
        if len(out) >= n:
            break
    return out
