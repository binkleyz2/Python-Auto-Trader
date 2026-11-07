"""Results website: every trading day's results as they build up, always on, separate from the dashboard.

    py -m swingbot site              run it (Ctrl+C stops it)
    py -m swingbot site --install    start it now and every time you log on to Windows (hidden, no window)
    py -m swingbot site --stop       stop the copy running in the background
    py -m swingbot site --uninstall  stop it and don't start it at logon any more

It reads the files the bot already writes - logs/daily-results.csv, trades.csv, the day reports and
dashboard pictures in logs/, and the zips in backtests/Results - each time the page is opened, so it
grows by itself after every session. It never talks to a broker and has nothing that can place orders.

https: set site_cert in config.yaml to a PEM file with the certificate and its private key (a key with
a password: SITE_CERT_PASSWORD in .env). A renewed certificate is picked up without a restart. Viewers
need the key (site_key, or dashboard_key when that's blank) once: https://<host>:4334/?key=...  after
which the browser remembers it for 30 days.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("swingbot.site")

PAGE = Path(__file__).with_name("results_site.html")
COOKIE = "swingbot_site"
COOKIE_DAYS = 30
MAX_FAILS, FAIL_WINDOW = 10, 600              # wrong keys from one address in 10 minutes -> locked out a while
LOG_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(report\.html|dashboard\.html|dashboard\.jpg|chart-[A-Za-z0-9_.\-]+\.png)$")
SHEET_FILE = re.compile(r"^(results-[a-z0-9\-]+\.xlsx|daily-results\.csv)$")
ZIP_FILE = re.compile(r"^[A-Za-z0-9_.,=\-]+\.zip$")
STARTUP_NAME = "swingbot-site.vbs"


# ------------------------------------------------------------------------------------------- the data
def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _drawdown(values: list[float]) -> float:
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, (v / peak - 1) * 100)
    return round(worst, 2)


def _group(days: list[dict], key) -> list[dict]:
    out: dict[str, list[dict]] = {}
    for d in days:
        out.setdefault(key(d), []).append(d)
    rows = []
    for k in sorted(out):
        g = out[k]
        rows.append(dict(period=k, days=len(g), start=g[0]["start"], end=g[-1]["end"],
                         change=round(g[-1]["end"] - g[0]["start"], 2),
                         change_pct=round((g[-1]["end"] / g[0]["start"] - 1) * 100, 2) if g[0]["start"] else 0.0,
                         bot_pnl=round(sum(d["bot_pnl"] for d in g), 2), trades=sum(d["trades"] for d in g),
                         wins=sum(d["wins"] for d in g), losses=sum(d["losses"] for d in g)))
    return rows


def _breakdown(trades: list[dict], key) -> list[dict]:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    rows = []
    for k, ts in g.items():
        wins = [t for t in ts if t["pnl"] > 0]
        rows.append(dict(name=k, trades=len(ts), win_pct=round(100 * len(wins) / len(ts), 1),
                         avg_r=round(sum(t["r"] for t in ts) / len(ts), 3), net=round(sum(t["pnl"] for t in ts), 2)))
    return sorted(rows, key=lambda r: -r["net"])


def collect(workdir: Path, cfg) -> dict:
    """Everything the page shows, from the files the bot writes."""
    from . import __version__
    from .history import CSV_NAME, account_label, read_days, read_trades
    logs = workdir / "logs"
    accounts: dict[str, list[dict]] = defaultdict(list)
    for d in sorted(read_days(logs / CSV_NAME), key=lambda d: d.date):
        files = {}
        for kind, name in (("report", f"{d.date}-report.html"), ("picture", f"{d.date}-dashboard.jpg"),
                           ("snapshot", f"{d.date}-dashboard.html")):
            if (logs / name).exists():
                files[kind] = "files/" + name
        accounts[d.account].append(dict(date=d.date, start=d.start_value, end=d.end_value,
                                         change=round(d.end_value - d.start_value, 2),
                                         change_pct=round((d.end_value / d.start_value - 1) * 100, 3)
                                         if d.start_value else 0.0,
                                         bot_pnl=d.bot_pnl, trades=d.trades, wins=d.wins, losses=d.losses,
                                         note=d.note, files=files))
    from . import settings_log
    srecs = settings_log.read(logs)
    acct = {}
    for name, days in accounts.items():
        tr = sum(d["trades"] for d in days)
        wins = sum(d["wins"] for d in days)
        ends = [days[0]["start"]] + [d["end"] for d in days]
        best = max(days, key=lambda d: d["bot_pnl"])
        worst = min(days, key=lambda d: d["bot_pnl"])
        acct[name] = dict(
            days=days,
            weeks=_group(days, lambda d: str(date.fromisoformat(d["date"]) -
                                             timedelta(days=date.fromisoformat(d["date"]).weekday()))),
            months=_group(days, lambda d: d["date"][:7]),
            periods=settings_log.periods(srecs, days, name),
            summary=dict(first=days[0]["date"], last=days[-1]["date"], trading_days=len(days),
                         start=days[0]["start"], latest=days[-1]["end"],
                         change=round(days[-1]["end"] - days[0]["start"], 2),
                         change_pct=round((days[-1]["end"] / days[0]["start"] - 1) * 100, 2) if days[0]["start"] else 0,
                         bot_pnl=round(sum(d["bot_pnl"] for d in days), 2), trades=tr, wins=wins,
                         win_pct=round(100 * wins / tr, 1) if tr else 0.0,
                         up_days=sum(d["bot_pnl"] > 0 for d in days), down_days=sum(d["bot_pnl"] < 0 for d in days),
                         best=dict(date=best["date"], pnl=best["bot_pnl"]),
                         worst=dict(date=worst["date"], pnl=worst["bot_pnl"]),
                         drawdown_pct=_drawdown(ends)))

    trades = read_trades(workdir / "trades.csv")
    for t in trades:
        t["side"] = "short" if t["setup"].startswith("short") else "long"
    trades.sort(key=lambda t: t["when"])
    breakdowns = dict(
        setup=_breakdown(trades, lambda t: t["setup"]),
        exit=_breakdown(trades, lambda t: t["exit_by"] or "?"),
        hour=sorted(_breakdown(trades, lambda t: t["when"][11:13] + ":00"), key=lambda r: r["name"]),
        symbol=_breakdown(trades, lambda t: t["symbol"]),
    ) if trades else {}

    downloads = [dict(name=p.name, href="files/" + p.name, size=p.stat().st_size)
                 for p in sorted(logs.glob("results-*.xlsx")) if SHEET_FILE.match(p.name)]
    if (logs / CSV_NAME).exists():
        downloads.append(dict(name=CSV_NAME, href="files/" + CSV_NAME, size=(logs / CSV_NAME).stat().st_size))
    if (workdir / "trades.csv").exists():
        downloads.append(dict(name="trades.csv", href="files/trades.csv", size=(workdir / "trades.csv").stat().st_size))

    backtests = []
    bdir = _backtest_dir(workdir, cfg)
    if bdir is not None and bdir.is_dir():
        zips = sorted((p for p in bdir.glob("*.zip") if ZIP_FILE.match(p.name)), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        backtests = [dict(name=p.name, href="backtests/" + quote(p.name), size=p.stat().st_size,
                          when=datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
                     for p in zips[:200]]

    checks = []
    for f in sorted(logs.glob("????-??-??-check.json")):        # (not update-check.json)
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        checks.append({k: c.get(k) for k in ("date", "live_trades", "bt_trades", "matched", "live_pnl", "bt_pnl", "gap",
                                              "slippage_dollars", "slippage_pct_per_side", "slippage_setting",
                                              "decisions_dollars", "sides_measured")}
                      | dict(live_only=len(c.get("live_only") or []), bt_only=len(c.get("bt_only") or [])))
    # weighted by how many fills each day measured (older files without the count weigh 1)
    w = [(c["slippage_pct_per_side"], c.get("sides_measured") or 1) for c in checks
         if c.get("slippage_pct_per_side") is not None]
    check_summary = dict(days=len(checks), sides=sum(n for _, n in w),
                         avg_pct_per_side=round(sum(x * n for x, n in w) / sum(n for _, n in w), 4) if w else None,
                         setting=getattr(cfg, "slippage_pct", None),
                         gap=round(sum(c.get("gap") or 0 for c in checks), 2))
    default = account_label(cfg)
    log_days = sorted((p.name[:10] for p in logs.glob("*.log") if LOG_NAME.match(p.name)), reverse=True)[:60]
    return dict(version=__version__, log_days=log_days, checks=checks[::-1], check_summary=check_summary, generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
                account=default if default in acct else (sorted(acct)[0] if acct else default),
                accounts=acct, trades=trades[-2000:], trade_count=len(trades), breakdowns=breakdowns,
                downloads=downloads, backtests=backtests,
                note="Trades come from trades.csv, which has every account's trades (live, paper and demo).")


def _backtest_dir(workdir: Path, cfg) -> Path | None:
    d = str(getattr(cfg, "backtest_results_dir", "") or "").strip()
    if not d:
        return None
    p = Path(d)
    return p if p.is_absolute() else workdir / p


def _sources(workdir: Path, cfg) -> tuple:
    """A signature of the files the page is built from: the data is rebuilt only when one changes."""
    logs = workdir / "logs"
    sig = []
    for p in [logs / "daily-results.csv", workdir / "trades.csv", logs / "settings-history.jsonl",
              *logs.glob("????-??-??-check.json"), *logs.glob("*-report.html"),
              *logs.glob("*-dashboard.jpg")]:
        try:
            st = p.stat()
            sig.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    b = _backtest_dir(workdir, cfg)
    if b is not None:
        try:
            sig.append(("backtests", b.stat().st_mtime_ns, len(list(b.glob("*.zip")))))
        except OSError:
            pass
    return tuple(sorted(sig)) + (date.today().isoformat(),)


# ------------------------------------------------------------------------------------------- health
def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l_) // 433
    month = (h + l_ - 7 * m + 90) // 25
    return date(y, month, (h + l_ - 7 * m + 33 * month + 19) % 32)


def _nth(y, month, weekday, n):            # n-th weekday of a month (n = -1: the last)
    if n > 0:
        d = date(y, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    else:
        d = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
        d -= timedelta(days=(d.weekday() - weekday) % 7)
    return d


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def market_holidays(y: int) -> set[date]:
    """NYSE full-day holidays (the regular rules; one-off closures aren't known)."""
    out = {date(y, 1, 1) if date(y, 1, 1).weekday() < 5 else _observed(date(y, 1, 1)), _nth(y, 1, 0, 3), _nth(y, 2, 0, 3), _easter(y) - timedelta(days=2),
           _nth(y, 5, 0, -1), _observed(date(y, 7, 4)), _nth(y, 9, 0, 1), _nth(y, 11, 3, 4),
           _observed(date(y, 12, 25))}
    if y >= 2022:
        out.add(_observed(date(y, 6, 19)))
    return {d for d in out if d.year == y}


def market_day(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def bot_status(workdir: Path, now: datetime | None = None) -> dict:
    """What the bot is doing, from logs/heartbeat.json (the bot writes it every 30 s or so while it runs)."""
    now = now or _et_now()
    today = now.date()
    try:
        hb = json.loads((workdir / "logs" / "heartbeat.json").read_text(encoding="utf-8"))
        at = datetime.fromisoformat(hb["time"])
    except (OSError, ValueError, KeyError):
        hb, at = None, None
    out = dict(market_day=market_day(today), heartbeat=hb)
    t = now.time()
    if hb is None or hb.get("date") != str(today):
        if not out["market_day"]:
            out.update(state="no session", text="No trading today")
        elif t < datetime.strptime("09:31", "%H:%M").time():
            out.update(state="waiting", text="Not started yet today (it should start before 9:30 ET)")
        elif t < datetime.strptime("16:00", "%H:%M").time():
            out.update(state="not started", text="The bot hasn't started today", problem=True)
        else:
            out.update(state="no session", text="The bot didn't run today")
        return out
    age = (now - at).total_seconds() / 60
    if hb.get("final"):
        out.update(state="finished", text=f"Finished for the day ({hb.get('state') or 'session done'})")
    elif age > 15:
        out.update(state="stopped", text=f"No sign of the bot since {at:%H:%M} ET ({age:.0f} min) - "
                                         f"it may have stopped", problem=True)
    else:
        out.update(state="running", text=(hb.get("state") or "Running") + (" · paused" if hb.get("paused") else ""))
    out["since"] = at.strftime("%H:%M")
    return out


BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def onedrive_problems(workdir: Path, limit: int = 8) -> list[str]:
    """Files whose names Windows/OneDrive can't handle (reserved device names, bad characters, a name ending
    in a dot or space). OneDrive's own sync status isn't something the bot can read."""
    from .data import WINDOWS_RESERVED
    bad = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for name in dirs + files:
            stem = name.split(".", 1)[0].upper()
            if stem in WINDOWS_RESERVED or BAD_CHARS.search(name) or name.endswith((".", " ")):
                bad.append(str(Path(root, name).relative_to(workdir)))
                if len(bad) >= limit:
                    return bad
    return bad


def health(workdir: Path, cfg) -> dict:
    from . import __version__
    from .updates import latest_release, newer
    logs = workdir / "logs"
    st = bot_status(workdir)
    reports = sorted(logs.glob("*-report.html"))
    last = None
    if reports:
        day = reports[-1].name[:10]
        sent = logs / f"{day}-report.sent"
        last = dict(date=day, href="files/" + reports[-1].name,
                    sent=sent.read_text(encoding="utf-8").strip() if sent.exists() else "")
    rel = latest_release(cfg.update_repo, logs / "update-check.json") if getattr(cfg, "update_check", True) else None
    running = (st.get("heartbeat") or {}).get("version") or __version__
    return dict(bot=st, report=last, onedrive=onedrive_problems(workdir),
                version=dict(installed=__version__, bot=running, latest=(rel or {}).get("tag", ""),
                             url=(rel or {}).get("url", ""),
                             update=bool(rel and rel.get("tag") and newer(rel["tag"], __version__))),
                email_report=bool(getattr(cfg, "email_report", False)))


class Watchdog:
    """Runs inside the results website: alerts when the bot hasn't started or has stopped responding."""

    def __init__(self, cfg, workdir: Path):
        from .notify import Notifier
        self.n = Notifier(cfg, "swingbot watchdog")
        self.workdir = workdir
        self.sent: set[str] = set()

    def check(self, now: datetime | None = None) -> str | None:
        now = now or _et_now()
        st = bot_status(self.workdir, now)
        if st.get("state") == "not started":
            key = f"{now.date()}-not-started"
            msg = ("The bot hasn't started today (no sign of it by " + now.strftime("%H:%M") + " ET). "
                   "Check the PC and the scheduled task that starts it.")
        elif st.get("state") == "stopped" and now.time() < datetime.strptime("15:58", "%H:%M").time():
            key = f"{now.date()}-stopped-{st.get('since')}"
            msg = (f"The bot has stopped responding: no sign of it since {st.get('since')} ET. "
                   "If it was in a trade, the broker's stop order is still there - check the account.")
        else:
            return None
        if key in self.sent:
            return None
        self.sent.add(key)
        log.warning("Watchdog: %s", msg)
        if self.n.any_on:
            self.n.alert("bot not running" if "started" in msg else "bot stopped", msg, key=key)
        return msg

    def loop(self, stop: threading.Event) -> None:
        while not stop.wait(120):
            try:
                self.check()
            except Exception as e:
                log.debug("watchdog check failed: %s", e)


# ------------------------------------------------------------------------------------------- the log
REDACT = [re.compile(r"(?i)(key=)[^\s&\"']+"), re.compile(r"(?i)(token[=:]\s*)[^\s&\"']+"),
          re.compile(r"(?i)(password[=:]\s*)\S+")]
LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.log$")


def log_text(workdir: Path, day: str, lines: int = 500) -> str:
    """The end of a day's activity log, with keys and tokens hidden."""
    p = workdir / "logs" / f"{day}.log"
    if not LOG_NAME.match(p.name) or not p.is_file():
        return ""
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        text = f.read().decode("utf-8", errors="replace")
    out = text.splitlines()[-lines:]
    joined = "\n".join(out)
    for r in REDACT:
        joined = r.sub(lambda m: m.group(1) + "…", joined)
    return joined


# ------------------------------------------------------------------------------------------- the server
class _TLSServer(ThreadingHTTPServer):
    """HTTPS server whose certificate can be swapped while it runs. The TLS handshake happens in each
    request's own thread (with a time limit), so one slow or broken visitor can't hold up the others."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"     # Linux/macOS: restart at once; Windows: see server_bind
    context: ssl.SSLContext | None = None

    def server_bind(self):
        if os.name == "nt":       # Windows: refuse to share the port with another copy
            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        super().server_bind()

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(15)
        if self.context is not None:
            sock = self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def finish_request(self, request, client_address):
        if isinstance(request, ssl.SSLSocket):
            try:
                request.do_handshake()
            except (ssl.SSLError, OSError):
                return                                    # e.g. plain http to the https port, or a scanner
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        log.debug("connection from %s ended with an error", client_address[0], exc_info=True)


def load_context(cert: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), password=os.environ.get("SITE_CERT_PASSWORD") or None)
    return ctx


class Site:
    def __init__(self, cfg, workdir: Path, port: int | None = None):
        self.cfg, self.workdir = cfg, Path(workdir)
        self.port = int(port or cfg.site_port)
        cert = str(cfg.site_cert or "").strip().strip('"')
        self.cert = Path(os.path.expandvars(os.path.expanduser(cert))) if cert else None
        self.lan = bool(cfg.site_lan) and self.cert is not None
        self.key = str(cfg.site_key or cfg.dashboard_key or "").strip()
        if self.lan and not self.key:
            raise SystemExit("site_lan is on but there's no key: set site_key (or dashboard_key) in config.yaml, "
                             "so only you can open it from other devices")
        secret = (self.key or "local") + "|" + str(self.workdir)
        self.cookie_value = hashlib.sha256(secret.encode()).hexdigest()[:40]
        self._data = (None, b"")
        self._lock = threading.Lock()
        self._fails: dict[str, deque] = defaultdict(deque)
        self._cert_sig = None
        self.server: _TLSServer | None = None

    # ---------------------------------------------------------------- data, cached until a file changes
    def data_json(self) -> bytes:
        sig = _sources(self.workdir, self.cfg)
        with self._lock:
            if self._data[0] == sig:
                return self._data[1]
        body = json.dumps(collect(self.workdir, self.cfg), separators=(",", ":")).encode()
        with self._lock:
            self._data = (sig, body)
        return body

    def health_json(self) -> bytes:
        return json.dumps(health(self.workdir, self.cfg), separators=(",", ":"), default=str).encode()

    # ---------------------------------------------------------------- certificate
    def _cert_stat(self):
        st = self.cert.stat()
        return st.st_mtime_ns, st.st_size

    def refresh_cert(self) -> bool:
        """Load the certificate, or load it again if the file changed. False if it can't be used."""
        if self.cert is None:
            return True
        try:
            sig = self._cert_stat()
            if sig == self._cert_sig and self.server and self.server.context is not None:
                return True
            ctx = load_context(self.cert)
        except FileNotFoundError:
            log.error("site_cert not found: %s", self.cert)
            return False
        except (ssl.SSLError, OSError, ValueError) as e:
            log.error("Can't use the certificate in %s (%s). It needs the certificate and its private key, "
                      "in PEM form (-----BEGIN ...-----). A key with a password: SITE_CERT_PASSWORD in .env.",
                      self.cert, e)
            return False
        if self.server is not None:
            self.server.context = ctx
        self._ctx, self._cert_sig = ctx, sig
        log.info("Certificate loaded: %s", self.cert.name)
        return True

    # ---------------------------------------------------------------- access
    def _locked_out(self, ip: str) -> bool:
        q = self._fails[ip]
        while q and q[0] < time.time() - FAIL_WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS

    def check(self, ip: str, query: dict, cookie_header: str | None) -> tuple[bool, bool]:
        """(allowed, set_cookie). http on this PC only needs no key."""
        if not self.lan:
            return True, False
        if self._locked_out(ip):
            return False, False
        c = SimpleCookie()
        try:
            c.load(cookie_header or "")
        except Exception:
            pass
        if COOKIE in c and hmac.compare_digest(c[COOKIE].value.encode(), self.cookie_value.encode()):
            return True, False
        given = (query.get("key") or [""])[0]
        if given and hmac.compare_digest(given.encode(), self.key.encode()):
            return True, True
        if given:
            self._fails[ip].append(time.time())
            log.warning("Wrong site key from %s", ip)
        return False, False

    # ---------------------------------------------------------------- serving
    def handler(self):
        site = self

        class H(BaseHTTPRequestHandler):
            server_version = "swingbot-site"
            sys_version = ""

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.client_address[0], fmt % args)

            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; frame-ancestors 'self'; form-action 'self'")
                if site.cert is not None:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _text(self, code: int, msg: str):
                self._send(code, msg.encode(), "text/plain; charset=utf-8")

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                url = urlparse(self.path)
                ip = self.client_address[0]
                if not site.lan:            # http on this PC only: refuse other host names (DNS rebinding)
                    host = (self.headers.get("Host") or "").lower()
                    if host not in (f"127.0.0.1:{site.port}", f"localhost:{site.port}"):
                        return self._text(403, f"Use http://127.0.0.1:{site.port}/")
                ok, set_cookie = site.check(ip, parse_qs(url.query), self.headers.get("Cookie"))
                if not ok:
                    if site._locked_out(ip):
                        return self._text(429, "Too many wrong keys - try again later.")
                    return self._text(403, "Add your key to the address once: /?key=...")
                extra = {}
                if set_cookie:
                    extra["Set-Cookie"] = (f"{COOKIE}={site.cookie_value}; Max-Age={COOKIE_DAYS * 86400}; Path=/; "
                                           f"HttpOnly; SameSite=Strict" + ("; Secure" if site.cert else ""))
                    if url.path == "/":     # take the key out of the address bar (and the history)
                        self.send_response(303)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", extra["Set-Cookie"])
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                path = url.path
                try:
                    if path == "/":
                        return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", extra)
                    if path == "/data.json":
                        return self._send(200, site.data_json(), "application/json", extra)
                    if path == "/health.json":
                        return self._send(200, site.health_json(), "application/json", extra)
                    if path == "/log.txt":
                        day = (parse_qs(url.query).get("date") or [str(_et_now().date())])[0]
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                            return self._text(400, "bad date")
                        return self._send(200, log_text(site.workdir, day).encode(), "text/plain; charset=utf-8", extra)
                    if path.startswith("/files/"):
                        return self._file(path[len("/files/"):], extra)
                    if path.startswith("/backtests/"):
                        return self._zip(path[len("/backtests/"):], extra)
                    if path == "/favicon.ico":
                        return self._send(204, b"", "image/x-icon")
                except (ConnectionError, ssl.SSLError):
                    return
                except Exception as e:
                    log.exception("page error")
                    return self._text(500, f"Something went wrong: {e}")
                return self._text(404, "Not found")

            def _file(self, name: str, extra):
                from urllib.parse import unquote
                name = unquote(name)
                logs = site.workdir / "logs"
                if LOG_FILE.match(name) or SHEET_FILE.match(name):
                    p = logs / name
                elif name == "trades.csv":
                    p = site.workdir / "trades.csv"
                else:
                    return self._text(404, "Not found")
                if not p.is_file():
                    return self._text(404, "Not found")
                ctype = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".png": "image/png",
                         ".csv": "text/csv; charset=utf-8",
                         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[p.suffix]
                if p.suffix in (".csv", ".xlsx"):
                    extra = dict(extra, **{"Content-Disposition": f'attachment; filename="{p.name}"'})
                self._send(200, p.read_bytes(), ctype, extra)

            def _zip(self, name: str, extra):
                from urllib.parse import unquote
                name = unquote(name)
                b = _backtest_dir(site.workdir, site.cfg)
                if b is None or not ZIP_FILE.match(name) or not (b / name).is_file():
                    return self._text(404, "Not found")
                self._send(200, (b / name).read_bytes(), "application/zip",
                           dict(extra, **{"Content-Disposition": f'attachment; filename="{name}"'}))

        return H

    def start(self) -> str:
        if self.cert is not None and not self.refresh_cert():
            raise RuntimeError("no usable certificate")
        host = "0.0.0.0" if self.lan else "127.0.0.1"
        self.server = _TLSServer((host, self.port), self.handler())
        if self.cert is not None:
            self.server.context = self._ctx
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="site", daemon=True).start()
        return self.url()

    def url(self) -> str:
        if self.cert is None:
            return f"http://127.0.0.1:{self.port}/"
        return f"https://{_cert_host(self.cert) or socket.gethostname()}:{self.port}/"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def _cert_host(cert: Path) -> str:
    """A name to open the site by, from the certificate (*.example.com -> swingbot.example.com)."""
    try:
        import base64
        pem = cert.read_text(encoding="ascii", errors="ignore")
        m = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", pem, re.S)
        if not m:
            return ""
        der = base64.b64decode("".join(m.group(1).split()))
        found = []
        for i in re.finditer(rb"\x82([\x01-\x7f])", der):            # dNSName entries (subjectAltName)
            n = der[i.end():i.end() + i.group(1)[0]]
            if re.fullmatch(rb"(\*\.)?[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+", n):
                found.append(n.decode())
        if not found:
            return ""
        name = found[0]
        return "swingbot" + name[1:] if name.startswith("*.") else name
    except Exception:
        return ""


