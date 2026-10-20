"""Build a `watchlist:` line for config.yaml from the Russell 2000 (iShares IWM holdings).

Run on your PC (it downloads the current holdings file from iShares):

    py tools\\russell2000_watchlist.py                    # all ~2,000 stocks
    py tools\\russell2000_watchlist.py --top 200          # the 200 largest
    py tools\\russell2000_watchlist.py --min-price 10     # skip stocks under $10

It saves the list to watchlist_russell2000.yaml in the folder you run it from (your
swingbot folder). To use it, set this in config.yaml (no copying and pasting):

    watchlist_file: watchlist_russell2000.yaml

Rerun this script now and then to pick up index changes. Or point it at a file you saved yourself:

    py tools\\russell2000_watchlist.py --csv IWM_holdings.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

PAGE = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"
URLS = [
    PAGE + "/latest-holdings.csv",
    PAGE + "/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEBUG = "iwm_download_debug.txt"


def download():
    """Fetch the holdings file and parse it. Tries each address until one gives a real CSV."""
    import requests
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(PAGE, timeout=60)                      # the site sets cookies on the fund page first
    except Exception:
        pass
    problems, last_body = [], b""
    for url in URLS:
        try:
            r = s.get(url, timeout=60, headers={"Referer": PAGE})
        except Exception as e:
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: {e}")
            continue
        last_body = r.content
        if not r.ok:
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: HTTP {r.status_code}")
            continue
        try:
            return parse(r.content.decode("utf-8-sig", errors="replace"))
        except ValueError as e:
            kind = r.headers.get("Content-Type", "?").split(";")[0]
            problems.append(f"{url.rsplit('/', 1)[-1][:30]}: got {kind}, {e}")
    Path(DEBUG).write_bytes(last_body[:200_000])
    sys.exit("Could not get the holdings file from iShares:\n  " + "\n  ".join(problems) +
             f"\nWhat came back is saved in {DEBUG}.\n"
             "Workaround: open this address in your browser (it downloads latest-holdings.csv), move the file into your swingbot folder, and run the script with --csv:\n"
             f"  {URLS[0]}\n"
             "  py tools\\russell2000_watchlist.py --csv latest-holdings.csv --top 300 --min-price 10")


def _cells(line: str) -> list[str]:
    return [c.strip().strip('"').strip() for c in next(csv.reader([line]), [])]


def parse(text: str):
    """Returns (as_of date text, [(ticker, weight, price), ...]) for the equity rows."""
    lines = text.splitlines()
    as_of = "?"
    for l in lines[:20]:
        c = _cells(l)
        if c and c[0].lower().startswith("fund holdings as of") and len(c) > 1:
            as_of = c[1]
    start = next((i for i, l in enumerate(lines[:60])
                  if {"Ticker", "Asset Class"} <= set(_cells(l))), None)
    if start is None:
        raise ValueError("no Ticker / Asset Class header (not an iShares holdings CSV)")
    header = _cells(lines[start])
    rows = []
    for vals in csv.reader(lines[start + 1:]):
        rec = dict(zip(header, (v.strip() for v in vals)))
        if not rec.get("Ticker") or (rec.get("Asset Class") or "") != "Equity":
            continue                                   # skips the footer, cash and futures
        tick = rec["Ticker"].upper()
        if not re.fullmatch(r"[A-Z]{1,5}( [A-Z])?", tick):
            continue                                   # odd entries (rights, placeholders)
        num = lambda s: float(re.sub(r"[^0-9.\-]", "", s or "") or "nan")
        rows.append((tick, num(rec.get("Weight (%)")), num(rec.get("Price"))))
    if not rows:
        raise ValueError("no equity rows found")
    return as_of, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="use a holdings file you downloaded instead of fetching it")
    ap.add_argument("--top", type=int, help="keep only the N largest stocks (by index weight)")
    ap.add_argument("--min-price", type=float, default=0, help="skip stocks priced under this")
    ap.add_argument("--class-sep", default=".", help="separator for share classes, e.g. MOG A -> MOG.A "
                    "(Alpaca uses '.'; try '/' if your broker rejects it)")
    ap.add_argument("--out", default="watchlist_russell2000.yaml")
    args = ap.parse_args()

    if args.csv:
        try:
            as_of, rows = parse(Path(args.csv).read_text(encoding="utf-8-sig", errors="replace"))
        except ValueError as e:
            sys.exit(f"{args.csv}: {e}")
    else:
        as_of, rows = download()
    total = len(rows)
    rows = [r for r in rows if not (r[2] < args.min_price)]
    rows.sort(key=lambda r: -(r[1] if r[1] == r[1] else 0))      # largest first
    if args.top:
        rows = rows[: args.top]
    ticks = sorted({t.replace(" ", args.class_sep) for t, _, _ in rows})

    note = f"# Russell 2000 (iShares IWM holdings as of {as_of})"
    if args.top or args.min_price:
        note += f": {len(ticks)} of {total}"
        note += f", largest {args.top}" if args.top else ""
        note += f", price >= ${args.min_price:g}" if args.min_price else ""
    body = ",\n            ".join(", ".join(ticks[i:i + 10]) for i in range(0, len(ticks), 10))
    out = f"{note}\nwatchlist: [{body}]\n"
    Path(args.out).write_text(out, encoding="utf-8")
    print(out)
    print(f"{len(ticks)} symbols - saved to {args.out}\n"
          f"To use it, set this in config.yaml:  watchlist_file: {Path(args.out).name}", file=sys.stderr)


if __name__ == "__main__":
    main()
