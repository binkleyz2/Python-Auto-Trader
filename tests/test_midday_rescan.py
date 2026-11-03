"""Automatic midday rescan: runs once at midday_rescan_time; in a trade it waits until the trade closes,
as long as that is before midday_rescan_by."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from swingbot import live
from swingbot.config import Config
from test_restart import _setup

ET = ZoneInfo("America/New_York")


def at(hh, mm):
    return datetime(2026, 11, 3, hh, mm, tzinfo=ET)


@pytest.fixture
def bot(tmp_path):
    b = live.Bot(None, Config(), live=False, workdir=tmp_path)
    b.symbol = "AAPL"
    return b


def test_not_before_the_time(bot):
    bot.check_midday_rescan(at(12, 40))
    assert not bot.pending_rescan and not bot.midday_done


def test_runs_once_when_flat(bot):
    bot.check_midday_rescan(at(12, 45))
    assert bot.pending_rescan and bot.midday_done
    bot.pending_rescan = False
    bot.check_midday_rescan(at(13, 0))
    assert not bot.pending_rescan                      # only once a day


def test_waits_for_the_trade_to_close(bot):
    bot.pos = {"stop": 10.0, "side": 1}
    bot.check_midday_rescan(at(12, 45))
    assert not bot.pending_rescan and bot.midday_waiting and not bot.midday_done
    assert "rescan after it closes" in bot._auto_state()
    bot.check_midday_rescan(at(13, 30))
    assert not bot.pending_rescan                      # still in the trade
    bot.pos = None                                     # trade closed
    bot.check_midday_rescan(at(14, 10))
    assert bot.pending_rescan and bot.midday_done and not bot.midday_waiting


def test_dropped_if_the_trade_closes_at_or_after_the_cutoff(bot):
    bot.pos = {"stop": 10.0, "side": 1}
    bot.check_midday_rescan(at(12, 45))
    bot.pos = None
    bot.check_midday_rescan(at(15, 0))
    assert not bot.pending_rescan and bot.midday_done


def test_started_after_the_cutoff(bot):
    bot.check_midday_rescan(at(15, 20))
    assert not bot.pending_rescan and bot.midday_done


@pytest.mark.parametrize("setup", [
    lambda b: setattr(b, "forced_symbol", "AAPL"),
    lambda b: setattr(b, "halted", True),
    lambda b: setattr(b, "trades_today", 99),
])
def test_skipped_when_it_could_not_matter(bot, setup):
    setup(bot)
    bot.check_midday_rescan(at(12, 45))
    assert not bot.pending_rescan and bot.midday_done


def test_off_and_custom_times(tmp_path):
    off = live.Bot(None, Config(midday_rescan_time=""), live=False, workdir=tmp_path)
    off.check_midday_rescan(at(13, 0))
    assert not off.pending_rescan and not off.midday_done
    early = live.Bot(None, Config(midday_rescan_time="11:00", midday_rescan_by="12:00"), live=False, workdir=tmp_path)
    early.check_midday_rescan(at(11, 5))
    assert early.pending_rescan


@pytest.mark.parametrize("key,value", [("midday_rescan_time", "noon"), ("midday_rescan_by", "25:00"),
                                       ("midday_rescan_by", "")])
def test_bad_times_are_refused(key, value):
    with pytest.raises(ValueError, match=key):
        Config(**{key: value}).validate()


def test_a_restart_does_not_rescan_again(tmp_path, monkeypatch):
    new_bot, t, clock = _setup(tmp_path, monkeypatch)
    a = new_bot()
    a.prepare()
    a.midday_done = True
    a.save_state()
    b = new_bot()
    b.prepare()
    assert b.midday_done
