"""Picture of the end-of-day dashboard page (logs/<date>-dashboard.jpg).

Uses the Edge or Chrome already on the PC, started hidden ("headless") on the saved snapshot page, so no
extra browser download is needed. Pillow turns the screenshot into a JPG and trims the empty space below
the page. Any problem is logged as a warning; it never stops the bot.

Make a picture of an older snapshot by hand:

    py -m swingbot.screenshot logs\\2026-10-23-dashboard.html
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("swingbot")

WIDTH = 1280                 # the page is at most 1180 px wide, so this shows it with margins
HEIGHTS = (2400, 4800, 9600) # tried in turn until the whole page fits
QUALITY = 88


def find_browser(configured: str = "") -> str | None:
    """Path of Edge or Chrome (or `configured`, when set and it exists)."""
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        for r in rel:
            for root in roots:
                if root and (Path(root) / r).exists():
                    return str(Path(root) / r)
    elif sys.platform == "darwin":
        for p in ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            if Path(p).exists():
                return p
    for name in ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _shoot(browser: str, page: Path, png: Path, height: int, profile: str) -> None:
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions", "--force-color-profile=srgb",
           "--blink-settings=preferredColorScheme=1",      # light theme, whatever Windows is set to
           f"--user-data-dir={profile}", f"--window-size={WIDTH},{height}",
           "--virtual-time-budget=5000",                   # let the page draw its charts first
           f"--screenshot={png}", page.resolve().as_uri()]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")                      # Linux as root only (not used on Windows)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
    if not png.exists() or png.stat().st_size == 0:
        raise RuntimeError(f"{Path(browser).name} made no screenshot")


def _content_bottom(img) -> int:
    """Row just below the last row that isn't plain page background."""
    from PIL import ImageChops
    rgb = img.convert("RGB")
    bg = rgb.getpixel((2, rgb.height - 1))
    plain = rgb.copy()
    plain.paste(bg, (0, 0, rgb.width, rgb.height))
    box = ImageChops.difference(rgb, plain).getbbox()
    return box[3] if box else rgb.height


def html_to_jpg(page: Path, jpg: Path | None = None, browser: str = "") -> Path:
    """Save a JPG of `page` (a dashboard snapshot). Raises with a plain message on failure."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow isn't installed: run  py -m pip install --user -r requirements.txt") from None
    exe = find_browser(browser)
    if not exe:
        raise RuntimeError("no Edge or Chrome found (set dashboard_jpg_browser in config.yaml)")
    page = Path(page)
    jpg = Path(jpg) if jpg else page.with_suffix(".jpg")
    with tempfile.TemporaryDirectory(prefix="swingbot-shot-") as tmp:
        png = Path(tmp) / "page.png"
        for h in HEIGHTS:
            png.unlink(missing_ok=True)
            _shoot(exe, page, png, h, str(Path(tmp) / "profile"))
            with Image.open(png) as im:
                bottom = _content_bottom(im)
                if bottom < im.height - 8 or h == HEIGHTS[-1]:
                    im.convert("RGB").crop((0, 0, im.width, min(im.height, bottom + 24))) \
                      .save(jpg, "JPEG", quality=QUALITY, optimize=True)
                    break
    return jpg


def save_jpg(page: Path, browser: str = "") -> Path | None:
    """End-of-day hook: never raises."""
    try:
        jpg = html_to_jpg(page, browser=browser)
        log.info("Dashboard picture saved: %s", jpg)
        return jpg
    except Exception as e:
        log.warning("Couldn't save the dashboard picture (%s). The .html snapshot is still there.", e)
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: py -m swingbot.screenshot logs\\<date>-dashboard.html [more files...]")
    for f in sys.argv[1:]:
        try:
            print("saved", html_to_jpg(Path(f)))
        except Exception as e:
            print(f"{f}: {e}")
