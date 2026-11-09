# swingbot in a container, for Public.com (this image has no Alpaca or NinjaTrader). The image holds only the program: your config.yaml, .env (API keys), logs and
# backtest data live in a folder on the host mounted at /data, so the image can be shared and updated freely.
#
#   docker run --rm -v "$PWD/swingbot-data:/data" IMAGE setup     first time: creates config.yaml and .env
#   docker run -d --name swingbot --restart unless-stopped -p 8765:8765 -v "$PWD/swingbot-data:/data" IMAGE
#
# The default command is the always-on dry run ("daemon"); for real orders: ... IMAGE daemon --live --yes
# See README.md, "Docker".
FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="swingbot" \
      org.opencontainers.image.description="Swing-point trend-line day trading bot for Public.com" \
      org.opencontainers.image.source="https://github.com/binkleyz2/Python-Auto-Trader" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York PYTHONPATH=/app SWINGBOT_DOCKER=public

WORKDIR /app
COPY requirements.txt .
# Public.com only: no test tools, no Alpaca library
RUN grep -viE '^(pytest|alpaca)' requirements.txt > /tmp/req.txt \
 && pip install -r /tmp/req.txt tzdata \
 && rm /tmp/req.txt \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin swingbot

COPY swingbot/ swingbot/
COPY config.example.yaml .env.example watchlist_*.yaml LICENSE README.md CHANGELOG.md ./
COPY docker/ /tmp/docker/
# this image's own config.example.yaml and .env.example: Public.com only (the repo's files are unchanged);
# the Alpaca and NinjaTrader code is left out
RUN python /tmp/docker/templates.py /app /app \
 && rm -f swingbot/alpaca_live.py swingbot/ninja_live.py \
 && install -m 755 /tmp/docker/entrypoint.sh /usr/local/bin/swingbot-entrypoint \
 && rm -rf /tmp/docker \
 && python -m compileall -q swingbot

VOLUME /data
WORKDIR /data
EXPOSE 8765 4334
ENTRYPOINT ["swingbot-entrypoint"]
CMD ["daemon"]
