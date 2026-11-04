"""Excel versions of backtest results, with line charts of the account value.

``run_workbook``: one backtest (Summary with the chart and the breakdown tables, Equity by day, Trades).
``compare_workbook``: a --try comparison (the side-by-side table with every combination's account value
on one chart, Equity by day for each combination, and all trades with the settings that made them).
The CSV files are still written next to them for anyone without Excel.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

F = "Arial"
USD = '$#,##0.00;[Red]-$#,##0.00'
PRICE = "0.00"
PCT = '0.00"%";[Red]-0.00"%"'
CHART_LINES = 10                       # more lines than this and a chart can't be read; the best ones are drawn

# how each trade column is shown
TRADE_COLS = [("date", "Date", 11, None), ("symbol", "Symbol", 9, None), ("side", "Side", 7, None),
              ("kind", "Setup", 11, None), ("scan", "Scan", 9, None), ("rank", "Rank", 6, "0"),
              ("entry_time", "Entry", 8, None), ("entry", "Entry price", 11, PRICE),
              ("stop", "Initial stop", 11, PRICE), ("final_stop", "Final stop", 11, PRICE),
              ("target", "Target", 10, PRICE), ("shares", "Shares", 8, "#,##0"), ("exit_time", "Exit", 8, None),
              ("exit", "Exit price", 11, PRICE), ("reason", "Exit by", 9, None), ("pnl", "P&L", 12, USD),
              ("r_multiple", "R", 7, '+0.00;[Red]-0.00')]


def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    return dict(base=Font(name=F, size=10), bold=Font(name=F, size=10, bold=True),
                title=Font(name=F, size=14, bold=True), muted=Font(name=F, size=9, italic=True, color="666666"),
                fill=PatternFill("solid", fgColor="E7E6E0"), line=Border(bottom=Side(style="thin", color="BBBBBB")),
                center=Alignment(horizontal="center", vertical="center", wrap_text=True))


def _header(ws, row: int, cols: list[tuple[str, float]], st, freeze: bool = True) -> None:
    from openpyxl.utils import get_column_letter
    for i, (name, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=name)
        c.font, c.fill, c.border, c.alignment = st["bold"], st["fill"], st["line"], st["center"]
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[row].height = 30
    if freeze:                     # (setting it and then clearing it leaves a broken view that Excel "repairs")
        ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _cell(ws, row, col, value, fmt=None, font=None):
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        value = None if value != value else ("inf" if value > 0 else "-inf")   # Excel has no NaN or infinity
    c = ws.cell(row=row, column=col, value=value)
    if font is not None:
        c.font = font
    if fmt:
        c.number_format = fmt
    return c


def _trades_sheet(ws, trades: pd.DataFrame, st, settings_col: str | None = None) -> None:
    cols = [c for c in TRADE_COLS if c[0] in trades.columns]
    lead = [(settings_col, "Settings", 34, None)] if settings_col else []
    cols = lead + cols
    _header(ws, 1, [(label, w) for _, label, w, _ in cols], st)
    for r, d in enumerate(trades.to_dict("records"), 2):
        for c, (key, _, _, fmt) in enumerate(cols, 1):
            _cell(ws, r, c, d.get(key), fmt, st["base"])
    if len(trades):
        ws.auto_filter.ref = ws.dimensions


def _equity_chart(ws, data_ws, first_row: int, last_row: int, cols: list[int], title: str):
    """Line chart of columns ``cols`` (one line each, header in ``first_row - 1``) against the dates in column A."""
    from openpyxl.chart import LineChart, Reference
    ch = LineChart()
    ch.title, ch.y_axis.title, ch.x_axis.title = title, "Account value", None
    ch.height, ch.width = 10, 26
    for col in cols:
        ch.add_data(Reference(data_ws, min_col=col, min_row=first_row - 1, max_row=last_row), titles_from_data=True)
    ch.set_categories(Reference(data_ws, min_col=1, min_row=first_row, max_row=last_row))
    ch.x_axis.number_format = "mmm d"
    ch.y_axis.numFmt = "$#,##0"
    ch.x_axis.delete = ch.y_axis.delete = False                     # newer Excel hides axes unless told
    for s in ch.series:
        s.smooth = False
        s.graphicalProperties.line.width = 19050                    # 1.5 pt
    if len(cols) == 1:
        ch.legend = None
    else:
        ch.legend.position = "b"
    return ch


def run_workbook(path: Path, trades: pd.DataFrame, curve: pd.Series, cfg, title: str) -> Path:
    from openpyxl import Workbook
    from . import backtest as bt
    st = _styles()
    wb = Workbook()
    S = wb.active
    S.title = "Summary"
    E, T = wb.create_sheet("Equity"), wb.create_sheet("Trades")

    # Equity: one row per day
    _header(E, 1, [("Date", 12), ("Account value", 15), ("Change $", 12), ("Drawdown %", 12)], st)
    peak = curve.cummax() if len(curve) else curve
    prev = cfg.starting_equity
    for r, (day, eq) in enumerate(curve.items(), 2):
        _cell(E, r, 1, pd.Timestamp(day).to_pydatetime(), "yyyy-mm-dd", st["base"])
        _cell(E, r, 2, float(eq), USD, st["base"])
        _cell(E, r, 3, float(eq) - prev, USD, st["base"])
        _cell(E, r, 4, 100 * (float(eq) / float(peak.loc[day]) - 1), PCT, st["base"])
        prev = float(eq)
    last = len(curve) + 1

    # Summary: totals, the chart, then the breakdown tables
    S.column_dimensions["A"].width, S.column_dimensions["B"].width = 26, 16
    _cell(S, 1, 1, title or "Backtest", font=st["title"])
    _cell(S, 2, 1, f"Starting with ${cfg.starting_equity:,.0f}; slippage {cfg.slippage_pct}% a side.", font=st["muted"])
    s = bt.summarize(trades, curve, cfg)
    rows = [("Days", s.get("days", len(curve)), "0"), ("Trades", s.get("trades", 0), "0"),
            ("Win rate", s.get("win_rate_pct"), PCT), ("Average R", s.get("avg_r"), "+0.00;[Red]-0.00"),
            ("Profit factor", s.get("profit_factor"), "0.00"), ("Net", s.get("net_pnl"), USD),
            ("Return", s.get("return_pct"), PCT), ("Largest drawdown", s.get("max_drawdown_pct"), PCT),
            ("Final account value", float(curve.iloc[-1]) if len(curve) else cfg.starting_equity, USD)]
    for i, (label, v, fm) in enumerate(rows, 4):
        _cell(S, i, 1, label, font=st["bold"])
        _cell(S, i, 2, v, fm, st["base"])
    row = 4 + len(rows) + 1
    if len(curve) >= 2:
        S.add_chart(_equity_chart(S, E, 2, last, [2], "Account value at the end of each day"), "D4")
        row = max(row, 26)
    for name, df in bt.breakdown(trades).items():
        _cell(S, row, 1, name, font=st["bold"])
        heads = [(str(df.index.name or ""), 26), ("Trades", 16), ("Win %", 10), ("Avg R", 10), ("Net", 14)]
        for c, (h, _) in enumerate(heads, 1):
            x = _cell(S, row + 1, c, h, font=st["bold"])
            x.fill, x.border = st["fill"], st["line"]
        for r, (key, rec) in enumerate(df.iterrows(), row + 2):
            _cell(S, r, 1, str(key), font=st["base"])
            for c, (col, fm) in enumerate([("trades", "0"), ("win_pct", PCT), ("avg_r", "+0.00;[Red]-0.00"),
                                           ("net", USD)], 2):
                _cell(S, r, c, rec.get(col), fm, st["base"])
        row += len(df) + 3
    for col, w in zip("CDE", (10, 10, 14)):
        S.column_dimensions[col].width = max(S.column_dimensions[col].width or 0, w)

    _trades_sheet(T, trades, st)
    wb.save(path)
    return path


def compare_workbook(path: Path, table: pd.DataFrame, curves: dict[str, pd.Series],
                     trades: dict[str, pd.DataFrame], cfg) -> Path:
    """``table``: one row per combination (index = settings label); ``curves``/``trades`` by the same label."""
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill
    st = _styles()
    wb = Workbook()
    C = wb.active
    C.title = "Compare"
    E, T = wb.create_sheet("Equity"), wb.create_sheet("Trades")
    labels = list(table.index)

    # Equity: dates down, one column per combination
    days = sorted(set().union(*[set(c.index) for c in curves.values()])) if curves else []
    _header(E, 1, [("Date", 12)] + [(lab, max(14, min(34, len(lab) * 0.9))) for lab in labels], st)
    for r, day in enumerate(days, 2):
        _cell(E, r, 1, pd.Timestamp(day).to_pydatetime(), "yyyy-mm-dd", st["base"])
        for c, lab in enumerate(labels, 2):
            cv = curves.get(lab)
            if cv is not None and day in cv.index:
                _cell(E, r, c, float(cv.loc[day]), USD, st["base"])
    last = len(days) + 1

    # Compare: the table, best net highlighted, then the chart under it
    _cell(C, 1, 1, "Backtest comparison", font=st["title"])
    _cell(C, 2, 1, f"Starting with ${cfg.starting_equity:,.0f} each. Every combination ran on the same days and stocks.",
          font=st["muted"])
    fmts = {"trades": "0", "win %": PCT, "avg R": "+0.00;[Red]-0.00", "profit factor": "0.00", "net $": USD,
            "return %": PCT, "max drawdown %": PCT}
    cols = list(table.columns)
    _header(C, 4, [("Settings", 34)] + [(c, 13) for c in cols], st, freeze=False)
    best = table["net $"].astype(float).idxmax() if "net $" in table and table["net $"].notna().any() else None
    good = PatternFill("solid", fgColor="DDEFD9")
    for r, lab in enumerate(labels, 5):
        _cell(C, r, 1, lab, font=st["bold"] if lab == best else st["base"])
        for c, col in enumerate(cols, 2):
            x = _cell(C, r, c, table.loc[lab, col], fmts.get(col), st["base"])
            if lab == best:
                x.fill = good
        if lab == best:
            C.cell(row=r, column=1).fill = good
    after = 5 + len(labels) + 1
    if best is not None:
        more = (f" The chart shows the {CHART_LINES} best by net; every combination is on the Equity tab."
                if len(labels) > CHART_LINES else "")
        _cell(C, after, 1, f"Best net: {best} (shaded).{more}", font=st["muted"])
    if len(days) >= 2 and labels:
        order = table["net $"].astype(float).fillna(float("-inf")).sort_values(ascending=False).index \
            if "net $" in table else labels
        drawn = list(order)[:CHART_LINES]
        chart_cols = [labels.index(lab) + 2 for lab in labels if lab in drawn]    # keep the table's order
        title = "Account value by day" + (f" ({len(drawn)} best by net)" if len(labels) > CHART_LINES else "")
        C.add_chart(_equity_chart(C, E, 2, last, chart_cols, title), f"A{after + 2}")

    # Trades: every combination's trades, with its settings first (filter on that column)
    frames = [t.assign(_settings=lab) for lab, t in trades.items() if len(t)]
    allt = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["_settings"])
    _trades_sheet(T, allt, st, settings_col="_settings")
    wb.save(path)
    return path
