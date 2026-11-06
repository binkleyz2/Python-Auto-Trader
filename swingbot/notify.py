"""Alerts and trade notifications: a push to your phone (the free ntfy app) and/or an email.

What sends one:
- anything the bot logs as an ERROR (an order it couldn't confirm, a stop it couldn't move, a failed exit...)
- a few warnings that matter: an entry order refused, the day report not made or not sent
- with notify_trades on: each entry and exit (phone push only, not email)
- the results website (always on) watches the bot: it alerts if the bot hasn't started by 9:31 ET on a
  trading day, or stops responding during the session

Sending never holds up trading: it happens in the background, a failure to send is only logged, the same
alert isn't repeated within 15 minutes, and at most 30 alerts go out a day.

Set up (config.yaml):  ntfy_topic: <a long random name>   (and install ntfy on your phone, subscribe to it)
                        alert_email: true                    (alerts by email too, to email_to, via .env SMTP)
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import threading
import time
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

log = logging.getLogger("swingbot.notify")

REPEAT_SECONDS = 15 * 60
MAX_PER_DAY = 30


class Notifier:
    def __init__(self, cfg, source: str = "swingbot"):
        self.cfg, self.source = cfg, source
        self._recent: dict[str, float] = {}
        self._day, self._count = "", 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- what's set up
    @property
    def push_on(self) -> bool:
        return bool(str(getattr(self.cfg, "ntfy_topic", "") or "").strip())

    @property
    def email_on(self) -> bool:
        return bool(getattr(self.cfg, "alert_email", False)) and bool(str(self.cfg.email_to or "").strip())

    @property
    def any_on(self) -> bool:
        return self.push_on or self.email_on

    # ---------------------------------------------------------------- sending
    def _allow(self, key: str) -> bool:
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            if today != self._day:
                self._day, self._count, self._recent = today, 0, {}
            if now - self._recent.get(key, 0) < REPEAT_SECONDS or self._count >= MAX_PER_DAY:
                return False
            self._recent[key] = now
            self._count += 1
            return True

    def alert(self, title: str, message: str, urgent: bool = True, email: bool = True, key: str | None = None,
              wait: bool = False) -> bool:
        """Send an alert (push and/or email) in the background. False if nothing is set up or it was a repeat."""
        if not self.any_on or not self._allow(key or title + "|" + _strip_numbers(message)):
            return False
        jobs = []
        if self.push_on:
            jobs.append(lambda: self._push(title, message, urgent))
        if email and self.email_on:
            jobs.append(lambda: self._email(title, message))
        if not jobs:
            return False

        def go():
            for j in jobs:
                try:
                    j()
                except Exception as e:                      # never raise, never log as an error (no loops)
                    log.info("Couldn't send the alert %r: %s", title, e)
        if wait:
            go()
        else:
            threading.Thread(target=go, name="alert", daemon=True).start()
        return True

    def _push(self, title: str, message: str, urgent: bool) -> None:
        server = str(getattr(self.cfg, "ntfy_server", "") or "https://ntfy.sh").rstrip("/")
        topic = str(self.cfg.ntfy_topic).strip().strip("/")
        req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"), method="POST")
        req.add_header("Title", _ascii(f"{self.source}: {title}"))
        req.add_header("Priority", "high" if urgent else "default")
        req.add_header("Tags", "warning" if urgent else "chart_with_upwards_trend")
        token = os.environ.get("NTFY_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()

    def _email(self, title: str, message: str) -> None:
        user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
        if not user or not pw:
            raise RuntimeError("SMTP_USER / SMTP_PASSWORD not set in .env")
        to = [a.strip() for a in re.split(r"[,;]", self.cfg.email_to) if a.strip()]
        msg = EmailMessage()
        msg["Subject"] = f"{self.source} alert: {title}"
        msg["From"] = self.cfg.email_from or user
        msg["To"] = ", ".join(to)
        msg["Message-ID"] = make_msgid(domain="swingbot.local")
        msg.set_content(message + "\n\n(Sent by swingbot. Alerts: ntfy_topic / alert_email in config.yaml.)")
        if int(self.cfg.smtp_port) == 465:
            server = smtplib.SMTP_SSL(self.cfg.smtp_host, 465, timeout=30)
        else:
            server = smtplib.SMTP(self.cfg.smtp_host, int(self.cfg.smtp_port), timeout=30)
            server.starttls()
        try:
            server.login(user, pw)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode()                # HTTP headers must be plain ASCII


def _strip_numbers(s: str) -> str:
    return re.sub(r"[\d.,:]+", "#", s)[:160]                    # "stop at 175.88" and "at 175.90": one alert


# ------------------------------------------------------------------------------------------ from the log
ALERT, TRADE = "alert", "trade"


class AlertHandler(logging.Handler):
    """Turns the bot's ERROR lines (and warnings marked ``extra={"notify": "alert"}``) into alerts, and with
    notify_trades on, lines marked ``extra={"notify": "trade"}`` into trade pushes."""

    def __init__(self, notifier: Notifier, trades: bool = False):
        super().__init__(level=logging.INFO)
        self.n, self.trades = notifier, trades

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("swingbot.notify"):
            return
        kind = getattr(record, "notify", None)
        try:
            text = record.getMessage()
        except Exception:
            return
        if kind == TRADE:
            if self.trades and self.n.push_on:
                first = text.split(" ", 2)
                self.n.alert(" ".join(first[:2]) if len(first) > 1 else text[:40], text, urgent=False, email=False,
                             key="trade|" + text)
            return
        if record.levelno >= logging.ERROR or kind == ALERT:
            self.n.alert(_title(text), text[:1500], urgent=True)


def _title(text: str) -> str:
    t = re.split(r"[.:\-–(]", text, maxsplit=1)[0].strip()
    return (t[:60] + "…") if len(t) > 60 else (t or "problem")


def install(cfg, source: str = "swingbot") -> Notifier | None:
    """Attach alerts to the bot's log. Returns the notifier (None if no alert channel is set up)."""
    n = Notifier(cfg, source)
    if not n.any_on:
        return None
    logging.getLogger("swingbot").addHandler(AlertHandler(n, trades=bool(getattr(cfg, "notify_trades", False))))
    return n


def main(argv=None) -> None:
    """py -m swingbot.notify  - send a test alert with your settings."""
    from .__main__ import _env
    from .config import Config
    _env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load("config.yaml")
    n = Notifier(cfg)
    if not n.any_on:
        raise SystemExit("No alerts set up: put ntfy_topic (phone) and/or alert_email: true in config.yaml.")
    n.alert("test", "This is a test alert from swingbot. If you can read this, alerts work.", urgent=False,
            key=f"test{time.time()}", wait=True)
    print("Sent a test alert" + (" to your phone (ntfy topic)" if n.push_on else "")
          + (" and by email to " + cfg.email_to if n.email_on else "") + ". Check it arrived.")


if __name__ == "__main__":
    main()
