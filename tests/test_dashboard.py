"""The live page: served on this computer only, read-only, and never able to break trading."""
import json
import urllib.request

from swingbot import live
from swingbot.config import Config
from swingbot.dashboard import Dashboard


def test_serves_page_and_status_on_localhost_only(tmp_path):
    d = Dashboard(8811)
    url = d.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        assert d._server.server_address[0] == "127.0.0.1"
        d.update({"symbol": "NVDA", "pos": None})
        page = urllib.request.urlopen(url).read().decode()
        status = json.loads(urllib.request.urlopen(url + "status.json").read())
        assert "<svg" in page and status["symbol"] == "NVDA"
        snap = d.snapshot(tmp_path / "snap.html").read_text(encoding="utf-8")
        assert "window.__STATUS__" in snap and "NVDA" in snap
    finally:
        d.stop()


def test_dashboard_failure_never_breaks_the_bot(tmp_path):
    class Broken:
        def update(self, status):
            raise RuntimeError("boom")

    bot = live.Bot(client=None, cfg=Config(), live=False, workdir=tmp_path)
    bot.dashboard = Broken()
    bot.publish("anything")                       # must not raise
    assert bot.status()["mode"] == "DRY RUN"
    assert bot.status()["point_value"] == 1.0     # the chart's hover works out P&L and R with it


def test_network_sharing_requires_the_key(tmp_path):
    import urllib.error
    d = Dashboard(8821, lan=True, key="s3cret-key")
    url = d.start()
    try:
        assert d._server.server_address[0] == "0.0.0.0"
        assert url.endswith("?key=s3cret-key") and "s3cret-key" in d.lan_url()
        d.update({"symbol": "NVDA"})
        base = f"http://127.0.0.1:{d.port}/"
        for bad in (base, base + "status.json", base + "status.json?key=wrong"):
            try:
                urllib.request.urlopen(bad)
                raise AssertionError("served without the right key: " + bad)
            except urllib.error.HTTPError as e:
                assert e.code == 403
        assert json.loads(urllib.request.urlopen(base + "status.json?key=s3cret-key").read())["symbol"] == "NVDA"
    finally:
        d.stop()


def test_network_sharing_always_has_a_key():
    d = Dashboard(8831, lan=True)
    assert len(d.key) >= 12
    assert Dashboard(8832).key == ""                        # this-PC-only mode needs no key
