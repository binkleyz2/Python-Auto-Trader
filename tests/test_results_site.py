"""The results website: https with a PEM holding the certificate and key, the access key, what it serves."""
import http.client
import json
import shutil
import ssl
import subprocess

import pytest

from swingbot import results_site as rs
from swingbot.config import Config

DAYS = """date,account,start_value,end_value,bot_pnl,trades,wins,losses,note
2026-11-03,alpaca-paper,97746.03,98315.66,569.64,6,3,3,
2026-11-04,alpaca-paper,98312.55,99059.23,746.68,1,1,0,
"""
TRADES = """time,mode,symbol,kind,event,shares,entry,initial_stop,stop,target,exit,pnl
2026-11-04T10:20:26-05:00,live,PLTR,short bounce,entry,139,175.87,179.20,179.20,168.33,,0.0
2026-11-04T15:50:48-05:00,live,PLTR,short bounce,end of day,139,175.87,179.20,175.88,168.33,170.50,746.68
"""


def _pem(folder, name="both.txt"):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl to make a test certificate")
    k, c = folder / "k.pem", folder / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(k), "-out", str(c),
                    "-days", "2", "-subj", "/CN=*.example.test", "-addext", "subjectAltName=DNS:*.example.test"],
                   check=True, capture_output=True)
    both = folder / name
    both.write_text(k.read_text(encoding="ascii") + c.read_text(encoding="ascii"), encoding="ascii")        # private key and certificate in one file
    k.unlink(), c.unlink()
    return both


