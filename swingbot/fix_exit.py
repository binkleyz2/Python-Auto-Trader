"""Correct a trade's recorded sale price after the fact.

    py -m swingbot.fix_exit 2026-10-26 BUSE           (Alpaca: looks the real fills up)
    py -m swingbot.fix_exit 2026-10-26 BUSE 30.67     (any broker: the price you were filled at)

Fixes that trade in logs/<date>-state.json, trades.csv, logs/daily-results.csv and the end-of-day
dashboard copy (.html and .jpg), then rebuilds the results spreadsheet. Only the records change; nothing
is sent to the broker.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _alpaca_price(cfg, day: str, sym: str, t_in: str, t_out: str) -> float:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from .alpaca_live import make_alpaca_client
    d = date.fromisoformat(day)
    at = lambda hhmm, extra=0: (datetime(d.year, d.month, d.day, *map(int, hhmm.split(":")), tzinfo=ET)
                                + timedelta(minutes=extra))
    start, end = at(t_in), at(t_out, 10)
    client = make_alpaca_client(cfg)
    try:
        raw = client.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym], nested=True,
                                                         after=start - timedelta(minutes=5), until=end, limit=200))
    finally:
        client.close()
    orders = []
    for o in raw or []:
        orders += [o] + list(getattr(o, "legs", None) or [])
    qty = value = 0.0
    for o in orders:
        side = str(getattr(o.side, "value", o.side)).lower()
        filled_at = getattr(o, "filled_at", None)
        if side == "sell" and float(o.filled_qty or 0) > 0 and o.filled_avg_price is not None \
                and filled_at is not None and start <= filled_at <= end:
            qty += float(o.filled_qty)
            value += float(o.filled_qty) * float(o.filled_avg_price)
    if not qty:
        raise SystemExit(f"Alpaca shows no {sym} sale between {t_in} and {t_out} on {day}. "
                         f"Give the price yourself:  py -m swingbot.fix_exit {day} {sym} PRICE")
    return round(value / qty, 4)


def _fix_trade(t: dict, price: float, point_value: float = 1.0) -> tuple[float, float]:
    old = t["exit"]
    t["exit"] = price
    t["pnl"] = round((price - t["entry"]) * t["shares"] * point_value, 2)
    return old, t["pnl"]


def _reason(tc: Path, day: str, sym: str, old_exit: float, price: float, was: str) -> str:
    """Target, trail or stop for the corrected price, from the order levels in trades.csv."""
    if not tc.exists():
        return was
    with tc.open(newline="", encoding="utf-8") as fh:
        for r in csv.reader(fh):
            if len(r) >= 12 and r[0].startswith(day) and r[2].upper() == sym and r[4] != "entry" and r[10] \
                    and abs(float(r[10]) - float(old_exit)) < 0.01:
                try:
                    init, stop = float(r[7]), float(r[8])
                    target = float(r[9]) if r[9] else None
                except ValueError:
                    return was
                if was in ("end of day", "kill switch"):
                    return was
                if target is not None and price >= target - 0.005:
                    return "target"
                return "trail" if stop > init else "stop"
    return was


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    day, sym = argv[0], argv[1].upper()
    price = float(argv[2]) if len(argv) == 3 else None
    from .config import Config
    cfg = Config.load("config.yaml")
    logs = Path("logs")
    sp = logs / f"{day}-state.json"
    if not sp.exists():
        sys.exit(f"{sp} not found - run this in the bot's folder.")
    st = json.loads(sp.read_text(encoding="utf-8"))
    mine = [t for t in st.get("closed") or [] if str(t.get("symbol", "")).upper() == sym]
    if not mine:
        sys.exit(f"No closed {sym} trade in {sp}.")
    t = mine[-1]
    if price is None:
        if cfg.broker != "alpaca":
            sys.exit(f"Give the price you were filled at:  py -m swingbot.fix_exit {day} {sym} PRICE")
        price = _alpaca_price(cfg, day, sym, t["entry_time"], t["exit_time"])
    pv = 1.0
    if cfg.broker == "ninjatrader" and t.get("entry"):
        pv = (t["pnl"] / ((t["exit"] - t["entry"]) * t["shares"])) if t["exit"] != t["entry"] else 1.0
    old_exit, old_pnl = t["exit"], t["pnl"]
    _fix_trade(t, price, pv)
    t["reason"] = _reason(Path("trades.csv"), day, sym, old_exit, price, t["reason"])
    sp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    print(f"{sym} {day}: exit {old_exit:.2f} -> {price:.2f}, P&L {old_pnl:,.2f} -> {t['pnl']:,.2f}")

    # trades.csv
    tc = Path("trades.csv")
    if tc.exists():
        with tc.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        n = 0
        for r in rows[1:]:
            if len(r) >= 12 and r[0].startswith(day) and r[2].upper() == sym and r[4] != "entry" and r[10] \
                    and abs(float(r[10]) - float(old_exit)) < 0.01:
                r[4], r[10], r[11] = t["reason"], str(round(price, 4)), str(t["pnl"])
                n += 1
        with tc.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        print(f"trades.csv: {n} line(s) corrected")

    # dashboard copy
    html = logs / f"{day}-dashboard.html"
    if html.exists():
        s = html.read_text(encoding="utf-8")
        m = re.search(r"window\.__STATUS__ = (.*?);</script>", s, re.S)
        if m:
            status = json.loads(m.group(1).replace("<\\/", "</"))
            for tr in status.get("trades") or []:
                if str(tr.get("symbol", "")).upper() == sym and tr.get("entry_time") == t["entry_time"]:
                    tr.update(exit=price, pnl=t["pnl"], reason=t["reason"])
            if not status.get("pos"):
                status["day_pnl"] = round(sum(float(x.get("pnl") or 0) for x in status.get("trades") or []), 2)
            data = json.dumps(status).replace("</", "<\\/")
            s = s[:m.start(1)] + data + s[m.end(1):]
            html.write_text(s, encoding="utf-8")
            print(f"{html.name}: corrected")
            if html.with_suffix(".jpg").exists():
                from .screenshot import save_jpg
                save_jpg(html, cfg.dashboard_jpg_browser)

    # results record and spreadsheet
    from . import history as h
    path = logs / h.CSV_NAME
    days = h.read_days(path)
    acct = h.account_label(cfg)
    pnl, n, w, l = h._trade_counts(st.get("closed") or [])
    for d in days:
        if d.date == day and d.account == acct:
            d.bot_pnl, d.trades, d.wins, d.losses = pnl, n, w, l
    if days:
        h.write_days(path, days)
    out = h.update(logs, cfg)
    if out:
        print(f"{out.name}: rebuilt")


if __name__ == "__main__":
    main()
