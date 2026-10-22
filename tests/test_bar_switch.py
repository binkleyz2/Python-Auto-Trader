"""Changing the bar size during a session from the dashboard dropdown."""
import json
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingbot import live
from swingbot.config import Config
from swingbot.dashboard import Dashboard
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")
AGG = {"ONE_MINUTE": 1, "FIVE_MINUTES": 5, "TEN_MINUTES": 10, "FIFTEEN_MINUTES": 15, "THIRTY_MINUTES": 30}


class AnySizeClient:
    """Public-style fake: holds 1-minute bars and serves whatever size is asked for, up to 'now'."""

    def __init__(self, df1, fail_sizes=()):
        self.df1, self.now, self.sizes, self.fail_sizes = df1, None, [], set(fail_sizes)

    def get_bars(self, symbol, period, aggregation=None, **kw):
        m = AGG[aggregation.name]
        self.sizes.append(m)
        if m in self.fail_sizes:
            raise RuntimeError("bars unavailable")
        d = self.df1[self.df1.index < pd.Timestamp(self.now)]
        if period.name == "DAY":
            d = d[d.index.date == self.now.date()]
        if m > 1:   # bars counted from 9:30 each day (origin 'start_day' + 30 min offset)
            d = (d.groupby(d.index.date, group_keys=False)
                  .apply(lambda g: g.resample(f"{m}min", origin="start_day", offset="30min")
                         .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                         .dropna()))
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def place_order(self, *a, **k):
        raise AssertionError("dry run must not place orders")


