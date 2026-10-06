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

RUN useradd --create-home bot && mkdir -p /app/logs && chown -R bot /app/logs
USER bot

# Apply pending migrations, then start the bot.
CMD ["sh", "-c", "alembic upgrade head && python -m bot.main"]
