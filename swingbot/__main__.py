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
import sys
from pathlib import Path

from .config import Config


def _env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


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
    ap.add_argument("--out", default="results")
    ap.add_argument("--live", action="store_true", help="send REAL orders")
    ap.add_argument("--bars", type=int, help="bar size in minutes for this run (overrides bar_minutes)")
    ap.add_argument("--symbol", help="skip the morning scan and trade this symbol (or contract) only")
    ap.add_argument("--yes", action="store_true", help="skip the live confirmation prompt (for schedulers)")
    args = ap.parse_args(argv)

    _env()
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
        else:
            hist = load_dir(args.data, [s.upper() for s in cfg.watchlist])
            if not hist:
                sys.exit(f"No CSVs for the watchlist found in {args.data}/")
        trades, curve, picks = bt.run(hist, cfg)
        out = Path(args.out)
        out.mkdir(exist_ok=True)
        trades.to_csv(out / "trades.csv", index=False)
        curve.to_csv(out / "equity.csv")
        (out / "picks.json").write_text(json.dumps(picks, indent=1), encoding="utf-8")
        print(json.dumps(bt.summarize(trades, curve, cfg), indent=2))
        print(f"\nDetails saved in {out}/ (trades.csv, equity.csv, picks.json)")
        return

    from .live import is_weekday, wait_for_open
    from .data import append_csv
    _logging(workdir)
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
            if dash:
                from datetime import date
                snap = dash.snapshot(workdir / "logs" / f"{date.today()}-dashboard.html")
                logging.getLogger("swingbot").info("Dashboard snapshot saved: %s", snap)
                dash.stop()
    finally:
        client.close()


if __name__ == "__main__":
    main()
