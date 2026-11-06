"""Which settings were in force each trading day: logs/settings-history.jsonl (one line per day).

Each line: the day, the account, the trading settings from config.yaml, and anything changed on the
dashboard for that day only (bar size, trail distance, breakeven, profit target). The results website groups
the days into periods with the same settings and shows how each period did, so a change can be judged.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

log = logging.getLogger("swingbot")
FILE = "settings-history.jsonl"

# not trading settings: display, mail, alerts, files, backtest-only
SKIP_PREFIX = ("dashboard", "email", "smtp", "site_", "ntfy", "alert_", "notify_", "update_")
SKIP = {"results_spreadsheet", "backtest_results_dir", "scan_workers", "starting_equity", "watchlist",
        "dashboard_jpg", "dashboard_jpg_browser"}


def trading_settings(cfg) -> dict:
    out = {}
    for k, v in dataclasses.asdict(cfg).items():
        if k in SKIP or k.startswith(SKIP_PREFIX):
            continue
        out[k] = list(v) if isinstance(v, (list, tuple)) else v
    wl = getattr(cfg, "watchlist", None) or []
    out["watchlist"] = f"{len(wl)} stocks" if len(wl) > 12 else list(wl)
    return out


def record(bot, cfg, logs: Path) -> Path | None:
    """Add (or replace) today's line. Never raises."""
    try:
        if not (bot.prepared and (bot.last_bar_seen is not None or bot.trades_today)):
            return None
        from .history import account_label
        from .live import now_et
        base = getattr(bot, "cfg0", None) or cfg
        today_only = {k: v for k, v in (getattr(bot, "overrides", None) or {}).items()}
        if getattr(bot, "bm", None) and int(bot.bm) != int(base.bar_minutes):
            today_only["bar_minutes"] = int(bot.bm)
        if getattr(bot, "forced_symbol", None):
            today_only["symbol"] = bot.forced_symbol
        rec = dict(date=str(now_et().date()), account=account_label(cfg), live=bool(bot.live),
                   settings=trading_settings(base), today_only=today_only)
        path = logs / FILE
        lines = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not (r.get("date") == rec["date"] and r.get("account") == rec["account"]):
                    lines.append(line)
        lines.append(json.dumps(rec, sort_keys=True, default=str))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except Exception as e:
        log.warning("Couldn't record today's settings (%s)", e)
        return None


def read(logs: Path) -> list[dict]:
    path = logs / FILE
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _fmt(v) -> str:
    if v is None or v == "":
        return "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def changes(old: dict, new: dict) -> list[str]:
    return [f"{k} {_fmt(old.get(k))} → {_fmt(new.get(k))}" for k in sorted(set(old) | set(new))
            if old.get(k) != new.get(k)]


def periods(records: list[dict], days: list[dict], account: str) -> list[dict]:
    """Group the account's trading days into runs with the same config.yaml settings, with how each did."""
    by_day = {r["date"]: r for r in records if r.get("account") == account}
    groups: list[dict] = []
    for d in days:
        r = by_day.get(d["date"])
        settings = r["settings"] if r else None
        if not groups or groups[-1]["_s"] != settings:
            groups.append(dict(_s=settings, days=[]))
        g = groups[-1]
        g["days"].append(d)
    out, prev = [], None
    for g in groups:
        ds = g["days"]
        tr = sum(d["trades"] for d in ds)
        wins = sum(d["wins"] for d in ds)
        pnl = round(sum(d["bot_pnl"] for d in ds), 2)
        if g["_s"] is None:
            what = ["(before settings were recorded)"]
        elif prev is None:
            what = ["(first recorded settings)"]
        else:
            what = changes(prev, g["_s"]) or ["(no change)"]
        today_only = [f"{d['date']}: " + ", ".join(f"{k} {_fmt(v)}" for k, v in by_day[d["date"]]["today_only"].items())
                      for d in ds if d["date"] in by_day and by_day[d["date"]].get("today_only")]
        out.append(dict(start=ds[0]["date"], end=ds[-1]["date"], days=len(ds), bot_pnl=pnl,
                        per_day=round(pnl / len(ds), 2), trades=tr, win_pct=round(100 * wins / tr, 1) if tr else 0.0,
                        changes=what, today_only=today_only, recorded=g["_s"] is not None))
        if g["_s"] is not None:
            prev = g["_s"]
    return out
