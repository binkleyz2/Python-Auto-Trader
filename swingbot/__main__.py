"""Command line: python -m swingbot <command>

  demo                      backtest on SYNTHETIC data (checks the code runs; says nothing about real performance)
  backtest --data DIR       backtest on your bar CSVs (same bar size as bar_minutes) (one SYMBOL.csv per stock)
  download --days 180 --universe sp500,dow   save Alpaca history for backtests (no backtest; resumable)
  fetch --data DIR          save recent 5-minute bars from your broker into DIR (run daily to build history)
  scan                      show this morning's ranking of your watchlist (needs API key)
  run                       trade today's session in DRY RUN (needs API key; no orders sent)
  run --bars 1 --symbol NVDA  watch only NVDA, on 1-minute bars, starting now (works with --live too)
  run --live                trade today's session with orders (asks you to confirm). With broker: alpaca
                            and alpaca_paper: true these go to your Alpaca PAPER account (fake money).
  daemon                    run every market day by itself (Docker, a server): daemon --live --yes
  site                      the results website (every day's results; https with site_cert). --install keeps
                            it running (starts at logon), --stop / --uninstall
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


def _data_client():
    import os
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit("History comes from Alpaca: set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env (paper keys are fine).")
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])


def _symbols(cfg: Config, args, workdir: Path) -> list[str]:
    """The stocks --symbol / --universe name."""
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
    return syms


def _alpaca_history(cfg: Config, args, workdir: Path) -> dict:
    """Months of 5-minute bars from Alpaca for the backtest (downloaded once, then cached in data/)."""
    from .history_data import load_history
    client = _data_client()
    syms = _symbols(cfg, args, workdir)
    if len(syms) > args.max_symbols:
        print(f"{len(syms)} stocks - using the first {args.max_symbols} (--max-symbols); more stocks need a lot of "
              f"memory and time")
        syms = syms[:args.max_symbols]
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


def _date_range(args):
    """--from / --to as dates; --from without --days fetches enough history to cover it."""
    from datetime import date
    out = []
    for name, v in (("--from", getattr(args, "from_date", None)), ("--to", getattr(args, "to_date", None))):
        if not v:
            out.append(None)
            continue
        try:
            out.append(date.fromisoformat(v))
        except ValueError:
            sys.exit(f"{name} {v}: write the date as YYYY-MM-DD, e.g. 2026-09-01")
    start, end = out
    if start and end and end < start:
        sys.exit(f"--to {end} is before --from {start}")
    from .config import DOCKER
    if start and not getattr(args, "days", None) and args.command == "backtest" and not getattr(args, "data_given", False) \
            and not DOCKER:
        args.days = (date.today() - start).days + 21            # plus three weeks for the lines' history
        print(f"--from {start}: using {args.days} days of history (--days {args.days})")
    if start or end:
        print(f"Trading days: {start or 'the start'} to {end or 'the end'} of the data")
    return start, end


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


DOWNLOAD_CHUNK = 100                 # stocks held in memory at once while downloading


def _download(cfg: Config, args, workdir: Path) -> None:
    """Fill the backtest history cache (data/alpaca-<N>min/) for whole indexes, without running a backtest.
    A chunk of stocks at a time, each saved as it arrives; stocks already up to date are skipped, so it can
    be stopped and run again to carry on."""
    import time
    from .history_data import load_history
    if not args.days:
        sys.exit("download needs --days, e.g. py -m swingbot download --days 180 --universe sp500,dow")
    client = _data_client()
    syms = _symbols(cfg, args, workdir)
    folder = workdir / "data" / f"alpaca-{cfg.bar_minutes}min"
    print(f"Downloading {args.days} days of {cfg.bar_minutes}-minute bars for {len(syms)} stocks into {folder}")
    print("Stocks that are already up to date are skipped. Stop any time with Ctrl+C; run it again to carry on.",
          flush=True)
    got, t0 = 0, time.time()
    try:
        for i in range(0, len(syms), DOWNLOAD_CHUNK):
            chunk = syms[i:i + DOWNLOAD_CHUNK]
            got += len(load_history(client, chunk, args.days, folder, cfg.bar_minutes))
            mins = (time.time() - t0) / 60
            print(f"  {i + len(chunk)} of {len(syms)} stocks done ({mins:.0f} min)", flush=True)
    except KeyboardInterrupt:
        print("\nStopped. Everything downloaded so far is saved; run the same command again to carry on.")
        return
    size = sum(p.stat().st_size for p in folder.glob("*.csv.gz")) / 1e6
    print(f"Done: {got} of {len(syms)} stocks have history ({size:,.0f} MB in {folder}).")
    if got < len(syms):
        print(f"{len(syms) - got} had none from Alpaca (new listings, funds or delisted stocks); they're tried again "
              f"next time.")
    print(f"Backtests of up to {args.days} days on these stocks now start right away.")


def _excel(write, path: Path) -> bool:
    """Write an extra copy of the results (Excel, or the interactive chart); the CSVs are kept either way."""
    try:
        write(path)
        return True
    except Exception as e:
        print(f"(No {path.name} this time - {e}. The CSV files have everything.)")
        return False


def _bar_size(hist: dict) -> int | None:
    """The bar size of loaded history, in minutes (the usual gap between bars within a day)."""
    import pandas as pd
    for df in hist.values():
        if df is None or len(df) < 3:
            continue
        idx = df.index
        gaps = pd.Series(idx[1:] - idx[:-1])[pd.Series(idx[1:].date) == pd.Series(idx[:-1].date)]
        if len(gaps):
            return max(1, int(round(gaps.mode().iloc[0].total_seconds() / 60)))
    return None


def _compare(hist: dict, cfg: Config, tries: list[str], out: Path, forced: list[str] | None = None,
             steps: list[str] | None = None, start=None, end=None, quick: bool = False) -> list[Path]:
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
    # bar_minutes: the history was loaded at one size; other sizes are made from it (5-minute bars combine
    # into 15 or 30), so every combination really runs on its own bar size
    loaded = _bar_size(hist) or cfg.bar_minutes
    bar_hist = {loaded: hist}
    for combo in combos:
        m = dict(combo).get("bar_minutes", loaded)
        if m != loaded and (m < loaded or m % loaded):
            sys.exit(f"--try bar_minutes={m}: the history is {loaded}-minute bars, so only multiples of {loaded} "
                     f"can be made from it. Load finer bars (--bars 1 or 5), or run that size on its own: --bars {m}")
    if len(combos) > MAX_RUNS:
        sys.exit(f"--try: {len(combos)} combinations - keep it to {MAX_RUNS} or fewer (fewer values, or a bigger step)")
    if len(combos) > 1:
        print(f"{len(combos)} combinations to run", flush=True)
    out.mkdir(exist_ok=True)
    rows, scan_cache, prepared, written, curves, run_trades = [], {}, {}, [], {}, {}
    # robustness: each combination also on two random halves of the stocks (the same halves for all), so a
    # result that only came from which stocks happened to be picked shows up as such
    import random
    syms = sorted(hist)
    halves, half_caches = None, {}
    if not quick and not forced and len(syms) >= 40:
        random.Random(7).shuffle(syms)
        halves = (set(syms[::2]), set(syms[1::2]))
        print(f"Each combination also runs on two random halves of the {len(syms)} stocks, to check it holds up "
              f"(--quick skips this)", flush=True)
    for n, combo in enumerate(combos, 1):
        c = dataclasses.replace(cfg, **dict(combo))
        try:
            c.validate()
        except ValueError as e:
            sys.exit(f"--try {dict(combo)}: {e}")
        label = ", ".join(f"{k}={v if v != '' else 'off'}" for k, v in combo)
        print(f"[{n}/{len(combos)}] {label}", flush=True)
        bm = c.bar_minutes if "bar_minutes" in dict(combo) else loaded
        if bm not in bar_hist:
            from .data import resample_bars
            print(f"  making {bm}-minute bars from the {loaded}-minute history", flush=True)
            bar_hist[bm] = {k: resample_bars(v, bm) for k, v in hist.items()}
        h = bar_hist[bm]
        pk = (bm, c.swing_n, c.atr_period, c.direction != "long")
        if pk not in prepared:
            prepared[pk] = bt.prepare(h, c)
        trades, curve, _ = bt.run(h, c, scan_cache=scan_cache.setdefault(bm, {}), prepared=prepared[pk],
                                  progress=_progress("days"), forced=forced, start=start, end=end)
        s = bt.summarize(trades, curve, c)
        checks = _periods(trades, curve)
        if halves is not None:
            for i, half in enumerate(halves):
                sub = {k: v for k, v in prepared[pk].items() if k in half}
                hc = half_caches.setdefault((bm, i), {})
                tr_h, _, _ = bt.run(h, c, scan_cache=hc, prepared=sub, start=start, end=end)
                checks.append(round(float(tr_h["pnl"].sum()), 2) if len(tr_h) else 0.0)
        tag = "-".join(f"{k}{v if v != '' else 'off'}" for k, v in combo).replace(" ", "").replace(":", "")
        trades.to_csv(out / f"trades-{tag}.csv", index=False)
        (out / f"report-{tag}.txt").write_text(bt.report_text(trades, curve, c, label) + "\n", encoding="utf-8")
        written += [out / f"report-{tag}.txt", out / f"trades-{tag}.csv"]
        curves[label], run_trades[label] = curve, trades
        rows.append({"settings": label, "trades": s.get("trades", 0), "win %": s.get("win_rate_pct"),
                     "avg R": s.get("avg_r"), "profit factor": s.get("profit_factor"), "net $": s.get("net_pnl"),
                     "return %": s.get("return_pct"), "max drawdown %": s.get("max_drawdown_pct"),
                     "1st half $": checks[0], "2nd half $": checks[1],
                     **({"stocks A $": checks[2], "stocks B $": checks[3]} if len(checks) > 2 else {}),
                     "holds up": f"{sum(x > 0 for x in checks)}/{len(checks)}"})
    import pandas as pd
    table = pd.DataFrame(rows).set_index("settings")
    table.to_csv(out / "compare.csv")
    (out / "compare.txt").write_text(table.to_string() + "\n", encoding="utf-8")
    from .backtest_xlsx import compare_workbook
    excel = _excel(lambda p: compare_workbook(p, table, curves, run_trades, cfg), out / "compare.xlsx")
    from .backtest_chart import compare_chart
    chart = _excel(lambda p: compare_chart(p, table, curves, run_trades, cfg), out / "compare-chart.html")
    print("\n" + table.to_string())
    n = 4 if halves is not None else 2
    print(f"\nholds up = in how many of the {n} checks it made money: the first and second half of the period"
          + (", and two random halves of the stocks" if halves is not None else "") + f". Only settings at {n}/{n} "
          "made money every way; a big net without that is probably luck - the stocks or weeks it happened to get.")
    if not cfg.backtest_results_dir:
        print(f"\nEach run's trades and full report are in {out}/ (compare.csv, report-*.txt, trades-*.csv)")
    return ([out / "compare-chart.html"] if chart else []) + ([out / "compare.xlsx"] if excel else []) + \
        [out / "compare.txt", out / "compare.csv"] + written


def _periods(trades, curve) -> list[float]:
    """Net P&L in the first and second half of the trading days."""
    days = sorted(curve.index) if len(curve) else []
    if len(days) < 2 or not len(trades):
        return [0.0, 0.0]
    import pandas as pd
    mid = pd.Timestamp(days[len(days) // 2]).date()
    d = pd.to_datetime(trades["date"]).dt.date
    return [round(float(trades.loc[d < mid, "pnl"].sum()), 2), round(float(trades.loc[d >= mid, "pnl"].sum()), 2)]


def _logging(workdir: Path) -> None:
    from datetime import date
    (workdir / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(workdir / "logs" / f"{date.today()}.log")],
    )


def _check_updates(cfg, bot, workdir: Path) -> None:
    """In the background: note a newer release for the dashboard and the log. Never holds anything up."""
    if not cfg.update_check:
        return
    import threading

    def go():
        from .updates import check
        up = check(cfg, workdir)
        if up:
            bot.update = up
            logging.getLogger("swingbot").info(
                "swingbot %s is out (you have %s) - after the close: git pull, then start the bot again. %s",
                up["tag"], up["current"], up.get("url", ""))
    threading.Thread(target=go, name="update-check", daemon=True).start()


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
            dash.on_close = bot.request_close           # "Close now": sells a trade that's ahead, on request
            dash.on_pause = bot.request_pause           # "Pause new trades" / "Resume"
            dash.on_stop = bot.request_stop             # "Move" on the Stop tile: a stop price you choose
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


# the switches each command reads (the rest would be silently ignored, so they're refused instead)
USES = {
    "fetch": {"config", "data", "bars"},
    "download": {"config", "bars", "days", "universe", "symbol"},
    "demo": {"config", "bars", "out", "symbol", "tries", "steps", "quick"},
    "backtest": {"config", "data", "out", "bars", "symbol", "days", "universe", "max_symbols", "tries", "steps",
                 "from_date", "to_date", "quick"},
    "scan": {"config", "bars", "symbol"},
    "run": {"config", "live", "bars", "symbol", "yes"},
    "site": {"config", "install", "uninstall", "stop"},
    "daemon": {"config", "live", "yes", "bars"},
}
HINT = {   # where a switch belongs, for the message
    "data": "fetch / backtest", "out": "backtest", "live": "run / daemon", "yes": "run / daemon", "days": "backtest / download",
    "universe": "backtest / download", "max_symbols": "backtest", "tries": "backtest", "steps": "backtest",
    "install": "site", "uninstall": "site", "stop": "site", "symbol": "run / scan / backtest / download",
    "bars": "run / scan / backtest / download / fetch",
    "from_date": "backtest", "to_date": "backtest", "quick": "backtest --try",
}


def _refuse_unused(ap, args, argv: list[str]) -> None:
    """Stop with a clear message when a switch is given that this command doesn't use."""
    uses = USES.get(args.command)
    if uses is None:
        return
    given = []
    for tok in argv:
        if tok.startswith("--"):
            act = ap._option_string_actions.get(tok.split("=", 1)[0])
            if act is not None and act.dest not in uses and act.dest not in ("help", "version"):
                given.append(tok.split("=", 1)[0])
    if not given:
        return
    which = ", ".join(dict.fromkeys(given))
    if args.command == "fetch":
        if args.symbol:
            what = f"--symbol {args.symbol}"
        elif "--universe" in given:
            what = f"--universe {args.universe}"
        else:
            what = "--universe sp500"
        sys.exit(f"fetch doesn't use {which}: it only gets the last week of bars for the watchlist in your config.\n"
                 f"For indexes, chosen stocks or more history, use download:\n"
                 f"  py -m swingbot download --days {args.days or 180} {what}")
    where = "; ".join(f"{opt} is for {HINT[ap._option_string_actions[opt].dest]}" for opt in dict.fromkeys(given)
                      if ap._option_string_actions[opt].dest in HINT)
    msg = f"{args.command} doesn't use {which}" + (f" ({where})" if where else "")
    if args.command == "run":                  # never stop a scheduled start over a stray switch: warn and go on
        print(f"Note: {msg} - ignoring it.", file=sys.stderr)
        return
    sys.exit(msg + ". Nothing was done.\npy -m swingbot --help lists what each switch is for.")


