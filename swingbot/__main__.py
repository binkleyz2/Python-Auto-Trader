"""Command line: python -m swingbot <command>

  demo                      backtest on SYNTHETIC data (checks the code runs; says nothing about real performance)
  backtest --data DIR       backtest on your bar CSVs (same bar size as bar_minutes) (one SYMBOL.csv per stock)
  fetch --data DIR          save recent 5-minute bars from your broker into DIR (run daily to build history)
  scan                      show this morning's ranking of your watchlist (needs API key)
  run                       trade today's session in DRY RUN (needs API key; no orders sent)
  run --bars 1 --symbol NVDA  watch only NVDA, on 1-minute bars, starting now (works with --live too)
  run --live                trade today's session with orders (asks you to confirm). With broker: alpaca
                            and alpaca_paper: true these go to your Alpaca PAPER account (fake money).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .config import Config


def _env():
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")         # the folder you run it from
        load_dotenv()                            # and the bot's own folder (keeps values already set)
    except ImportError:
        pass


EXAMPLE = "config.example.yaml"


def _first_config(path: Path) -> None:
    """A fresh copy has config.example.yaml but no config.yaml yet: start from the example."""
    if not path.exists() and path.name == "config.yaml" and (path.parent / EXAMPLE).exists():
        import shutil
        shutil.copyfile(path.parent / EXAMPLE, path)
        print(f"Created {path} from {EXAMPLE}. Edit config.yaml for your settings; updates never change it.\n")


def _new_settings(path: Path) -> list[str]:
    """Settings in config.example.yaml that your config.yaml doesn't have (the bot uses their defaults)."""
    try:
        import yaml
        ex = yaml.safe_load((path.parent / EXAMPLE).read_text(encoding="utf-8-sig")) or {}
        text = path.read_text(encoding="utf-8-sig")
        mine = yaml.safe_load(text) or {}
        # a setting you commented out (#watchlist_file: ...) was left out on purpose
        return [k for k in ex if k not in mine and not re.search(rf"^\s*#\s*{re.escape(k)}\s*:", text, re.M)]
    except Exception:
        return []


def _progress(what: str):
    state = {"last": 0.0}

    def show(done, total):
        import time as _t
        if _t.monotonic() - state["last"] > 2 or done == total:
            state["last"] = _t.monotonic()
            print(f"  {done} of {total} {what}", flush=True)
    return show


def _alpaca_history(cfg: Config, args, workdir: Path) -> dict:
    """Months of 5-minute bars from Alpaca for the backtest (downloaded once, then cached in data/)."""
    import os
    from .config import read_watchlist  # noqa: F401
    from .history_data import load_history
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit("Backtests with --days use Alpaca's history: set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env "
                 "(paper keys are fine).")
    if args.symbol:                                    # --symbol AAPL (or AAPL,MSFT): just those
        syms = [x.strip().upper() for x in args.symbol.split(",") if x.strip()]
    elif args.universe.strip().lower() == "watchlist":
        syms = list(dict.fromkeys(s.upper() for s in cfg.watchlist))
        if cfg.watchlist_indexes:
            from .indexes import daily_watchlist
            syms, _ = daily_watchlist(list(cfg.watchlist_indexes), syms, workdir / "logs" / "indexes", cfg.min_price,
                                      fallback_dir=workdir)
    else:
        from .indexes import INDEXES, index_symbols
        names = [u.strip().lower() for u in args.universe.split(",") if u.strip()]
        bad = [n for n in names if n not in INDEXES]
        if bad:
            sys.exit(f"--universe: unknown {', '.join(bad)} (choose from {', '.join(INDEXES)} or 'watchlist')")
        syms = []
        for n in names:
            syms += index_symbols(n, workdir / "logs" / "indexes", cfg.min_price, fallback_dir=workdir)
        syms = list(dict.fromkeys(syms))
    if len(syms) > args.max_symbols:
        print(f"{len(syms)} stocks - using the first {args.max_symbols} (--max-symbols); more stocks need a lot of "
              f"memory and time")
        syms = syms[:args.max_symbols]
    from alpaca.data.historical import StockHistoricalDataClient
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    print(f"Getting {args.days} days of {cfg.bar_minutes}-minute bars for {len(syms)} stocks "
          f"(the first time takes a few minutes; later runs only fetch new days)...", flush=True)
    hist = load_history(client, syms, args.days, workdir / "data" / f"alpaca-{cfg.bar_minutes}min", cfg.bar_minutes,
                        progress=_progress("stocks downloaded"))
    if not hist:
        sys.exit("No history came back from Alpaca.")
    return hist


