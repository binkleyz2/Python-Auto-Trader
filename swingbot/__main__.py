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
    if args.universe.strip().lower() == "watchlist":
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


def _compare(hist: dict, cfg: Config, tries: list[str], out: Path) -> None:
    """Run the backtest for every combination of the --try values and print them side by side."""
    import dataclasses
    import itertools
    import yaml
    from . import backtest as bt
    options = []
    for t in tries:
        if "=" not in t:
            sys.exit(f"--try {t}: write it as setting=value1,value2")
        key, vals = t.split("=", 1)
        key = key.strip()
        if not hasattr(cfg, key):
            sys.exit(f"--try: unknown setting {key!r}")
        options.append([(key, yaml.safe_load(v)) for v in vals.split(",") if v.strip()])
    combos = list(itertools.product(*options))
    if len(combos) > 16:
        sys.exit(f"--try: {len(combos)} combinations - keep it to 16 or fewer")
    out.mkdir(exist_ok=True)
    rows, scan_cache, prepared = [], {}, {}
    for n, combo in enumerate(combos, 1):
        c = dataclasses.replace(cfg, **dict(combo))
        try:
            c.validate()
        except ValueError as e:
            sys.exit(f"--try {dict(combo)}: {e}")
        label = ", ".join(f"{k}={v}" for k, v in combo)
        print(f"[{n}/{len(combos)}] {label}", flush=True)
        pk = (c.swing_n, c.atr_period, c.direction != "long")
        if pk not in prepared:
            prepared[pk] = bt.prepare(hist, c)
        trades, curve, _ = bt.run(hist, c, scan_cache=scan_cache, prepared=prepared[pk], progress=_progress("days"))
        s = bt.summarize(trades, curve, c)
        tag = "-".join(f"{k}{v}" for k, v in combo).replace(" ", "")
        trades.to_csv(out / f"trades-{tag}.csv", index=False)
        (out / f"report-{tag}.txt").write_text(bt.report_text(trades, curve, c, label) + "\n", encoding="utf-8")
        rows.append({"settings": label, "trades": s.get("trades", 0), "win %": s.get("win_rate_pct"),
                     "avg R": s.get("avg_r"), "profit factor": s.get("profit_factor"), "net $": s.get("net_pnl"),
                     "return %": s.get("return_pct"), "max drawdown %": s.get("max_drawdown_pct")})
    import pandas as pd
    table = pd.DataFrame(rows).set_index("settings")
    table.to_csv(out / "compare.csv")
    print("\n" + table.to_string())
    print(f"\nEach run's trades and full report are in {out}/ (compare.csv, report-*.txt, trades-*.csv)")


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
    ap.add_argument("--symbol", help="skip the morning scan and trade this symbol (or contract) only")
    ap.add_argument("--yes", action="store_true", help="skip the live confirmation prompt (for schedulers)")
    ap.add_argument("--days", type=int, help="backtest: this many calendar days of Alpaca history (e.g. 120)")
    ap.add_argument("--universe", default="sp500",
                    help="backtest --days: stocks to test on: index names (sp500, nasdaq100, dow, russell2000, nasdaq) "
                         "joined with commas, or 'watchlist' for your config's list (default sp500)")
    ap.add_argument("--max-symbols", type=int, default=800, help="backtest --days: at most this many stocks")
    ap.add_argument("--try", dest="tries", action="append", default=[], metavar="SETTING=A,B",
                    help="backtest: compare values side by side, e.g. --try swing_n=4,5 --try breakeven_at_r=1,1.5")
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
        if args.tries:
            _compare(hist, cfg, args.tries, Path(args.out))
            return
        print(f"Backtesting {len(hist)} stocks...", flush=True)
        trades, curve, picks = bt.run(hist, cfg, progress=_progress("days"))
        out = Path(args.out)
        out.mkdir(exist_ok=True)
        trades.to_csv(out / "trades.csv", index=False)
        curve.to_csv(out / "equity.csv")
        (out / "picks.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
        text = bt.report_text(trades, curve, cfg, f"Backtest with your config.yaml settings ({len(hist)} stocks)")
        (out / "report.txt").write_text(text + "\n", encoding="utf-8")
        print("\n" + text)
        print(f"\nDetails saved in {out}/ (report.txt, trades.csv, equity.csv, picks.json)")
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
