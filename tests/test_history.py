"""Results spreadsheet: daily rows for 30 days, weekly rows before that, from the daily record."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from swingbot import history as h
from swingbot.config import Config

openpyxl = pytest.importorskip("openpyxl")


def _days(start=date(2026, 7, 1), end=date(2026, 10, 23)):
    out, v, d = [], 100_000.0, start
    while d <= end:
        if d.weekday() < 5:
            s, v = v, v + 10
            out.append(h.Day(str(d), "alpaca-paper", s, v, 10.0, 1, 1, 0))
        d += timedelta(days=1)
    return out


def test_daily_for_30_days_weekly_before(tmp_path):
    days = _days()
    out = h.build_workbook(days, "alpaca-paper", tmp_path / "r.xlsx", today=date(2026, 10, 23))
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Summary", "Last 30 days", "Weekly (older)", "All days", "By setup", "Trades"]
    D, W, A = wb["Last 30 days"], wb["Weekly (older)"], wb["All days"]
    daily = [r[0].value.date() for r in D.iter_rows(min_row=5) if r[0].value]
    assert daily[0] == date(2026, 9, 24) and daily[-1] == date(2026, 10, 23)        # 30 calendar days
    assert len(daily) == sum(1 for d in days if d.date >= "2026-09-24")
    weeks = [r for r in W.iter_rows(min_row=5) if r[0].value]
    assert weeks[0][0].value.date() == date(2026, 6, 29)                            # week of the first day
    assert weeks[-1][0].value.date() == date(2026, 9, 21) and weeks[-1][1].value == 3   # Mon-Wed only
    assert sum(r[1].value for r in weeks) + len(daily) == len(days)                 # every day exactly once
    assert weeks[1][2].value == days[3].start_value and weeks[1][3].value == days[7].end_value   # Jul 1 is a Wednesday
    assert D["E5"].value == "=D5-C5" and D["F5"].value.startswith("=IF(C5=0")         # formulas, not numbers
    assert A.max_row == len(days) + 1
    assert wb["Summary"]["B5"].value == 100_000.0


def test_end_of_day_record_and_restart_same_day(tmp_path, monkeypatch):
    from swingbot import live
    monkeypatch.setattr(live, "now_et", lambda: __import__("datetime").datetime(2026, 10, 26, 15, 51))
    cfg = Config(broker="alpaca")
    bot = SimpleNamespace(live=True, prepared=True, start_equity=100_000.0, last_bar_seen=object(), trades_today=2,
                          closed=[{"pnl": 50.0}, {"pnl": -20.0}], not_flat=None, account_equity=lambda: 100_030.0)
    out = h.record_session(bot, cfg, tmp_path)
    assert out == tmp_path / "results-alpaca-paper.xlsx" and out.exists()
    bot.account_equity = lambda: 100_045.0                       # restarted later the same day
    h.record_session(bot, cfg, tmp_path)
    rows = h.read_days(tmp_path / h.CSV_NAME)
    assert len(rows) == 1 and rows[0].end_value == 100_045.0
    assert (rows[0].bot_pnl, rows[0].trades, rows[0].wins, rows[0].losses) == (30.0, 2, 1, 1)


def test_dry_runs_and_days_before_the_open_are_not_recorded(tmp_path):
    cfg = Config(broker="alpaca")
    dry = SimpleNamespace(live=False, prepared=True, start_equity=25_000.0, last_bar_seen=object(), trades_today=0)
    assert h.record_session(dry, cfg, tmp_path) is None
    early = SimpleNamespace(live=True, prepared=True, start_equity=1.0, last_bar_seen=None, trades_today=0)
    assert h.record_session(early, cfg, tmp_path) is None
    assert not (tmp_path / h.CSV_NAME).exists()


def test_backfill_from_state_files(tmp_path):
    for d, eq, closed in (("2026-10-20", 100_000.0, [{"pnl": 25.0}]), ("2026-10-21", 100_030.0, []),
                          ("2026-10-22", 100_168.1, [{"pnl": 28.38}])):
        (tmp_path / f"{d}-state.json").write_text(json.dumps(
            dict(date=d, broker="Alpaca", live=True, start_equity=eq, closed=closed)), encoding="utf-8")
    (tmp_path / "2026-10-19-state.json").write_text(json.dumps(                     # dry run: ignored
        dict(date="2026-10-19", broker="Alpaca", live=False, start_equity=25_000, closed=[])), encoding="utf-8")
    h.update(tmp_path, Config(broker="alpaca"))
    rows = {r.date: r for r in h.read_days(tmp_path / h.CSV_NAME)}
    assert sorted(rows) == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert rows["2026-10-20"].end_value == 100_030.0 and "next day" in rows["2026-10-20"].note
    assert rows["2026-10-22"].end_value == 100_196.48 and rows["2026-10-22"].trades == 1
    assert (tmp_path / "results-alpaca-paper.xlsx").exists()


def test_open_in_excel_is_a_warning(tmp_path, monkeypatch, caplog):
    def locked(*a, **k):
        raise PermissionError("in use")
    monkeypatch.setattr(h, "build_workbook", locked)
    h.write_days(tmp_path / h.CSV_NAME, _days(date(2026, 10, 1), date(2026, 10, 2)))
    assert h.update(tmp_path, Config(broker="alpaca")) is None
    assert "open in Excel" in caplog.text


def test_by_setup_sheet_from_trades_csv(tmp_path):
    import csv
    rows = [["time", "mode", "symbol", "kind", "event", "shares", "entry", "initial_stop", "stop", "target", "exit", "pnl"],
            ["2026-10-27T10:20:17-04:00", "live", "CELC", "short bounce", "entry", 376, 66.24, 67.4, 67.4, "", "", 0],
            ["2026-10-27T10:55:15-04:00", "live", "CELC", "short bounce", "stop", 376, 66.24, 67.4, 67.4, "", 67.54, -488.8],
            ["2026-10-27T14:25:16-04:00", "live", "NAMS", "breakout", "stop", 1216, 20.53, 20.43, 20.43, "", 20.45, -97.28],
            ["2026-10-28T11:00:00-04:00", "paper", "XYZ", "bounce", "stop", 10, 10, 9, 9, "", 9, -10]]   # dry run: out
    with (tmp_path / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    trades = h.read_trades(tmp_path / "trades.csv")
    assert [(t["setup"], t["r"]) for t in trades] == [("short bounce", -1.121), ("long breakout", -0.8)]
    out = h.build_workbook(_days(), "alpaca-paper", tmp_path / "r.xlsx", today=date(2026, 10, 23), trades=trades)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames[-2:] == ["By setup", "Trades"]
    bs = wb["By setup"]
    assert [bs.cell(row=r, column=1).value for r in range(5, 10)] == list(h.SETUPS) + ["All"]
    assert bs["B5"].value.startswith("=COUNTIF(Trades!$C$2:$C$3")              # formulas over the Trades sheet
    assert wb["Trades"]["C2"].value == "short bounce" and wb["Trades"].max_row == 3
