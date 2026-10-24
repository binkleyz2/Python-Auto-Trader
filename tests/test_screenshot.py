"""End-of-day picture of the dashboard (logs/<date>-dashboard.jpg)."""
import logging

import pytest

from swingbot import screenshot
from swingbot.dashboard import Dashboard


def _snapshot(tmp_path):
    d = Dashboard(8871)
    d.update({"symbol": "NVDA", "pick_reason": "test", "closed": [], "bars": []})
    return d.snapshot(tmp_path / "2026-10-23-dashboard.html")


def test_jpg_of_the_snapshot(tmp_path):
    pytest.importorskip("PIL")
    if not screenshot.find_browser():
        pytest.skip("no Edge or Chrome on this computer")
    from PIL import Image
    jpg = screenshot.html_to_jpg(_snapshot(tmp_path))
    assert jpg == tmp_path / "2026-10-23-dashboard.jpg"
    with Image.open(jpg) as im:
        assert im.format == "JPEG" and im.width == screenshot.WIDTH
        assert 300 < im.height < screenshot.HEIGHTS[0]          # trimmed to the page, not the whole window


def test_no_browser_is_a_warning_not_a_crash(tmp_path, caplog):
    page = _snapshot(tmp_path)
    with caplog.at_level(logging.WARNING, logger="swingbot"):
        assert screenshot.save_jpg(page, browser=str(tmp_path / "no-such-browser.exe")) is None
    assert "Couldn't save the dashboard picture" in caplog.text
    assert page.exists() and not page.with_suffix(".jpg").exists()


def test_setting_is_on_by_default():
    from swingbot.config import Config
    assert Config().dashboard_jpg is True and Config().dashboard_jpg_browser == ""
