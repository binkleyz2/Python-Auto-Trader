"""Evening check: what the backtest says should have happened today, next to what the bot really did.

After the session the bot backtests the day on the same stocks it watched, with the settings it used
(including the day's dashboard changes) and the same starting account value, then lines the trades up:
  - the same trade in both: how much worse (or better) the real entry and exit prices were  -> slippage
  - a trade only the bot made, or only the backtest made                                    -> differences
It's saved as logs/<day>-check.json, goes into the day report and the results website, and the log gets a
one-line summary. If real fills keep coming in worse than the backtest assumes (slippage_pct in
config.yaml), backtests will look better than reality - raise slippage_pct to match.

By hand (for today, or a day in the last week):  py -m swingbot.daycheck [YYYY-MM-DD]

What it can't copy exactly: a midday rescan (the backtest watches all of the day's stocks from the open), a
change of bar size during the day, a trade you closed with "Close now", and partial fills.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger("swingbot")


def _mins(hhmm: str) -> int:
    try:
        h, m = str(hhmm)[:5].split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -10_000


def compare(live: list[dict], bt: pd.DataFrame, bar_minutes: int) -> dict:
    """Match the bot's trades with the backtest's (same stock and side, entries within two bars)."""
    btl = bt.to_dict("records") if len(bt) else []
    used, pairs, live_only = set(), [], []
    for t in live:
        side = "short" if int(t.get("side", 1)) < 0 else "long"
        best, gap = None, None
        for i, b in enumerate(btl):
            if i in used or b["symbol"] != t["symbol"] or b.get("side", "long") != side:
                continue
            g = abs(_mins(b["entry_time"]) - _mins(t.get("entry_time", "")))
            if g <= 2 * bar_minutes and (gap is None or g < gap):
                best, gap = i, g
        if best is None:
            live_only.append(t)
            continue
        used.add(best)
        b = btl[best]
        sgn = -1 if side == "short" else 1
        shares = int(t.get("shares") or 0)
        entry_cost = round((float(t["entry"]) - float(b["entry"])) * sgn, 4)     # + = the bot paid more
        exit_cost = round((float(b["exit"]) - float(t["exit"])) * sgn, 4)        # + = the bot got less
        pairs.append(dict(symbol=t["symbol"], side=side, kind=t.get("kind", ""), shares=shares,
                          live_in=t.get("entry_time"), bt_in=b["entry_time"],
                          live_entry=float(t["entry"]), bt_entry=float(b["entry"]), entry_cost=entry_cost,
                          live_exit=float(t["exit"]), bt_exit=float(b["exit"]), exit_cost=exit_cost,
                          live_reason=t.get("reason", ""), bt_reason=b["reason"],
                          live_pnl=round(float(t["pnl"]), 2), bt_pnl=round(float(b["pnl"]), 2),
                          cost_dollars=round((entry_cost + exit_cost) * shares, 2)))
    bt_only = [dict(symbol=b["symbol"], side=b.get("side", "long"), kind=b["kind"], entry_time=b["entry_time"],
                    entry=float(b["entry"]), exit=float(b["exit"]), reason=b["reason"], pnl=round(float(b["pnl"]), 2))
               for i, b in enumerate(btl) if i not in used]
    lo = [dict(symbol=t["symbol"], side="short" if int(t.get("side", 1)) < 0 else "long", kind=t.get("kind", ""),
               entry_time=t.get("entry_time"), entry=float(t["entry"]), exit=float(t["exit"]),
               reason=t.get("reason", ""), pnl=round(float(t["pnl"]), 2)) for t in live_only]
    live_pnl = round(sum(float(t["pnl"]) for t in live), 2)
    bt_pnl = round(float(bt["pnl"].sum()), 2) if len(bt) else 0.0
    per_share = [p["entry_cost"] + p["exit_cost"] for p in pairs]
    pct = [(p["entry_cost"] / p["bt_entry"] + p["exit_cost"] / p["bt_exit"]) * 100 / 2
           for p in pairs if p["bt_entry"] and p["bt_exit"]]
    return dict(live_trades=len(live), bt_trades=len(btl), matched=len(pairs), live_pnl=live_pnl, bt_pnl=bt_pnl,
                gap=round(live_pnl - bt_pnl, 2), slippage_dollars=round(sum(p["cost_dollars"] for p in pairs), 2),
                slippage_per_share=round(sum(per_share) / len(per_share), 4) if per_share else None,
                slippage_pct_per_side=round(sum(pct) / len(pct), 4) if pct else None,
                pairs=pairs, live_only=lo, bt_only=bt_only)


def run(bot, cfg, logs: Path, day: str | None = None, live_trades: list[dict] | None = None,
        watched: list[str] | None = None) -> dict | None:
    """Backtest the day on the stocks the bot watched and compare. Saves logs/<day>-check.json. Never raises."""
    try:
        from . import backtest as bt
        from .live import now_et
        day = day or str(now_et().date())
        live_trades = list(bot.closed) if live_trades is None else live_trades
        watched = list(dict.fromkeys(watched if watched is not None else (getattr(bot, "watched_today", None)
                                                                          or getattr(bot, "watch", None) or [])))
        if not watched:
            return None
        c = dataclasses.replace(bot.cfg, starting_equity=float(getattr(bot, "start_equity", 0) or bot.cfg.starting_equity),
                                bar_minutes=int(getattr(bot, "bm", bot.cfg.bar_minutes)))
        hist = {}
        for sym in watched:
            try:
                df = bot.fetch_bars(sym, "WEEK")
                if df is not None and len(df):
                    hist[sym] = df
            except Exception as e:
                log.debug("check: bars for %s failed (%s)", sym, e)
        if not hist:
            return None
        d0 = date.fromisoformat(day)
        n_days = len(set().union(*[set(pd.DatetimeIndex(d.index).tz_convert("America/New_York").date
                                       if d.index.tz is not None else d.index.date) for d in hist.values()]))
        trades, _, _ = bt.run(hist, c, forced=[s for s in watched if s in hist], start=d0, end=d0,
                              warmup_days=min(3, max(0, n_days - 1)))
        if len(trades):
            trades = trades[trades["date"].astype(str) == day]
        res = compare(live_trades, trades, c.bar_minutes)
        res.update(date=day, watched=list(hist), slippage_setting=c.slippage_pct)
        (logs / f"{day}-check.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        log.info("Evening check: the bot made %d trade(s) for %+.2f; the backtest of the same stocks %d for %+.2f "
                 "(%d the same%s). %s", res["live_trades"], res["live_pnl"], res["bt_trades"], res["bt_pnl"],
                 res["matched"], f", {len(res['live_only'])} only the bot, {len(res['bt_only'])} only the backtest"
                 if res["live_only"] or res["bt_only"] else "", _slip_text(res))
        return res
    except Exception as e:
        log.warning("Evening check couldn't run (%s) - the day's trades are unaffected.", e)
        return None


def _slip_text(res: dict) -> str:
    if res.get("slippage_pct_per_side") is None:
        return ""
    return (f"Real fills vs the backtest: {res['slippage_dollars']:+.2f} in all, about "
            f"{res['slippage_pct_per_side']:+.3f}% a side (the backtest assumes {res.get('slippage_setting')}%).")


def html_section(res: dict) -> str:
    """The check, for the day report (inline styles, for mail programs)."""
    import html
    S = 'style="padding:5px 9px;border-bottom:1px solid #e1e0d9;text-align:{a}"'
    td = lambda v, a="right": f"<td {S.format(a=a)}>{html.escape(str(v))}</td>"
    th = lambda v, a="right": f'<th {S.format(a=a)} bgcolor="#f1f0eb">{html.escape(v)}</th>'
    out = ['<h3 style="margin:14px 0 6px">Live vs backtest</h3>',
           f'<p style="margin:0 0 8px;color:#52514e">The backtest of the same {len(res.get("watched", []))} stocks with '
           f'today\'s settings: {res["bt_trades"]} trade(s), {res["bt_pnl"]:+,.2f}. The bot: {res["live_trades"]} '
           f'trade(s), {res["live_pnl"]:+,.2f}. {res["matched"]} the same. {html.escape(_slip_text(res))}</p>']
    if res["pairs"]:
        out.append('<table style="border-collapse:collapse;margin-bottom:10px;font-size:13px"><tr>'
                   + th("Stock", "left") + th("Entry: bot / test") + th("Exit: bot / test") + th("Exit by: bot / test", "left")
                   + th("P&L: bot / test") + th("Fill cost") + "</tr>")
        for p in res["pairs"]:
            out.append("<tr>" + td(f"{p['symbol']} {p['side']}", "left")
                       + td(f"{p['live_entry']:.2f} / {p['bt_entry']:.2f}") + td(f"{p['live_exit']:.2f} / {p['bt_exit']:.2f}")
                       + td(f"{p['live_reason']} / {p['bt_reason']}", "left")
                       + td(f"{p['live_pnl']:+,.2f} / {p['bt_pnl']:+,.2f}") + td(f"{p['cost_dollars']:+,.2f}") + "</tr>")
        out.append("</table>")
    for name, rows in (("Only the bot", res["live_only"]), ("Only the backtest", res["bt_only"])):
        if rows:
            out.append(f'<p style="margin:4px 0;color:#52514e">{name}: ' + "; ".join(
                html.escape(f"{r['symbol']} {r['side']} {r['kind']} in {r['entry_time']} {r['pnl']:+,.2f} ({r['reason']})")
                for r in rows) + "</p>")
    return "".join(out)


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    from .__main__ import _env
    from .config import Config
    from .report import _data_bot, load_day
    _env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load("config.yaml")
    from .live import now_et
    day = argv[0] if argv else str(now_et().date())
    logs = Path("logs")
    st = load_day(logs, day)
    if st is None:
        sys.exit(f"No logs/{day}-state.json - nothing to check for {day}.")
    bot = _data_bot(cfg, int(st.get("bar_minutes") or cfg.bar_minutes))
    bot.start_equity = st.get("start_equity")
    for k, v in (st.get("overrides") or {}).items():          # the day's dashboard changes
        if k in bot.ADJUSTABLE:
            bot._apply_setting(k, float(v), announce=False)
    watched = st.get("watched_today") or [st.get("symbol")]
    res = run(bot, cfg, logs, day, list(st.get("closed") or []), [w for w in watched if w])
    if res is None:
        sys.exit("The check couldn't run (no bars for the day's stocks - only the last week is available).")
    print(f"Saved logs/{day}-check.json")


if __name__ == "__main__":
    main()
