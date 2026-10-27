"""watchlist_indexes: index members downloaded each morning, combined, and only tradable stocks kept."""
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from swingbot import indexes as ix, live
from swingbot.alpaca_live import AlpacaBot, AlpacaClients
from swingbot.config import Config

NAN = float("nan")
LISTS = {
    "sp500": [("AAPL", 7.0, 230.0), ("BRK B", 1.6, 480.0), ("BF B", 0.0, 8.0), ("MSFT", 6.0, 500.0)],
    "dow": [("AAPL", NAN, NAN), ("MSFT", NAN, NAN), ("GS", NAN, NAN)],
    "nasdaq": [("AAPL", NAN, NAN), ("TINY", NAN, NAN), ("ZZZZ", NAN, NAN)],
    "nasdaq100": [("AAPL", NAN, 230.0), ("MSFT", NAN, 500.0)],
    "russell2000": [("LIVN", 0.1, 50.0), ("BUSE", 0.1, 30.0), ("PENY", 0.1, 3.0)],
}


def _dl(calls):
    def download(index):
        calls.append(index)
        return "Oct 27, 2026", LISTS[index]
    return download


def test_combined_once_each_with_your_list_and_price_filter(tmp_path):
    calls = []
    syms, counts = ix.daily_watchlist(list(LISTS), ["NVDA", "aapl"], tmp_path, min_price=10, download=_dl(calls))
    assert syms[:3] == ["AAPL", "BRK.B", "MSFT"] and "BF.B" not in syms and "PENY" not in syms   # priced lists
    assert "TINY" in syms                                      # no price in the Nasdaq list: the scan filters it
    assert len(syms) == len(set(syms)) and syms[-1] == "NVDA"
    assert counts == {"S&P 500": 3, "Dow Jones Industrial Average": 3, "Nasdaq Composite": 3, "Nasdaq-100": 2,
                      "Russell 2000": 2, "your watchlist": 2}
    assert sorted(calls) == sorted(LISTS)


def test_downloaded_once_a_day_and_saved_copy_used_when_a_download_fails(tmp_path, caplog):
    calls = []
    ix.index_symbols("sp500", tmp_path, today=date(2026, 10, 27), download=_dl(calls))
    ix.index_symbols("sp500", tmp_path, today=date(2026, 10, 27), download=_dl(calls))
    assert calls == ["sp500"]                                  # second time: today's saved copy

    def down(index):
        raise ix.DownloadError("x", "u", ["HTTP 403"])
    syms = ix.index_symbols("sp500", tmp_path, today=date(2026, 10, 28), download=down)
    assert syms == ["AAPL", "BF.B", "BRK.B", "MSFT"] and "copy from 2026-10-27" in caplog.text
    assert ix.index_symbols("dow", tmp_path, today=date(2026, 10, 28), download=down) == []
    assert "left out today" in caplog.text


def test_nasdaq100_list_parses():
    import itertools, string
    names = ["".join(p) for p in itertools.product(string.ascii_uppercase, repeat=2)][:100]
    rows = [{"symbol": n, "lastSalePrice": "$123.45"} for n in names]
    rows[0]["symbol"] = "GOOGL"
    rows[1]["symbol"] = "BRK.B"
    as_of, out = ix.parse_nasdaq100(json.dumps({"data": {"date": "Oct 27, 2026", "data": {"rows": rows}}}))
    assert out[0][0] == "GOOGL" and out[0][2] == 123.45
    assert ("BRK B" in [r[0] for r in out]) and len(out) == 100
    with pytest.raises(ValueError):
        ix.parse_nasdaq100("<html>blocked</html>")


def test_unknown_index_is_refused():
    Config(watchlist_indexes=["sp500", "russell2000"]).validate()
    with pytest.raises(ValueError, match="unknown"):
        Config(watchlist_indexes=["ftse100"]).validate()


class Assets:
    def __init__(self, tradable):
        self.tradable = tradable

    def get_all_assets(self, req):
        return [SimpleNamespace(symbol=s, tradable=t) for s, t in self.tradable.items()]


def test_only_stocks_tradable_at_alpaca_and_on_public_are_scanned(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(ix, "members", lambda index, warn=None: ("today", LISTS[index]))
    monkeypatch.setattr(live, "now_et", lambda: datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    trading = Assets({"AAPL": True, "MSFT": True, "BRK.B": True, "GS": False, "LIVN": True, "BUSE": True,
                      "TINY": True, "ZZZZ": True, "NVDA": True})
    public = SimpleNamespace(get_all_instruments=lambda req: SimpleNamespace(instruments=[
        SimpleNamespace(instrument=SimpleNamespace(symbol=s), trading=t) for s, t in
        [("AAPL", "BUY_AND_SELL"), ("MSFT", "BUY_AND_SELL"), ("BRK.B", "BUY_AND_SELL"), ("LIVN", "BUY_AND_SELL"),
         ("BUSE", "BUY_AND_SELL"), ("TINY", "LIQUIDATION_ONLY"), ("NVDA", "BUY_AND_SELL"), ("GS", "BUY_AND_SELL")]]))
    cfg = Config(broker="alpaca", price_data="public", watchlist=["NVDA"], min_price=10,
                 watchlist_indexes=["sp500", "dow", "nasdaq", "russell2000"])
    bot = AlpacaBot(AlpacaClients(trading, None, "iex", True, public=public), cfg, live=False, workdir=tmp_path)
    syms = bot.scan_symbols(bot.scan_cfg())
    assert syms == ["AAPL", "BRK.B", "MSFT", "BUSE", "LIVN", "NVDA"]
    assert "GS" not in syms and "TINY" not in syms and "ZZZZ" not in syms      # Alpaca no / Public no / neither
    assert "not tradable at Alpaca and on Public's price data" in caplog.text
    trading.tradable["AAPL"] = False
    assert bot.scan_symbols(bot.scan_cfg()) == syms             # worked out once a day (rescans reuse it)


def test_public_broker_filters_on_public_only(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "now_et", lambda: datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    client = SimpleNamespace(get_all_instruments=lambda req: SimpleNamespace(instruments=[
        SimpleNamespace(instrument=SimpleNamespace(symbol="AAPL"), trading="BUY_AND_SELL"),
        SimpleNamespace(instrument=SimpleNamespace(symbol="XYZ"), trading="DISABLED")]))
    bot = live.Bot(client, Config(watchlist=["AAPL", "XYZ", "QQQQ"]), live=False, workdir=tmp_path)
    assert bot.scan_symbols(bot.cfg) == ["AAPL"]


def test_without_indexes_the_watchlist_is_unchanged_apart_from_tradability(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "now_et", lambda: datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    bot = live.Bot(SimpleNamespace(), Config(watchlist=["AAPL", "MSFT"]), live=False, workdir=tmp_path)
    assert bot.scan_symbols(bot.cfg) == ["AAPL", "MSFT"]        # tradable list unavailable: nothing dropped
    assert not (tmp_path / "logs" / "indexes").exists()
