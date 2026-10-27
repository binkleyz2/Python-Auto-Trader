"""End-of-day report: the day's results, every trade, and a chart per traded symbol, by email.

Turned on with ``email_report: true`` in config.yaml. The mail is sent after the session (after the results
spreadsheet and the dashboard picture, which are attached). The same report is always saved in the logs
folder as logs/<date>-report.html with the charts next to it (logs/<date>-chart-<SYMBOL>.png), so you can
open it even if the mail can't be sent.

Mail goes out through an SMTP server, Gmail by default. Put the login in .env (never in config.yaml):

    SMTP_USER=you@gmail.com
    SMTP_PASSWORD=abcd efgh ijkl mnop      (a Gmail "app password", not your normal password)

Make or resend a report by hand (in the bot's folder):

    py -m swingbot.report                 today's report: save it and show where
    py -m swingbot.report 2026-10-27      another day (within the last week: the charts need its bars)
    py -m swingbot.report 2026-10-27 --send
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

log = logging.getLogger("swingbot")


# ------------------------------------------------------------------------------------------ data
def _minutes(ts: str | datetime, day: date) -> float | None:
    """Minutes after 9:30 on ``day`` (None if another day)."""
    import pandas as pd
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("America/New_York")
    if t.date() != day:
        return None
    return (t.hour * 60 + t.minute - 570) + t.second / 60


def _clock(hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m - 570


def load_day(logs: Path, day: str) -> dict | None:
    p = logs / f"{day}-state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("report: can't read %s (%s)", p.name, e)
        return None


def log_problems(logs: Path, day: str, limit: int = 12) -> tuple[int, int, list[str]]:
    """(warnings, errors, the error lines) from the day's log."""
    p = logs / f"{day}.log"
    if not p.exists():
        return 0, 0, []
    warn = err = 0
    lines = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if " WARNING " in line:
            warn += 1
        elif " ERROR " in line or " CRITICAL" in line:
            err += 1
            if len(lines) < limit:
                lines.append(line[:300])
    return warn, err, lines