MAX_RUNS = 64
_RANGE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*(?:[:/]\s*(\d+(?:\.\d+)?))?\s*$")


def try_values(vals: str, step=None) -> list:
    """Values for --try: a list ("1,1.5,2"), a range ("4-8", every whole number), a range with a step
    ("1-2:0.25" = 1, 1.25, 1.5, 1.75, 2), or a mix ("2,4-6"). true/false and numbers as in config.yaml.
    ``step`` (from --step) is used for ranges that don't give their own."""
    import yaml
    from decimal import Decimal
    out = []
    step = None if step is None else str(step)
    for part in [v.strip() for v in vals.split(",") if v.strip()]:
        m = _RANGE.match(part)
        if not m:
            out.append(yaml.safe_load(part))
            continue
        lo, hi, own = m.group(1), m.group(2), m.group(3)
        st_txt = own or step
        whole = "." not in lo + hi + (st_txt or "")
        if st_txt is None and not whole:
            raise ValueError(f"{part}: give a step for decimals, e.g. {lo}-{hi}:0.25 or --step 0.25")
        a, b, st = Decimal(lo), Decimal(hi), Decimal(st_txt or "1")
        if st <= 0 or b < a:
            raise ValueError(f"{part}: write ranges low-high with a step above 0")
        n = int((b - a) / st) + 1
        if n > MAX_RUNS:
            raise ValueError(f"{part} gives {n} values - use a bigger step")
        out += [int(a + st * k) if whole else float(a + st * k) for k in range(n)]
    return list(dict.fromkeys(out))


def _forced(args) -> list[str] | None:
    return [x.strip().upper() for x in args.symbol.split(",") if x.strip()] if getattr(args, "symbol", None) else None


def _steps(steps: list[str] | None, cfg: Config) -> tuple[str | None, dict[str, str]]:
    """--step 0.5 (every range without its own step) and --step setting=0.5 (just that one)."""
    every, each = None, {}
    for t in steps or []:
        key, _, val = t.rpartition("=")
        key, val = key.strip(), val.strip()
        try:
            if float(val) <= 0:
                raise ValueError
        except ValueError:
            sys.exit(f"--step {t}: give a number above 0, e.g. --step 0.5 or --step breakeven_at_r=0.5")
        if not key:
            every = val
        elif not hasattr(cfg, key):
            sys.exit(f"--step: unknown setting {key!r}")
        else:
            each[key] = val
    return every, each


def _whole(cfg: Config, key: str) -> bool:
    v = getattr(cfg, key)
    return isinstance(v, int) and not isinstance(v, bool)


