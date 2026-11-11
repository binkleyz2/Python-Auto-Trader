"""strategy: spy_momentum - the noise band, the checks, the backtest and the live bot (dry run)."""
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from swingbot import live, momentum as mm
from swingbot.config import Config
from swingbot.data import synthetic

ET = ZoneInfo("America/New_York")


def _day(d: str, opens_closes, start="09:30", minutes=30):
    """One day of bars: [(open, close), ...] every `minutes` from `start`."""
    idx = pd.date_range(f"{d} {start}", periods=len(opens_closes), freq=f"{minutes}min", tz=ET)
    o = [a for a, _ in opens_closes]
    c = [b for _, b in opens_closes]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c,
                         "volume": 1000}, index=idx)


def test_bands_by_hand():
    cfg = Config(strategy="spy_momentum", momentum_lookback_days=2)
    # 30-minute bars: the 9:30 bar ends at 10:00.
    # day 1: open 100, 10:00 close 101 (+1%);  day 2: open 200, 10:00 close 196 (-2%), closes 198
    d1 = _day("2026-11-02", [(100, 101), (101, 100.5)])
    d2 = _day("2026-11-03", [(200, 196), (196, 197), (197, 198)])
    d3 = _day("2026-11-04", [(197, 197.5), (197.5, 198)])
    days = mm.Days(pd.concat([d1, d2, d3]))
    s = days.sigma(date(2026, 11, 4), 2)
    assert s["10:00"] == pytest.approx((0.01 + 0.02) / 2)               # the average absolute move by 10:00
    b = mm.bands(days, date(2026, 11, 4), cfg)
    # today's open 197 is below yesterday's close 198: upper uses 198, lower uses 197
    assert b.at["10:00", "upper"] == pytest.approx(198 * 1.015)
    assert b.at["10:00", "lower"] == pytest.approx(197 * 0.985)
    assert mm.bands(days, date(2026, 11, 3), cfg) is None                # only one earlier day: not enough
    # live: today isn't in the history - the open is given
    b2 = mm.bands(mm.Days(pd.concat([d1, d2])), date(2026, 11, 4), cfg, day_open=197)
    assert b2.at["10:00", "upper"] == pytest.approx(b.at["10:00", "upper"])


def test_checks_and_decisions():
    cfg = Config(strategy="spy_momentum", direction="both")
    assert mm.is_check("10:00", cfg) and mm.is_check("15:30", cfg)
    assert not mm.is_check("09:30", cfg) and not mm.is_check("10:05", cfg) and not mm.is_check("10:15", cfg)
    # flat: above the band buys, below sells short, inside does nothing
    assert mm.decide(0, 105, 104, 96, 100, cfg, True) == (False, 1)
    assert mm.decide(0, 95, 104, 96, 100, cfg, True) == (False, -1)
    assert mm.decide(0, 100, 104, 96, 100, cfg, True) == (False, 0)
    assert mm.decide(0, 105, 104, 96, 100, cfg, False) == (False, 0)      # past the last entry time
    # long: held above max(upper, VWAP); out below it; turned around below the lower band
    assert mm.decide(1, 106, 104, 96, 105, cfg, True) == (False, 0)
    assert mm.decide(1, 104.5, 104, 96, 105, cfg, True) == (True, 0)      # under VWAP
    assert mm.decide(1, 95, 104, 96, 100, cfg, True) == (True, -1)
    assert mm.decide(1, 95, 104, 96, 100, Config(direction="long"), True) == (True, 0)   # long only: just out
    assert mm.decide(-1, 97, 104, 96, 95, cfg, True) == (True, 0)         # short: above min(lower, VWAP)
    assert mm.stop_level(1, 104, 96, 105, cfg) == 105 and mm.stop_level(-1, 104, 96, 95, cfg) == 95


def test_sizing_targets_volatility():
    cfg = Config(strategy="spy_momentum", momentum_lookback_days=3, momentum_vol_target_pct=2, momentum_max_leverage=4)
    closes = [100, 101, 99, 100, 100.5]
    df = pd.concat([_day(f"2026-11-0{i + 2}", [(c, c), (c, c)]) for i, c in enumerate(closes)])
    days = mm.Days(df)
    vol = days.daily_vol(date(2026, 11, 7), 3)
    assert vol == pytest.approx(np.std(np.diff([101, 99, 100, 100.5]) / np.array([101, 99, 100]), ddof=1))
    assert mm.leverage(days, date(2026, 11, 7), cfg) == pytest.approx(min(4, 0.02 / vol))
    assert mm.shares_for(10_000, days, date(2026, 11, 7), Config(momentum_lookback_days=3), day_open=50) == 200  # 1x


