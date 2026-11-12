"""Results spreadsheet: logs/results-<account>.xlsx, updated at the end of every trading day.

The day's numbers are first added to logs/daily-results.csv (one line per day and account; this file is
the record and is never thinned out). The spreadsheet is then rebuilt from it:

  Summary              starting value, latest value, total change, win rate, best/worst day, a chart
  Last 30 days         one row per trading day in the last 30 calendar days
  Weekly (older)       one row per week (Monday to Friday) for days older than that
  All days             every day, for your own sums and charts

Only days with orders (live, or Alpaca/NinjaTrader paper and demo accounts) are recorded: a dry run has
no account behind it. Days from before this feature are filled in from the logs/<date>-state.json files.

Rebuild it by hand at any time (for example after copying older logs into the logs folder):

    py -m swingbot.history
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, asdict, fields
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("swingbot")

CSV_NAME = "daily-results.csv"
DAILY_DAYS = 30


@dataclass
class Day:
    date: str                 # YYYY-MM-DD
    account: str              # e.g. alpaca-paper
    start_value: float        # account value when the bot started that day
    end_value: float          # account value at the end of the session
    bot_pnl: float            # sum of the bot's closed trades
    trades: int
    wins: int
    losses: int
    note: str = ""


def account_label(cfg) -> str:
    if cfg.broker == "alpaca":
        return "alpaca-paper" if cfg.alpaca_paper else "alpaca-live"
    if cfg.broker == "ninjatrader":
        return f"ninjatrader-{cfg.ninja_env}"
    return "public"


def _broker_name(cfg) -> str:
    return {"alpaca": "Alpaca", "ninjatrader": "NinjaTrader"}.get(cfg.broker, "Public")


# ------------------------------------------------------------------------------------------ the record
def read_days(path: Path) -> list[Day]:
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            try:
                out.append(Day(date=r["date"], account=r["account"], start_value=float(r["start_value"]),
                               end_value=float(r["end_value"]), bot_pnl=float(r["bot_pnl"] or 0),
                               trades=int(r["trades"] or 0), wins=int(r["wins"] or 0),
                               losses=int(r["losses"] or 0), note=r.get("note") or ""))
            except (KeyError, ValueError):
                continue
    return out


def write_days(path: Path, days: list[Day]) -> None:
    days = sorted(days, key=lambda d: (d.account, d.date))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[f.name for f in fields(Day)])
        w.writeheader()
        for d in days:
            w.writerow(asdict(d))
    os.replace(tmp, path)


def upsert(days: list[Day], day: Day) -> list[Day]:
    """Replace that date's line for that account (a restart later the same day updates it)."""
    return [d for d in days if not (d.date == day.date and d.account == day.account)] + [day]


def _trade_counts(closed: list[dict]) -> tuple[float, int, int, int]:
    pnls = [float(t.get("pnl") or 0) for t in closed]
    return round(sum(pnls), 2), len(pnls), sum(p > 0 for p in pnls), sum(p < 0 for p in pnls)


def backfill(days: list[Day], logs: Path, cfg) -> list[Day]:
    """Days from logs/<date>-state.json files that aren't in the record yet (end value estimated)."""
    acct, broker = account_label(cfg), _broker_name(cfg)
    have = {d.date for d in days if d.account == acct}
    states = []
    for f in sorted(logs.glob("*-state.json")):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("live") and st.get("broker") == broker and st.get("start_equity"):
            states.append(st)
    starts = {st["date"]: float(st["start_equity"]) for st in states}
    order = sorted(starts)
    for st in states:
        if st["date"] in have:
            continue
        pnl, n, w, l = _trade_counts(st.get("closed") or [])
        later = [d for d in order if d > st["date"]]
        if later:   # next trading day's starting value = this day's closing value (as near as we can tell)
            end, note = starts[later[0]], "end value = next day's starting value (filled in from the state file)"
        else:
            end, note = float(st["start_equity"]) + pnl, "end value = start + trades (filled in from the state file)"
        days.append(Day(st["date"], acct, float(st["start_equity"]), round(end, 2), pnl, n, w, l, note))
    return days


