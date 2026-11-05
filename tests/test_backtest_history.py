"""Backtests on months of Alpaca history, the watch-list engine, and side-by-side comparisons."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from swingbot import backtest as bt
from swingbot.config import Config
from swingbot.data import synthetic
from swingbot.history_data import load_history
from swingbot.scanner import scan

ET = ZoneInfo("America/New_York")


class FakeAlpacaData:
    def __init__(self, frames):
        self.frames, self.requests = frames, []

    def get_stock_bars(self, req):
        syms = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
        utc = lambda x: pd.Timestamp(x).tz_localize("UTC") if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)
        start, end = utc(req.start), utc(req.end)
        self.requests.append((list(syms), start, end, str(req.feed)))
        parts = {}
        for s in syms:
            d = self.frames[s].tz_convert("UTC")
            d = d[(d.index >= start) & (d.index <= end)]
            if len(d):
                parts[s] = d
        if not parts:
            return SimpleNamespace(df=pd.DataFrame())
        return SimpleNamespace(df=pd.concat(parts, names=["symbol", "timestamp"]))


def test_history_is_downloaded_once_then_only_new_days(tmp_path):
    full = {s: synthetic(s, days=40, start="2026-08-03", seed=k) for k, s in enumerate(["AAA", "BBB", "CCC"])}
    upto = lambda day: {s: df[df.index.date <= pd.Timestamp(day).date()] for s, df in full.items()}
    client = FakeAlpacaData(upto("2026-09-11"))
    now = datetime(2026, 9, 12, 9, 0, tzinfo=ET)
    got = load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=now)
    assert set(got) == {"AAA", "BBB", "CCC"} and "sip" in client.requests[0][3].lower()
    assert got["AAA"].index.min().date() >= pd.Timestamp("2026-08-13").date()
    assert (tmp_path / "AAA.csv.gz").exists()
    first = len(client.requests)
    client.frames = upto("2026-09-18")                       # a week later: only the new days are asked for
    got2 = load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=datetime(2026, 9, 19, 9, 0, tzinfo=ET))
    new = client.requests[first:]
    assert new and all(r[1] >= pd.Timestamp("2026-09-11", tz="UTC") for r in new)
    assert got2["AAA"].index.max().date() == pd.Timestamp("2026-09-18").date()
    pd.testing.assert_frame_equal(got2["AAA"][got2["AAA"].index.date <= pd.Timestamp("2026-09-11").date()],
                                  got["AAA"][got["AAA"].index >= got2["AAA"].index.min()], check_freq=False)
    n = len(client.requests)
    load_history(client, ["AAA", "BBB", "CCC"], 30, tmp_path, now=datetime(2026, 9, 19, 9, 5, tzinfo=ET))
    assert len(client.requests) == n                          # up to date: nothing downloaded


def test_fast_backtest_scan_ranks_exactly_like_the_live_scan():
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=16, seed=i) for i in range(25)}
    cfg = Config(direction="both", min_avg_dollar_volume=0)
    prep = bt.prepare(hist, cfg)
    days = sorted(set(next(iter(prep.values()))[3]))
    for k in range(3, len(days)):
        day, ws = days[k], days[max(0, k - 10)]
        live = {s: h[(np.array(h.index.date) >= ws) & (np.array(h.index.date) < day)] for s, h in hist.items()}
        pre, prior = {}, {}
        for s, (d, lo, hi, dates, md) in prep.items():
            a, b = np.searchsorted(dates, ws), np.searchsorted(dates, day)
            prior[s] = d.iloc[a:b]
            pre[s] = (prior[s], lo[a:b], hi[a:b], md.iloc[a:b])
        assert [(r.symbol, round(r.score, 9)) for r in scan(live, cfg)] == \
               [(r.symbol, round(r.score, 9)) for r in scan(prior, cfg, pre=pre)]


def test_watch_engine_follows_the_live_rules():
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=30, seed=i) for i in range(30)}
    cfg = Config(direction="both", min_avg_dollar_volume=0, watch_count=10, max_trades_per_day=3)
    trades, curve, picks = bt.run(hist, cfg)
    assert len(trades) > 20
    per_day = trades.groupby("date").size()
    assert per_day.max() <= 3                                # trades per day
    for _, g in trades.groupby("date"):                      # one position at a time
        spans = sorted(zip(g.entry_time, g.exit_time))
        assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    assert trades["rank"].max() > 1 and trades["rank"].max() <= 10       # not only the top pick trades
    assert all(len(p["watched"]) <= 10 for p in picks)
    assert (trades.entry_time >= "09:45").all() and (trades.exit_time <= "15:50").all()
    one = bt.run(hist, Config(direction="both", min_avg_dollar_volume=0, watch_count=1, max_trades_per_day=3))[0]
    assert (one["rank"] == 1).all()
    tables = bt.breakdown(trades)
    assert {"By setup", "By entry hour", "By exit", "By month"} <= set(tables)
    assert set(tables["By setup"].index) <= {"long bounce", "long breakout", "short bounce", "short breakdown"}
    assert tables["By setup"].trades.sum() == len(trades)


def test_compare_runs_each_combination(tmp_path, monkeypatch, capsys):
    from swingbot import __main__ as m
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=14, seed=i) for i in range(12)}
    cfg = Config(min_avg_dollar_volume=0, direction="both")
    m._compare(hist, cfg, ["breakeven_at_r=1,1.5", "trail_buffer_atr=0.25,0.4"], tmp_path)
    table = pd.read_csv(tmp_path / "compare.csv", index_col=0)
    assert len(table) == 4 and "breakeven_at_r=1.5, trail_buffer_atr=0.4" in table.index
    assert (tmp_path / "report-breakeven_at_r1.5-trail_buffer_atr0.4.txt").exists()
    with pytest.raises(SystemExit):
        m._compare(hist, cfg, ["nonsense=1,2"], tmp_path)


# ---------------------------------------------------------------- the midday rescan in the backtest
def _hist(n=20, days=18):
    return {f"S{i:02d}": synthetic(f"S{i:02d}", days=days, seed=i) for i in range(n)}


def test_midday_rescan_picks_new_stocks_from_1245(tmp_path):
    hist = _hist()
    on = Config(direction="both", min_avg_dollar_volume=0, watch_count=3, max_trades_per_day=6)
    trades, _, picks = bt.run(hist, on)
    mids = [p["midday"] for p in picks if "midday" in p]
    assert mids and all("12:45" <= m["time"] < "15:00" for m in mids)
    after = trades[trades.scan == "midday"]
    assert len(after) and (after.entry_time >= "12:45").all()
    assert {"By scan (morning picks vs midday rescan)"} <= set(bt.breakdown(trades))
    off = Config(direction="both", min_avg_dollar_volume=0, watch_count=3, max_trades_per_day=6, midday_rescan_time="")
    t_off, _, p_off = bt.run(hist, off)
    assert (t_off.scan == "morning").all() and not any("midday" in p for p in p_off)
    same = ["date", "symbol", "kind", "entry_time", "exit_time", "reason", "r_multiple"]   # (sizes differ: equity)
    pd.testing.assert_frame_equal(trades[trades.entry_time < "12:45"][same].reset_index(drop=True),
                                  t_off[t_off.entry_time < "12:45"][same].reset_index(drop=True))


def test_midday_rescan_sees_no_bars_from_after_its_time():
    hist = _hist(15, 12)
    cfg = Config(direction="both", min_avg_dollar_volume=0, watch_count=3)
    _, _, picks = bt.run(hist, cfg)
    day = next(p for p in picks if "midday" in p)
    d0 = pd.Timestamp(day["date"]).date()
    cut = pd.Timestamp(f"{day['date']} {day['midday']['time']}", tz="America/New_York")
    changed = {}
    for s, df in hist.items():                                             # scramble everything from 12:45 on
        df = df.copy()
        late = (df.index >= cut) & (df.index.date == d0)
        df.loc[late, ["open", "high", "low", "close"]] *= 1.07
        changed[s] = df
    _, _, picks2 = bt.run(changed, cfg)
    assert next(p for p in picks2 if p["date"] == day["date"])["midday"] == day["midday"]


def test_midday_rescan_waits_for_the_open_trade(monkeypatch):
    from swingbot import strategy
    hist = _hist(6, 8)
    cfg = Config(min_avg_dollar_volume=0, watch_count=2, use_target=False, trail_stop=False, breakeven_at_r=0)
    prep = bt.prepare(hist, cfg)
    day = sorted(set(next(iter(prep.values()))[3]))[-1]
    sessions = [(s, d, lo, hi, md, np.flatnonzero(dates == day)) for s, (d, lo, hi, dates, md) in list(prep.items())[:2]]
    calls = []

    def run_with(exit_at):
        """One signal at 12:00 on the first stock; its stop is hit at ``exit_at`` (None = never)."""
        def fake_signals(d, md, t, cfg_, lo, hi):
            if d.index[t].strftime("%H:%M") == "12:00" and not calls:
                c = float(d["close"].iat[t])
                return strategy.Signal("bounce", t, None, c, c * 0.5, c)
            return None
        monkeypatch.setattr(bt, "signals_at", fake_signals)
        d = sessions[0][1]
        if exit_at:
            d = d.copy()
            hit = [i for i in sessions[0][5] if d.index[i].strftime("%H:%M") == exit_at][0]
            d.iloc[hit, d.columns.get_loc("low")] = 0.01
            ses = [(sessions[0][0], d) + sessions[0][2:]] + sessions[1:]
        else:
            ses = sessions
        got = []
        bt.simulate_watch(ses, 25_000, cfg, rescan=lambda ts: got.append(ts.strftime("%H:%M")))
        return got
    assert run_with("13:30") == ["13:35"]            # in a trade at 12:45: rescans at the first bar after it closes
    assert run_with(None) == []                       # still open at 15:00: dropped for the day
    assert run_with("15:20") == []                    # closed after 15:00: too late



def test_try_keeps_times_as_text_and_understands_off(tmp_path):
    from swingbot import __main__ as m
    hist = _hist(8, 10)
    m._compare(hist, Config(min_avg_dollar_volume=0), ["midday_rescan_time=12:45,off"], tmp_path)
    table = pd.read_csv(tmp_path / "compare.csv", index_col=0)
    assert list(table.index) == ["midday_rescan_time=12:45", "midday_rescan_time=off"]
    assert (tmp_path / "report-midday_rescan_time1245.txt").exists()          # no ":" in Windows file names



def test_one_symbol_is_watched_every_day_without_a_scan(tmp_path):
    hist = _hist(6, 14)
    cfg = Config(direction="both", min_avg_dollar_volume=1e12, max_trades_per_day=5)   # the scan would pick nothing
    assert bt.run(hist, cfg)[0].empty
    trades, _, picks = bt.run(hist, cfg, forced=["S03"])
    assert len(trades) and set(trades.symbol) == {"S03"} and (trades.scan == "morning").all()
    assert all(p["watched"] == ["S03"] for p in picks)
    from swingbot import __main__ as m
    m._compare(hist, cfg, ["swing_n=4,5"], tmp_path, forced=["S03"])
    assert (pd.read_csv(tmp_path / "compare.csv", index_col=0).trades > 0).all()



def test_try_ranges():
    from swingbot.__main__ import try_values
    assert try_values("4-8") == [4, 5, 6, 7, 8]
    assert try_values("1-2:0.25") == [1.0, 1.25, 1.5, 1.75, 2.0]
    assert try_values("0.25-0.5/0.05") == [0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
    assert try_values("2,4-6,6") == [2, 4, 5, 6]
    assert try_values("1,1.5") == [1, 1.5] and try_values("true,false") == [True, False]
    for bad in ("1-2.5", "8-4", "1-2:0", "1-1000"):
        with pytest.raises(ValueError):
            try_values(bad)


def test_try_range_runs_every_value(tmp_path):
    from swingbot import __main__ as m
    hist = _hist(6, 10)
    m._compare(hist, Config(min_avg_dollar_volume=0), ["swing_n=3-5", "breakeven_at_r=1-1.5:0.5"], tmp_path)
    table = pd.read_csv(tmp_path / "compare.csv", index_col=0)
    assert len(table) == 6 and "swing_n=4, breakeven_at_r=1.5" in table.index


def test_step_switch(tmp_path, capsys):
    from swingbot.__main__ import try_values, _compare
    from swingbot.config import Config
    assert try_values("1-2", "0.5") == [1.0, 1.5, 2.0]
    assert try_values("1-2:0.25", "0.5") == [1.0, 1.25, 1.5, 1.75, 2.0]     # its own step wins
    assert try_values("4-8", "2") == [4, 6, 8]
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=12, seed=i)
            for i in range(3)}
    cfg = Config(min_avg_dollar_volume=0)
    _compare(hist, cfg, ["swing_n=4-5", "breakeven_at_r=1-2"], tmp_path, steps=["0.5"])
    out = capsys.readouterr().out
    assert "swing_n: 4, 5" in out and "breakeven_at_r: 1.0, 1.5, 2.0" in out and "6 combinations" in out
    _compare(hist, cfg, ["swing_n=4-8"], tmp_path, steps=["swing_n=2"])
    assert "swing_n: 4, 6, 8" in capsys.readouterr().out
    for bad in (["swing_n=0.5"], ["0"], ["nonsense=1"], ["x"]):
        with pytest.raises(SystemExit):
            _compare(hist, cfg, ["swing_n=4-5"], tmp_path, steps=bad)


def test_each_run_is_kept_as_a_zip(tmp_path, monkeypatch):
    import zipfile
    from swingbot import __main__ as m
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=12, seed=i) for i in range(3)}
    cfg = Config(min_avg_dollar_volume=0)
    (tmp_path / "config.yaml").write_text("swing_n: 5\n", encoding="utf-8")
    monkeypatch.setattr(m.sys, "argv", ["swingbot", "backtest", "--symbol", "S01", "--try", "swing_n=4-5"])
    args = SimpleNamespace(symbol="S01", universe="sp500", days=180, tries=["swing_n=4-5"], steps=["0.5"],
                           config=str(tmp_path / "config.yaml"))
    files = m._compare(hist, cfg, args.tries, tmp_path / "bt", steps=args.steps)
    when = datetime(2026, 11, 3, 19, 30, 5)
    first = m._archive(files, cfg, args, tmp_path, now=when)
    assert not any(f.exists() for f in files)                       # moved into the zip
    second = m._archive(files, cfg, args, tmp_path, now=when)      # same second: a new name, nothing overwritten
    assert first.parent == tmp_path / "backtests" / "Results" and first != second and second.exists()
    assert first.name == "2026-11-03_193005-S01-180d-swing_n=4-5-step0.5.zip"
    names = zipfile.ZipFile(first).namelist()
    assert {"run.txt", "config.yaml", "compare.xlsx", "compare.txt", "compare.csv", "report-swing_n4.txt",
            "trades-swing_n5.csv"} <= set(names)
    assert "--try swing_n=4-5" in zipfile.ZipFile(first).read("run.txt").decode()
    assert m._archive(files, Config(backtest_results_dir=""), args, tmp_path) is None


def test_excel_results_with_line_charts(tmp_path):
    import openpyxl
    from swingbot import __main__ as m
    from swingbot.backtest_xlsx import run_workbook, CHART_LINES
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=20, seed=i) for i in range(6)}
    cfg = Config(min_avg_dollar_volume=0, direction="both")
    trades, curve, _ = bt.run(hist, cfg)
    wb = openpyxl.load_workbook(run_workbook(tmp_path / "backtest.xlsx", trades, curve, cfg, "one run"))
    assert wb.sheetnames == ["Summary", "Equity", "Trades"]
    assert len(wb["Summary"]._charts) == 1 and wb["Equity"].max_row == len(curve) + 1
    assert wb["Trades"].max_row == len(trades) + 1 and wb["Trades"]["P1"].value == "P&L"
    files = m._compare(hist, cfg, ["swing_n=3-5", "breakeven_at_r=1-2"], tmp_path / "c", steps=["0.25"])  # 15 runs
    xl = next(f for f in files if f.name == "compare.xlsx")
    assert (tmp_path / "c" / "compare.csv").exists()                                    # CSVs kept too
    wb = openpyxl.load_workbook(xl)
    chart = wb["Compare"]._charts[0]
    assert len(chart.series) == CHART_LINES                     # only the best 10 of 15 drawn...
    assert wb["Equity"].max_column == 16                        # ...but every combination is on the Equity tab
    assert wb["Trades"]["A1"].value == "Settings" and wb["Trades"].max_row > 1


def test_excel_files_open_without_repairs(tmp_path):
    """Excel 'repairs' a sheet whose selection points at a frozen pane that isn't there."""
    import re
    import zipfile
    from swingbot import __main__ as m
    from swingbot.backtest_xlsx import run_workbook
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=12, seed=i) for i in range(4)}
    cfg = Config(min_avg_dollar_volume=0)
    trades, curve, _ = bt.run(hist, cfg)
    books = [run_workbook(tmp_path / "backtest.xlsx", trades, curve, cfg, "one run"),
             next(f for f in m._compare(hist, cfg, ["swing_n=4,5"], tmp_path / "c") if f.suffix == ".xlsx")]
    for book in books:
        z = zipfile.ZipFile(book)
        for name in [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]:
            view = re.search(r"<sheetView.*?</sheetView>|<sheetView[^>]*/>", z.read(name).decode()).group(0)
            if 'pane="' in view:
                assert "<pane " in view, f"{book.name} {name}: {view}"


