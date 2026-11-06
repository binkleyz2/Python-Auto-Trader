"""Is a newer swingbot out? One look at the GitHub releases a few times a day (cached), never in the way."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("swingbot.updates")
CACHE_HOURS = 6


def _ver(tag: str) -> tuple:
    parts = []
    for x in str(tag).lstrip("vV").split("."):
        num = "".join(ch for ch in x if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def newer(latest: str, current: str) -> bool:
    return _ver(latest) > _ver(current)


def latest_release(repo: str, cache: Path | None = None, timeout: float = 8) -> dict | None:
    """{"tag", "url", "published"} of the newest release, or None if it can't be found out right now."""
    if cache is not None:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("repo") == repo and time.time() - c.get("checked", 0) < CACHE_HOURS * 3600:
                return c.get("release")
        except (OSError, ValueError):
            pass
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "swingbot"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        rel = dict(tag=d.get("tag_name", ""), url=d.get("html_url", ""), published=d.get("published_at", "")[:10])
    except Exception as e:
        log.debug("update check failed: %s", e)
        return None
    if cache is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(dict(repo=repo, checked=time.time(), release=rel)), encoding="utf-8")
        except OSError:
            pass
    return rel


def check(cfg, workdir: Path) -> dict | None:
    """A newer release than this one: {"tag", "url", "published", "current"}; else None."""
    from . import __version__
    if not getattr(cfg, "update_check", True) or not str(getattr(cfg, "update_repo", "") or "").strip():
        return None
    rel = latest_release(cfg.update_repo.strip(), Path(workdir) / "logs" / "update-check.json")
    if rel and rel.get("tag") and newer(rel["tag"], __version__):
        return dict(rel, current=__version__)
    return None