# ------------------------------------------------------------------------------------------ the sheet
def _weeks(days: list[Day]) -> list[dict]:
    groups: dict[date, list[Day]] = {}
    for d in days:
        dt = date.fromisoformat(d.date)
        groups.setdefault(dt - timedelta(days=dt.weekday()), []).append(d)
    out = []
    for monday in sorted(groups):
        g = sorted(groups[monday], key=lambda d: d.date)
        out.append(dict(week=monday, days=len(g), start=g[0].start_value, end=g[-1].end_value,
                        bot_pnl=round(sum(d.bot_pnl for d in g), 2), trades=sum(d.trades for d in g),
                        wins=sum(d.wins for d in g), losses=sum(d.losses for d in g),
                        note="; ".join(sorted({d.note for d in g if d.note}))))
    return out


def read_trades(path: Path) -> list[dict]:
    """Finished trades from trades.csv (orders sent: live or a paper/demo account - not dry runs)."""
    if not path or not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("event") in (None, "", "entry") or r.get("mode") != "live" or not r.get("exit"):
                continue
            try:
                entry, init, shares, pnl = float(r["entry"]), float(r["initial_stop"]), int(float(r["shares"])), \
                    float(r["pnl"])
            except (KeyError, ValueError):
                continue
            kind = r.get("kind") or ""
            risk = abs(entry - init) * shares
            out.append(dict(when=r["time"][:16].replace("T", " "), symbol=r.get("symbol", ""),
                            setup=kind if kind.startswith("short") else f"long {kind}", shares=shares, entry=entry,
                            initial_stop=init, exit=float(r["exit"]), exit_by=r.get("event", ""), pnl=pnl,
                            r=round(pnl / risk, 3) if risk else 0.0))
    return out


SETUPS = ("long bounce", "long breakout", "short bounce", "short breakdown")


