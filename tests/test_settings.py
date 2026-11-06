"""Settings shown on the dashboard, and the three that can be changed for the rest of the day."""
import json
import urllib.request
from types import SimpleNamespace

import pandas as pd
import pytest

from swingbot import live
from swingbot.config import Config
from swingbot.dashboard import Dashboard
from test_alpaca import _bot


def test_values_are_checked_and_queued(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    for key, bad in (("trail_buffer_atr", 0), ("trail_buffer_atr", 9), ("target_r", 0.2), ("breakeven_at_r", -1),
                     ("risk_per_trade_pct", 1), ("target_r", "x")):
        with pytest.raises(ValueError):
            bot.request_setting(key, bad)
    assert "rest of today" in bot.request_setting("trail_buffer_atr", 0.75)
    assert bot.setting_value("trail_buffer_atr") == 0.25                    # queued, not applied yet
    bot.apply_pending_settings()
    assert bot.cfg.trail_buffer_atr == 0.75 and bot.cfg.stop_buffer_atr == 0.25   # initial stops unchanged
    assert bot.cfg0.trail_buffer_atr is None                                # config.yaml copy untouched


def test_settings_rows_show_config_and_todays_changes(tmp_path):
    bot = live.Bot(None, Config(breakeven_at_r=1.0), live=False, workdir=tmp_path)
    bot.request_setting("breakeven_at_r", 2)
    bot.apply_pending_settings()
    rows = {i.get("key") or i["label"]: i for g in bot.status()["settings"] for i in g["items"]}
    assert rows["breakeven_at_r"]["value"] == 2 and rows["breakeven_at_r"]["default"] == 1.0
    assert rows["breakeven_at_r"]["changed"] and not rows["trail_buffer_atr"]["changed"]
    assert rows["Risk per trade"]["text"].startswith("0.5%") and rows["Sell everything"]["text"] == "15:50 ET"
    assert rows["target_r"]["value"] == 0.0                                 # target off in config


def test_looser_trail_in_the_strategy():
    from swingbot.strategy import trailing_stop
    n = 30
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.0, "close": 100.0, "volume": 1.0, "atr": 1.0},
                      index=range(n))
    import numpy as np
    lo = np.zeros(n, bool)
    lo[10] = True
    assert trailing_stop(df, 20, Config(min_stop_pct=0), lo, 0, 90.0) == pytest.approx(98.75)    # 0.25 ATR
    assert trailing_stop(df, 20, Config(min_stop_pct=0, trail_buffer_atr=1.0), lo, 0, 90.0) == pytest.approx(98.0)


def test_profit_target_on_the_open_alpaca_trade(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot, "_last_trade_price", lambda: 50.5)
    bot.enter(50.0, 49.0, "bounce")                                         # 1R = 1.00
    leg = next(o for o in t.all.values() if o.order_type == "limit" and o.status == "new")
    assert float(leg.limit_price) == 75.0                                   # parked: no target
    bot.request_setting("target_r", 2)
    bot.apply_pending_settings()
    legs = [o for o in t.all.values() if o.order_type == "limit" and o.status == "new"]
    assert [float(o.limit_price) for o in legs] == [52.0] and bot.pos["target"] == 52.0
    bot.request_setting("target_r", 0.5)                                    # 50.50: at the price - refused
    bot.apply_pending_settings()
    assert bot.pos["target"] == 52.0 and bot.cfg.reward_risk == 0.5        # next trades still get 0.5R
    bot.request_setting("target_r", 0)                                      # remove it: park the leg again
    bot.apply_pending_settings()
    legs = [o for o in t.all.values() if o.order_type == "limit" and o.status == "new"]
    assert [float(o.limit_price) for o in legs] == [75.0] and bot.pos["target"] is None
    assert not bot.cfg.use_target


