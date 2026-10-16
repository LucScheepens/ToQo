# ToQo

Create, publish and run sports tournaments: categories, teams, round robin / single &
double elimination / Swiss / pools → playoffs, advancement between stages, automatic
scheduling onto courts and fields, live results, standings and brackets, and a public
tournament site with a QR code.

The full technical specification is in [SPEC.md](SPEC.md).

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo      # optional: demo event, mid-tournament
.\.venv\Scripts\python.exe manage.py runserver
```

Open http://127.0.0.1:8000. The demo login is `demo` / `demo-pass-123`.
`seed_demo --finish` plays the demo to the end.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest engine.tests     # pure engine
.\.venv\Scripts\python.exe manage.py test tournaments   # services, views, full lifecycle
```

## Layout

| Path | What |
|---|---|
| `engine/` | Framework-free tournament logic: round robin, brackets, Swiss pairing, standings/tiebreakers, scheduler |
| `tournaments/models.py` | Data model (SPEC §3) |
| `tournaments/services/` | Domain operations: `structure` (formats, stages, slots), `resolution` (re-derives teams/byes/advancement from results), `results`, `transitions`, `scheduling`, `publishing`, `access` |
| `tournaments/views/` | `setup` and `run` (organizer), `public` (participant site), `api` (JSON) |
| `templates/`, `static/` | Server-rendered UI, one CSS file, one small JS file (drag & drop, live updates) |

The key rule: **results are the only source of truth.** After any change,
`services.resolution.recompute_category` re-derives bracket sides, byes, Swiss pairings
and advanced slots. If that would wipe other results, the change is rolled back and the
organizer is asked to confirm first.

## Deploying with Docker

The repo ships a two-container setup:

| Container | What it does |
|---|---|
| `web` | Django under Gunicorn. Runs migrations on start. Serves CSS/JS itself (WhiteNoise). |
| `caddy` | Public entry point on ports 80/443. Gets and renews the HTTPS certificate automatically and serves uploaded images (`/media`). |

The SQLite database and uploads live in the `toqo-data` volume (`/data` in the container), so
they survive rebuilds and updates.

### First deploy

You need a Linux server with Docker installed (`curl -fsSL https://get.docker.com | sh`) and a
domain name whose DNS A record points at the server's IP. Ports 80 and 443 must be open.

```sh
git clone <your repo url> toqo && cd toqo
cp .env.example .env
nano .env                      # set TOQO_DOMAIN, TOQO_ALLOWED_HOSTS and TOQO_SECRET_KEY
docker compose up -d --build
docker compose exec web python manage.py createsuperuser   # or bring your existing database, below
```

Open `https://<your domain>`. The first request can take a few seconds while Caddy fetches the
certificate.

### Bringing your existing data

Copy `db.sqlite3` (and the `media/` folder, if you uploaded banners or images) to the server, then:

```sh
docker compose stop web
docker compose run --rm -T --entrypoint sh web -c 'cat > /data/db.sqlite3' < db.sqlite3
tar -c media | docker compose run --rm -T --entrypoint sh web -c 'tar -x -C /data'
docker compose start web       # migrations run automatically
```

### Everyday commands

```sh
docker compose logs -f web                  # app logs
git pull && docker compose up -d --build    # update to a new version
docker compose run --rm -T --entrypoint sh web -c 'cat /data/db.sqlite3' > backup-$(date +%F).sqlite3
```

Make the backup **while the site is quiet**, or stop `web` first, so the copy is consistent.

## Production notes

Settings come from environment variables: `TOQO_SECRET_KEY`, `TOQO_DEBUG` (the image sets `0`),
`TOQO_ALLOWED_HOSTS`, `TOQO_TIME_ZONE`, `TOQO_DB_PATH`, `TOQO_MEDIA_ROOT` and `TOQO_HTTPS`.
SQLite runs in WAL mode, which comfortably handles a handful of scorekeepers entering results at once. Payments are a stub
(`tournaments/services/publishing.py: ensure_paid`), and email uses the console backend.
