"""Always-on mode: run the trading session every market day by itself (for Docker, a server, a NAS).

    py -m swingbot daemon              dry run every day (no orders)
    py -m swingbot daemon --live --yes real orders every day, without the LIVE prompt

It does what Windows Task Scheduler does on a PC: on each market day (weekends and NYSE holidays skipped) it
starts `run` at `daemon_start_time` (08:00 ET), waits for the session to finish, then sleeps until the next
one. Started during a session (after a restart, say), it joins today's session at once, and the bot picks
up today's state and any open trade as usual. If a session ends with an error it's started again (up to 3
times a day, a minute apart). The bot's watchdog alerts and the results website work the same way.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("swingbot.daemon")
RETRIES = 3


def _hm(s: str):
    return datetime.strptime(str(s).strip(), "%H:%M").time()


def next_start(now: datetime, cfg, done_today: bool) -> datetime | None:
    """When to start the next session (``now`` or later); None = start now."""
    from .results_site import market_day
    start, end = _hm(cfg.daemon_start_time), _hm(cfg.flatten_time)
    if market_day(now.date()) and not done_today and now.time() < end:
        if now.time() >= start:
            return None
        return datetime.combine(now.date(), start, tzinfo=ET)
    d = now.date() + timedelta(days=1)
    while not market_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, start, tzinfo=ET)


def _session_done(workdir, day) -> bool:
    import json
    try:
        st = json.loads((workdir / "logs" / f"{day}-state.json").read_text(encoding="utf-8"))
        return bool(st.get("final"))
    except (OSError, ValueError):
        return False


def main(cfg, args, workdir) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    run_args = ["run", "--config", args.config]
    if args.live:
        if not args.yes:
            sys.exit("daemon --live places real orders every day without asking: add --yes to confirm.")
        run_args += ["--live", "--yes"]
    if args.bars:
        run_args += ["--bars", str(args.bars)]
    print(f"swingbot daemon: {'LIVE orders' if args.live else 'dry run (no orders)'}, sessions start at "
          f"{cfg.daemon_start_time} ET on market days. Ctrl+C stops it.", flush=True)
    tries: dict[str, int] = {}
    while True:
        now = datetime.now(ET)
        day = str(now.date())
        done = _session_done(workdir, day) or tries.get(day, 0) >= RETRIES
        at = next_start(now, cfg, done)
        if at is not None:
            print(f"{now:%Y-%m-%d %H:%M} ET - next session {at:%a %Y-%m-%d %H:%M} ET", flush=True)
            while datetime.now(ET) < at:                  # short sleeps: survives clock changes and suspends
                time.sleep(min(300, max(1.0, (at - datetime.now(ET)).total_seconds())))
            continue
        tries[day] = tries.get(day, 0) + 1
        print(f"{now:%H:%M} ET - starting today's session (try {tries[day]})", flush=True)
        code = subprocess.call([sys.executable, "-m", "swingbot", *run_args], cwd=str(workdir))
        if code == 0:
            tries[day] = RETRIES                          # finished normally: done for today
        else:
            print(f"The session ended with an error (exit {code}); trying again in a minute", flush=True)
            time.sleep(60)
