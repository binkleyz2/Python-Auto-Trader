"""End-of-day report: saved in logs, and mailed when email_report is on."""
import email
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import history as h, live, report
from swingbot.config import Config
from swingbot.data import synthetic

pytest.importorskip("matplotlib")
ET = ZoneInfo("America/New_York")


def _day(tmp_path, monkeypatch, direction="both"):
    """A whole dry-run day on synthetic bars that makes long and short trades."""
    import test_bar_switch as tb
    df1 = synthetic("AAA", days=4, minutes=1, seed=3)
    c = tb.AnySizeClient(df1)
    day = sorted(set(df1.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    c.now = clock["t"]
    cfg = Config(watchlist=["AAA"], direction=direction, max_trades_per_day=4, email_report=True,
                 email_to="me@example.com, you@example.com")
    bot = live.Bot(c, cfg, live=False, workdir=tmp_path)
    monkeypatch.setattr(bot, "short_check", lambda s, q: None)
    bot.forced_symbol = "AAA"
    bot.prepare()
    t = clock["t"]
    while t.time() < live._t("15:50"):
        clock["t"] = c.now = t
        bot.step()
        t += pd.Timedelta(minutes=5)
    clock["t"] = c.now = t
    bot.flatten("end of day")
    bot.final = True
    bot.save_state()
    return bot, cfg, str(day)


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.login_as = host, port, False, None

    def starttls(self):
        self.tls = True

    def login(self, u, p):
        self.login_as = (u, p)

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))

    def quit(self):
        pass


def test_report_is_saved_and_mailed_with_trades_and_charts(tmp_path, monkeypatch):
    bot, cfg, day = _day(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    (logs / f"{day}-dashboard.jpg").write_bytes(b"\xff\xd8jpg")
    monkeypatch.setenv("SMTP_USER", "bot@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app pass")
    FakeSMTP.sent = []
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    out = report.end_of_day(bot, cfg, logs)
    assert out == logs / f"{day}-report.html" and out.exists()
    assert (logs / f"{day}-chart-AAA.png").read_bytes()[:4] == b"\x89PNG"
    page = out.read_text(encoding="utf-8")
    assert f'src="{day}-chart-AAA.png"' in page and "short bounce" in page and "Initial stop" in page
    (srv, msg), = FakeSMTP.sent
    assert srv.host == "smtp.gmail.com" and srv.port == 587 and srv.tls and srv.login_as == ("bot@gmail.com", "app pass")
    assert msg["To"] == "me@example.com, you@example.com" and msg["From"] == "bot@gmail.com"
    n = len(bot.closed)
    assert msg["Subject"].startswith(f"swingbot {day}: ") and f"{n} trades" in msg["Subject"]
    parsed = email.message_from_bytes(msg.as_bytes())
    kinds = [(p.get_content_type(), p.get("Content-ID"), p.get_filename()) for p in parsed.walk()]
    assert ("image/png", "<AAA>", f"{day}-chart-AAA.png") in kinds
    assert any(f == f"{day}-dashboard.jpg" for _, _, f in kinds)
    html = next(p for p in parsed.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert 'src="cid:AAA"' in html
    for t in bot.closed:
        assert t["entry_time"] in html and f"{t['exit']:.2f}" in html


def test_no_mail_when_off_and_nothing_before_the_open(tmp_path, monkeypatch):
    bot, cfg, day = _day(tmp_path, monkeypatch)
    cfg.email_report = False
    FakeSMTP.sent = []
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    assert report.end_of_day(bot, cfg, tmp_path / "logs").exists()        # still saved
    assert not FakeSMTP.sent
    early = SimpleNamespace(prepared=True, last_bar_seen=None, trades_today=0)
    assert report.end_of_day(early, cfg, tmp_path / "logs") is None       # weekend / never opened


def test_mail_problems_are_a_warning_not_a_crash(tmp_path, monkeypatch, caplog):
    bot, cfg, day = _day(tmp_path, monkeypatch)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    assert report.end_of_day(bot, cfg, tmp_path / "logs") is None
    assert "SMTP_USER" in caplog.text and (tmp_path / "logs" / f"{day}-report.html").exists()


def test_ssl_port_and_settings_check(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="email_to"):
        Config(email_report=True).validate()
    used = []

    class FakeSSL(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout)
            used.append(port)
    monkeypatch.setattr(report.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    cfg = Config(email_report=True, email_to="a@b.c", smtp_host="smtp.example.com", smtp_port=465)
    report.send(cfg, dict(subject="s", html="<p>x</p>", text="x", charts={}))
    assert used == [465]


def test_report_by_hand_for_an_earlier_day(tmp_path, monkeypatch, capsys):
    bot, cfg, day = _day(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("watchlist: [AAA]\n", encoding="utf-8")
    monkeypatch.setattr(report, "_data_bot", lambda cfg, bm: SimpleNamespace(
        fetch_bars=lambda s, p: bot.fetch_bars(s, "DAY"), client=SimpleNamespace(close=lambda: None)))
    report.main([day])
    assert f"{day}-report.html" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        report.main(["2000-01-01"])


def test_closed_trades_keep_what_the_chart_needs(tmp_path, monkeypatch):
    bot, cfg, day = _day(tmp_path, monkeypatch)
    st = json.loads((tmp_path / "logs" / f"{day}-state.json").read_text(encoding="utf-8"))
    for t in st["closed"]:
        assert t["init_stop"] and t["stop_steps"] and t["line"]["pts"]
        assert {"side", "final_stop", "target"} <= set(t)



def test_report_is_emailed_once_a_day(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    bot, cfg, day = _day(tmp_path, monkeypatch)
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    FakeSMTP.sent = []
    monkeypatch.setattr(report.smtplib, "SMTP", FakeSMTP)
    report.end_of_day(bot, cfg, tmp_path / "logs")
    report.end_of_day(bot, cfg, tmp_path / "logs")              # started again after the close
    assert len(FakeSMTP.sent) == 1 and "already emailed today" in caplog.text
