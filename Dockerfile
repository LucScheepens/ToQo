# ToQo: Django served by Gunicorn. Static files are served by WhiteNoise; uploaded media by Caddy.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOQO_DEBUG=0 \
    TOQO_DB_PATH=/data/db.sqlite3 \
    TOQO_MEDIA_ROOT=/data/media

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# collectstatic needs some secret key; the real one comes from the environment at runtime.
RUN TOQO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && chmod +x docker/entrypoint.sh \
    && useradd --create-home --uid 1000 toqo \
    && mkdir -p /data/media && chown -R toqo:toqo /data /app

USER toqo
VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "toqo.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
