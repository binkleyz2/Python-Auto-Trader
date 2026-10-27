"""Index members for the morning scan, downloaded fresh each day.

``watchlist_indexes`` in config.yaml names the indexes to scan; their members are combined (each stock
once) with your own ``watchlist_file`` / ``watchlist``, and only stocks your broker can trade are kept.

    sp500         S&P 500                        iShares IVV holdings (with prices and weights)
    dow           Dow Jones Industrial Average   SPDR DIA holdings (built-in list if that fails)
    nasdaq100     Nasdaq-100                     Nasdaq's own Nasdaq-100 list
    nasdaq        Nasdaq Composite               Nasdaq's list of Nasdaq-listed common stocks and ADRs
    russell2000   Russell 2000                   iShares IWM holdings (with prices and weights)

Each list is saved in logs/indexes/<name>.txt. It is downloaded again once a day; if a download fails,
the saved copy is used (with a warning) so the bot still starts.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger("swingbot")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,application/json,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ISHARES = {
    "russell2000": ("Russell 2000", "IWM", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"),
    "sp500": ("S&P 500", "IVV", "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf"),
}
DIA_XLSX = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-dia.xlsx")
NASDAQ_TXT = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
NASDAQ100_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
# Dow Jones Industrial Average members after the June 29, 2026 change (Alphabet in, Verizon out).
DOW30 = ["AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "GOOGL", "GS", "HD", "HON",
         "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V",
         "WMT"]
DOW30_AS_OF = "June 29, 2026"
# iShares writes class shares without a separator in its S&P 500 file
FIX = {"BRKB": "BRK B", "BFB": "BF B"}
NAME = {"sp500": "S&P 500", "dow": "Dow Jones Industrial Average", "nasdaq100": "Nasdaq-100",
        "nasdaq": "Nasdaq Composite", "russell2000": "Russell 2000"}
INDEXES = tuple(NAME)


class DownloadError(RuntimeError):
    """A list couldn't be downloaded. ``url`` is the address to open in a browser instead."""

    def __init__(self, what: str, url: str, problems: list[str], body: bytes = b""):
        super().__init__(f"could not get {what}: " + "; ".join(problems))
        self.what, self.url, self.problems, self.body = what, url, problems, body


def _session():
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# --------------------------------------------------------------------------- iShares (CSV)
def _cells(line: str) -> list[str]:
    return [c.strip().strip('"').strip() for c in next(csv.reader([line]), [])]


def parse_ishares(text: str):
    """(as_of, [(ticker, weight, price), ...]) for the equity rows of an iShares holdings CSV."""
    lines = text.splitlines()
    as_of = "?"
    for l in lines[:20]:
        c = _cells(l)
        if c and c[0].lower().startswith("fund holdings as of") and len(c) > 1:
            as_of = c[1]
    start = next((i for i, l in enumerate(lines[:60]) if {"Ticker", "Asset Class"} <= set(_cells(l))), None)
    if start is None:
        raise ValueError("no Ticker / Asset Class header (not an iShares holdings CSV)")
    header = _cells(lines[start])
    rows = []
    for vals in csv.reader(lines[start + 1:]):
        rec = dict(zip(header, (v.strip() for v in vals)))
        if not rec.get("Ticker") or (rec.get("Asset Class") or "") != "Equity":
            continue                                   # skips the footer, cash and futures
        tick = FIX.get(rec["Ticker"].upper(), rec["Ticker"].upper())
        if not re.fullmatch(r"[A-Z]{1,5}( [A-Z])?", tick):
            continue                                   # odd entries (rights, placeholders)
        num = lambda s: float(re.sub(r"[^0-9.\-]", "", s or "") or "nan")
        rows.append((tick, num(rec.get("Weight (%)")), num(rec.get("Price"))))
    if not rows:
        raise ValueError("no equity rows found")
    return as_of, rows


