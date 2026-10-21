# Twitch Growth

[![Tests](https://github.com/snuffsprojects/twitch-growth/actions/workflows/tests.yml/badge.svg)](https://github.com/snuffsprojects/twitch-growth/actions/workflows/tests.yml)

Self-hosted Twitch stream analytics and interactive broadcast tools. Twitch Growth records your own channel data in SQLite, turns it into evidence-conscious coaching insights, recommends games from your Steam library, and provides OBS-ready Streamline and Stream Boss overlays.

The application starts empty. It does not ship with demonstration analytics, credentials, or somebody else's channel history.

## Features

- Stream, viewer, chat, category, clip, raid and community-event history
- Twitch EventSub WebSocket events plus one-minute Helix viewer sampling
- Evidence-weighted schedule, retention, category-switch and coaching analysis
- Private stream journal, campaign redirects and JSON/Markdown exports
- Steam Game Finder using owned games, Twitch demand and your channel history
- The Streamline live journey visualisation and transparent OBS overlay
- Stream Boss chat-powered encounters and transparent OBS overlays
- Protected settings, write-only secrets, admin sessions and CSRF protection
- SQLite storage with no external database service

## Quick start with Docker Compose

Requirements: Docker with the Compose plugin and a Twitch Developer application.

```bash
git clone https://github.com/snuffsprojects/twitch-growth.git
cd twitch-growth
cp .env.example .env
```

Generate an admin password hash and session secret:

```bash
python -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash("choose-a-strong-password"))'
python -c 'import secrets; print(secrets.token_hex(32))'
```

Put those values and your Twitch Client ID into `.env`, then start both processes:

```bash
docker compose up -d --build
```

Open `http://127.0.0.1:5120/`, sign in, and authorize your Twitch account. The app and collector share the named `twitch-growth-data` volume.

The supplied Compose file binds only to loopback. Use a TLS-enabled reverse proxy for access from another machine. Do not publish port 5120 directly to the internet.

## Python installation

Twitch Growth requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Export the settings from `.env`, then run the web process and collector separately:

```bash
.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:5120 app:app
.venv/bin/python collector.py --loop
```

Generic systemd unit and environment examples are in `systemd/`. Adjust their user, group, paths and prefix for your host before installing them.

## Twitch setup

1. Create an application in the [Twitch Developer Console](https://dev.twitch.tv/console/apps).
2. Set `TWITCH_CLIENT_ID`. A client secret is optional for the device authorization flow but recommended for token refresh compatibility.
3. Open Twitch Growth and select **Authorise with Twitch**.
4. Complete the device authorization in Twitch.
5. Keep the collector running continuously.

Access and refresh tokens are stored server-side in SQLite and are never returned by the API. Protect the database as you would any other credential store.

## Configuration

The protected `/settings` page manages the Twitch application identity, optional client secret, Steam connection, channel URL and timezone. Environment variables take precedence where noted.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TWITCH_CLIENT_ID` | empty | Twitch application Client ID |
| `TWITCH_CLIENT_SECRET` | empty | Optional Twitch application secret |
| `TWITCH_GROWTH_ADMIN_PASSWORD_HASH` | empty | Werkzeug password hash enabling admin protection |
| `TWITCH_GROWTH_ADMIN_PASSWORD` | empty | Plain-text development fallback; avoid in production |
| `TWITCH_GROWTH_SESSION_SECRET` | random per start | Persistent Flask session signing secret |
| `TWITCH_GROWTH_SECURE_COOKIES` | `false` | Send the admin cookie only over HTTPS |
| `TWITCH_GROWTH_PREFIX` | `/intranet/twitch-growth` | URL path prefix; use an empty value at the domain root |
| `TWITCH_GROWTH_TIMEZONE` | `Europe/London` | IANA timezone used in analysis and exports |
| `TWITCH_GROWTH_CHANNEL_URL` | empty | Default campaign destination |
| `TWITCH_GROWTH_DASHBOARD_URL` | `/intranet/Dashboard2/` | Optional route used by the back link |
| `TWITCH_GROWTH_DB` | `./twitch_growth.db` | SQLite database path |
| `STEAM_ID64` / `STEAM_API_KEY` | empty | Optional Game Finder configuration |
| `PORT` | `5120` | Flask development-server port |

For a generic installation, explicitly set `TWITCH_GROWTH_PREFIX` and `TWITCH_GROWTH_DASHBOARD_URL`. The historical defaults keep existing intranet installations compatible.

## Security model

When `TWITCH_GROWTH_ADMIN_PASSWORD_HASH` or `TWITCH_GROWTH_ADMIN_PASSWORD` is set, dashboards, exports, settings and private APIs require an authenticated session. State-changing requests also require a CSRF token.

OBS overlays, their read-only data feeds, health checks, campaign redirects and token-protected marker hooks remain accessible so broadcast software can use them. Overlay data can include public Twitch display names; choose an appropriate network boundary.

If no admin credential is configured, legacy unrestricted LAN behaviour is retained and the Settings page is disabled. This mode must not be exposed to an untrusted network.

## Steam Game Finder

Add a 17-digit Steam ID64 and Steam Web API key in Settings or on the Game Finder page. Steam **Game details** must be public. The collector refreshes a bounded candidate set every 30 minutes and treats recommendations as observational suggestions rather than forecasts.

## OBS and Streamer.bot

- Streamline desk: `/streamline`
- Streamline overlay: `/streamline/overlay`
- Stream Boss desk: `/stream-boss`
- Stream Boss overlays: `/stream-boss/overlay` and `/stream-boss/overlay/compact`

All routes are below `TWITCH_GROWTH_PREFIX`. The dashboard generates complete OBS URLs. The integration endpoint returns a token-bearing marker webhook suitable for Streamer.bot; treat that URL as a secret.

## Development and tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Tests always use temporary databases. Never point `TWITCH_GROWTH_DB` at a production database while developing.

## Data and backups

Back up the configured SQLite database using SQLite's online backup API or while both processes are stopped. The database, WAL files, `.env`, logs and local virtual environments are excluded from Git.

## Licence

Twitch Growth is available under the MIT licence. Twitch and Steam are trademarks of their respective owners; this project is not affiliated with either company.
