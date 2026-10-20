"""Loading intraday bars from CSV files or from Public's API."""
from __future__ import annotations

from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

ET = "America/New_York"
COLS = ["open", "high", "low", "close", "volume"]


def empty_bars() -> pd.DataFrame:
    """No bars, in the same shape as real ones (time-indexed, Eastern time)."""
    return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz=ET), dtype=float)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Index by ET timestamps (bar start), keep regular-hours bars only, sort, de-duplicate."""
    if df is None or len(df) == 0:
        return empty_bars()
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df = df.copy()
    df.index = idx.tz_convert(ET)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    t = df.index.time
    df = df[(t >= time(9, 30)) & (t < time(16, 0))]
    return df[COLS].astype(float)


def load_csv(path: str | Path) -> pd.DataFrame:
    """CSV with columns: timestamp,open,high,low,close,volume.

    Timestamps may be ISO strings with an offset, or naive (treated as UTC).
    Column names are matched case-insensitively; 'datetime'/'date'/'time' also work.
    """
    raw = pd.read_csv(path)
    raw.columns = [c.strip().lower() for c in raw.columns]
    ts_col = next((c for c in ("timestamp", "datetime", "date", "time") if c in raw.columns), None)
    if ts_col is None:
        raise ValueError(f"{path}: no timestamp column")
    raw.index = pd.to_datetime(raw[ts_col], utc=True)
    return normalize(raw)


def load_dir(folder: str | Path, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Load every SYMBOL.csv in a folder (or just the listed symbols)."""
    folder = Path(folder)
    out = {}
    for p in sorted(folder.glob("*.csv")):
        sym = p.stem.upper()
        if symbols and sym not in symbols:
            continue
        out[sym] = load_csv(p)
    return out


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    out = df.copy()
    out.index = out.index.tz_convert("UTC")
    out.index.name = "timestamp"
    out.to_csv(path)


def append_csv(df: pd.DataFrame, path: str | Path) -> int:
    """Merge new bars into an existing CSV archive. Returns number of bars stored."""
    path = Path(path)
    if path.exists():
        df = pd.concat([load_csv(path), df])
    df = normalize(df)
    save_csv(df, path)
    return len(df)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def bars_from_public(resp) -> pd.DataFrame:
    """Convert an SDK BarsResponse (regular_market section) to a DataFrame."""
    section = getattr(resp, "regular_market", None)
    bars = getattr(section, "bars", None) or []
    rows = []
    for b in bars:
        rows.append({
            "timestamp": pd.Timestamp(b.timestamp),
            "open": float(b.open), "high": float(b.high), "low": float(b.low),
            "close": float(b.close), "volume": float(b.volume or 0),
        })
    if not rows:
        return empty_bars()
    df = pd.DataFrame(rows).set_index("timestamp")
    return normalize(df)


def _public_aggregation(minutes: int):
    from public_api_sdk import BarAggregation
    table = {1: "ONE_MINUTE", 5: "FIVE_MINUTES", 10: "TEN_MINUTES", 15: "FIFTEEN_MINUTES", 30: "THIRTY_MINUTES"}
    if minutes not in table:
        raise ValueError(f"Public offers 1, 5, 10, 15 or 30-minute bars, not {minutes}")
    return BarAggregation[table[minutes]]


def fetch_public(client, symbol: str, period: str = "WEEK", minutes: int = 5) -> pd.DataFrame:
    """Regular-hours bars of ``minutes`` for ``symbol`` over ``period`` (DAY, WEEK, MONTH...)."""
    from public_api_sdk import BarAggregation, BarPeriod, TradingSessionToggle

    resp = client.get_bars(
        symbol,
        BarPeriod[period],
        aggregation=_public_aggregation(minutes),
        trading_session_toggle=TradingSessionToggle.REGULAR_HOURS,
    )
    return bars_from_public(resp)


# --------------------------------------------------------------------------- #
# Synthetic data — for testing the code paths ONLY, not for judging the strategy
# --------------------------------------------------------------------------- #
def synthetic(symbol: str, days: int = 60, start: str = "2026-06-01", seed: int = 0,
              price: float = 100.0, minutes: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed + sum(map(ord, symbol)))
    sessions = pd.bdate_range(start, periods=days)
    rows = []
    drift = 0.0
    for d in sessions:
        if rng.random() < 0.3:
            drift = rng.normal(0, 0.0004)
        stamps = pd.date_range(f"{d.date()} 09:30", periods=390 // minutes, freq=f"{minutes}min", tz=ET)
        price *= 1 + rng.normal(0, 0.006)          # overnight gap
        for ts in stamps:
            k = minutes / 5                                  # keep volatility per unit of time the same
            ret = drift * k + 0.0006 * k * np.sin(len(rows) * k / 9) + rng.normal(0, 0.0018 * k ** 0.5)
            o = price
            c = price * (1 + ret)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0008)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.0008)))
            v = float(rng.integers(50_000, 400_000))
            rows.append((ts, o, h, l, c, v))
            price = c
    df = pd.DataFrame(rows, columns=["timestamp"] + COLS).set_index("timestamp")
    return normalize(df)