def download_ishares(index: str):
    _, fund, page = ISHARES[index]
    urls = [page + "/latest-holdings.csv",
            page + f"/1467271812596.ajax?fileType=csv&fileName={fund}_holdings&dataType=fund"]
    s = _session()
    try:
        s.get(page, timeout=60)                      # the site sets cookies on the fund page first
    except Exception:
        pass
    problems, body = [], b""
    for url in urls:
        try:
            r = s.get(url, timeout=60, headers={"Referer": page})
        except Exception as e:
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: {e}")
            continue
        body = r.content
        if not r.ok:
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: HTTP {r.status_code}")
            continue
        try:
            return parse_ishares(r.content.decode("utf-8-sig", errors="replace"))
        except ValueError as e:
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: got {r.headers.get('Content-Type', '?').split(';')[0]}, {e}")
    raise DownloadError(f"the {fund} holdings file from iShares", urls[0], problems, body)


# --------------------------------------------------------------------------- Dow (SPDR DIA, xlsx)
def parse_dia(data: bytes):
    import pandas as pd
    raw = pd.read_excel(io.BytesIO(data), header=None)
    hdr = next((i for i in range(min(len(raw), 30)) if "Ticker" in [str(v).strip() for v in raw.iloc[i]]), None)
    if hdr is None:
        raise ValueError("no Ticker column (not a SPDR holdings file)")
    as_of = "?"
    for i in range(hdr):
        cells = [str(v) for v in raw.iloc[i] if str(v) != "nan"]
        if cells and cells[0].lower().startswith("holdings"):
            as_of = cells[-1].replace("As of", "").strip()
    cols = [str(v).strip() for v in raw.iloc[hdr]]
    df = raw.iloc[hdr + 1:].set_axis(cols, axis=1)
    rows = []
    for _, r in df.iterrows():
        tick = str(r.get("Ticker", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]{1,5}([ .][A-Z])?", tick):
            continue                                   # cash line, blanks, footer text
        try:
            w = float(r.get("Weight", "nan"))
        except (TypeError, ValueError):
            w = float("nan")
        rows.append((tick.replace(".", " "), w, float("nan")))
    if not 25 <= len(rows) <= 35:
        raise ValueError(f"found {len(rows)} stocks, expected 30")
    return as_of, rows


def download_dow(warn=None):
    try:
        r = _session().get(DIA_XLSX, timeout=60)
        r.raise_for_status()
        return parse_dia(r.content)
    except Exception as e:
        (warn or log.warning)(f"SPDR DIA holdings not available ({e}) - using the built-in Dow list as of "
                              f"{DOW30_AS_OF}.")
        return f"{DOW30_AS_OF} (built-in)", [(t, float("nan"), float("nan")) for t in DOW30]


# --------------------------------------------------------------------------- Nasdaq Composite
SKIP_NAME = re.compile(r"\b(warrants?|rights?|units?|preferred|notes?|debentures?|subordinated|"
                       r"depositary shares? representing .*preferred)\b|%", re.I)
BAD_STATUS = set("DEQGHJK")                           # deficient, delinquent, bankrupt (and combinations)


def parse_nasdaq(text: str):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("Symbol|"):
        raise ValueError("not Nasdaq's symbol directory file")
    header = lines[0].split("|")
    as_of = "?"
    rows = []
    for l in lines[1:]:
        if l.startswith("File Creation Time"):
            as_of = l.split(":", 1)[1].strip(" |")
            continue
        rec = dict(zip(header, l.split("|")))
        sym = (rec.get("Symbol") or "").strip().upper()
        if (rec.get("ETF") == "Y" or rec.get("Test Issue") == "Y" or rec.get("NextShares") == "Y"
                or (rec.get("Financial Status") or "N") in BAD_STATUS
                or SKIP_NAME.search(rec.get("Security Name") or "")
                or not re.fullmatch(r"[A-Z]{1,5}", sym)):
            continue
        rows.append((sym, float("nan"), float("nan")))
    if not rows:
        raise ValueError("no common stocks found")
    return as_of, rows


def download_nasdaq():
    s = _session()
    try:
        r = s.get(NASDAQ_TXT, timeout=60)
        if r.ok:
            return parse_nasdaq(r.content.decode("utf-8", errors="replace"))
        problems, body = [f"HTTP {r.status_code}"], r.content
    except Exception as e:
        problems, body = [str(e)], b""
    raise DownloadError("Nasdaq's list of listed securities", NASDAQ_TXT, problems, body)


# --------------------------------------------------------------------------- Nasdaq-100
def parse_nasdaq100(text: str):
    """Nasdaq's own JSON list: data.data.rows[] with symbol and lastSalePrice."""
    try:
        data = json.loads(text)
        rows_in = data["data"]["data"]["rows"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("not Nasdaq's Nasdaq-100 list")
    as_of = str((data.get("data") or {}).get("date") or "?")
    rows = []
    for r in rows_in or []:
        sym = str(r.get("symbol") or "").strip().upper().replace(".", " ").replace("/", " ")
        if not re.fullmatch(r"[A-Z]{1,5}( [A-Z])?", sym):
            continue
        price = float(re.sub(r"[^0-9.\-]", "", str(r.get("lastSalePrice") or "")) or "nan")
        rows.append((sym, float("nan"), price))
    if not 90 <= len(rows) <= 110:
        raise ValueError(f"found {len(rows)} stocks, expected about 100")
    return as_of, rows


def download_nasdaq100():
    s = _session()
    try:
        r = s.get(NASDAQ100_API, timeout=60, headers={"Accept": "application/json", "Origin": "https://www.nasdaq.com",
                                                      "Referer": "https://www.nasdaq.com/"})
        if r.ok:
            return parse_nasdaq100(r.content.decode("utf-8", errors="replace"))
        problems, body = [f"HTTP {r.status_code}"], r.content
    except ValueError as e:
        problems, body = [str(e)], b""
    except Exception as e:
        problems, body = [str(e)], b""
    raise DownloadError("Nasdaq's Nasdaq-100 list", NASDAQ100_API, problems, body)


def members(index: str, warn=None):
    """(as_of, [(ticker with class as 'BRK B', weight, price), ...]) downloaded now."""
    if index in ISHARES:
        return download_ishares(index)
    if index == "dow":
        return download_dow(warn)
    if index == "nasdaq":
        return download_nasdaq()
    if index == "nasdaq100":
        return download_nasdaq100()
    raise ValueError(f"unknown index {index!r} (choose from {', '.join(INDEXES)})")


# --------------------------------------------------------------------------- daily lists for the bot
def _cache_read(path: Path) -> tuple[str, list[str]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        when = lines[0].split("downloaded ", 1)[1].split()[0] if lines and "downloaded " in lines[0] else "?"
        return when, [l.strip() for l in lines[1:] if l.strip() and not l.startswith("#")]
    except Exception:
        return None


def index_symbols(index: str, folder: Path, min_price: float = 0.0, today: date | None = None,
                  download=None) -> list[str]:
    """Members of ``index`` as broker symbols ('BRK.B'), from today's download or the saved copy.
    Stocks priced under ``min_price`` are left out where the list has prices (S&P 500, Russell 2000,
    Nasdaq-100); the scan's own min_price filter catches the rest."""
    today = today or date.today()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{index}.txt"
    cached = _cache_read(path) if path.exists() else None
    if cached and cached[0] == str(today):
        return cached[1]
    try:
        as_of, rows = (download or members)(index)
    except Exception as e:
        if cached:
            log.warning("%s: today's list couldn't be downloaded (%s) - using the copy from %s", NAME[index], e,
                        cached[0])
            return cached[1]
        log.error("%s: list couldn't be downloaded (%s) and there is no saved copy - left out today",
                  NAME[index], e)
        return []
    syms = sorted({t.replace(" ", ".") for t, _, p in rows if not (min_price and p == p and p < min_price)})
    path.write_text(f"# {NAME[index]} (as of {as_of}), downloaded {today}\n" + "\n".join(syms) + "\n",
                    encoding="utf-8")
    return syms


def daily_watchlist(indexes: list[str], extra: list[str], folder: Path, min_price: float = 0.0,
                    download=None) -> tuple[list[str], dict[str, int]]:
    """Each index's members plus your own list, each stock once (in that order). Also returns counts."""
    out, counts = {}, {}
    for idx in indexes:
        syms = index_symbols(idx, folder, min_price, download=download)
        counts[NAME[idx]] = len(syms)
        for s in syms:
            out.setdefault(s.upper(), None)
    if extra:
        counts["your watchlist"] = len(extra)
        for s in extra:
            out.setdefault(s.upper(), None)
    return list(out), counts
