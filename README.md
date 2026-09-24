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

## Production notes

Set `TOQO_SECRET_KEY`, `TOQO_DEBUG=0` and `TOQO_ALLOWED_HOSTS`, point `DATABASES` at
PostgreSQL if you expect concurrent scorekeepers, run `collectstatic`, and serve media
files (banners) from your web server. Payments are a stub
(`tournaments/services/publishing.py: ensure_paid`), and email uses the console backend.
