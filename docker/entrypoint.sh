#!/bin/sh
# swingbot container start: first-run setup, file ownership, then the command (as an ordinary user).
set -e
cd /data

setup() {
  created=""
  if [ ! -f config.yaml ]; then
    # this image's example: Public.com only, with the settings a container needs
    cp /app/config.example.yaml config.yaml
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
    echo "  1. Put your Public.com API secret key and account ID in .env (and the SMTP login for the email"
    echo "     report, if you want it)."
    echo "  2. Check config.yaml: the watchlist, risk, and set your own dashboard_key."
    echo "  3. Start the container again. It's a dry run (no orders) until you start it with: daemon --live --yes"
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