def _workdir(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "daily-results.csv").write_text(DAYS, encoding="utf-8")
    (tmp_path / "trades.csv").write_text(TRADES, encoding="utf-8")
    (tmp_path / "logs" / "2026-11-04-report.html").write_text("<p>report</p>", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("secret: yes", encoding="utf-8")
    (tmp_path / "backtests" / "Results").mkdir(parents=True)
    (tmp_path / "backtests" / "Results" / "2026-11-03_205443-dow-180d-swing_n=4-6.zip").write_bytes(b"PK")
    return tmp_path


def _get(site, path, cookie=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    c = http.client.HTTPSConnection("127.0.0.1", site.port, context=ctx, timeout=10)
    c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_collect_builds_the_page_data(tmp_path):
    d = rs.collect(_workdir(tmp_path), Config())
    a = d["accounts"]["alpaca-paper"]
    assert d["account"] == "alpaca-paper" and len(a["days"]) == 2
    assert a["summary"]["bot_pnl"] == 1316.32 and a["summary"]["latest"] == 99059.23 and a["summary"]["trades"] == 7
    assert a["days"][1]["files"] == {"report": "files/2026-11-04-report.html"}
    assert d["trade_count"] == 1 and d["trades"][0]["symbol"] == "PLTR" and d["trades"][0]["r"] > 1.5
    assert d["backtests"][0]["name"].endswith("swing_n=4-6.zip")
    assert {f["name"] for f in d["downloads"]} == {"daily-results.csv", "trades.csv"}


def test_https_key_cookie_and_only_its_own_files(tmp_path):
    wd = _workdir(tmp_path)
    cfg = Config(site_cert=str(_pem(tmp_path)), site_key="s3cret", site_port=0)
    site = rs.Site(cfg, wd)
    assert site.lan
    site.start()
    try:
        assert _get(site, "/")[0] == 403                                   # no key
        assert _get(site, "/?key=wrong")[0] == 403
        code, h, _ = _get(site, "/?key=s3cret")
        assert code == 303 and h["Location"] == "/"                        # key taken out of the address
        cookie = h["Set-Cookie"].split(";")[0]
        assert "Secure" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"]
        code, h, body = _get(site, "/", cookie)
        assert code == 200 and b"swingbot results" in body and "Content-Security-Policy" in h
        code, _, body = _get(site, "/data.json", cookie)
        assert code == 200 and json.loads(body)["accounts"]["alpaca-paper"]["days"][-1]["date"] == "2026-11-04"
        assert _get(site, "/files/2026-11-04-report.html", cookie)[0] == 200
        assert _get(site, "/backtests/2026-11-03_205443-dow-180d-swing_n%3D4-6.zip", cookie)[0] == 200
        for bad in ("/files/../config.yaml", "/files/..%2Fconfig.yaml", "/files/config.yaml", "/files/.env",
                    "/backtests/..%2F..%2Fconfig.yaml", "/config.yaml"):
            assert _get(site, bad, cookie)[0] == 404, bad
        # the page data follows the files: a new day shows up without a restart
        with (wd / "logs" / "daily-results.csv").open("a", encoding="utf-8") as f:
            f.write("2026-11-05,alpaca-paper,99059.23,99100.00,40.77,2,1,1,\n")
        days = json.loads(_get(site, "/data.json", cookie)[2])["accounts"]["alpaca-paper"]["days"]
        assert days[-1]["date"] == "2026-11-05"
    finally:
        site.stop()


def test_too_many_wrong_keys_locks_an_address_out(tmp_path):
    site = rs.Site(Config(site_cert=str(_pem(tmp_path)), site_key="right", site_port=0), _workdir(tmp_path))
    for _ in range(rs.MAX_FAILS):
        assert site.check("10.0.0.9", {"key": ["nope"]}, None) == (False, False)
    assert site.check("10.0.0.9", {"key": ["right"]}, None) == (False, False)    # locked, even with the key
    assert site.check("10.0.0.8", {"key": ["right"]}, None) == (True, True)      # others aren't


def test_without_a_certificate_it_is_http_on_this_pc_only(tmp_path):
    site = rs.Site(Config(site_cert="", site_key="", dashboard_key="", site_port=0), _workdir(tmp_path))
    assert not site.lan and site.check("127.0.0.1", {}, None) == (True, False)
    site.start()
    try:
        assert site.server.server_address[0] == "127.0.0.1" and site.url().startswith("http://127.0.0.1:")
        c = http.client.HTTPConnection("127.0.0.1", site.port, timeout=5)
        c.request("GET", "/data.json")
        assert c.getresponse().status == 200
        c = http.client.HTTPConnection("127.0.0.1", site.port, timeout=5)
        c.request("GET", "/", headers={"Host": "evil.example:80"})         # DNS rebinding
        assert c.getresponse().status == 403
    finally:
        site.stop()


def test_network_access_needs_a_key(tmp_path):
    with pytest.raises(SystemExit, match="no key"):
        rs.Site(Config(site_cert=str(_pem(tmp_path)), site_key="", dashboard_key=""), _workdir(tmp_path))
    assert rs.Site(Config(site_cert=str(tmp_path / "both.txt"), dashboard_key="dk"), tmp_path).key == "dk"


def test_bad_certificate_is_reported_and_a_renewed_one_is_picked_up(tmp_path, caplog):
    bad = tmp_path / "bad.txt"
    bad.write_text("-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n", encoding="ascii")
    site = rs.Site(Config(site_cert=str(bad), site_key="k", site_port=0), _workdir(tmp_path))
    assert not site.refresh_cert() and "private key" in caplog.text
    with pytest.raises(RuntimeError):
        site.start()
    good = _pem(tmp_path, "bad.txt")                                       # the file is replaced (renewal)
    assert good == bad and site.refresh_cert()
    site.start()
    try:
        assert _get(site, "/")[0] == 403                                   # serving https
        first = site.server.context
        _pem(tmp_path, "bad.txt")
        import os
        os.utime(bad, ns=(1, 1))                                           # a different file stamp
        assert site.refresh_cert() and site.server.context is not first
        assert _get(site, "/")[0] == 403
    finally:
        site.stop()


def test_cert_name_and_windows_launcher(tmp_path):
    assert rs._cert_host(_pem(tmp_path)) == "swingbot.example.test"
    v = rs.launcher_text(tmp_path, r"C:\bot\config.yaml", r"C:\Python\pythonw.exe")
    assert 'sh.CurrentDirectory = "' in v and "-m swingbot site" in v and ', 0, False' in v   # 0 = no window
    assert '""C:\\Python\\pythonw.exe""' in v


def test_site_on_port_recognises_a_running_site(tmp_path):
    import socket
    wd = _workdir(tmp_path)
    for cert in (str(_pem(tmp_path)), ""):                     # https and plain http
        site = rs.Site(Config(site_cert=cert, site_key="k", dashboard_key="", site_port=0), wd)
        site.start()
        try:
            assert rs._site_on_port(site.port)
        finally:
            site.stop()
    other = socket.socket()                                    # something else on a port: not a site
    other.bind(("127.0.0.1", 0)); other.listen(1)
    try:
        assert not rs._site_on_port(other.getsockname()[1])
    finally:
        other.close()


def test_a_second_copy_stops_instead_of_retrying(tmp_path, monkeypatch, caplog):
    first = rs.Site(Config(site_cert="", site_key="", dashboard_key="", site_port=0), _workdir(tmp_path))
    first.start()
    try:
        cfg = Config(site_cert="", site_key="", dashboard_key="", site_port=first.port, alert_email=False)
        monkeypatch.setattr(rs.Watchdog, "loop", lambda self, stop: None)
        monkeypatch.setattr(rs.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("retried")))
        rs.serve(cfg, tmp_path)                                # returns at once
        assert "Another copy of the results site is already running" in caplog.text
    finally:
        first.stop()


def test_windows_finds_every_copy_of_the_site(monkeypatch):
    out = ("11\tC:\\Python\\pythonw.exe -m swingbot site --config \"C:\\b\\config.yaml\"\n"
           "12\tC:\\Python\\python.exe -m swingbot site --install\n"
           "13\tC:\\Python\\python.exe -m swingbot run --live\n"
           "14\t\"C:\\Python\\python.exe\" -m  swingbot site\n"
           "15\tC:\\Python\\python.exe -m swingbot site --stop\n")
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert rs._windows_site_pids() == [11, 14]
    net = ("  Proto  Local Address          Foreign Address        State           PID\n"
           "  TCP    0.0.0.0:4334           0.0.0.0:0              LISTENING       5280\n"
           "  TCP    0.0.0.0:43340          0.0.0.0:0              LISTENING       77\n"
           "  TCP    192.168.0.61:4334      192.168.0.9:5123       ESTABLISHED     5280\n")
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, net, ""))
    assert rs._windows_port_owner(4334) == 5280 and rs._windows_port_owner(8765) is None


def test_stop_running_reports_what_it_could_not_stop(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "site.pid").write_text("21", encoding="utf-8")
    monkeypatch.setattr(rs.os, "name", "nt")
    monkeypatch.setattr(rs, "_windows_site_pids", lambda: [21, 22])
    monkeypatch.setattr(rs, "_windows_port_owner", lambda port: 22)
    monkeypatch.setattr(rs, "_windows_kill", lambda n: None if n == 21 else f"process {n}: Access is denied.")
    stopped, problems = rs.stop_running(tmp_path, 4334)
    assert stopped == [21] and problems == ["process 22: Access is denied."]
    assert (tmp_path / "logs" / "site.pid").exists()           # kept while a copy is still running
    monkeypatch.setattr(rs, "_windows_kill", lambda n: None)
    monkeypatch.setattr(rs, "_windows_port_owner", lambda port: None)
    assert rs.stop_running(tmp_path, 4334) == ([21, 22], []) and not (tmp_path / "logs" / "site.pid").exists()