# ------------------------------------------------------------------------------------------ charts
def chart_png(sym: str, bars, trades: list[dict], day: date, bm: int, path: Path) -> Path:
    """Today's bars for ``sym`` with each trade: entry, exit, initial stop, the stop as it moved, the line."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    ink, muted, grid = "#2b2b2b", "#898781", "#e1e0d9"
    entry_c, stop_c, good, bad = "#2a78d6", "#d03b3b", "#0ca30c", "#d03b3b"
    sup_c, res_c = "#1baf7a", "#eb6834"
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=100)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fcfcfb")
    lows, highs = [], []
    for ts, r in bars.iterrows():
        x = _minutes(ts, day)
        if x is None or x < 0 or x >= 390:
            continue
        xc = x + bm / 2
        ax.vlines(xc, r.low, r.high, color=ink, linewidth=0.8, zorder=2)
        lo, hi = sorted((r.open, r.close))
        up = r.close >= r.open
        ax.add_patch(Rectangle((xc - bm * 0.35, lo), bm * 0.7, max(hi - lo, 1e-9), facecolor="white" if up else ink,
                               edgecolor=ink, linewidth=0.8, zorder=3))
        lows.append(r.low)
        highs.append(r.high)
    ys = lows + highs
    pnl_total = 0.0
    for t in trades:
        side = int(t.get("side", 1))
        xe, xx = _clock(t["entry_time"]), _clock(t["exit_time"])
        pnl_total += float(t["pnl"])
        L = t.get("line")
        if L and L.get("pts"):
            (ta, va), (tb, vb) = L["pts"]
            xa, xb = _minutes(ta, day), _minutes(tb, day)
            if xa is not None and xb is not None:
                ax.plot([xa + bm / 2, xb + bm / 2], [va, vb], color=sup_c if L["kind"] == "support" else res_c,
                        linewidth=1.6, zorder=4)
                ys += [va, vb]
        if t.get("init_stop") is not None:
            ax.hlines(t["init_stop"], xe, xx, colors=muted, linestyles=(0, (4, 3)), linewidth=1, zorder=4)
            ys.append(t["init_stop"])
        steps = t.get("stop_steps") or []
        if steps:
            px, py = [], []
            for i, (sx, sy) in enumerate(steps):
                x0 = max(xe, sx * bm)
                if i:
                    px.append(x0)
                    py.append(py[-1])
                px.append(x0)
                py.append(sy)
            px.append(xx)
            py.append(py[-1])
            ax.plot(px, py, color=stop_c, linewidth=1.8, zorder=5)
            ys += py
        if t.get("target"):
            ax.hlines(t["target"], xe, xx, colors=good, linestyles=":", linewidth=1.2, zorder=4)
            ys.append(t["target"])
        c = good if t["pnl"] >= 0 else bad
        ax.plot([xe, xx], [t["entry"], t["exit"]], color=c, linestyle=(0, (3, 3)), linewidth=1.3, zorder=6)
        ax.plot(xe, t["entry"], marker="^" if side > 0 else "v", color=entry_c, markersize=9, zorder=7)
        ax.plot(xx, t["exit"], marker="o", color=c, markersize=7, zorder=7)
        ax.annotate(f"{'Short' if side < 0 else 'Buy'} {t['entry']:.2f}", (xe, t["entry"]), textcoords="offset points",
                    xytext=(-6, 10 if side > 0 else -16), ha="right", fontsize=8, color=entry_c,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85), zorder=8)
        ax.annotate(f"{t['reason']} {t['exit']:.2f}  {t['pnl']:+,.2f}", (xx, t["exit"]), textcoords="offset points",
                    xytext=(6, -14 if side > 0 else 8), fontsize=8, color=c,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85), zorder=8)
        ys += [t["entry"], t["exit"]]
    if ys:
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.08 or 0.5
        ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(0, 390)
    ax.set_xticks(range(0, 391, 60))
    ax.set_xticklabels(["9:30", "10:30", "11:30", "12:30", "1:30", "2:30", "3:30"])
    ax.axvline(_clock("15:50"), color=muted, linewidth=0.8, linestyle=":")
    ax.grid(axis="y", color=grid, linewidth=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8, colors="#52514e")
    ax.yaxis.tick_right()
    n = len(trades)
    ax.set_title(f"{sym}  ·  {n} trade{'s' if n != 1 else ''}  ·  P&L {pnl_total:+,.2f}", loc="left",
                 fontsize=11, fontweight="bold", color=ink)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)
    return path


# ------------------------------------------------------------------------------------------ the report
def _money(x) -> str:
    return "–" if x is None else f"{'+' if x > 0 else '−' if x < 0 else ''}${abs(x):,.2f}"


def _pct(x) -> str:
    return "–" if x is None else f"{x:+.2%}"


def build(logs: Path, cfg, day: str, bars_for=None, extra: dict | None = None) -> dict | None:
    """Collect the day and write logs/<day>-report.html (+ chart PNGs). Returns what the mail needs."""
    from . import history as h
    st = load_day(logs, day)
    if st is None:
        return None
    extra = extra or {}
    d = date.fromisoformat(day)
    closed = list(st.get("closed") or [])
    bm = int(st.get("bar_minutes") or cfg.bar_minutes)
    acct = h.account_label(cfg)
    rows = [r for r in h.read_days(logs / h.CSV_NAME) if r.account == acct]
    row = next((r for r in rows if r.date == day), None)
    first = min(rows, key=lambda r: r.date) if rows else None
    mode = "LIVE" if st.get("live") else "DRY RUN"
    start = row.start_value if row else st.get("start_equity")
    end = row.end_value if row else None
    pnl_trades = round(sum(float(t.get("pnl") or 0) for t in closed), 2)
    change = (end - start) if (end is not None and start) else None
    total = (end - first.start_value) if (end is not None and first) else None

    charts = {}
    syms = list(dict.fromkeys(t["symbol"] for t in closed))
    if syms and bars_for is not None:
        for sym in syms:
            try:
                bars = bars_for(sym)
                idx = bars.index.tz_convert("America/New_York") if bars.index.tz is not None else bars.index
                bars = bars[idx.date == d]
                if len(bars):
                    charts[sym] = chart_png(sym, bars, [t for t in closed if t["symbol"] == sym], d, bm,
                                            logs / f"{day}-chart-{sym}.png")
            except ImportError:
                log.warning("report: charts need matplotlib - run  py -m pip install --user -r requirements.txt")
                break
            except Exception as e:
                log.warning("report: chart for %s failed (%s)", sym, e)

    warn, err, err_lines = log_problems(logs, day)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    subject = (f"swingbot {day}: {_money(pnl_trades) if closed else 'no trades'}"
               + (f", {len(closed)} trade{'s' if len(closed) != 1 else ''}" if closed else "")
               + (f" · account {_money(change)}" if change is not None else "")
               + (" · DRY RUN" if mode != "LIVE" else "")
               + (" · ERRORS" if err else ""))

    # ---- HTML (inline styles: mail programs ignore style sheets)
    S = 'style="padding:6px 10px;border-bottom:1px solid #e1e0d9;text-align:{a};{x}"'
    td = lambda v, a="left", x="": f"<td {S.format(a=a, x=x)}>{v}</td>"
    th = lambda v, a="left": f"<th {S.format(a=a, x='background:#f1f0eb;font-weight:600;')}>{v}</th>"
    tone = lambda v: "color:#006300;" if v and v > 0 else ("color:#b42d2d;" if v and v < 0 else "")
    alerts = []
    if extra.get("not_flat"):
        nf = extra["not_flat"]
        alerts.append(f"{nf['symbol']}: {nf['shares']} shares are STILL OPEN with no stop. Close them in your broker's app.")
    if extra.get("leftover"):
        lo_ = extra["leftover"]
        alerts.append(f"{lo_['symbol']}: {lo_['shares']} shares from {lo_['date']} are still open.")
    if err:
        alerts.append(f"{err} error{'s' if err != 1 else ''} in the log (listed at the bottom).")
    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#0b0b0b;max-width:1040px">',
             f'<h2 style="margin:0 0 4px">swingbot · {d:%A %B} {d.day}, {d.year}</h2>',
             f'<div style="color:#52514e;margin-bottom:12px">{mode} · {html.escape(acct)} · '
             f'{html.escape(cfg.direction)} · {bm}-minute bars</div>']
    for a in alerts:
        parts.append(f'<div style="background:#fdecec;border:1px solid #e9b4b4;padding:8px 12px;border-radius:6px;'
                     f'margin-bottom:10px">⚠ {html.escape(a)}</div>')
    summary = [("Trades", f"{len(closed)} of {cfg.max_trades_per_day}" + (f" · {wins} won" if closed else "")),
               ("Bot trades P&L", f'<span style="{tone(pnl_trades)}">{_money(pnl_trades)}</span>'),
               ("Account at start", f"${start:,.2f}" if start else "–"),
               ("Account at end", f"${end:,.2f}" if end is not None else "–"),
               ("Day change", f'<span style="{tone(change)}">{_money(change)} '
                              f'({_pct(change / start) if change is not None and start else "–"})</span>'),
               ("Since the first day", f'<span style="{tone(total)}">{_money(total)} '
                                       f'({_pct(total / first.start_value) if total is not None and first else "–"})'
                                       f'</span>' + (f' <span style="color:#898781">since {first.date}</span>' if first else ""))]
    parts.append('<table style="border-collapse:collapse;margin-bottom:16px">'
                 + "".join(f"<tr>{td(k, x='color:#52514e;')}{td(v)}</tr>" for k, v in summary) + "</table>")
    if closed:
        hdr = ["Symbol", "Side", "Type", "Shares", "In", "Entry", "Initial stop", "Out", "Exit", "Exit by",
               "Final stop", "P&L", "R"]
        parts.append('<h3 style="margin:8px 0">Trades</h3><table style="border-collapse:collapse;margin-bottom:16px">'
                     "<tr>" + "".join(th(x, "right" if i >= 3 else "left") for i, x in enumerate(hdr)) + "</tr>")
        for t in closed:
            side = int(t.get("side", 1))
            risk = abs(t["entry"] - t["init_stop"]) * t["shares"] if t.get("init_stop") is not None else 0
            r = t["pnl"] / risk if risk else None
            parts.append("<tr>" + td(html.escape(t["symbol"])) + td("short" if side < 0 else "long")
                         + td(html.escape(t["kind"])) + td(t["shares"], "right") + td(t["entry_time"], "right")
                         + td(f"{t['entry']:.2f}", "right")
                         + td(f"{t['init_stop']:.2f}" if t.get("init_stop") is not None else "–", "right")
                         + td(t["exit_time"], "right") + td(f"{t['exit']:.2f}", "right") + td(html.escape(t["reason"]), "right")
                         + td(f"{t['final_stop']:.2f}" if t.get("final_stop") is not None else "–", "right")
                         + td(_money(t["pnl"]), "right", tone(t["pnl"]))
                         + td(f"{r:+.2f}R" if r is not None else "–", "right") + "</tr>")
        parts.append("</table>")
    else:
        parts.append('<p style="color:#52514e">No trades today.</p>')
    for sym, path in charts.items():
        parts.append(f'<div style="margin:10px 0"><img src="cid:{sym}" alt="{html.escape(sym)} chart" '
                     f'style="max-width:100%;border:1px solid #e1e0d9;border-radius:6px"></div>')
    if extra.get("jpg"):
        parts.append('<p style="color:#52514e">The whole end-of-day dashboard is attached as a picture'
                     + (", and the results spreadsheet too." if extra.get("xlsx") else ".") + "</p>")
    if err_lines:
        parts.append('<h3 style="margin:8px 0">Errors in the log</h3><pre style="font-size:12px;background:#f6f6f3;'
                     'padding:8px;white-space:pre-wrap">' + html.escape("\n".join(err_lines)) + "</pre>")
    if warn:
        parts.append(f'<p style="color:#898781;font-size:12px">{warn} warning{"s" if warn != 1 else ""} in the log.</p>')
    parts.append("</div>")
    body = "".join(parts)

    # the saved copy shows the charts from the files next to it
    saved = body
    for sym, path in charts.items():
        saved = saved.replace(f'src="cid:{sym}"', f'src="{path.name}"')
    out = logs / f"{day}-report.html"
    out.write_text(f"<!doctype html><meta charset='utf-8'><title>swingbot {day}</title><body>{saved}</body>",
                   encoding="utf-8")
    return dict(subject=subject, html=body, charts=charts, path=out,
                text=f"{subject}\n\nOpen the HTML version of this mail to see the trades and charts.")


# ------------------------------------------------------------------------------------------ mail
def send(cfg, rep: dict, attachments: list[Path] = ()) -> None:
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    if not user or not pw:
        raise RuntimeError("set SMTP_USER and SMTP_PASSWORD in .env")
    to = [a.strip() for a in re.split(r"[,;]", cfg.email_to) if a.strip()]
    if not to:
        raise RuntimeError("email_to is empty in config.yaml")
    msg = EmailMessage()
    msg["Subject"] = rep["subject"]
    msg["From"] = cfg.email_from or user
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid(domain="swingbot.local")
    msg.set_content(rep["text"])
    msg.add_alternative(rep["html"], subtype="html")
    part = msg.get_payload()[1]
    for sym, path in rep["charts"].items():
        part.add_related(Path(path).read_bytes(), "image", "png", cid=f"<{sym}>", filename=Path(path).name)
    for a in attachments:
        a = Path(a)
        if a.exists():
            ctype = mimetypes.guess_type(a.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(a.read_bytes(), maintype=maintype, subtype=subtype, filename=a.name)
    if int(cfg.smtp_port) == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, 465, timeout=60)
    else:
        server = smtplib.SMTP(cfg.smtp_host, int(cfg.smtp_port), timeout=60)
        server.starttls()
    try:
        server.login(user, pw)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def end_of_day(bot, cfg, logs: Path) -> Path | None:
    """After the session: save the report and, with email_report on, mail it. Never raises."""
    try:
        if not (bot.prepared and (bot.last_bar_seen is not None or bot.trades_today)):
            return None                                       # never got to the open (weekend, holiday, error)
        from .live import now_et
        day = str(now_et().date())
        bot.save_state()

        def bars_for(sym):
            return bot.fetch_bars(sym, "DAY")
        jpg, xlsx = logs / f"{day}-dashboard.jpg", None
        from . import history as h
        xl = logs / f"results-{h.account_label(cfg)}.xlsx"
        if cfg.results_spreadsheet and xl.exists():
            xlsx = xl
        rep = build(logs, cfg, day, bars_for, dict(not_flat=bot.not_flat, leftover=bot.leftover,
                                                    jpg=jpg if jpg.exists() else None, xlsx=xlsx))
        if rep is None:
            return None
        log.info("Day report saved: %s", rep["path"])
        if cfg.email_report:
            send(cfg, rep, [p for p in (jpg, xlsx) if p is not None and p.exists()])
            log.info("Day report emailed to %s", cfg.email_to)
        return rep["path"]
    except Exception as e:
        log.warning("Couldn't %s the day report (%s). The trades are in the log and trades.csv.",
                    "send" if cfg.email_report else "make", e)
        return None


# ------------------------------------------------------------------------------------------ by hand
def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    do_send = "--send" in argv
    argv = [a for a in argv if a != "--send"]
    from .__main__ import _env
    _env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from .config import Config
    cfg = Config.load("config.yaml")
    from .live import now_et
    day = argv[0] if argv else str(now_et().date())
    logs = Path("logs")
    st = load_day(logs, day)
    if st is None:
        sys.exit(f"No logs/{day}-state.json - nothing to report for {day}.")
    bot = _data_bot(cfg, int(st.get("bar_minutes") or cfg.bar_minutes))
    try:
        jpg = logs / f"{day}-dashboard.jpg"
        from . import history as h
        xl = logs / f"results-{h.account_label(cfg)}.xlsx"
        rep = build(logs, cfg, day, lambda s: bot.fetch_bars(s, "WEEK"),
                    dict(jpg=jpg if jpg.exists() else None, xlsx=xl if xl.exists() else None))
    finally:
        try:
            bot.client.close()
        except Exception:
            pass
    print(f"Saved {rep['path']}")
    if do_send:
        send(cfg, rep, [p for p in (jpg, xl) if p.exists()])
        print(f"Sent to {cfg.email_to}")


def _data_bot(cfg, bm: int):
    """A bot that only reads prices (no orders), on the configured broker."""
    workdir = Path.cwd()
    if cfg.broker == "ninjatrader":
        from .ninja_live import NinjaBot, make_ninja_client
        bot = NinjaBot(make_ninja_client(cfg, workdir), cfg, live=False, workdir=workdir)
    elif cfg.broker == "alpaca":
        from .alpaca_live import AlpacaBot, make_alpaca_client
        bot = AlpacaBot(make_alpaca_client(cfg), cfg, live=False, workdir=workdir)
    else:
        from .live import Bot, make_client
        bot = Bot(make_client(), cfg, live=False, workdir=workdir)
    bot.bm = bm
    return bot


if __name__ == "__main__":
    main()