def test_interactive_charts(tmp_path):
    import json
    import re
    from swingbot import __main__ as m
    from swingbot.backtest_chart import run_chart, equity_points, BEST
    hist = {f"S{i:02d}": synthetic(f"S{i:02d}", days=20, seed=i) for i in range(6)}
    cfg = Config(min_avg_dollar_volume=0, direction="both")
    trades, curve, _ = bt.run(hist, cfg)
    days = [d.strftime("%Y-%m-%d") for d in curve.index]
    pts = equity_points(trades, days, cfg.starting_equity)
    assert pts[0][1] == cfg.starting_equity and len(pts) == len(trades) + 2      # start, each exit, end of last day
    assert abs(pts[-1][1] - curve.iloc[-1]) < 0.02                                 # ends where the daily curve ends
    assert all(a[0] <= b[0] for a, b in zip(pts, pts[1:]))                         # in time order
    first = next(p for p in pts if p[4])                                           # a trade: date, time, symbol, P&L
    assert re.match(r"\d{4}-\d\d-\d\d$", first[2]) and re.match(r"\d\d:\d\d$", first[3]) and first[9] is not None
    page = run_chart(tmp_path / "backtest-chart.html", trades, curve, cfg, "one run").read_text(encoding="utf-8")
    assert "<script src" not in page and "http" not in page.split("<script>")[1]   # works offline
    data = json.loads(re.search(r"const D = (\{.*?\});\n", page).group(1))
    assert len(data["series"]) == 1 and data["series"][0]["pts"] == json.loads(json.dumps(pts))
    files = m._compare(hist, cfg, ["swing_n=3-5", "breakeven_at_r=1-2"], tmp_path / "c", steps=["0.25"])
    assert files[0].name == "compare-chart.html"
    data = json.loads(re.search(r"const D = (\{.*?\});\n", files[0].read_text(encoding="utf-8")).group(1))
    assert len(data["series"]) == 15 and sum(s["on"] for s in data["series"]) == BEST


