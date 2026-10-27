"""Build a watchlist file for the bot from a stock index.

Run on your PC from the swingbot folder (it downloads the current members):

    py tools\\index_watchlist.py sp500                    # S&P 500        -> watchlist_sp500.yaml
    py tools\\index_watchlist.py dow                      # Dow Jones 30   -> watchlist_dow30.yaml
    py tools\\index_watchlist.py nasdaq                   # Nasdaq Composite -> watchlist_nasdaq.yaml
    py tools\\index_watchlist.py nasdaq100                # Nasdaq-100     -> watchlist_nasdaq100.yaml
    py tools\\index_watchlist.py russell2000 --top 300 --min-price 10

Options: --top N keeps the N largest (S&P 500, Russell 2000: by index weight; Dow: by weight);
--min-price P skips stocks under $P (S&P 500 and Russell 2000 only - the Dow and Nasdaq files have
no prices, and the bot's own min_price filter still applies when it scans).

Then point config.yaml at the file, e.g.   watchlist_file: watchlist_sp500.yaml
Rerun now and then to pick up index changes. (Or let the bot download them itself each morning:
watchlist_indexes in config.yaml.) If a download is blocked, save the file in your
browser and pass it with --file (the error message says which address).

Sources: S&P 500 and Russell 2000 = iShares IVV / IWM holdings; Dow = SPDR DIA holdings
(a built-in list is used if that download fails); Nasdaq-100 = Nasdaq's own list; Nasdaq Composite = Nasdaq's own list of
Nasdaq-listed securities, keeping common stocks and ADRs (no ETFs, warrants, units, rights,
preferreds, notes, test issues, or companies flagged deficient/delinquent/bankrupt).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swingbot import indexes as ix  # noqa: E402
from swingbot.indexes import (DOW30, ISHARES, NAME, DownloadError, parse_dia, parse_ishares,  # noqa: E402,F401
                              parse_nasdaq, parse_nasdaq100)

OUT = ix.OUT
EXT = {"dow": ".xlsx", "nasdaq": ".txt", "nasdaq100": ".json"}


def _fail(e: DownloadError, index: str):
    dbg = f"{index}_download_debug.txt"
    Path(dbg).write_bytes(e.body[:200_000])
    sys.exit(f"Could not get {e.what}:\n  " + "\n  ".join(e.problems) + f"\nWhat came back is saved in {dbg}.\n"
             f"Workaround: open this address in your browser, move the downloaded file into your swingbot "
             f"folder, and run:\n  {e.url}\n  py tools\\index_watchlist.py {index} --file <the file"
             f"{EXT.get(index, '.csv')}>")


# --------------------------------------------------------------------------- main
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index", choices=list(ix.INDEXES))
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
            elif idx == "nasdaq100":
                as_of, rows = parse_nasdaq100(raw.decode("utf-8-sig", errors="replace"))
            else:
                as_of, rows = parse_nasdaq(raw.decode("utf-8", errors="replace"))
        except ValueError as e:
            sys.exit(f"{args.file}: {e}")
    else:
        try:
            as_of, rows = ix.members(idx, warn=lambda m: print(m, file=sys.stderr))
        except DownloadError as e:
            _fail(e, idx)

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