def main(argv=None) -> None:
    from . import __version__
    ap = argparse.ArgumentParser(prog="swingbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"swingbot {__version__}")
    ap.add_argument("command", choices=["demo", "backtest", "download", "fetch", "scan", "run", "site", "daemon"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="backtests")
    ap.add_argument("--live", action="store_true", help="send REAL orders")
    ap.add_argument("--bars", type=int, help="bar size in minutes for this run (overrides bar_minutes)")
    ap.add_argument("--symbol", help="skip the morning scan and trade this symbol (or contract) only; "
                                     "backtest: e.g. NVDA or NVDA,AMD, watched every day")
    ap.add_argument("--yes", action="store_true", help="skip the live confirmation prompt (for schedulers)")
    ap.add_argument("--days", type=int, help="backtest/download: this many calendar days of Alpaca history (e.g. 120)")
    ap.add_argument("--universe", default="sp500",
                    help="backtest --days and download: stocks to use: index names (sp500, nasdaq100, dow, russell2000, nasdaq) "
                         "joined with commas, or 'watchlist' for your config's list (default sp500)")
    ap.add_argument("--max-symbols", type=int, default=800, help="backtest --days: at most this many stocks")
    ap.add_argument("--try", dest="tries", action="append", default=[], metavar="SETTING=A,B",
                    help="backtest: compare values side by side, e.g. --try swing_n=4-8 --try breakeven_at_r=1-2:0.5 "
                         "(lists like 1,1.5 work too)")
    ap.add_argument("--step", dest="steps", action="append", default=[], metavar="N or SETTING=N",
                    help="backtest --try: gap between values in a range, e.g. --try breakeven_at_r=1-2 --step 0.5 "
                         "(1, 1.5, 2); --step breakeven_at_r=0.5 for just that setting. Whole-number settings "
                         "like swing_n keep steps of 1 unless given a whole step")
    ap.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD",
                    help="backtest: trade only from this day (earlier days still give the lines their history)")
    ap.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="backtest: trade only up to this day")
    ap.add_argument("--quick", action="store_true",
                    help="backtest --try: skip the checks on two halves of the stocks (about 3x faster)")
    ap.add_argument("--install", action="store_true",
                    help="site: start the results website now and every time you log on to Windows")
    ap.add_argument("--uninstall", action="store_true", help="site: stop it and don't start it at logon any more")
    ap.add_argument("--stop", action="store_true", help="site: stop the copy running in the background")
    args = ap.parse_args(argv)
    raw = sys.argv[1:] if argv is None else list(argv)
    _refuse_unused(ap, args, raw)
    args.data_given = any(t.split("=", 1)[0] == "--data" for t in raw)

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

    from .config import DOCKER
    if DOCKER and (args.command == "download" or (args.command == "backtest" and args.days)):
        sys.exit("Months of history for backtests come from Alpaca's market data, which isn't in this Public.com-only "
                 "Docker image.\nBacktest with your own CSV files instead (backtest --data FOLDER), or try the "
                 "strategy on made-up data with demo.")

    if args.command == "download":
        _download(cfg, args, workdir)
        return

    if args.command == "site":
        from . import results_site
        results_site.main(cfg, args, workdir)
        return

    if args.command == "daemon":
        from . import daemon
        daemon.main(cfg, args, workdir)
        return

    if args.command in ("demo", "backtest"):
        from . import backtest as bt
        start, end = _date_range(args)
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
            files = _compare(hist, cfg, args.tries, Path(args.out), _forced(args), args.steps, start, end, args.quick)
            if args.command == "backtest" and (kept := _archive(files, cfg, args, workdir)):
                print(f"Saved as {kept}")
            return
        print(f"Backtesting {len(hist)} stocks...", flush=True)
        trades, curve, picks = bt.run(hist, cfg, progress=_progress("days"), forced=_forced(args), start=start, end=end)
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
    from .data import append_csv, file_stem
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
                n = append_csv(fetcher.fetch_bars(s, "WEEK"), folder / f"{file_stem(s)}.csv")
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
        from . import notify
        if notify.install(cfg, "swingbot" if args.live else "swingbot (paper run)"):
            logging.getLogger("swingbot").info("Alerts on: %s", " and ".join(
                x for x, on in (("phone (ntfy)", bool(cfg.ntfy_topic)), ("email", cfg.alert_email)) if on))
        _check_updates(cfg, bot, workdir)
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
            bot.beat(force=True)                 # the results website then knows the bot has stopped
            if args.live:
                from .settings_log import record as record_settings
                record_settings(bot, cfg, workdir / "logs")
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
            if bot.prepared and bot.last_bar_seen is not None and bot.final:
                from .daycheck import run as evening_check
                evening_check(bot, cfg, workdir / "logs")      # live vs backtest, before the report so it's in it
            from .report import end_of_day
            end_of_day(bot, cfg, workdir / "logs")
    finally:
        client.close()


if __name__ == "__main__":
    main()
