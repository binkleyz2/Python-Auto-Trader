"""Builds the Docker image's config.example.yaml and .env.example from the normal ones: Public.com only.

Run while the image is built (python docker/templates.py SRC_DIR OUT_DIR). The normal files in the repo are
left alone; only the copies inside the image change:
  config: broker fixed to public; the Alpaca and NinjaTrader settings removed; the settings a container needs
          (dashboard reachable through the published port, no browser to open, no dashboard picture)
  .env:   the Public keys, email and alerts only - no Alpaca or NinjaTrader keys
"""
import re
import sys
from pathlib import Path

BROKER = """# Broker: this Docker image trades on Public.com only (it has no Alpaca or NinjaTrader).
# Public has no paper (practice) account: the container is a DRY RUN - prices, signals and the dashboard, but no
# orders - until you start it with "daemon --live --yes". Watch it as a dry run for a while first.
broker: public
"""
SET = {
    "dashboard_lan": "dashboard_lan: true        # in Docker: reachable through the published port (needs the key)",
    "dashboard_open_browser": "dashboard_open_browser: false   # no browser in a container",
    "dashboard_jpg": "dashboard_jpg: false       # no browser in the image to take the picture",
}


def config(text: str) -> str:
    out, broker_done = [], False
    for line in text.splitlines():
        key = line.split(":", 1)[0].strip() if re.match(r"^[a-z_]+\s*:", line) else None
        if line.startswith("# Broker:"):
            continue                                    # replaced below
        if key == "broker":
            out.append(BROKER.rstrip("\n"))
            broker_done = True
            continue
        if key and (key.startswith(("alpaca_", "ninja_")) or key == "price_data"):
            continue                                    # Alpaca / NinjaTrader settings: not in this image
        if key in SET:
            out.append(SET[key])
            continue
        out.append(line)
    if not broker_done:
        out.insert(0, BROKER.rstrip("\n"))
    text = "\n".join(out) + "\n"
    text = text.replace("only stocks your broker can trade", "only stocks Public can trade")
    return text


def env(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("# ---"):
            skip = bool(re.search(r"alpaca|ninjatrader", line, re.I))
        if skip:
            continue
        out.append(line)
    text = "\n".join(out).rstrip() + "\n"
    text += ("\n# --- Optional ---\n# A login token for your own ntfy server (alerts), and the password of the private key in\n"
             "# site_cert, if it has one:\n#NTFY_TOKEN=\n#SITE_CERT_PASSWORD=\n")
    return re.sub(r"\n{3,}", "\n\n", text)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    (dst / "config.example.yaml").write_text(config((src / "config.example.yaml").read_text(encoding="utf-8")),
                                             encoding="utf-8")
    (dst / ".env.example").write_text(env((src / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
    print("Docker templates: Public.com only")