def test_todays_changes_survive_a_restart(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(live, "now_et", lambda: datetime(2026, 10, 22, 11, 0, tzinfo=ZoneInfo("America/New_York")))
    a = live.Bot(None, Config(), live=False, workdir=tmp_path)
    a.prepared = True
    a.request_setting("trail_buffer_atr", 0.6)
    a.apply_pending_settings()
    st = a.load_state()
    b = live.Bot(None, Config(), live=False, workdir=tmp_path)
    b._restore_day(st)
    assert b.cfg.trail_buffer_atr == 0.6 and b.overrides == {"trail_buffer_atr": 0.6}


def test_dashboard_setting_endpoint(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    d = Dashboard(8881)
    d.on_setting = bot.request_setting
    d.start()
    try:
        base = f"http://127.0.0.1:{d.port}/setting"

        def post(body, token=None):
            req = urllib.request.Request(base, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            if token:
                req.add_header("X-Swingbot-Token", token)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, {}
        assert post({"key": "target_r", "value": 2})[0] == 403                  # no page token
        assert post({"key": "risk_per_trade_pct", "value": 2}, d.token)[0] == 400
        code, res = post({"key": "target_r", "value": 2}, d.token)
        assert code == 200 and "rest of today" in res["message"] and bot.pending_settings == {"target_r": 2.0}
    finally:
        d.stop()


def test_trail_settings_greyed_out_when_trailing_is_off(tmp_path):
    bot = live.Bot(None, Config(trail_stop=False), live=False, workdir=tmp_path)
    rows = {i.get("key") or i["label"]: i for g in bot.status()["settings"] for i in g["items"]}
    for k in ("trail_buffer_atr", "Trail step"):
        assert rows[k]["off"] and "trail_stop is off" in rows[k]["note"]
    assert not rows["breakeven_at_r"].get("off") and "trailing" not in rows["With no target"]["text"]
    with pytest.raises(ValueError, match="trailing stop is off"):
        bot.request_setting("trail_buffer_atr", 0.5)
    on = live.Bot(None, Config(), live=False, workdir=tmp_path / "on")
    rows = {i.get("key") or i["label"]: i for g in on.status()["settings"] for i in g["items"]}
    assert not rows["trail_buffer_atr"].get("off") and "trailing stop" in rows["With no target"]["text"]


def test_close_now_only_when_ahead_and_checked_again(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no open trade"):
        bot.request_close()
    bot.enter(50.0, 49.0, "bounce")
    price = {"p": 49.80}
    monkeypatch.setattr(bot, "_last_trade_price", lambda: price["p"])
    t.price = 49.80
    bot.refresh_quote()
    with pytest.raises(ValueError, match="only when the trade is ahead"):   # behind: refused
        bot.request_close()
    assert not bot.status()["can_close"]
    price["p"] = t.price = 50.60
    bot.refresh_quote()
    assert bot.status()["can_close"]
    assert "Closing AAPL" in bot.request_close() and bot.pending_close
    price["p"] = t.price = 49.90                                            # fell back before the bot got to it
    bot.close_by_hand()
    assert bot.pos is not None and not bot.pending_close and bot.bot_shares() == 125   # left open, stop in place
    price["p"] = t.price = 50.70
    bot.refresh_quote()
    bot.request_close()
    bot.close_by_hand()
    assert bot.pos is None and bot.bot_shares() == 0
    assert bot.closed[-1]["reason"] == "closed by you" and bot.closed[-1]["pnl"] > 0
    assert "closed by you" in (tmp_path / "trades.csv").read_text(encoding="utf-8")
    assert not [o for o in t.all.values() if o.status in ("new", "held")]  # stop and target legs cancelled


def test_close_now_in_a_dry_run_and_on_the_page(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    bot.symbol = "AAA"
    bot.pos = dict(kind="bounce", side=1, shares=10, entry=20.0, stop=19.0, init_stop=19.0, target=None,
                   entry_x=3, entry_clock="10:00", stop_steps=[[3, 19.0]])
    bot.last_price = 20.5
    bot.request_close()
    bot.refresh_quote = lambda: None
    bot.close_by_hand()
    assert bot.pos is None and bot.closed[-1]["pnl"] == pytest.approx(5.0)
    d = Dashboard(8882)
    d.on_close = bot.request_close
    d.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{d.port}/close", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-Swingbot-Token": d.token})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400 and b"no open trade" in e.value.read()   # reached the bot, which said no
    finally:
        d.stop()


def test_move_the_stop_by_hand(tmp_path, monkeypatch):
    bot, t = _bot(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no open trade"):
        bot.request_stop(49.5)
    bot.enter(50.0, 49.0, "bounce")
    price = {"p": 50.40}
    monkeypatch.setattr(bot, "_last_trade_price", lambda: price["p"])
    t.price = 50.40
    bot.refresh_quote()
    lo, hi = bot.status()["stop_range"]
    assert lo == 49.0 and hi == pytest.approx(50.27, abs=0.01)          # initial stop .. 0.25% under the price
    for bad, why in ((48.5, "between 49.00"), (50.35, "between 49.00"), ("abc", "enter a price"), (49.0, "already at")):
        with pytest.raises(ValueError, match=why):
            bot.request_stop(bad)
    assert "Moving the stop to 50.10" in bot.request_stop(50.10)        # above the entry: locks in a little
    bot.move_stop_by_hand()
    stops = [o for o in t.all.values() if o.status in ("new", "held") and o.type == "stop"]
    assert bot.pos["stop"] == 50.10 and bot.pos["manual_stop"] and len(stops) == 1
    assert float(stops[0].stop_price) == 50.10
    bot.request_stop(49.40)                                             # looser again: allowed, back toward the start
    bot.move_stop_by_hand()
    assert bot.pos["stop"] == 49.40
    bot.request_stop(50.20)
    price["p"] = t.price = 50.15                                        # fell before the bot got to it: now too close
    bot.move_stop_by_hand()
    assert bot.pos["stop"] == 49.40 and bot.pending_stop is None
    assert bot.status()["stop_manual"]
    bot.prepared = True
    bot.save_state()
    assert bot.load_state()["pos"]["manual_stop"]                       # kept across a restart


def test_manual_stop_turns_off_automatic_moves(tmp_path):
    bot = live.Bot(None, Config(breakeven_at_r=1.0), live=False, workdir=tmp_path)
    bot.symbol = "AAA"
    bot.pos = dict(kind="short bounce", side=-1, shares=10, entry=20.0, stop=21.0, init_stop=21.0, target=None,
                   entry_x=3, entry_clock="10:00", stop_steps=[[3, 21.0]])
    bot.last_price = 19.0
    bot.refresh_quote = lambda: None
    lo, hi = bot.stop_range()
    assert lo == pytest.approx(19.05) and hi == 21.0                    # short: 0.25% above the price .. initial stop
    bot.request_stop(20.5)
    bot.move_stop_by_hand()
    assert bot.pos["stop"] == 20.5 and bot.pos["manual_stop"]
    src = open(live.__file__, encoding="utf-8").read()
    assert "not manual and self.symbol in frames" in src and "cfg.trail_stop and not manual" in src