def build_workbook(days: list[Day], account: str, out: Path, today: date | None = None,
                   trades: list[dict] | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    today = today or date.today()
    days = sorted((d for d in days if d.account == account), key=lambda d: d.date)
    cutoff = today - timedelta(days=DAILY_DAYS - 1)
    recent = [d for d in days if date.fromisoformat(d.date) >= cutoff]
    older = [d for d in days if date.fromisoformat(d.date) < cutoff]

    F = "Arial"
    base, bold = Font(name=F, size=10), Font(name=F, size=10, bold=True)
    inp = Font(name=F, size=10, color="0000FF")                 # numbers recorded by the bot
    title = Font(name=F, size=14, bold=True)
    muted = Font(name=F, size=9, italic=True, color="666666")
    head_fill = PatternFill("solid", fgColor="E7E6E0")
    thin = Border(bottom=Side(style="thin", color="BBBBBB"))
    USD = '$#,##0.00;[Red]($#,##0.00);"-"'
    PCT = '0.00%;[Red]-0.00%;"-"'
    INT = '0;-0;"-"'

    wb = Workbook()
    wb.calculation.fullCalcOnLoad = True
    S = wb.active
    S.title = "Summary"
    A, D, W = wb.create_sheet("All days"), wb.create_sheet("Last 30 days"), wb.create_sheet("Weekly (older)")
    wb.move_sheet(A, offset=2)                                   # Summary, Last 30 days, Weekly, All days
    BS, TR = wb.create_sheet("By setup"), wb.create_sheet("Trades")

    START = "Summary!$B$5"                                       # first day's starting value

    def header(ws, row, cols, widths):
        for i, (name, wd) in enumerate(zip(cols, widths), 1):
            c = ws.cell(row=row, column=i, value=name)
            c.font, c.fill, c.border = bold, head_fill, thin
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = wd
        ws.row_dimensions[row].height = 30
        ws.freeze_panes = ws.cell(row=row + 1, column=1)

    def day_rows(ws, rows, first):
        """Daily table from `first`: A date, B day, C start, D end, E chg $, F chg %, G bot P&L, H trades,
        I wins, J losses, K cumulative $, L cumulative %, M note."""
        for i, d in enumerate(rows):
            r = first + i
            dt = date.fromisoformat(d.date)
            vals = [dt, dt.strftime("%a"), d.start_value, d.end_value, f"=D{r}-C{r}",
                    f'=IF(C{r}=0,"",E{r}/C{r})', d.bot_pnl, d.trades, d.wins, d.losses,
                    f"=D{r}-{START}", f'=IF({START}=0,"",K{r}/{START})', d.note]
            fmts = ["yyyy-mm-dd", None, USD, USD, USD, PCT, USD, INT, INT, INT, USD, PCT, None]
            for col, (v, fm) in enumerate(zip(vals, fmts), 1):
                c = ws.cell(row=r, column=col, value=v)
                c.font = inp if col in (3, 4, 7, 8, 9, 10) else (muted if col == 13 else base)
                if fm:
                    c.number_format = fm

    DAY_COLS = ["Date", "Day", "Start value", "End value", "Change $", "Change %", "Bot trades P&L",
                "Trades", "Wins", "Losses", "Total change $ (since first day)", "Total change %", "Note"]
    DAY_W = [12, 6, 14, 14, 12, 10, 14, 8, 7, 8, 18, 12, 60]

    # ---- All days
    header(A, 1, DAY_COLS, DAY_W)
    day_rows(A, days, 2)
    last = max(2, len(days) + 1)
    rng = lambda col: f"'All days'!${col}$2:${col}${last}"

    # ---- Last 30 days
    D["A1"], D["A1"].font = f"Daily results, {cutoff:%b %d} to {today:%b %d, %Y} (last {DAILY_DAYS} days)", title
    D["A2"], D["A2"].font = ("Change = end of the session minus the start that day. Total change = this day's end "
                             "value minus the first day's start (includes overnight moves and any deposits)."), muted
    header(D, 4, DAY_COLS, DAY_W)
    day_rows(D, recent, 5)
    if not recent:
        D["A5"], D["A5"].font = "No trading days in the last 30 days.", muted

    # ---- Weekly (older)
    W["A1"], W["A1"].font = f"Weekly results, days before {cutoff:%b %d, %Y}", title
    W["A2"], W["A2"].font = ("One row per week (Monday to Friday). Start = the first trading day's start, "
                             "end = the last trading day's end."), muted
    WK_COLS = ["Week of", "Days traded", "Start value", "End value", "Change $", "Change %", "Bot trades P&L",
               "Trades", "Wins", "Losses", "Win rate", "Total change $ (since first day)", "Total change %", "Note"]
    header(W, 4, WK_COLS, [12, 8, 14, 14, 12, 10, 14, 8, 7, 8, 9, 18, 12, 50])
    for i, wk in enumerate(_weeks(older)):
        r = 5 + i
        vals = [wk["week"], wk["days"], wk["start"], wk["end"], f"=D{r}-C{r}", f'=IF(C{r}=0,"",E{r}/C{r})',
                wk["bot_pnl"], wk["trades"], wk["wins"], wk["losses"], f'=IF(H{r}=0,"",I{r}/H{r})',
                f"=D{r}-{START}", f'=IF({START}=0,"",L{r}/{START})', wk["note"]]
        fmts = ["yyyy-mm-dd", INT, USD, USD, USD, PCT, USD, INT, INT, INT, "0%", USD, PCT, None]
        for col, (v, fm) in enumerate(zip(vals, fmts), 1):
            c = W.cell(row=r, column=col, value=v)
            c.font = inp if col in (2, 3, 4, 7, 8, 9, 10) else (muted if col == 14 else base)
            if fm:
                c.number_format = fm
    if not older:
        W["A5"], W["A5"].font = "Nothing yet: days move here once they are more than 30 days old.", muted

    # ---- Summary
    S.column_dimensions["A"].width, S.column_dimensions["B"].width = 30, 18
    S["A1"], S["A1"].font = f"swingbot results - {account}", title
    S["A2"], S["A2"].font = (f"Updated {datetime.now():%Y-%m-%d %H:%M}. Blue numbers were recorded by the bot; "
                             "black ones are formulas."), muted
    first_row = f"MATCH(MAX('All days'!$A$2:$A${last}),'All days'!$A$2:$A${last},0)"
    rows = [
        ("First day", f"=IF(COUNT({rng('A')})=0,\"\",MIN({rng('A')}))", "yyyy-mm-dd"),
        ("Starting value", days[0].start_value if days else 0, USD),
        ("Latest value", f"=IF(COUNT({rng('A')})=0,0,INDEX({rng('D')},{first_row}))", USD),
        ("Total change $", "=B6-B5", USD),
        ("Total change %", '=IF(B5=0,"",B7/B5)', PCT),
        ("Bot trades P&L (all days)", f"=SUM({rng('G')})", USD),
        ("Days traded", f"=COUNT({rng('A')})", INT),
        ("Trades", f"=SUM({rng('H')})", INT),
        ("Wins / losses", f'=SUM({rng("I")})&" / "&SUM({rng("J")})', None),
        ("Win rate", f'=IF(B11=0,"",SUM({rng("I")})/B11)', "0%"),
        ("Best day", f'=IF(B10=0,"",MAX({rng("F")}))', PCT),
        ("Worst day", f'=IF(B10=0,"",MIN({rng("F")}))', PCT),
        ("Average day", f'=IF(B10=0,"",AVERAGE({rng("F")}))', PCT),
    ]
    for i, (label, v, fm) in enumerate(rows):
        r = 4 + i
        S.cell(row=r, column=1, value=label).font = bold
        c = S.cell(row=r, column=2, value=v)
        c.font = inp if label == "Starting value" else base
        c.alignment = Alignment(horizontal="right")
        if fm:
            c.number_format = fm
    S["A18"], S["A18"].font = ("Total change uses account values, so it includes positions held overnight and any "
                               "deposits or withdrawals. 'Bot trades P&L' is only the bot's own closed trades."), muted
    if len(days) >= 2:
        ch = LineChart()
        ch.title, ch.y_axis.title, ch.height, ch.width, ch.legend = "Account value at the end of each day", "$", 9, 22, None
        ch.add_data(Reference(A, min_col=4, min_row=1, max_row=last), titles_from_data=True)
        ch.set_categories(Reference(A, min_col=1, min_row=2, max_row=last))
        ch.x_axis.number_format = "mmm d"
        ch.y_axis.numFmt = "$#,##0"
        ch.series[0].smooth = False
        S.add_chart(ch, "A20")

    # ---- Trades and By setup: which of the four setups earn their keep
    trades = trades or []
    TR_COLS = ["When (ET)", "Symbol", "Setup", "Shares", "Entry", "Initial stop", "Exit", "Exit by", "P&L", "R"]
    header(TR, 1, TR_COLS, [17, 9, 16, 8, 10, 11, 10, 9, 12, 8])
    for i, t in enumerate(trades, 2):
        vals = [t["when"], t["symbol"], t["setup"], t["shares"], t["entry"], t["initial_stop"], t["exit"],
                t["exit_by"], t["pnl"], t["r"]]
        fmts = [None, None, None, INT, "0.00", "0.00", "0.00", None, USD, '+0.00"R";[Red]-0.00"R";0.00"R"']
        for col, (v, fm) in enumerate(zip(vals, fmts), 1):
            c = TR.cell(row=i, column=col, value=v)
            c.font = inp if col in (4, 5, 6, 7, 9, 10) else base
            if fm:
                c.number_format = fm
    tl = max(2, len(trades) + 1)
    rngT = lambda col: f"Trades!${col}$2:${col}${tl}"
    BS["A1"], BS["A1"].font = "Results by setup (every trade with orders, all days)", title
    BS["A2"], BS["A2"].font = ("R = profit or loss divided by the trade's initial risk (entry to initial stop x shares). "
                               "Average R above 0 means the setup has paid for its losers."), muted
    header(BS, 4, ["Setup", "Trades", "Wins", "Win rate", "Net P&L", "Average P&L", "Average R"],
           [18, 9, 8, 10, 13, 13, 11])
    for i, name in enumerate(SETUPS, 5):
        BS.cell(row=i, column=1, value=name).font = base
        formulas = [f'=COUNTIF({rngT("C")},A{i})', f'=COUNTIFS({rngT("C")},A{i},{rngT("I")},">0")',
                    f'=IF(B{i}=0,"",C{i}/B{i})', f'=SUMIF({rngT("C")},A{i},{rngT("I")})', f'=IF(B{i}=0,"",E{i}/B{i})',
                    f'=IF(B{i}=0,"",SUMIF({rngT("C")},A{i},{rngT("J")})/B{i})']
        for col, (f, fm) in enumerate(zip(formulas, [INT, INT, "0%", USD, USD, '+0.00"R";[Red]-0.00"R";0.00"R"']), 2):
            c = BS.cell(row=i, column=col, value=f)
            c.number_format = fm
            c.font = base
    i = 5 + len(SETUPS)
    BS.cell(row=i, column=1, value="All").font = bold
    for col, f, fm in ((2, f"=SUM(B5:B{i - 1})", INT), (3, f"=SUM(C5:C{i - 1})", INT), (4, f'=IF(B{i}=0,"",C{i}/B{i})', "0%"),
                       (5, f"=SUM(E5:E{i - 1})", USD), (6, f'=IF(B{i}=0,"",E{i}/B{i})', USD),
                       (7, f'=IF(B{i}=0,"",SUM({rngT("J")})/B{i})', '+0.00"R";[Red]-0.00"R";0.00"R"')):
        c = BS.cell(row=i, column=col, value=f)
        c.number_format, c.font = fm, bold
    if not trades:
        BS.cell(row=i + 2, column=1, value="No finished trades recorded yet.").font = muted

    for ws in (S, D, W, A, BS, TR):
        for row in ws.iter_rows():
            for c in row:
                if c.font is None or c.font.name != F:
                    c.font = Font(name=F, size=c.font.size if c.font else 10, bold=c.font.b if c.font else False)

    tmp = out.with_name(out.stem + ".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)                                         # fails if the file is open in Excel
    return out


# ------------------------------------------------------------------------------------------ hooks
def update(logs: Path, cfg, today_day: Day | None = None) -> Path | None:
    """Add today's line (if any), fill in missing days from state files, rebuild the spreadsheet. Never raises."""
    try:
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / CSV_NAME
        days = read_days(path)
        if today_day:
            days = upsert(days, today_day)
        days = backfill(days, logs, cfg)
        write_days(path, days)
        acct = account_label(cfg)
        if not any(d.account == acct for d in days):
            log.info("Results spreadsheet: no trading days with orders recorded yet for %s.", acct)
            return None
        out = build_workbook(days, acct, logs / f"results-{acct}.xlsx", trades=read_trades(logs.parent / "trades.csv"))
        log.info("Results spreadsheet updated: %s", out)
        return out
    except PermissionError:
        log.warning("Couldn't update the results spreadsheet - is it open in Excel? Close it; the day is saved in "
                    "%s and will be added next time (or run: py -m swingbot.history).", logs / CSV_NAME)
    except Exception as e:
        log.warning("Couldn't update the results spreadsheet (%s). The day is still in the log.", e)
    return None


def record_session(bot, cfg, logs: Path) -> Path | None:
    """End-of-day hook: record the day the bot traded (orders sent), then rebuild the spreadsheet."""
    if not (bot.live and bot.prepared and bot.start_equity):
        return None
    if bot.last_bar_seen is None and not bot.trades_today:
        return None                                              # stopped before the market opened
    try:
        end = float(bot.account_equity())
    except Exception as e:
        log.warning("Results spreadsheet: couldn't read the account value (%s); trying again next run.", e)
        return update(logs, cfg)
    pnl, n, w, l = _trade_counts(bot.closed)
    note = ""
    if getattr(bot, "not_flat", None):
        note = f"{bot.not_flat['shares']} {bot.not_flat['symbol']} still held at the close"
    from .live import now_et
    day = Day(str(getattr(bot, "day", None) or now_et().date()), account_label(cfg),
              round(float(bot.start_equity), 2), round(end, 2), pnl, n, w, l, note)
    return update(logs, cfg, day)


if __name__ == "__main__":
    import sys
    from .config import Config
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        cfg = Config.load(cfg_path)
    except Exception as e:
        sys.exit(f"Problem in {cfg_path}: {e}")
    if not update(Path("logs"), cfg):
        sys.exit(1)
