"""Months of 5-minute bars for backtests, from Alpaca's historical data.

Alpaca's free data plan includes full-market (SIP) history as long as it is more than 15 minutes old,
so a backtest can use every exchange's trades, years back. Bars are kept in data/alpaca-<N>min/ as one
compressed CSV per stock; later runs only download what's new. Needs ALPACA_API_KEY and
ALPACA_SECRET_KEY in .env (paper keys work; nothing is traded).
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .data import COLS, empty_bars, file_stem, normalize

log = logging.getLogger("swingbot")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
BATCH = 50                       # symbols per request (Alpaca pages through the rest)


def _path(folder: Path, sym: str) -> Path:
    p = folder / f"{file_stem(sym)}.csv.gz"
    old = folder / f"{sym.replace('/', '_')}.csv.gz"            # before v1.19.3: CON.csv.gz, which OneDrive can't sync
    if old != p and not p.exists():
        try:
            if old.exists():
                old.replace(p)
        except OSError:
            pass
    return p


def load_cached(folder: Path, sym: str) -> pd.DataFrame:
    p = _path(folder, sym)
    if not p.exists():
        return empty_bars()
    try:
        df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        return normalize(df)
    except Exception as e:
        log.warning("history: %s unreadable (%s) - downloading it again", p.name, e)
        return empty_bars()


def _save(folder: Path, sym: str, df: pd.DataFrame) -> None:
    out = df.copy()
    out.index = out.index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    out.index.name = "timestamp"
    tmp = _path(folder, sym).with_suffix(".tmp")
    out[COLS].to_csv(tmp, compression="gzip")
    tmp.replace(_path(folder, sym))


def _request(data_client, symbols, start, end, minutes):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    for attempt in range(5):
        try:
            return data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                start=start, end=end, feed=DataFeed.SIP, adjustment=Adjustment.ALL)).df
        except Exception as e:
            msg = str(e)
            if "429" in msg or "too many" in msg.lower() or "timed out" in msg.lower():
                _time.sleep(10 * (attempt + 1))          # free plan: 200 requests a minute
                continue
            raise
    raise RuntimeError("Alpaca kept refusing (rate limit)")


def load_history(data_client, symbols: list[str], days: int, folder: Path, minutes: int = 5,
                 now: datetime | None = None, progress=None) -> dict[str, pd.DataFrame]:
    """``days`` calendar days of bars per symbol (cached; only missing days are downloaded).
    Returns {symbol: bars} for symbols with data; split- and dividend-adjusted, regular hours only."""
    folder.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(ET)
    end = (now - timedelta(minutes=16)).astimezone(UTC)  # free plan: full-market data must be 15+ min old
    start_all = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    have = {s: load_cached(folder, s) for s in dict.fromkeys(symbols)}
    # the latest session that has finished (weekdays; a holiday just means one empty download)
    e = end.astimezone(ET)
    last_session = e.date() if (e.weekday() < 5 and e.hour >= 16) else None
    if last_session is None:
        last_session = (e - timedelta(days=1)).date()
        while last_session.weekday() >= 5:
            last_session -= timedelta(days=1)
    # group by what each symbol still needs: everything, or just the newest days
    need: dict[pd.Timestamp, list[str]] = {}
    for s, df in have.items():
        if len(df) and df.index.min() <= pd.Timestamp(start_all) + pd.Timedelta(days=4):
            if df.index.max().date() >= last_session:
                continue                                   # already has the latest finished session
            frm = (df.index.max() + pd.Timedelta(minutes=minutes)).tz_convert("UTC")
            key = pd.Timestamp(frm.date(), tz="UTC")
        else:
            key = pd.Timestamp(start_all)
            have[s] = empty_bars()
        need.setdefault(key, []).append(s)
    todo = sum(len(v) for v in need.values())
    done = 0
    if todo:
        log.info("History: downloading %d of %d symbols from Alpaca (%d days of %d-minute bars)...",
                 todo, len(have), days, minutes)
    for frm, syms in sorted(need.items()):
        for i in range(0, len(syms), BATCH):
            chunk = syms[i:i + BATCH]
            try:
                raw = _request(data_client, chunk, frm.to_pydatetime(), end, minutes)
            except Exception as e:
                log.warning("history: %s..%s failed (%s) - they're left out", chunk[0], chunk[-1], e)
                done += len(chunk)
                continue
            for s in chunk:
                new = empty_bars()
                if raw is not None and len(raw) and isinstance(raw.index, pd.MultiIndex) \
                        and s in raw.index.get_level_values(0):
                    new = normalize(raw.xs(s, level=0)[COLS])
                df = normalize(pd.concat([have[s], new])) if len(have[s]) else new
                df = df[df.index >= pd.Timestamp(start_all)]
                if len(df):
                    _save(folder, s, df)
                have[s] = df
            done += len(chunk)
            if progress:
                progress(done, todo)
    cut = pd.Timestamp(start_all)
    return {s: df[df.index >= cut] for s, df in have.items() if len(df)}