def _archive(files: list[Path], cfg: Config, args, workdir: Path, now=None) -> Path | None:
    """Keep this run: its files, the command and the config.yaml used, zipped into
    <backtest_results_dir>/<date>_<time>-<what was tested>.zip, then the loose files are removed
    (they're in the zip). Never stops the backtest."""
    import zipfile
    from datetime import datetime
    from . import __version__
    if not cfg.backtest_results_dir:
        return None
    try:
        folder = Path(cfg.backtest_results_dir)
        folder = folder if folder.is_absolute() else workdir / folder
        folder.mkdir(parents=True, exist_ok=True)
        now = now or datetime.now()
        what = [getattr(args, "symbol", None) or getattr(args, "universe", None) or "csv"]
        if getattr(args, "days", None):
            what.append(f"{args.days}d")
        what += [t.replace(" ", "") for t in getattr(args, "tries", []) or []]
        what += [f"step{s.replace(' ', '')}" for s in getattr(args, "steps", []) or []]
        tag = re.sub(r"[^A-Za-z0-9._=,-]+", "_", "-".join(what)).strip("_")[:100]
        base = f"{now:%Y-%m-%d_%H%M%S}-{tag}"
        dest, n = folder / f"{base}.zip", 2
        while dest.exists():
            dest, n = folder / f"{base}-{n}.zip", n + 1
        command = "py -m swingbot " + " ".join(a if " " not in a else f'"{a}"' for a in sys.argv[1:])
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("run.txt", f"swingbot {__version__}\n{now:%Y-%m-%d %H:%M:%S}\n{command}\n")
            conf = Path(getattr(args, "config", "config.yaml"))
            if conf.exists():
                z.write(conf, "config.yaml")
            for f in files:
                if f.exists():
                    z.write(f, f.name)
        for f in files:                                  # moved, not copied: they're safely in the zip now
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass                                     # open in Excel, say - it just stays behind
        return dest
    except Exception as e:                               # a full disk or locked file shouldn't lose the printout
        print(f"(Couldn't save the run to {cfg.backtest_results_dir}/: {e})")
        return None


def _excel(write, path: Path) -> bool:
    """Write an extra copy of the results (Excel, or the interactive chart); the CSVs are kept either way."""
    try:
        write(path)
        return True
    except Exception as e:
        print(f"(No {path.name} this time - {e}. The CSV files have everything.)")
        return False


