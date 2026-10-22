"""Live dashboard: a tiny web server run by the bot.

The bot hands it a status snapshot after every check; the page in your browser
polls /status.json every few seconds. By default only this computer can reach it
(127.0.0.1). With dashboard_lan on, other devices on your home network can too,
and every request must carry the access key (?key=...) or it is refused.

The page has exactly one control: the bar-size dropdown (POST /bar-size). Nothing
on the page can place, change or cancel orders. The control is offered only to a
browser on this computer (unless dashboard_lan_control is on), and every change
must carry a per-run token that is written into the page itself, so another
website open in your browser cannot send one.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
from urllib.parse import parse_qs, urlparse
import logging
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PAGE = Path(__file__).with_name("dashboard.html")


class _EventLog(logging.Handler):
    """Keeps the bot's recent log lines for the page's activity panel."""

    def __init__(self, maxlen: int = 40):
        super().__init__(level=logging.INFO)
        self.events: deque = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            t = datetime.fromtimestamp(record.created, ET).strftime("%H:%M:%S")
            self.events.append({"t": t, "level": record.levelname, "msg": record.getMessage()})
        except Exception:
            pass


class Dashboard:
    def __init__(self, port: int = 8765, lan: bool = False, key: str = "", lan_control: bool = False):
        self.port = port
        self.lan = lan
        self.lan_control = lan_control
        self.on_bar_size = None                 # set by the bot: callable(minutes) -> message
        self.on_rescan = None                   # set by the bot: callable() -> message
        self._chart_json: dict[str, bytes] = {} # status with another watched symbol's chart
        self.token = secrets.token_urlsafe(18)  # proves a change came from a page this server served
        # On the network a key is always required; one is generated if you didn't set one.
        self.key = (key or secrets.token_urlsafe(9)) if lan else key
        self._json = b"{}"
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.events = _EventLog()
        logging.getLogger("swingbot").addHandler(self.events)

    # ---------------------------------------------------------------- server
    def start(self) -> str | None:
        """Start serving. Tries a few ports if the first is busy. Returns the URL, or None."""
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def _allowed(self, url) -> bool:
                # local-only mode: refuse requests addressed to any other host name
                # (stops "DNS rebinding" tricks that point a web address at 127.0.0.1)
                if not dash.lan:
                    host = (self.headers.get("Host") or "").lower()
                    if host not in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}"):
                        self.send_error(403, "Use http://127.0.0.1:%d/" % dash.port)
                        return False
                if dash.key:
                    given = (parse_qs(url.query).get("key") or [""])[0]
                    if not hmac.compare_digest(given.encode(), dash.key.encode()):
                        self.send_error(403, "Missing or wrong key")
                        return False
                return True

            def _may_control(self) -> bool:
                if dash.on_bar_size is None and dash.on_rescan is None:
                    return False
                try:
                    local = ipaddress.ip_address(self.client_address[0].split("%")[0]).is_loopback
                except ValueError:
                    local = False
                return local or dash.lan_control

            def _json_reply(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                url = urlparse(self.path)
                if not self._allowed(url):
                    return
                if url.path not in ("/bar-size", "/rescan"):
                    self.send_error(404)
                    return
                handler = dash.on_bar_size if url.path == "/bar-size" else dash.on_rescan
                if not self._may_control() or handler is None:
                    self._json_reply(403, {"message": "use the dashboard on the PC running the bot"})
                    return
                given = self.headers.get("X-Swingbot-Token") or ""
                if not hmac.compare_digest(given.encode(), dash.token.encode()):
                    self._json_reply(403, {"message": "page is out of date - reload it"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if not 0 < n <= 200:
                        raise ValueError("bad request")
                    body = json.loads(self.rfile.read(n))
                    msg = handler(body["minutes"]) if url.path == "/bar-size" else handler()
                except (ValueError, KeyError, TypeError) as e:
                    self._json_reply(400, {"message": str(e) or "bad request"})
                    return
                except Exception as e:                      # never let the page crash the bot
                    self._json_reply(500, {"message": f"failed: {e}"})
                    return
                self._json_reply(200, {"message": msg})

            def do_GET(self):
                url = urlparse(self.path)
                path = url.path
                if not self._allowed(url):
                    return
                if path in ("/", "/index.html"):
                    html = PAGE.read_text(encoding="utf-8")
                    if self._may_control():
                        html = html.replace("<!--CONTROL-->", "<script>window.__CONTROL__ = %s;</script>"
                                            % json.dumps({"token": dash.token}))
                    body, ctype = html.encode("utf-8"), "text/html; charset=utf-8"
                elif path == "/status.json":
                    chart = (parse_qs(url.query).get("chart") or [""])[0]
                    with dash._lock:
                        body = dash._chart_json.get(chart, dash._json)
                    ctype = "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):     # keep the console quiet
                pass

        for port in range(self.port, self.port + 10):
            try:
                self._server = ThreadingHTTPServer(("0.0.0.0" if self.lan else "127.0.0.1", port), Handler)
                self.port = port
                break
            except OSError:
                continue
        if self._server is None:
            return None
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port}/" + self._q()

    def _q(self) -> str:
        return f"?key={self.key}" if self.key else ""

    def lan_url(self) -> str | None:
        """Address other devices on your network can use (None when sharing is off)."""
        if not (self.lan and self._server):
            return None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))      # picks the network interface; sends nothing
                ip = s.getsockname()[0]
        except OSError:
            ip = socket.gethostbyname(socket.gethostname())
        return f"http://{ip}:{self.port}/" + self._q()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    # ---------------------------------------------------------------- data
    def update(self, status: dict) -> None:
        status = dict(status, events=list(self.events.events)[::-1])
        views = status.pop("views", None) or {}
        status["chart_symbol"] = status.get("symbol")
        data = json.dumps(status, default=_jsonable).encode()
        charts = {}
        for sym, view in views.items():            # the page asks for another watched symbol's chart
            if sym != status.get("symbol"):         # with ?chart=SYM; everything else stays the same
                charts[sym] = json.dumps(dict(status, **view, chart_symbol=sym), default=_jsonable).encode()
        with self._lock:
            self._json = data
            self._chart_json = charts

    def snapshot(self, path: Path) -> Path:
        """Save a self-contained copy of the page with the final status baked in (opens offline)."""
        with self._lock:
            data = self._json.decode()
        data = data.replace("</", "<\\/")
        html = PAGE.read_text(encoding="utf-8").replace(
            "<!--SNAPSHOT-->", f"<script>window.__STATUS__ = {data};</script>")
        path.write_text(html, encoding="utf-8")
        return path


def _jsonable(o):
    if hasattr(o, "isoformat"):
        return o.isoformat()
    try:
        return float(o)
    except Exception:
        return str(o)
