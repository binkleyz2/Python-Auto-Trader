"""Alerts, the watchdog, update check, pause, heartbeat, the site's status and log, and switch checks."""
import json
import logging
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import live, notify, updates
from swingbot import results_site as rs
from swingbot.config import Config
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")


class Sent:
    def __init__(self):
        self.push, self.mail = [], []


@pytest.fixture
def sent(monkeypatch):
    s = Sent()

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"ok"

    def urlopen(req, timeout=0):
        s.push.append((req.full_url, dict(req.header_items()), req.data.decode()))
        return Resp()

    class SMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, m): s.mail.append(m)
        def quit(self): pass

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(notify.smtplib, "SMTP", SMTP)
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    return s


def test_alert_goes_to_phone_and_email_once(sent):
    n = notify.Notifier(Config(ntfy_topic="swingbot-test-x9", alert_email=True, email_to="me@example.com"))
    assert n.alert("stop not moved", "Could not move the stop (keeping 175.88)", wait=True)
    url, headers, body = sent.push[0]
    assert url == "https://ntfy.sh/swingbot-test-x9" and "keeping 175.88" in body
    assert headers["Priority"] == "high" and headers["Title"].startswith("swingbot:")
    assert sent.mail[0]["To"] == "me@example.com" and "stop not moved" in sent.mail[0]["Subject"]
    assert not n.alert("stop not moved", "Could not move the stop (keeping 175.91)", wait=True)  # a repeat
    assert len(sent.push) == 1


def test_nothing_set_up_sends_nothing(sent):
    n = notify.Notifier(Config())
    assert not n.any_on and not n.alert("x", "y", wait=True) and not sent.push and not sent.mail
    assert notify.install(Config()) is None


def test_log_lines_become_alerts_and_trade_pushes(sent, monkeypatch):
    cfg = Config(ntfy_topic="t-abc", notify_trades=True)
    n = notify.Notifier(cfg)
    monkeypatch.setattr(n, "alert", lambda *a, **k: sent.push.append((a, k)) or True)
    h = notify.AlertHandler(n, trades=True)
    lg = logging.getLogger("swingbot.test_alerts")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.info("just information")
        lg.warning("liquidity check failed")                       # an ordinary warning: no alert
        assert not sent.push
        lg.error("sell not confirmed: timeout - CHECK THE ACCOUNT")
        lg.warning("Entry order failed: rejected", extra={"notify": "alert"})
        lg.info("ENTER bounce AAPL x125 ~50.00 stop 49.00", extra={"notify": "trade"})
        assert len(sent.push) == 3
        assert sent.push[0][1]["urgent"] and sent.push[2][1]["urgent"] is False and sent.push[2][1]["email"] is False
        h.trades = False
        lg.info("EXIT AAPL @ 51.00 (target) P&L 125.00", extra={"notify": "trade"})
        assert len(sent.push) == 3                                  # trade pushes off
    finally:
        lg.removeHandler(h)


def test_update_check(monkeypatch, tmp_path):
    assert updates.newer("v1.22.0", "1.21.1") and not updates.newer("v1.21.1", "1.21.1")
    assert updates.newer("v1.10.0", "1.9.9") and not updates.newer("v1.9.9", "1.10.0")
    monkeypatch.setattr(updates, "latest_release", lambda repo, cache=None: dict(tag="v99.0.0", url="u", published="x"))
    up = updates.check(Config(), tmp_path)
    assert up["tag"] == "v99.0.0" and up["current"]
    monkeypatch.setattr(updates, "latest_release", lambda repo, cache=None: dict(tag="v0.0.1", url="u", published="x"))
    assert updates.check(Config(), tmp_path) is None
    assert updates.check(Config(update_check=False), tmp_path) is None


class FakeClient:
    def __init__(self, df):
        self.df, self.now = df, None

    def get_bars(self, symbol, period, minutes=5):
        return self.df[self.df.index <= pd.Timestamp(self.now)]


def _session(tmp_path, monkeypatch, paused):
    cfg = Config(watchlist=["AAA"])
    for seed in range(12):
        df = synthetic("AAA", days=8, seed=seed)
        day = sorted(set(df.index.date))[-1]
        wd = tmp_path / f"{paused}{seed}"
        wd.mkdir()
        bot = live.Bot(None, cfg, live=False, workdir=wd)
        bot.fetch_bars = lambda sym, period, _df=df: _df[_df.index <= pd.Timestamp(clock["t"])]
        clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)}
        monkeypatch.setattr(live, "now_et", lambda: clock["t"])
        try:
            bot.morning_scan()
        except Exception:
            continue
        if not bot.symbol:
            continue
        bot.paused = paused
        t = clock["t"]
        while t.time() < live._t(cfg.flatten_time):
            clock["t"] = t
            bot.step()
            t += pd.Timedelta(minutes=5)
        yield seed, bot


def test_pause_stops_new_entries(tmp_path, monkeypatch):
    traded = None
    for seed, bot in _session(tmp_path, monkeypatch, False):
        if bot.trades_today:
            traded = seed
            break
    if traded is None:
        pytest.skip("no synthetic day with a trade")
    for seed, bot in _session(tmp_path, monkeypatch, True):
        if seed == traded:
            assert bot.trades_today == 0 and bot._auto_state().startswith("Paused")
            break