def _compare(hist: dict, cfg: Config, tries: list[str], out: Path, forced: list[str] | None = None,
             steps: list[str] | None = None) -> list[Path]:
    """Run the backtest for every combination of the --try values and print them side by side.
    Returns the files it wrote."""
    import dataclasses
    import itertools
    import yaml
    from . import backtest as bt
    every, each = _steps(steps, cfg)
    options = []
    for t in tries:
        if "=" not in t:
            sys.exit(f"--try {t}: write it as setting=value1,value2")
        key, vals = t.split("=", 1)
        key = key.strip()
        if not hasattr(cfg, key):
            sys.exit(f"--try: unknown setting {key!r}")
        if isinstance(getattr(cfg, key), str):          # times stay text ("12:45" isn't a number); off = ""
            vs = [v.strip().strip("\"'") for v in vals.split(",")]
            options.append([(key, "" if v.lower() in ("off", "none") else v) for v in vs if v or len(vs) == 1])
        else:
            step = each.get(key)
            if step is None and every is not None and not (_whole(cfg, key) and "." in every):
                step = every                             # a 0.5 step doesn't apply to whole-number settings
            if step is not None and _whole(cfg, key) and "." in step:
                sys.exit(f"--step {key}={step}: {key} takes whole numbers")
            try:
                vs = try_values(vals, step)
            except ValueError as e:
                sys.exit(f"--try {t}: {e}")
            if _whole(cfg, key) and any(isinstance(v, float) and v != int(v) for v in vs):
                sys.exit(f"--try {t}: {key} takes whole numbers")
            options.append([(key, v) for v in vs])
            print(f"{key}: {', '.join(str(v) for v in vs)}", flush=True)
    combos = list(itertools.product(*options))
    if len(combos) > MAX_RUNS:
        sys.exit(f"--try: {len(combos)} combinations - keep it to {MAX_RUNS} or fewer (fewer values, or a bigger step)")
    if len(combos) > 1:
        print(f"{len(combos)} combinations to run", flush=True)
    out.mkdir(exist_ok=True)
    rows, scan_cache, prepared, written, curves, run_trades = [], {}, {}, [], {}, {}
    for n, combo in enumerate(combos, 1):
        c = dataclasses.replace(cfg, **dict(combo))
        try:
            c.validate()
        except ValueError as e:
            sys.exit(f"--try {dict(combo)}: {e}")
        label = ", ".join(f"{k}={v if v != '' else 'off'}" for k, v in combo)
        print(f"[{n}/{len(combos)}] {label}", flush=True)
        pk = (c.swing_n, c.atr_period, c.direction != "long")
        if pk not in prepared:
            prepared[pk] = bt.prepare(hist, c)
        trades, curve, _ = bt.run(hist, c, scan_cache=scan_cache, prepared=prepared[pk], progress=_progress("days"),
                                  forced=forced)
        s = bt.summarize(trades, curve, c)
        tag = "-".join(f"{k}{v if v != '' else 'off'}" for k, v in combo).replace(" ", "").replace(":", "")
        trades.to_csv(out / f"trades-{tag}.csv", index=False)
        (out / f"report-{tag}.txt").write_text(bt.report_text(trades, curve, c, label) + "\n", encoding="utf-8")
        written += [out / f"report-{tag}.txt", out / f"trades-{tag}.csv"]
        curves[label], run_trades[label] = curve, trades
        rows.append({"settings": label, "trades": s.get("trades", 0), "win %": s.get("win_rate_pct"),
                     "avg R": s.get("avg_r"), "profit factor": s.get("profit_factor"), "net $": s.get("net_pnl"),
                     "return %": s.get("return_pct"), "max drawdown %": s.get("max_drawdown_pct")})
    import pandas as pd
    table = pd.DataFrame(rows).set_index("settings")
    table.to_csv(out / "compare.csv")
    (out / "compare.txt").write_text(table.to_string() + "\n", encoding="utf-8")
    from .backtest_xlsx import compare_workbook
    excel = _excel(lambda p: compare_workbook(p, table, curves, run_trades, cfg), out / "compare.xlsx")
    from .backtest_chart import compare_chart
    chart = _excel(lambda p: compare_chart(p, table, curves, run_trades, cfg), out / "compare-chart.html")
    print("\n" + table.to_string())
    if not cfg.backtest_results_dir:
        print(f"\nEach run's trades and full report are in {out}/ (compare.csv, report-*.txt, trades-*.csv)")
    return ([out / "compare-chart.html"] if chart else []) + ([out / "compare.xlsx"] if excel else []) + \
        [out / "compare.txt", out / "compare.csv"] + written


def _logging(workdir: Path) -> None:
    from datetime import date
    (workdir / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(workdir / "logs" / f"{date.today()}.log")],
    )


def _start_dashboard(cfg, bot):
    """Start the local live page (127.0.0.1 only). Any problem here never stops trading."""
    if not cfg.dashboard:
        return None
    try:
        from .dashboard import Dashboard
        dash = Dashboard(cfg.dashboard_port, lan=cfg.dashboard_lan, key=cfg.dashboard_key,
                         lan_control=cfg.dashboard_lan_control)
        if cfg.dashboard_bar_control:
            dash.on_bar_size = bot.request_bar_size     # the page's controls: bar size and rescan, never orders
            dash.on_rescan = bot.request_rescan
            dash.on_setting = bot.request_setting       # trail distance, breakeven, profit target - for today
        url = dash.start()
        if not url:
            logging.getLogger("swingbot").warning("Dashboard: no free port near %d - running without it.",
                                                  cfg.dashboard_port)
            return None
        bot.dashboard = dash
        bot.publish("Starting")
        logging.getLogger("swingbot").info("Live dashboard: %s", url)
        if dash.lan_url():
            logging.getLogger("swingbot").info("Dashboard on your network: %s  (%s; keep the key private)",
                                               dash.lan_url(),
                                               "can change bar size" if cfg.dashboard_lan_control
                                               and cfg.dashboard_bar_control else "view-only")
        if cfg.dashboard_open_browser:
            import webbrowser
            webbrowser.open(url)
        return dash
    except Exception as e:
        logging.getLogger("swingbot").warning("Dashboard could not start (%s) - running without it.", e)
        return None


