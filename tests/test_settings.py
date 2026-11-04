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
