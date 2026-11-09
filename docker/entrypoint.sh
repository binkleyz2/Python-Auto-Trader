#!/bin/sh
# swingbot container start: first-run setup, file ownership, then the command (as an ordinary user).
set -e
cd /data

setup() {
  created=""
  if [ ! -f config.yaml ]; then
    # the example, with the settings a container needs: the dashboard reachable from outside the container,
    # no browser to open, no dashboard picture (no browser in the image)
    sed -e 's/^dashboard_lan: .*/dashboard_lan: true        # in Docker: reachable through the published port (needs the key)/' \
        -e 's/^dashboard_open_browser: .*/dashboard_open_browser: false/' \
        -e 's/^dashboard_jpg: .*/dashboard_jpg: false       # no browser in the image/' \
        /app/config.example.yaml > config.yaml
    created="$created config.yaml"
  fi
  if [ ! -f .env ]; then
    cp /app/.env.example .env
    chmod 600 .env
    created="$created .env"
  fi
  mkdir -p logs
  if [ -n "$created" ]; then
    echo "swingbot: created$created in the folder you mounted at /data."
    echo "  1. Put your broker API keys in .env (and the SMTP login for the email report, if you want it)."
    echo "  2. Check config.yaml: broker, alpaca_paper, watchlist, risk, and set your own dashboard_key."
    echo "  3. Start the container again."
    return 1
  fi
  return 0
}

if [ "$1" = "setup" ]; then
  setup || true
  [ "$(id -u)" = "0" ] && chown -R 1000:1000 /data 2>/dev/null || true
  exit 0
fi

if ! setup; then
  [ "$(id -u)" = "0" ] && chown -R 1000:1000 /data 2>/dev/null || true
  exit 1
fi

# running as root (the default): make the data folder the bot's, then drop to the swingbot user
if [ "$(id -u)" = "0" ]; then
  chown -R 1000:1000 /data 2>/dev/null || echo "swingbot: couldn't change the owner of /data - writes may fail"
  exec setpriv --reuid=1000 --regid=1000 --init-groups python -m swingbot "$@"
fi
exec python -m swingbot "$@"