def test_download_command_fills_the_cache_in_chunks(tmp_path, monkeypatch, capsys):
    from swingbot import __main__ as m
    from swingbot import history_data
    calls = []

    def fake_load(client, syms, days, folder, minutes=5, **kw):
        calls.append(list(syms))
        folder.mkdir(parents=True, exist_ok=True)
        for s in syms:
            if s != "NODATA":
                (folder / f"{s}.csv.gz").write_bytes(b"x" * 1000)
        return {s: None for s in syms if s != "NODATA"}

    monkeypatch.setattr(m, "_data_client", lambda: object())
    monkeypatch.setattr(history_data, "load_history", fake_load)
    monkeypatch.setattr(m, "DOWNLOAD_CHUNK", 3)
    syms = ",".join([f"S{i}" for i in range(7)] + ["NODATA"])
    args = SimpleNamespace(days=180, symbol=syms, universe="sp500", max_symbols=2)   # no cap for downloads
    m._download(Config(), args, tmp_path)
    assert [len(c) for c in calls] == [3, 3, 2]                     # held in memory a chunk at a time
    out = capsys.readouterr().out
    assert "8 of 8 stocks done" in out and "Done: 7 of 8 stocks" in out and "1 had none" in out
    assert len(list((tmp_path / "data" / "alpaca-5min").glob("*.csv.gz"))) == 7
    with pytest.raises(SystemExit):
        m._download(Config(), SimpleNamespace(days=None, symbol="A", universe="sp500"), tmp_path)


