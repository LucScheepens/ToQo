#!/bin/sh
# Apply database migrations on every start, then run the given command (Gunicorn by default).
set -e
mkdir -p "$(dirname "$TOQO_DB_PATH")" "$TOQO_MEDIA_ROOT"
python manage.py migrate --noinput
exec "$@"
