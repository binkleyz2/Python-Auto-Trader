"""tools/index_watchlist.py: parsing each index source into a watchlist file."""
import io
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import index_watchlist as iw  # noqa: E402
from swingbot.config import read_watchlist  # noqa: E402

ISHARES_CSV = '''﻿iShares Core S&P 500 ETF
Fund Holdings as of,"Oct 21, 2026"
 
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date
"NVDA","NVIDIA CORP","Information Technology","Equity","1","7.10","1","1","180.00","United States","NASDAQ","USD","1.00","USD","-"
"BRKB","BERKSHIRE HATHAWAY INC CLASS B","Financials","Equity","1","1.60","1","1","480.00","United States","NYSE","USD","1.00","USD","-"
"BFB","BROWN FORMAN CORP CLASS B","Consumer Staples","Equity","1","0.01","1","1","8.00","United States","NYSE","USD","1.00","USD","-"
"USD","USD CASH","Cash and/or Derivatives","Cash","1","0.10","1","1","1.00","United States","-","USD","1.00","USD","-"
 
"The content contained herein is owned by iShares"
'''

NASDAQ_TXT = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
AACG|ATA Creativity Global - American Depositary Shares, each representing two common shares|S|N|D|100|N|N
AACI|Armada Acquisition Corp. III - Class A Ordinary Share|G|N|N|100|N|N
AACIU|Armada Acquisition Corp. III - Units|G|N|N|100|N|N
AACIW|Armada Acquisition Corp. III - Warrant|G|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCP|Example Bank - 7.00% Fixed-Rate Preferred Stock|G|N|N|100|N|N
ASML|ASML Holding N.V. - New York Registry Shares|Q|N|N|100|N|N
File Creation Time: 1021202617:30|||||||
"""


def test_sp500_from_ishares(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ivv.csv").write_text(ISHARES_CSV, encoding="utf-8")
    iw.main(["sp500", "--file", "ivv.csv", "--min-price", "10"])
    syms = read_watchlist(tmp_path / "watchlist_sp500.yaml")
    assert syms == ["BRK.B", "NVDA"]                                   # BRKB fixed; BF.B under $10; cash out
    assert "Oct 21, 2026" in (tmp_path / "watchlist_sp500.yaml").read_text(encoding="utf-8")


def test_nasdaq_composite_common_stocks_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "n.txt").write_text(NASDAQ_TXT, encoding="utf-8")
    iw.main(["nasdaq", "--file", "n.txt", "--top", "5"])                  # --top is ignored (no weights)
    assert read_watchlist(tmp_path / "watchlist_nasdaq.yaml") == ["AACI", "AAPL", "ASML"]


def test_dow_from_spdr_xlsx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    head = pd.DataFrame([["Fund Name:", "SPDR Dow Jones Industrial Average ETF Trust", None, None],
                         ["Holdings:", "As of 21-Oct-2026", None, None], [None] * 4,
                         ["Name", "Ticker", "Weight", "Sector"]])
    body = pd.DataFrame([[f"Co {t}", t, 3.3, "x"] for t in iw.DOW30] + [["US DOLLAR", "CASH_USD", 0.1, "-"]])
    buf = io.BytesIO()
    pd.concat([head, body]).to_excel(buf, header=False, index=False)
    (tmp_path / "dia.xlsx").write_bytes(buf.getvalue())
    iw.main(["dow", "--file", "dia.xlsx"])
    assert read_watchlist(tmp_path / "watchlist_dow30.yaml") == sorted(iw.DOW30)


def test_dow_falls_back_to_the_built_in_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    class Down:
        def get(self, *a, **k):
            raise ConnectionError("blocked")
    monkeypatch.setattr(iw.ix, "_session", lambda: Down())
    iw.main(["dow"])
    assert len(read_watchlist(tmp_path / "watchlist_dow30.yaml")) == 30
    assert "built-in" in capsys.readouterr().err


def test_shipped_dow_list():
    syms = read_watchlist(Path(__file__).parent.parent / "watchlist_dow30.yaml")
    assert syms == sorted(iw.DOW30) and "GOOGL" in syms and "VZ" not in syms
