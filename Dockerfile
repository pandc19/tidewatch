# Built by Coolify on the droplet (push to main -> build -> deploy), and by `docker compose build`.
# Stages: build (locked dependencies into a virtualenv) -> runner (the venv, migrations, no tools).

FROM python:3.12-slim AS build
# uv installs exactly what uv.lock says. Pinned, like the lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
# Dependencies first, from the manifests alone, so this layer is rebuilt only when they change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
# Then the bot itself, installed (not editable) so the runner needs no source tree.
# --reinstall-package: uv rebuilds a local project only when pyproject.toml changes, so with the
# shared cache a build reused a wheel of older bot code (seen on Coolify, 2026-10-26). The diff
# fails the build if the installed bot is not exactly the source just copied.
COPY bot ./bot
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --reinstall-package tidewatch \
    && diff -r -x __pycache__ bot /opt/venv/lib/python3.12/site-packages/bot

FROM python:3.12-slim AS runner
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/opt/venv/bin:$PATH

# Never run as root. Nothing is written to disk but the heartbeat file in /tmp: logs go to
# stdout (the platform's log view, or docker compose logs).
RUN useradd --create-home bot
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER bot

# No port: the bot only makes outbound connections. Liveness is the heartbeat file the engine
# touches every tick (bot/instance.py); a hung engine stops it and the container turns unhealthy.
# The start period covers the migrations and the startup checks (a few seconds normally). During
# a deploy the new container then waits for the old one to release the database, and keeps
# beating while it waits. Coolify sits out the whole start period before its first check, so this
# value is also the minimum time a deploy takes beyond the build (it was 120 s on the first two).
HEALTHCHECK --interval=15s --timeout=10s --start-period=45s --retries=4 \
  CMD python -m bot.instance --path "${HEARTBEAT_PATH:-/tmp/tidewatch-heartbeat}" --max-age "${HEARTBEAT_MAX_AGE_SECONDS:-180}"

# SIGTERM reaches Python (the entrypoint execs it) and the bot shuts down cleanly: it flushes
# alerts and closes the Binance stream, which takes up to about 20 seconds. Give the container
# at least 40 seconds to stop (compose sets it; in Coolify see docs/coolify.md).
STOPSIGNAL SIGTERM
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
