"""Settings recorded per day (and grouped into periods), and the evening live-vs-backtest check."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from swingbot import backtest as bt
from swingbot import daycheck, live, settings_log
from swingbot import results_site as rs
from swingbot.config import Config
from swingbot.data import synthetic


def _bot(cfg, day_overrides=None, bm=5):
    return SimpleNamespace(prepared=True, last_bar_seen=pd.Timestamp("2026-11-05 15:45"), trades_today=1, live=True,
                           cfg0=cfg, overrides=day_overrides or {}, bm=bm, forced_symbol=None)


def test_settings_recorded_each_day_and_grouped(tmp_path, monkeypatch):
    days = ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    cfgs = [Config(min_stop_atr=0.5), Config(min_stop_atr=1.5, trail_stop=False), Config(min_stop_atr=1.5, trail_stop=False),
            Config(min_stop_atr=1.5, trail_stop=False)]
    over = [{}, {}, {"target_r": 2.0}, {}]
    for d, c, o in zip(days, cfgs, over):
        monkeypatch.setattr(live, "now_et", lambda d=d: pd.Timestamp(d + " 16:00", tz="America/New_York").to_pydatetime())
        settings_log.record(_bot(c, o, bm=10 if d == "2026-11-06" else 5), c, tmp_path)
    settings_log.record(_bot(cfgs[-1]), cfgs[-1], tmp_path)                  # same day again: replaced, not added
    recs = settings_log.read(tmp_path)
    assert [r["date"] for r in recs] == days and "dashboard_port" not in recs[0]["settings"]
    assert recs[2]["today_only"] == {"target_r": 2.0} and recs[3]["today_only"] == {}
    rows = [dict(date=d, bot_pnl=p, trades=2, wins=1) for d, p in zip(["2026-10-30"] + days, [-10, -50, 120, 80, 40])]
    per = settings_log.periods(recs, rows, recs[0]["account"])
    assert [(p["start"], p["days"]) for p in per] == [("2026-10-30", 1), ("2026-11-03", 1), ("2026-11-04", 3)]
    assert per[0]["changes"] == ["(before settings were recorded)"] and not per[0]["recorded"]
    assert "min_stop_atr 0.5 → 1.5" in per[2]["changes"] and "trail_stop on → off" in per[2]["changes"]
    assert per[2]["bot_pnl"] == 240 and per[2]["per_day"] == 80
    assert per[2]["today_only"] == ["2026-11-05: target_r 2"]


def test_compare_matches_trades_and_measures_fills():
    bt_trades = pd.DataFrame([
        dict(symbol="PLTR", side="short", kind="short bounce", entry_time="10:20", entry=176.09, exit=170.60,
             reason="flatten", pnl=763.0),
        dict(symbol="AAPL", side="long", kind="bounce", entry_time="11:00", entry=200.0, exit=198.0, reason="stop",
             pnl=-200.0)])
    live_trades = [dict(symbol="PLTR", side=-1, kind="short bounce", entry_time="10:20", entry=175.87, exit=170.50,
                        pnl=746.68, reason="end of day", shares=139),
                   dict(symbol="MSFT", side=1, kind="breakout", entry_time="13:00", entry=400.0, exit=401.0, pnl=50.0,
                        reason="closed by you", shares=50)]
    r = daycheck.compare(live_trades, bt_trades, 5)
    assert r["matched"] == 1 and r["live_trades"] == 2 and r["bt_trades"] == 2
    p = r["pairs"][0]
    assert p["entry_cost"] == pytest.approx(0.22) and p["exit_cost"] == pytest.approx(-0.10)   # short: sold lower, covered lower
    assert p["cost_dollars"] == pytest.approx(0.12 * 139)
    assert [x["symbol"] for x in r["live_only"]] == ["MSFT"] and [x["symbol"] for x in r["bt_only"]] == ["AAPL"]
    assert r["gap"] == pytest.approx(796.68 - 563.0)
    html = daycheck.html_section(dict(r, watched=["PLTR", "AAPL"], slippage_setting=0.02))
    assert "Live vs backtest" in html and "PLTR short" in html and "Only the bot" in html


def test_evening_check_end_to_end(tmp_path):
    """The bot's own trades replayed: everything matches and costs nothing."""
    cfg = Config(min_avg_dollar_volume=0, direction="both", slippage_pct=0)
    found = None
    for seed in range(30):
        df = synthetic("AAA", days=6, seed=seed)
        trades, _, _ = bt.run({"AAA": df}, cfg, forced=["AAA"])
        last = sorted(set(df.index.tz_convert("America/New_York").date))[-1]
        today = trades[trades["date"].astype(str) == str(last)] if len(trades) else trades
        if len(today):
            found = (df, today, last)
            break
    if found is None:
        pytest.skip("no synthetic day with a trade")
    df, today, last = found
    live_trades = [dict(symbol=t.symbol, side=-1 if t.side == "short" else 1, kind=t.kind, entry_time=t.entry_time,
                        entry=t.entry, exit=t.exit, pnl=t.pnl, reason=t.reason, shares=t.shares) for t in today.itertuples()]
    bot = SimpleNamespace(cfg=cfg, bm=5, start_equity=cfg.starting_equity, closed=live_trades, watched_today=["AAA"],
                          fetch_bars=lambda sym, period: df)
    res = daycheck.run(bot, cfg, tmp_path, str(last))
    assert res["matched"] == len(today) and not res["live_only"] and not res["bt_only"]
    assert res["slippage_dollars"] == pytest.approx(0, abs=1e-6)
    saved = json.loads((tmp_path / f"{last}-check.json").read_text(encoding="utf-8"))
    assert saved["matched"] == res["matched"]


def test_site_shows_periods_and_checks(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "daily-results.csv").write_text(
        "date,account,start_value,end_value,bot_pnl,trades,wins,losses,note\n"
        "2026-11-04,alpaca-paper,98312.55,99059.23,746.68,1,1,0,\n", encoding="utf-8")
    (tmp_path / "logs" / "settings-history.jsonl").write_text(json.dumps(dict(
        date="2026-11-04", account="alpaca-paper", live=True, settings={"min_stop_atr": 1.5}, today_only={})) + "\n",
        encoding="utf-8")
    (tmp_path / "logs" / "2026-11-04-check.json").write_text(json.dumps(dict(
        date="2026-11-04", live_trades=1, bt_trades=1, matched=1, live_pnl=746.68, bt_pnl=763.0, gap=-16.32,
        slippage_dollars=16.68, slippage_pct_per_side=0.03, slippage_setting=0.02, live_only=[], bt_only=[])),
        encoding="utf-8")
    (tmp_path / "logs" / "update-check.json").write_text('{"repo": "x"}', encoding="utf-8")   # not a day's check
    d = rs.collect(tmp_path, Config(alpaca_paper=True, broker="alpaca"))
    assert len(d["checks"]) == 1
    a = d["accounts"]["alpaca-paper"]
    assert a["periods"][0]["changes"] == ["(first recorded settings)"] and a["periods"][0]["days"] == 1
    assert d["checks"][0]["matched"] == 1 and d["check_summary"]["avg_pct_per_side"] == 0.03
