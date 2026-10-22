"""Build a watchlist file for the bot from a stock index.

Run on your PC from the swingbot folder (it downloads the current members):

    py tools\\index_watchlist.py sp500                    # S&P 500        -> watchlist_sp500.yaml
    py tools\\index_watchlist.py dow                      # Dow Jones 30   -> watchlist_dow30.yaml
    py tools\\index_watchlist.py nasdaq                   # Nasdaq Composite -> watchlist_nasdaq.yaml
    py tools\\index_watchlist.py russell2000 --top 300 --min-price 10

Options: --top N keeps the N largest (S&P 500, Russell 2000: by index weight; Dow: by weight);
--min-price P skips stocks under $P (S&P 500 and Russell 2000 only - the Dow and Nasdaq files have
no prices, and the bot's own min_price filter still applies when it scans).

Then point config.yaml at the file, e.g.   watchlist_file: watchlist_sp500.yaml
Rerun now and then to pick up index changes. If a download is blocked, save the file in your
browser and pass it with --file (the error message says which address).

Sources: S&P 500 and Russell 2000 = iShares IVV / IWM holdings; Dow = SPDR DIA holdings
(a built-in list is used if that download fails); Nasdaq Composite = Nasdaq's own list of
Nasdaq-listed securities, keeping common stocks and ADRs (no ETFs, warrants, units, rights,
preferreds, notes, test issues, or companies flagged deficient/delinquent/bankrupt).
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ISHARES = {
    "russell2000": ("Russell 2000", "IWM", "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"),
    "sp500": ("S&P 500", "IVV", "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf"),
}
DIA_XLSX = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-dia.xlsx")
NASDAQ_TXT = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
# Dow Jones Industrial Average members after the June 29, 2026 change (Alphabet in, Verizon out).
DOW30 = ["AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "GOOGL", "GS", "HD", "HON",
         "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V",
         "WMT"]
DOW30_AS_OF = "June 29, 2026"
# iShares writes class shares without a separator in its S&P 500 file
FIX = {"BRKB": "BRK B", "BFB": "BF B"}
OUT = {"sp500": "watchlist_sp500.yaml", "dow": "watchlist_dow30.yaml", "nasdaq": "watchlist_nasdaq.yaml",
       "russell2000": "watchlist_russell2000.yaml"}
NAME = {"sp500": "S&P 500", "dow": "Dow Jones Industrial Average", "nasdaq": "Nasdaq Composite",
        "russell2000": "Russell 2000"}


def _session():
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fail(what: str, url: str, problems: list[str], body: bytes, index: str, ext: str):
    dbg = f"{index}_download_debug.txt"
    Path(dbg).write_bytes(body[:200_000])
    sys.exit(f"Could not get {what}:\n  " + "\n  ".join(problems) + f"\nWhat came back is saved in {dbg}.\n"
             f"Workaround: open this address in your browser, move the downloaded file into your swingbot "
             f"folder, and run:\n  {url}\n  py tools\\index_watchlist.py {index} --file <the file{ext}>")


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
    _fail(f"the {fund} holdings file from iShares", urls[0], problems, body, index, ".csv")


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


def download_dow():
    try:
        r = _session().get(DIA_XLSX, timeout=60)
        r.raise_for_status()
        return parse_dia(r.content)
    except Exception as e:
        print(f"SPDR DIA holdings not available ({e}) - using the built-in Dow list as of {DOW30_AS_OF}.",
              file=sys.stderr)
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
        problems = [f"HTTP {r.status_code}"]
    except Exception as e:
        problems, r = [str(e)], None
    _fail("Nasdaq's list of listed securities", NASDAQ_TXT, problems, r.content if r is not None else b"",
          "nasdaq", ".txt")


# --------------------------------------------------------------------------- main
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index", choices=["sp500", "dow", "nasdaq", "russell2000"])
    ap.add_argument("--file", "--csv", dest="file", help="use a file you downloaded instead of fetching it")
    ap.add_argument("--top", type=int, help="keep only the N largest (by index weight)")
    ap.add_argument("--min-price", type=float, default=0, help="skip stocks priced under this (S&P 500, Russell)")
    ap.add_argument("--class-sep", default=".", help="separator for share classes, e.g. BRK B -> BRK.B "
                    "(Alpaca and Public use '.'; try '/' if your broker rejects it)")
    ap.add_argument("--out", help="output file (default watchlist_<index>.yaml)")
    args = ap.parse_args(argv)
    idx = args.index

    if args.file:
        raw = Path(args.file).read_bytes()
        try:
            if idx in ISHARES:
                as_of, rows = parse_ishares(raw.decode("utf-8-sig", errors="replace"))
            elif idx == "dow":
                as_of, rows = parse_dia(raw)
            else:
                as_of, rows = parse_nasdaq(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            sys.exit(f"{args.file}: {e}")
    else:
        as_of, rows = (download_ishares(idx) if idx in ISHARES else download_dow() if idx == "dow"
                       else download_nasdaq())

    total = len(rows)
    if args.min_price and all(p != p for _, _, p in rows):
        print("(--min-price ignored: this list has no prices; the bot's min_price still applies)", file=sys.stderr)
    else:
        rows = [r for r in rows if not (r[2] < args.min_price)]
    if args.top:
        if all(w != w for _, w, _ in rows):
            print("(--top ignored: this list has no index weights)", file=sys.stderr)
        else:
            rows = sorted(rows, key=lambda r: -(r[1] if r[1] == r[1] else 0))[: args.top]
    ticks = sorted({t.replace(" ", args.class_sep) for t, _, _ in rows})

    note = f"# {NAME[idx]} ({'iShares ' + ISHARES[idx][1] + ' holdings' if idx in ISHARES else 'members'} as of {as_of})"
    if len(ticks) != total:
        note += f": {len(ticks)} of {total}"
        note += f", largest {args.top}" if args.top else ""
        note += f", price >= ${args.min_price:g}" if args.min_price else ""
    body = ",\n            ".join(", ".join(ticks[i:i + 10]) for i in range(0, len(ticks), 10))
    out = args.out or OUT[idx]
    Path(out).write_text(f"{note}\nwatchlist: [{body}]\n", encoding="utf-8")
    print(f"{len(ticks)} symbols - saved to {out}\nTo use it, set this in config.yaml:  watchlist_file: {Path(out).name}")
    if len(ticks) > 1500:
        print(f"(A {len(ticks)}-symbol scan from Public's data takes about {len(ticks) / 8 / 60:.0f} minutes at 8 "
              f"requests a second - start the bot early.)")


if __name__ == "__main__":
    main()
