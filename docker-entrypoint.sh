#!/bin/sh
# Applies pending migrations, then starts the bot. A failed migration stops the container, so
# the platform keeps the previous version running instead of switching over.
# SKIP_MIGRATIONS=1 skips it (debugging a container by hand).
set -e
if [ "${SKIP_MIGRATIONS:-}" != "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi
# exec: Python becomes the main process, so `docker stop` (SIGTERM) reaches it.
exec "$@"