def main(argv=None) -> None:
    from . import __version__
    ap = argparse.ArgumentParser(prog="swingbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"swingbot {__version__}")
    ap.add_argument("command", choices=["demo", "backtest", "fetch", "scan", "run"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="backtests")
    ap.add_argument("--live", action="store_true", help="send REAL orders")
    ap.add_argument("--bars", type=int, help="bar size in minutes for this run (overrides bar_minutes)")
    ap.add_argument("--symbol", help="skip the morning scan and trade this symbol (or contract) only; "
                                     "backtest: e.g. NVDA or NVDA,AMD, watched every day")
    ap.add_argument("--yes", action="store_true", help="skip the live confirmation prompt (for schedulers)")
    ap.add_argument("--days", type=int, help="backtest: this many calendar days of Alpaca history (e.g. 120)")
    ap.add_argument("--universe", default="sp500",
                    help="backtest --days: stocks to test on: index names (sp500, nasdaq100, dow, russell2000, nasdaq) "
                         "joined with commas, or 'watchlist' for your config's list (default sp500)")
    ap.add_argument("--max-symbols", type=int, default=800, help="backtest --days: at most this many stocks")
    ap.add_argument("--try", dest="tries", action="append", default=[], metavar="SETTING=A,B",
                    help="backtest: compare values side by side, e.g. --try swing_n=4-8 --try breakeven_at_r=1-2:0.5 "
                         "(lists like 1,1.5 work too)")
    ap.add_argument("--step", dest="steps", action="append", default=[], metavar="N or SETTING=N",
                    help="backtest --try: gap between values in a range, e.g. --try breakeven_at_r=1-2 --step 0.5 "
                         "(1, 1.5, 2); --step breakeven_at_r=0.5 for just that setting. Whole-number settings "
                         "like swing_n keep steps of 1 unless given a whole step")
    args = ap.parse_args(argv)

    _env()
    _first_config(Path(args.config))
    try:
        cfg = Config.load(args.config)
        if args.bars:
            cfg.bar_minutes = args.bars
            cfg.validate()
    except ValueError as e:
        sys.exit(f"Problem in {args.config}: {e}")
    from .data import set_public_rate
    set_public_rate(cfg.public_rate_limit)
    workdir = Path.cwd()

    if args.command in ("demo", "backtest"):
        from . import backtest as bt
        from .data import load_dir, synthetic
        if args.command == "demo":
            print("Using SYNTHETIC random-walk data. Results only show the code works.\n")
            hist = {s: synthetic(s, days=40) for s in cfg.watchlist}
        elif args.days:
            _env()
            hist = _alpaca_history(cfg, args, workdir)
        else:
            hist = load_dir(args.data, [s.upper() for s in cfg.watchlist])
            if not hist:
                sys.exit(f"No CSVs for the watchlist found in {args.data}/ (or use --days 120 for Alpaca's history)")
        if args.steps and not args.tries:
            sys.exit("--step goes with --try, e.g. --try breakeven_at_r=1-2 --step 0.5")
        if args.tries:
            files = _compare(hist, cfg, args.tries, Path(args.out), _forced(args), args.steps)
            if args.command == "backtest" and (kept := _archive(files, cfg, args, workdir)):
                print(f"Saved as {kept}")
            return
        print(f"Backtesting {len(hist)} stocks...", flush=True)
        trades, curve, picks = bt.run(hist, cfg, progress=_progress("days"), forced=_forced(args))
        out = Path(args.out)
        out.mkdir(exist_ok=True)
        trades.to_csv(out / "trades.csv", index=False)
        curve.to_csv(out / "equity.csv")
        (out / "picks.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
        title = f"Backtest with your config.yaml settings ({len(hist)} stocks)"
        text = bt.report_text(trades, curve, cfg, title)
        (out / "report.txt").write_text(text + "\n", encoding="utf-8")
        from .backtest_xlsx import run_workbook
        excel = _excel(lambda p: run_workbook(p, trades, curve, cfg, title), out / "backtest.xlsx")
        from .backtest_chart import run_chart
        chart = _excel(lambda p: run_chart(p, trades, curve, cfg, title), out / "backtest-chart.html")
        print("\n" + text)
        names = (["backtest-chart.html"] if chart else []) + (["backtest.xlsx"] if excel else []) + \
            ["report.txt", "trades.csv", "equity.csv", "picks.json"]
        if args.command == "backtest" and (kept := _archive([out / n for n in names], cfg, args, workdir)):
            print(f"\nSaved as {kept} ({', '.join(names)})")
        else:
            print(f"\nDetails saved in {out}/ ({', '.join(names)})")
        return

    from .live import is_weekday, wait_for_open
    from .data import append_csv
    _logging(workdir)
    new = _new_settings(Path(args.config))
    if new:
        logging.getLogger("swingbot").info(
            "Settings in %s that your %s doesn't have (defaults used; copy them over to change them): %s",
            EXAMPLE, Path(args.config).name, ", ".join(new))
    if cfg.broker == "ninjatrader":
        from .ninja_live import NinjaBot as BotClass, make_ninja_client
        client = make_ninja_client(cfg, workdir)
        where = ("your NinjaTrader DEMO account (simulated money)" if cfg.ninja_env != "live"
                 else "your NinjaTrader account with REAL MONEY (futures, leveraged)")
    elif cfg.broker == "alpaca":
        from .alpaca_live import AlpacaBot as BotClass, make_alpaca_client
        client = make_alpaca_client(cfg)
        where = "your Alpaca PAPER account (fake money)" if cfg.alpaca_paper else "your Alpaca account with REAL MONEY"
    else:
        from .live import Bot as BotClass, make_client
        client = make_client()
        where = "your Public account with REAL MONEY"
    try:
        if args.command == "fetch":
            folder = Path(args.data)
            folder.mkdir(exist_ok=True)
            fetcher = BotClass(client, cfg, live=False, workdir=workdir)
            for s in cfg.watchlist:
                n = append_csv(fetcher.fetch_bars(s, "WEEK"), folder / f"{s}.csv")
                print(f"{s}: {n} bars stored")
            return

        if args.command == "scan":
            bot = BotClass(client, cfg, live=False, workdir=workdir)
            bot.forced_symbol = args.symbol.upper() if args.symbol else None
            bot.morning_scan()
            return

        if args.live and not args.yes:
            print(f"LIVE MODE: this will place orders in {where}.")
            if input("Type LIVE to continue: ").strip() != "LIVE":
                sys.exit("Cancelled.")
        bot = BotClass(client, cfg, live=args.live, workdir=workdir)
        bot.forced_symbol = args.symbol.upper() if args.symbol else None
        dash = _start_dashboard(cfg, bot)
        try:
            try:
                if is_weekday(bot):
                    bot.prepare()                # scan now (yesterday's data), so the pick is ready at the open
                    if bot.symbol and wait_for_open(bot):
                        bot.run_session()
            except Exception as e:               # into the log file too, not just the console
                logging.getLogger("swingbot").exception("The bot stopped on an error: %s", e)
                raise
        finally:
            if cfg.results_spreadsheet:
                from .history import record_session
                record_session(bot, cfg, workdir / "logs")
            if dash:
                from datetime import date
                snap = dash.snapshot(workdir / "logs" / f"{date.today()}-dashboard.html")
                logging.getLogger("swingbot").info("Dashboard snapshot saved: %s", snap)
                if cfg.dashboard_jpg:
                    from .screenshot import save_jpg
                    save_jpg(snap, cfg.dashboard_jpg_browser)
                dash.stop()
            from .report import end_of_day
            end_of_day(bot, cfg, workdir / "logs")
    finally:
        client.close()


if __name__ == "__main__":
    main()