# ------------------------------------------------------------------------------------------- always on
def _logging(workdir: Path) -> None:
    (workdir / "logs").mkdir(exist_ok=True)
    root = logging.getLogger("swingbot")
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(workdir / "logs" / "site.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root.addHandler(fh)
    if sys.stdout is not None and sys.stderr is not None:      # pythonw has no console
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        root.addHandler(sh)


def _pid_file(workdir: Path) -> Path:
    return workdir / "logs" / "site.pid"


def serve(cfg, workdir: Path) -> None:
    """Run until stopped. Problems (certificate not there yet, port busy) are retried, so a copy started
    at logon comes up by itself once OneDrive has synced the certificate."""
    _logging(workdir)
    pid = _pid_file(workdir)
    try:
        Site(cfg, workdir)
    except SystemExit as e:
        log.error("Results site not started: %s", e)
        raise
    site, wait = None, 30
    stop = threading.Event()
    threading.Thread(target=Watchdog(cfg, workdir).loop, args=(stop,), name="watchdog", daemon=True).start()
    try:
        while True:
            try:
                if site is None:
                    site = Site(cfg, workdir)
                    url = site.start()
                    pid.write_text(str(os.getpid()), encoding="utf-8")
                    log.info("Results site running: %s  (logs\\site.log; this window can be minimised)", url)
                    if site.lan:
                        log.info("Open it once with ?key=<your site key> on the end; the browser then remembers it.")
                    wait = 30
                time.sleep(300)
                site.refresh_cert()                    # a renewed certificate is used without a restart
            except OSError as e:
                if getattr(e, "winerror", None) in (10048, 10013) or e.errno in (98, 48, 13):
                    log.error("Port %s is in use - is the site already running? (retrying in %ds)", cfg.site_port, wait)
                else:
                    log.error("Site problem: %s (retrying in %ds)", e, wait)
                site = None
                time.sleep(wait)
                wait = min(wait * 2, 600)
            except RuntimeError as e:
                log.error("Site not started: %s (retrying in %ds)", e, wait)
                site = None
                time.sleep(wait)
                wait = min(wait * 2, 600)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if site is not None:
            site.stop()
        try:
            if pid.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid.unlink()
        except OSError:
            pass


def _startup_folder() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def launcher_text(workdir: Path, config: str, python: str) -> str:
    """A tiny Windows script that starts the site with no window, in the bot's folder."""
    q = lambda s: str(s).replace('"', '""')
    cmd = f'"{q(python)}" -m swingbot site --config "{q(config)}"'
    return ('Set sh = CreateObject("WScript.Shell")\r\n'
            f'sh.CurrentDirectory = "{q(workdir)}"\r\n'
            f'sh.Run "{q(cmd)}", 0, False\r\n')


def stop_running(workdir: Path) -> bool:
    pid = _pid_file(workdir)
    try:
        n = int(pid.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        # only if that process is still a Python (a stale file's number may belong to something else by now)
        r = subprocess.run(["tasklist", "/FI", f"PID eq {n}", "/FO", "CSV", "/NH"], capture_output=True, text=True)
        ok = "python" in (r.stdout or "").lower()
        if ok:
            ok = subprocess.run(["taskkill", "/PID", str(n), "/F"], capture_output=True, text=True).returncode == 0
    else:
        try:
            os.kill(n, 15)
            ok = True
        except OSError:
            ok = False
    try:
        pid.unlink()
    except OSError:
        pass
    return ok


def install(cfg, workdir: Path, config: str) -> None:
    if os.name != "nt":
        sys.exit("--install is for Windows (it adds the site to your Startup folder).")
    folder = _startup_folder()
    folder.mkdir(parents=True, exist_ok=True)
    vbs = folder / STARTUP_NAME
    vbs.write_text(launcher_text(workdir, str(Path(config).resolve()), _pythonw()), encoding="utf-16")
    if stop_running(workdir):
        print("Stopped the copy that was running.")
        time.sleep(2)
    subprocess.Popen(["wscript.exe", str(vbs)], cwd=str(workdir))
    site = Site(cfg, workdir)
    print(f"Results site started in the background, and set to start each time you log on:\n  {vbs}\n")
    print(f"Open: {site.url()}" + ("?key=<your site key>" if site.lan else ""))
    print("Log: logs\\site.log.  After a git pull, run --install again to restart it with the new version.")
    if site.lan:
        print("\nFor other devices: they need to reach this PC on port %d (Windows may ask to allow Python "
              "through the firewall - allow it for private networks), and the name in the address must match "
              "your certificate and point to this PC." % site.port)


def uninstall(workdir: Path) -> None:
    vbs = _startup_folder() / STARTUP_NAME
    if vbs.exists():
        vbs.unlink()
        print(f"Removed {vbs} - it won't start at logon any more.")
    print("Stopped it." if stop_running(workdir) else "It wasn't running.")


def main(cfg, args, workdir: Path) -> None:
    if getattr(args, "install", False):
        return install(cfg, workdir, args.config)
    if getattr(args, "uninstall", False):
        return uninstall(workdir)
    if getattr(args, "stop", False):
        print("Stopped it." if stop_running(workdir) else "It wasn't running (no logs\\site.pid).")
        return
    serve(cfg, workdir)