def test_backtest_on_a_random_walk_makes_nothing_before_costs():
    """No look-ahead: on prices with no memory the strategy can't make money before costs."""
    rng = np.random.default_rng(1)
    rows, p = [], 400.0
    for d in pd.bdate_range("2024-01-02", periods=300):
        p *= np.exp(rng.normal(0, 0.004))
        for ts in pd.date_range(d + pd.Timedelta(hours=9, minutes=30), periods=78, freq="5min", tz=ET):
            r = rng.normal(0, 0.0012, 5)
            path = p * np.exp(np.cumsum(r))
            rows.append((ts, p, max(p, path.max()), min(p, path.min()), path[-1], 1e5))
            p = path[-1]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    t, curve, _ = mm.run({"SPY": df}, Config(strategy="spy_momentum", direction="both", slippage_pct=0))
    assert len(t) > 200
    assert abs(t.r_multiple.mean()) < 0.06                     # avg trade within ±0.06% of the position
    assert set(t.entry_time.str[-2:]) <= {"00", "30"}           # entries only right after a check
    assert (t.exit_time <= "15:50").all()
    assert t.groupby("date").size().max() <= Config().max_trades_per_day


class FakeClient:
    def __init__(self, df):
        self.df, self.now = df, None

    def get_bars(self, symbol, period, **kw):
        from types import SimpleNamespace
        d = self.df[self.df.index < pd.Timestamp(self.now)]
        if period.name == "DAY":
            d = d[d.index.date == self.now.date()]
        bars = [SimpleNamespace(timestamp=ts.isoformat(), open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume) for ts, r in d.iterrows()]
        return SimpleNamespace(regular_market=SimpleNamespace(bars=bars))

    def place_order(self, *a, **k):
        raise AssertionError("dry run must not place orders")

    def get_portfolio(self):
        from types import SimpleNamespace
        return SimpleNamespace(total_account_value=25_000, equity=[], positions=[], orders=[])


def test_live_dry_run_matches_the_backtest(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg = Config(strategy="spy_momentum", direction="both", slippage_pct=0)
    df = synthetic("SPY", days=40, seed=5)
    days = sorted(set(df.index.date))
    client = FakeClient(df)
    traded = 0
    for day in days[-6:]:
        (tmp_path / str(day)).mkdir()
        bot = live.Bot(client, cfg, live=False, workdir=tmp_path / str(day))
        clock = {"t": datetime(day.year, day.month, day.day, 9, 0, tzinfo=ET)}
        monkeypatch.setattr(live, "now_et", lambda: clock["t"])
        client.now = clock["t"]
        bot.morning_scan()
        assert bot.symbol == "SPY" and bot.watch == ["SPY"] and "intraday momentum" in bot.pick_reason
        t = datetime(day.year, day.month, day.day, 9, 36, tzinfo=ET)
        while t.time() < live._t(cfg.flatten_time):
            clock["t"] = client.now = t
            bot.step()
            t += pd.Timedelta(minutes=5)
        bot.flatten("end of day")
        assert bot.pos is None
        bt, _, _ = mm.run({"SPY": df}, cfg, start=day, end=day)
        # (live, the entry clock is when the bot saw the bar: a minute after the check)
        live_tr = [(x["side"], x["entry_time"][:4]) for x in bot.closed]
        bt_tr = [(1 if r.side == "long" else -1, r.entry_time[:4]) for r in bt.itertuples()]
        assert live_tr == bt_tr, day                               # same trades, same times
        traded += len(live_tr)
        assert "bands" in bot.view and bot.view["bands"]
    assert traded > 0
    checks = [r.message for r in caplog.records if r.message.startswith("CHECK ")]
    assert checks and all(m.split()[1][-2:] in ("00", "30") for m in checks)


def test_config():
    with pytest.raises(ValueError, match="strategy"):
        Config(strategy="moon").validate()
    with pytest.raises(ValueError, match="momentum_check_minutes"):
        Config(momentum_check_minutes=7).validate()