def test_pause_request_heartbeat_and_restart(tmp_path, monkeypatch):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    bot.prepared = True
    (tmp_path / "logs").mkdir()
    assert "Paused" in bot.request_pause(True) and bot.status()["paused"]
    bot.publish("testing")
    hb = json.loads((tmp_path / "logs" / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["paused"] and hb["state"] == "testing" and not hb["final"] and hb["version"]
    assert live.Bot(None, Config(), live=False, workdir=tmp_path).load_state()["paused"]   # kept across a restart
    assert "Resumed" in bot.request_pause(False) and not bot.paused


def _hb(tmp_path, when, final=False, day=None):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "heartbeat.json").write_text(json.dumps(dict(
        time=when.isoformat(), date=str(day or when.date()), final=final, state="Watching", version="1.22.0",
        live=True)), encoding="utf-8")


def test_bot_status_and_watchdog(tmp_path, sent):
    thu = datetime(2026, 11, 5, 10, 0, tzinfo=ET)
    assert rs.bot_status(tmp_path, thu)["state"] == "not started"
    assert rs.bot_status(tmp_path, datetime(2026, 11, 5, 9, 10, tzinfo=ET))["state"] == "waiting"
    assert rs.bot_status(tmp_path, datetime(2026, 11, 26, 10, 0, tzinfo=ET))["state"] == "no session"   # Thanksgiving
    assert rs.bot_status(tmp_path, datetime(2026, 11, 7, 10, 0, tzinfo=ET))["state"] == "no session"    # Saturday
    w = rs.Watchdog(Config(ntfy_topic="t-x"), tmp_path)
    assert "hasn't started" in w.check(thu)
    assert w.check(thu.replace(minute=5)) is None                          # once a day
    _hb(tmp_path, thu.replace(hour=11, minute=0))
    assert rs.bot_status(tmp_path, thu.replace(hour=11, minute=5))["state"] == "running"
    later = thu.replace(hour=11, minute=30)
    assert rs.bot_status(tmp_path, later)["state"] == "stopped"
    assert "stopped responding" in w.check(later) and w.check(later.replace(minute=40)) is None
    _hb(tmp_path, thu.replace(hour=15, minute=51), final=True)
    assert rs.bot_status(tmp_path, thu.replace(hour=17))["state"] == "finished"


def test_market_holidays():
    h = rs.market_holidays(2026)
    assert date(2026, 4, 3) in h and date(2026, 7, 3) in h and date(2026, 11, 26) in h and date(2026, 6, 19) in h
    assert rs.market_day(date(2026, 11, 5)) and not rs.market_day(date(2026, 12, 25))
    assert date(2027, 12, 31) not in rs.market_holidays(2027) and date(2028, 1, 1) not in rs.market_holidays(2028)


def test_site_log_hides_keys_and_finds_onedrive_problems(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "2026-11-05.log").write_text(
        "08:00 INFO    Dashboard: http://192.168.1.5:8765/?key=Plugh!\n09:40 INFO    ENTER bounce AAPL\n",
        encoding="utf-8")
    text = rs.log_text(tmp_path, "2026-11-05")
    assert "Plugh" not in text and "key=…" in text and "ENTER bounce AAPL" in text
    assert rs.log_text(tmp_path, "../config") == ""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "AAPL.csv").write_text("x", encoding="utf-8")
    assert rs.onedrive_problems(tmp_path) == []
    (tmp_path / "data" / "PRN.csv").write_text("x", encoding="utf-8")
    assert rs.onedrive_problems(tmp_path) == [str(tmp_path.joinpath("data", "PRN.csv").relative_to(tmp_path))]


def test_unused_switches(tmp_path, monkeypatch, capsys):
    from swingbot.__main__ import _refuse_unused, main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["site", "--live"])
    assert "site doesn't use --live (--live is for run / daemon)" in str(e.value)
    with pytest.raises(SystemExit) as e:
        main(["scan", "--universe", "dow"])
    assert "Nothing was done" in str(e.value)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command")
    ap.add_argument("--days", type=int)
    ap.add_argument("--symbol")
    _refuse_unused(ap, SimpleNamespace(command="run", days=5, symbol=None), ["run", "--days", "5"])  # warns only
    assert "ignoring it" in capsys.readouterr().err


def test_daemon_picks_the_next_session():
    from swingbot.daemon import ET as DET, next_start
    cfg = Config()
    at = lambda *a: datetime(*a, tzinfo=DET)
    assert next_start(at(2026, 11, 9, 6, 0), cfg, False) == at(2026, 11, 9, 8, 0)        # Monday morning: today 8:00
    assert next_start(at(2026, 11, 9, 11, 0), cfg, False) is None                        # started mid-session: now
    assert next_start(at(2026, 11, 9, 11, 0), cfg, True) == at(2026, 11, 10, 8, 0)       # today done: tomorrow
    assert next_start(at(2026, 11, 9, 17, 0), cfg, False) == at(2026, 11, 10, 8, 0)      # after the close
    assert next_start(at(2026, 11, 13, 17, 0), cfg, False) == at(2026, 11, 16, 8, 0)     # Friday evening: Monday
    assert next_start(at(2026, 11, 25, 17, 0), cfg, False) == at(2026, 11, 27, 8, 0)     # skips Thanksgiving
    assert next_start(at(2026, 11, 26, 9, 0), cfg, False) == at(2026, 11, 27, 8, 0)      # on the holiday itself


def test_daemon_live_needs_yes(tmp_path, monkeypatch):
    from swingbot.__main__ import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["daemon", "--live"])
    assert "--yes" in str(e.value)