def _bot(tmp_path, monkeypatch, start=(9, 36), fail_sizes=()):
    df1 = synthetic("AAA", days=4, minutes=1, seed=3)
    c = AnySizeClient(df1, fail_sizes)
    day = sorted(set(df1.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, *start, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    c.now = clock["t"]
    bot = live.Bot(c, Config(watchlist=["AAA"], bar_minutes=5), live=False, workdir=tmp_path)
    bot.forced_symbol = "AAA"
    bot.morning_scan()
    return bot, c, clock


def test_request_is_checked_and_only_queued(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    with pytest.raises(ValueError):
        bot.request_bar_size(2)                      # Public has no 2-minute bars
    with pytest.raises(ValueError):
        bot.request_bar_size("abc")
    assert bot.request_bar_size(5).startswith("Already")
    assert "10-minute" in bot.request_bar_size(10)
    assert bot.bm == 5 and bot.pending_bm == 10      # nothing changes until the main loop applies it
    bot.request_bar_size(5)                          # changing your mind back cancels the request
    assert bot.pending_bm is None
    bot.final = True
    with pytest.raises(ValueError):
        bot.request_bar_size(10)


def test_switch_mid_trade_keeps_the_stop_and_keeps_trailing(tmp_path, monkeypatch):
    bot, c, clock = _bot(tmp_path, monkeypatch)
    t = clock["t"]
    while bot.pos is None and t.time() < live._t("15:00"):
        clock["t"] = c.now = t
        bot.step()
        t += pd.Timedelta(minutes=5)
    assert bot.pos, "test data should give a trade on 5-minute bars"
    stop_before, entry_x = bot.pos["stop"], bot.pos["entry_x"]
    steps_before = len(bot.pos["stop_steps"])

    bot.request_bar_size(10)
    assert bot.apply_pending_bar_size()
    assert bot.bm == 10 and c.sizes[-1] == 10
    assert bot.pos["stop"] == stop_before                       # stop untouched by the switch
    assert bot.pos["entry_x"] == pytest.approx(entry_x * 0.5)   # chart positions rescaled to 10-minute bars
    assert bot.history.index.max().date() < clock["t"].date()

    bot.step()                                                  # catch-up redraw only
    assert bot.pos and bot.pos["stop"] == stop_before and len(bot.pos["stop_steps"]) == steps_before
    st = bot.status()
    assert st["bar_minutes"] == 10 and st["pending_bars"] is None and 10 in st["bar_sizes"]
    assert max(b["x"] for b in st["bars"]) < 390 and len(st["bars"]) > 6

    stops = [bot.pos["stop"]]
    while bot.pos and t.time() < live._t("15:50"):
        t += pd.Timedelta(minutes=1)
        clock["t"] = c.now = t
        bot.step()
        if bot.pos:
            stops.append(bot.pos["stop"])
    assert stops == sorted(stops)                               # the stop only ever moved up
    assert len(set(stops)) > 1 or bot.pos is None               # it kept trailing (or the trade closed)
    bot.flatten("end of day")
    assert bot.pos is None
    for tr in bot.closed:
        assert 0 <= tr["entry_x"] <= tr["exit_x"] < 390


def test_failed_load_stays_on_the_old_size(tmp_path, monkeypatch):
    bot, c, clock = _bot(tmp_path, monkeypatch, start=(11, 0), fail_sizes={15})
    hist = bot.history
    bot.request_bar_size(15)
    assert not bot.apply_pending_bar_size()
    assert bot.bm == 5 and bot.history is hist and bot.pending_bm is None
    bot.step()                                                  # carries on normally on 5-minute bars
    assert bot.status()["bar_minutes"] == 5


def test_change_before_the_scan_just_applies(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    bot.request_bar_size(15)
    assert bot.apply_pending_bar_size() and bot.bm == 15


# --------------------------------------------------------------------- the page's control

def _post(base, body, token=None, host=None):
    req = urllib.request.Request(base + "bar-size", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Swingbot-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read() or b"{}") if e.headers.get("Content-Type") == "application/json" else {})


def test_dropdown_endpoint_needs_the_page_token(tmp_path):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    d = Dashboard(8841)
    d.on_bar_size = bot.request_bar_size
    url = d.start()
    try:
        page = urllib.request.urlopen(url).read().decode()
        assert "window.__CONTROL__ =" in page and d.token in page
        base = f"http://127.0.0.1:{d.port}/"
        assert _post(base, {"minutes": 10})[0] == 403                           # no token (e.g. another website)
        assert _post(base, {"minutes": 10}, token="guess")[0] == 403
        assert _post(base, {"minutes": 10}, token=d.token, host="evil.example:%d" % d.port)[0] == 403
        assert bot.pending_bm is None
        code, res = _post(base, {"minutes": 2}, token=d.token)                 # Public: no 2-minute bars
        assert code == 400 and "Public offers" in res["message"]
        code, res = _post(base, {"minutes": 10}, token=d.token)
        assert code == 200 and "10-minute" in res["message"] and bot.pending_bm == 10
        assert bot.bm == 5                                                     # applied later by the main loop
    finally:
        d.stop()


def test_no_dropdown_when_turned_off_or_in_snapshots(tmp_path):
    d = Dashboard(8851)                                  # on_bar_size not set = dashboard_bar_control: false
    url = d.start()
    try:
        assert "window.__CONTROL__ =" not in urllib.request.urlopen(url).read().decode()
        assert _post(f"http://127.0.0.1:{d.port}/", {"minutes": 10}, token=d.token)[0] == 403
        d.update({"symbol": "NVDA"})
        assert "window.__CONTROL__ =" not in d.snapshot(tmp_path / "s.html").read_text(encoding="utf-8")
    finally:
        d.stop()


def test_lan_viewers_get_no_control_unless_allowed(tmp_path, monkeypatch):
    bot = live.Bot(None, Config(), live=False, workdir=tmp_path)
    d = Dashboard(8861, lan=True, key="k3y-k3y-k3y")
    d.on_bar_size = bot.request_bar_size
    url = d.start()
    try:
        # pretend the request comes from a phone on the network
        import http.server
        orig = http.server.BaseHTTPRequestHandler.__init__

        def from_phone(self, request, client_address, server):
            orig(self, request, ("192.168.1.50", client_address[1]), server)
        monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "__init__", from_phone)
        assert "window.__CONTROL__ =" not in urllib.request.urlopen(url).read().decode()
        base = f"http://127.0.0.1:{d.port}/"
        assert _post(base, {"minutes": 10}, token=d.token)[0] == 403            # no key
        assert _post_key(base, "k3y-k3y-k3y", d.token)[0] == 403 and bot.pending_bm is None
        d.lan_control = True                                                   # dashboard_lan_control: true
        assert "window.__CONTROL__ =" in urllib.request.urlopen(url).read().decode()
        assert _post_key(base, "k3y-k3y-k3y", d.token)[0] == 200 and bot.pending_bm == 10
    finally:
        d.stop()


def _post_key(base, key, token):
    req = urllib.request.Request(base + "bar-size?key=" + key, data=b'{"minutes": 10}', method="POST",
                                 headers={"Content-Type": "application/json", "X-Swingbot-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_scan_runs_at_startup_before_the_open(tmp_path, monkeypatch):
    """Started at 9:00, the pick is made right away and the chart shows earlier days."""
    df1 = synthetic("AAA", days=4, minutes=1, seed=3)
    c = AnySizeClient(df1)
    day = sorted(set(df1.index.date))[-1]
    clock = {"t": datetime(day.year, day.month, day.day, 9, 0, tzinfo=ET)}
    monkeypatch.setattr(live, "now_et", lambda: clock["t"])
    c.now = clock["t"]
    bot = live.Bot(c, Config(watchlist=["AAA"]), live=False, workdir=tmp_path)
    bot.forced_symbol = "AAA"
    bot.prepare()
    assert bot.symbol == "AAA" and bot.prepared and clock["t"].hour == 9 and clock["t"].minute == 0
    st = bot.status()
    assert st["days"][-1]["back"] == 0 and len(st["bars"]) > 100           # earlier days drawn
    assert all(b["x"] < 0 for b in st["bars"])                               # nothing for today yet
    waits = []

    def fake_sleep(s):
        waits.append(s)
        clock["t"] += pd.Timedelta(seconds=s)
    monkeypatch.setattr(live._time, "sleep", fake_sleep)
    assert live.wait_for_open(bot)
    assert clock["t"].strftime("%H:%M") == "09:35" and "Picked AAA" in bot.state
    calls = len(c.sizes)
    clock["t"] = c.now = clock["t"] + pd.Timedelta(minutes=1)
    bot.step()                                                               # trading starts, no second scan
    assert len(c.sizes) == calls + 1 and len(bot.status()["bars"]) > 100