def test_windows_reserved_names_are_not_used_for_files(tmp_path):
    """CON (Concentra) would be saved as CON.csv.gz - a name Windows keeps for the console, which stops
    OneDrive syncing. Such symbols get an underscore, and an old CON.csv.gz is renamed when it's next used."""
    from swingbot.data import file_stem, load_dir, save_csv, symbol_from_stem
    assert [file_stem(s) for s in ("CON", "con", "PRN", "COM1", "LPT9", "NUL", "AAPL", "CONN", "BRK/B", "CON.A")] == \
        ["CON_", "con_", "PRN_", "COM1_", "LPT9_", "NUL_", "AAPL", "CONN", "BRK_B", "CON_.A"]
    assert symbol_from_stem("CON_") == "CON" and symbol_from_stem("CONN") == "CONN" and symbol_from_stem("A_") == "A_"
    full = {s: synthetic(s, days=10, start="2026-09-01", seed=k) for k, s in enumerate(["CON", "AUX"])}
    client = FakeAlpacaData(full)
    now = datetime(2026, 9, 16, 9, 0, tzinfo=ET)
    load_history(client, ["CON"], 5, tmp_path, now=now)
    assert (tmp_path / "CON_.csv.gz").exists()
    assert not [p.name for p in tmp_path.iterdir() if p.name.split(".")[0].upper() == "CON"]
    (tmp_path / "CON_.csv.gz").rename(tmp_path / "CON.csv.gz")            # as saved by v1.19.2 and earlier
    n = len(client.requests)
    got = load_history(client, ["CON"], 5, tmp_path, now=now)
    assert len(got["CON"]) and all(r[1] > client.requests[0][1] for r in client.requests[n:])  # found: only topped up
    assert (tmp_path / "CON_.csv.gz").exists() and not (tmp_path / "CON.csv.gz").exists()
    (tmp_path / "d").mkdir()
    save_csv(full["AUX"], tmp_path / "d" / f"{file_stem('AUX')}.csv")       # the fetch command's files
    assert list(load_dir(tmp_path / "d")) == ["AUX"]
