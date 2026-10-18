FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml ./
RUN mkdir bot && touch bot/__init__.py && pip install . && rm -rf bot

COPY alembic.ini ./
COPY migrations ./migrations
COPY bot ./bot
RUN pip install --no-deps .

# Never run as root. Nothing is written to disk: logs go to stdout (docker compose logs).
RUN useradd --create-home bot
USER bot

# Apply pending migrations, then start the bot. `exec` makes Python the container's main process,
# so `docker stop` (SIGTERM) reaches it and it shuts down cleanly (flushes alerts, closes the
# Binance stream). Without it the shell would swallow the signal and Docker would SIGKILL after
# the grace period.
CMD ["sh", "-c", "alembic upgrade head && exec python -m bot.main"]
