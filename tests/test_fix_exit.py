"""py -m swingbot.fix_exit: correct a recorded sale price (2026-10-26 BUSE read as 84.32)."""
import csv
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from swingbot import fix_exit, history as h
from swingbot.dashboard import Dashboard

ET = ZoneInfo("America/New_York")
DAY = "2026-10-26"
TRADES = [dict(symbol="BBIO", kind="bounce", shares=368, entry=68.26, exit=67.90, pnl=-132.42, reason="stop",
               entry_time="10:40", exit_time="10:45"),
          dict(symbol="EVRG", kind="bounce", shares=297, entry=84.33, exit=84.32, pnl=-2.97, reason="trail",
               entry_time="11:00", exit_time="13:50"),
          dict(symbol="BUSE", kind="bounce", shares=775, entry=30.46, exit=84.32, pnl=41741.50, reason="target",
               entry_time="13:55", exit_time="15:45")]


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("broker: alpaca\nwatchlist: [AAPL]\ndashboard_jpg: false\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / f"{DAY}-state.json").write_text(json.dumps(dict(date=DAY, broker="Alpaca", live=True,
                                                            start_equity=100_000.0, closed=TRADES)), encoding="utf-8")
    with (tmp_path / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "mode", "symbol", "kind", "event", "shares", "entry", "initial_stop", "stop", "target",
                    "exit", "pnl"])
        w.writerow([f"{DAY}T13:55:17-04:00", "live", "BUSE", "bounce", "entry", 775, 30.46, 30.39, 30.39, 30.67, "", 0])
        w.writerow([f"{DAY}T15:45:16-04:00", "live", "BUSE", "bounce", "target", 775, 30.46, 30.39, 30.46, 30.67,
                    84.32, 41741.5])
        w.writerow([f"{DAY}T13:50:00-04:00", "live", "EVRG", "bounce", "trail", 297, 84.33, 84.1, 84.32, "", 84.32, -2.97])
    d = Dashboard(8881)
    d.update(dict(symbol="BUSE", trades=TRADES, pos=None, day_pnl=41606.11))
    d.snapshot(logs / f"{DAY}-dashboard.html")
    h.write_days(logs / h.CSV_NAME, [h.Day(DAY, "alpaca-paper", 100_000.0, 99_952.96, 41606.11, 3, 1, 2)])
    return tmp_path


def _check(folder, price):
    st = json.loads((folder / "logs" / f"{DAY}-state.json").read_text(encoding="utf-8"))
    buse = st["closed"][-1]
    assert buse["exit"] == price and buse["pnl"] == round((price - 30.46) * 775, 2)
    assert st["closed"][1]["exit"] == 84.32                                     # EVRG untouched
    rows = list(csv.reader((folder / "trades.csv").open(encoding="utf-8")))
    assert float(rows[2][10]) == price and rows[3][10] == "84.32"
    day = h.read_days(folder / "logs" / h.CSV_NAME)[0]
    assert day.bot_pnl == round(-132.42 - 2.97 + buse["pnl"], 2) and day.end_value == 99_952.96
    page = (folder / "logs" / f"{DAY}-dashboard.html").read_text(encoding="utf-8")
    assert "41741" not in page and f'"day_pnl": {day.bot_pnl}' in page
    assert (folder / "logs" / "results-alpaca-paper.xlsx").exists()


def test_with_a_price(folder):
    fix_exit.main([DAY, "buse", "30.67"])
    _check(folder, 30.67)


def test_looks_up_alpaca_fills(folder, monkeypatch):
    t = lambda hh, mm: datetime(2026, 10, 26, hh, mm, 5, tzinfo=ET)
    leg = SimpleNamespace(side="sell", filled_qty="775", filled_avg_price="30.67", filled_at=t(15, 45), legs=[])
    old = SimpleNamespace(side="sell", filled_qty="100", filled_avg_price="29.00", filled_at=t(9, 50), legs=[])
    entry = SimpleNamespace(side="buy", filled_qty="775", filled_avg_price="30.46", filled_at=t(13, 55), legs=[leg])
    trading = SimpleNamespace(get_orders=lambda req: [entry, old])
    import swingbot.alpaca_live as al
    monkeypatch.setattr(al, "make_alpaca_client", lambda cfg: SimpleNamespace(trading=trading, close=lambda: None))
    fix_exit.main([DAY, "BUSE"])
    _check(folder, 30.67)


def test_breakeven_stop_fill_is_called_trail(folder):
    fix_exit.main([DAY, "BUSE", "30.46"])
    st = json.loads((folder / "logs" / f"{DAY}-state.json").read_text(encoding="utf-8"))
    assert st["closed"][-1]["reason"] == "trail" and st["closed"][-1]["pnl"] == 0
