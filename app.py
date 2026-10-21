#!/usr/bin/env python3
import json
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from game_finder import match_twitch_categories, recommendations as game_recommendations
from game_finder import sample_categories, sample_global_categories, sync_steam_games, twitch_get

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TWITCH_GROWTH_DB", os.path.join(BASE, "twitch_growth.db"))
PREFIX = os.environ.get("TWITCH_GROWTH_PREFIX", "/intranet/twitch-growth").rstrip("/")
DEFAULT_TIMEZONE = os.environ.get("TWITCH_GROWTH_TIMEZONE", "Europe/London")
DEFAULT_CHANNEL_URL = os.environ.get("TWITCH_GROWTH_CHANNEL_URL", "")
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
STEAM_ID64 = os.environ.get("STEAM_ID64", "")
AUTH_SCOPES = (
    "moderator:read:followers channel:read:subscriptions bits:read "
    "channel:read:redemptions user:read:chat channel:read:hype_train "
    "channel:read:polls channel:read:predictions"
)
CLICK_GUARD = {}
CLICK_GUARD_LOCK = threading.Lock()
CLICK_GUARD_SALT = secrets.token_bytes(16)
PREVIEW_BOTS = ("discordbot", "twitterbot", "facebookexternalhit", "slackbot", "telegrambot", "whatsapp")
BOSS_CATALOG = {
    "signal-gremlin": {"name": "The Signal Gremlin", "default_hp": 2500,
                       "description": "A brass-plated saboteur who feeds on chat chaos.",
                       "sprite": "signal-gremlin-sprites.png"},
    "ticket-tyrant": {"name": "The Ticket Tyrant", "default_hp": 3000,
                      "description": "An officious automaton with a devastating ticket punch.",
                      "sprite": "ticket-tyrant-sprites.png"},
    "coalheart-colossus": {"name": "Coalheart Colossus", "default_hp": 5000,
                           "description": "A furnace-powered heavyweight built for longer battles.",
                           "sprite": "coalheart-colossus-sprites.png"},
    "platform-phantom": {"name": "The Platform Phantom", "default_hp": 2200,
                         "description": "A theatrical stationmaster who refuses to depart.",
                         "sprite": "platform-phantom-sprites.png"},
    "delay-dragon": {"name": "The Delay Dragon", "default_hp": 4000,
                     "description": "A clock-armoured dragon that hoards everyone’s time.",
                     "sprite": "delay-dragon-sprites.png"},
    "neon-golden-knight": {"name": "The Neon Golden Knight", "default_hp": 4500,
                           "description": "A gold-armoured hockey champion powered by desert arena energy.",
                           "sprite": "neon-golden-knight-sprites.png"},
}

app = Flask(
    __name__,
    template_folder=os.path.join(BASE, "templates"),
    static_folder=os.path.join(BASE, "static"),
    static_url_path=f"{PREFIX}/static",
)
app.secret_key = os.environ.get("TWITCH_GROWTH_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("TWITCH_GROWTH_SECURE_COOKIES", "").lower() in ("1", "true", "yes"),
)


def local_timezone(db=None):
    if db is None:
        with connect() as connection:
            name = setting(connection, "timezone", DEFAULT_TIMEZONE)
    else:
        name = setting(db, "timezone", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def configured_client_id(db=None):
    if CLIENT_ID:
        return CLIENT_ID
    if db is not None:
        return setting(db, "twitch_client_id")
    with connect() as connection:
        return setting(connection, "twitch_client_id")


def admin_password_hash():
    configured = os.environ.get("TWITCH_GROWTH_ADMIN_PASSWORD_HASH", "")
    if configured:
        return configured
    password = os.environ.get("TWITCH_GROWTH_ADMIN_PASSWORD", "")
    return f"plain:{password}" if password else ""


def admin_enabled():
    return bool(admin_password_hash())


def admin_authenticated():
    return not admin_enabled() or bool(session.get("admin_authenticated"))


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


@app.context_processor
def template_configuration():
    return {
        "app_prefix": PREFIX,
        "dashboard_url": os.environ.get("TWITCH_GROWTH_DASHBOARD_URL", "/intranet/Dashboard2/"),
        "csrf_token": csrf_token(),
        "admin_enabled": admin_enabled(),
        "admin_authenticated": admin_authenticated(),
    }


@app.before_request
def protect_admin_routes():
    if not admin_enabled() or admin_authenticated():
        return None
    public_paths = {
        f"{PREFIX}/login", f"{PREFIX}/health",
        f"{PREFIX}/streamline/overlay", f"{PREFIX}/stream-boss/overlay",
        f"{PREFIX}/stream-boss/overlay/compact", f"{PREFIX}/api/streamline",
        f"{PREFIX}/api/stream-boss",
    }
    if request.path.startswith(f"{PREFIX}/static/") or request.path.startswith(f"{PREFIX}/r/") \
            or request.path.startswith(f"{PREFIX}/api/hooks/marker/") or request.path in public_paths:
        return None
    if request.path.startswith(f"{PREFIX}/api/") or request.path.startswith(f"{PREFIX}/export/"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.before_request
def protect_against_csrf():
    if not admin_enabled() or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.path == f"{PREFIX}/login" or request.path.startswith(f"{PREFIX}/api/hooks/marker/"):
        return None
    if not admin_authenticated():
        return jsonify({"error": "Authentication required"}), 401
    supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("_csrf_token", "")
    if not supplied or not secrets.compare_digest(supplied, session.get("csrf_token", "")):
        return jsonify({"error": "Invalid security token; reload the page and try again"}), 403


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    with connect() as db, open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as source:
        db.executescript(source.read())
        stream_columns = {row[1] for row in db.execute("PRAGMA table_info(streams)")}
        if "baseline_average_viewers" not in stream_columns:
            db.execute("ALTER TABLE streams ADD COLUMN baseline_average_viewers REAL")
        if "offline_pending_at" not in stream_columns:
            db.execute("ALTER TABLE streams ADD COLUMN offline_pending_at TEXT")
        sample_columns = {row[1] for row in db.execute("PRAGMA table_info(viewer_samples)")}
        if "tags_json" not in sample_columns:
            db.execute("ALTER TABLE viewer_samples ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        if "is_boosted" not in sample_columns:
            db.execute("ALTER TABLE viewer_samples ADD COLUMN is_boosted INTEGER NOT NULL DEFAULT 0")
        boss_columns = {row[1] for row in db.execute("PRAGMA table_info(stream_boss_runs)")}
        if boss_columns and "boss_key" not in boss_columns:
            db.execute("ALTER TABLE stream_boss_runs ADD COLUMN boss_key TEXT NOT NULL DEFAULT 'signal-gremlin'")
        recommendation_columns = {row[1] for row in db.execute("PRAGMA table_info(recommendations)")}
        if recommendation_columns and "supporting_dates" not in recommendation_columns:
            db.execute("ALTER TABLE recommendations ADD COLUMN supporting_dates TEXT NOT NULL DEFAULT '[]'")
        note_columns = {row[1] for row in db.execute("PRAGMA table_info(stream_notes)")}
        note_migrations = {
            "discord_promoted": "INTEGER", "instagram_promoted": "INTEGER", "threads_promoted": "INTEGER",
            "promotion_at": "TEXT", "guests": "TEXT NOT NULL DEFAULT ''",
            "special_event_type": "TEXT NOT NULL DEFAULT ''", "stream_plan": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in note_migrations.items():
            if column not in note_columns:
                db.execute(f"ALTER TABLE stream_notes ADD COLUMN {column} {definition}")
        snapshot_columns = {row[1] for row in db.execute("PRAGMA table_info(twitch_category_snapshots)")}
        if snapshot_columns and "channels_above_typical" not in snapshot_columns:
            db.execute("ALTER TABLE twitch_category_snapshots ADD COLUMN channels_above_typical INTEGER NOT NULL DEFAULT 0")
        if not setting(db, "marker_hook_token"):
            save_setting(db, "marker_hook_token", secrets.token_urlsafe(24))
        db.execute("""INSERT OR IGNORE INTO stream_chat_summary(stream_id,message_count,unique_chatters,first_time_chatters)
          SELECT stream_id,count(*),count(DISTINCT user_id),sum(is_first_message) FROM chat_activity
          WHERE stream_id IS NOT NULL GROUP BY stream_id""")


def setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(db, key, value):
    db.execute("""INSERT INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
      ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""", (key, str(value)))


def form_post(url, values):
    req = Request(url, data=urlencode(values).encode("utf-8"),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            body = {"message": "Twitch authorization failed"}
        return error.code, body


def rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def current_or_latest_stream(db, requested_id=None):
    if requested_id:
        return db.execute("SELECT * FROM streams WHERE id=?", (requested_id,)).fetchone()
    return db.execute("SELECT * FROM streams ORDER BY (ended_at IS NULL) DESC,started_at DESC LIMIT 1").fetchone()


def add_marker(db, label, marker_type="note", source="manual", detail=None, stream_id=None, occurred_at=None):
    stream = current_or_latest_stream(db, stream_id)
    if not stream:
        return None
    marker_id = db.execute("""INSERT INTO timeline_markers(stream_id,occurred_at,marker_type,label,source,detail)
      VALUES(?,?,?,?,?,?)""", (stream["id"], occurred_at or datetime.now(timezone.utc).isoformat(),
      marker_type, label, source, json.dumps(detail or {}))).lastrowid
    return marker_id


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def floor_five_minutes(value):
    moment = parse_time(value)
    return moment.replace(minute=(moment.minute // 5) * 5, second=0, microsecond=0).isoformat()


def daypart(hour):
    if 12 <= hour < 18: return "afternoon"
    if 18 <= hour < 23: return "evening"
    if hour >= 23 or hour < 3: return "night"
    return "morning"


def device_class(user_agent):
    value = (user_agent or "").lower()
    if any(token in value for token in ("bot", "crawler", "spider")): return "bot"
    if any(token in value for token in ("mobile", "android", "iphone")): return "mobile"
    if value: return "desktop"
    return "unknown"


def samples_between(db, stream_id, start, end):
    return [row[0] for row in db.execute("""SELECT viewer_count FROM viewer_samples
      WHERE stream_id=? AND sampled_at>=? AND sampled_at<? ORDER BY sampled_at""",
      (stream_id, start.isoformat(), end.isoformat()))]


def build_insights(db):
    completed = rows(db, "SELECT * FROM streams WHERE ended_at IS NOT NULL ORDER BY started_at")
    start_cells = {(day, bucket): {"day": day, "hour": bucket, "streams": 0, "values": [], "followers": 0, "hours": 0}
                   for day in range(7) for bucket in range(0, 24, 3)}
    finish_groups = defaultdict(list)
    chatter = []
    energy_groups = defaultdict(lambda: {"viewers": [], "engagement": []})
    clock_groups = defaultdict(list)
    broad_groups = defaultdict(list)
    for stream in completed:
        start = parse_time(stream["started_at"])
        end = parse_time(stream["ended_at"])
        local_start, local_end = start.astimezone(local_timezone()), end.astimezone(local_timezone())
        duration_hours = max((end - start).total_seconds() / 3600, 1 / 60)
        follows = db.execute("SELECT count(*) FROM community_events WHERE stream_id=? AND event_type='follow'", (stream["id"],)).fetchone()[0]
        value = stream["baseline_average_viewers"] if stream["baseline_average_viewers"] is not None else stream["average_viewers"]
        cell = start_cells[(local_start.weekday(), (local_start.hour // 3) * 3)]
        cell["streams"] += 1
        if value is not None: cell["values"].append(value)
        cell["followers"] += follows
        cell["hours"] += duration_hours
        if value is not None:
            clock_groups[(local_start.hour // 3) * 3].append(value)
            broad_groups[("weekend" if local_start.weekday() >= 5 else "weekday", daypart(local_start.hour))].append(value)
        prior = mean(samples_between(db, stream["id"], end - timedelta(minutes=60), end - timedelta(minutes=30)))
        closing = mean(samples_between(db, stream["id"], end - timedelta(minutes=30), end + timedelta(seconds=1)))
        if prior is not None and prior > 0 and closing is not None:
            finish_groups[(local_end.weekday(), (local_end.hour // 3) * 3)].append({"ratio":closing/prior,"stream_id":stream["id"],"date":local_end.date().isoformat()})
        summary = db.execute("SELECT * FROM stream_chat_summary WHERE stream_id=?", (stream["id"],)).fetchone()
        messages = summary["message_count"] if summary else 0
        cpm = messages / max(duration_hours * 60, 1)
        intensity = cpm / value if value and value > 0 else None
        chatter.append({"stream_id": stream["id"], "title": stream["title"], "messages_per_minute": round(cpm, 2),
                        "messages_per_minute_per_viewer": round(intensity, 3) if intensity is not None else None,
                        "messages": messages, "average_viewers": round(value, 1) if value is not None else None})
        note = db.execute("SELECT energy FROM stream_notes WHERE stream_id=?", (stream["id"],)).fetchone()
        if note and note["energy"]:
            energy_groups[note["energy"]]["viewers"].append(value)
            if intensity is not None: energy_groups[note["energy"]]["engagement"].append(intensity)

    schedule = []
    for cell in start_cells.values():
        avg = mean(cell.pop("values"))
        cell.update({"average_viewers": round(avg, 1) if avg is not None else None,
                     "followers_per_hour": round(cell["followers"] / cell["hours"], 2) if cell["hours"] else None,
                     "confidence": min(1, cell["streams"] / 5)})
        del cell["followers"]; del cell["hours"]
        schedule.append(cell)
    eligible_starts = [cell for cell in schedule if cell["streams"] >= 3 and cell["average_viewers"] is not None]
    best_start = max(eligible_starts, key=lambda cell: cell["average_viewers"], default=None)
    clock_windows = [{"hour": hour, "streams": len(values), "average_viewers": round(mean(values), 1)} for hour, values in sorted(clock_groups.items())]
    dayparts = [{"weekpart": key[0], "daypart": key[1], "streams": len(values), "average_viewers": round(mean(values), 1)}
                for key, values in sorted(broad_groups.items())]
    timing_fallback = None
    eligible_clock = [item for item in clock_windows if item["streams"] >= 3]
    eligible_broad = [item for item in dayparts if item["streams"] >= 3]
    if best_start:
        timing_fallback = {"level": "exact", **best_start}
    elif eligible_clock:
        timing_fallback = {"level": "clock", **max(eligible_clock, key=lambda item: item["average_viewers"])}
    elif eligible_broad:
        timing_fallback = {"level": "daypart", **max(eligible_broad, key=lambda item: item["average_viewers"])}
    finishes = [{"day": key[0], "hour": key[1], "streams": len(values), "momentum": round(mean([v["ratio"] for v in values]), 2),
                 "stream_ids":[v["stream_id"] for v in values],"dates":[v["date"] for v in values]}
                for key, values in finish_groups.items()]
    eligible_finishes = [item for item in finishes if item["streams"] >= 3]
    best_finish = max(eligible_finishes, key=lambda item: item["momentum"], default=None)

    transition_groups = defaultdict(list)
    all_segments = rows(db, "SELECT * FROM stream_segments ORDER BY stream_id,started_at")
    by_stream = defaultdict(list)
    for segment in all_segments: by_stream[segment["stream_id"]].append(segment)
    for stream_id, segments in by_stream.items():
        for left, right in zip(segments, segments[1:]):
            if not left["category_name"] or not right["category_name"] or left["category_name"] == right["category_name"]: continue
            change = parse_time(right["started_at"])
            before = mean(samples_between(db, stream_id, change - timedelta(minutes=15), change))
            after = mean(samples_between(db, stream_id, change, change + timedelta(minutes=15)))
            if before and after is not None:
                transition_groups[(left["category_name"], right["category_name"])].append({"ratio":after/before,"stream_id":stream_id,"date":change.astimezone(local_timezone()).date().isoformat()})
    transitions = [{"from": key[0], "to": key[1], "changes": len(values), "retention": round(mean([v["ratio"] for v in values]), 2),
                    "stream_ids":[v["stream_id"] for v in values],"dates":[v["date"] for v in values]}
                   for key, values in transition_groups.items()]

    raid_groups = defaultdict(lambda: {"raids": 0, "viewers": 0, "retentions": [], "followers": 0})
    for raid in rows(db, "SELECT * FROM community_events WHERE event_type='raid' ORDER BY occurred_at"):
        group = raid_groups[raid["user_name"] or "Unknown source"]
        announced = raid["amount"] or 0
        group["raids"] += 1; group["viewers"] += announced
        when = parse_time(raid["occurred_at"])
        before = mean(samples_between(db, raid["stream_id"], when - timedelta(minutes=5), when + timedelta(seconds=1))) if raid["stream_id"] else None
        after = mean(samples_between(db, raid["stream_id"], when + timedelta(minutes=27), when + timedelta(minutes=33))) if raid["stream_id"] else None
        if announced and before is not None and after is not None:
            group["retentions"].append(max(0, after - before) / announced)
        if raid["stream_id"]:
            group["followers"] += db.execute("""SELECT count(*) FROM community_events WHERE stream_id=? AND event_type='follow'
              AND occurred_at>=? AND occurred_at<?""", (raid["stream_id"], when.isoformat(), (when + timedelta(minutes=30)).isoformat())).fetchone()[0]
    raid_affinity = [{"source": source, "raids": group["raids"], "viewers": group["viewers"],
                      "retention_30": round(mean(group["retentions"]) * 100, 1) if group["retentions"] else None,
                      "followers_per_10": round(group["followers"] / group["viewers"] * 10, 2) if group["viewers"] else None}
                     for source, group in raid_groups.items()]
    energy = [{"energy": rating, "streams": len(values["viewers"]), "average_viewers": round(mean(values["viewers"]), 1) if mean(values["viewers"]) is not None else None,
               "engagement": round(mean(values["engagement"]), 3) if mean(values["engagement"]) is not None else None}
              for rating, values in sorted(energy_groups.items())]
    recommendations = []
    for item in transitions:
        if item["changes"] >= 3 and item["retention"] < .65:
            recommendations.append({"rule_id":"CATEGORY_TRANSITION_DROP","rule_version":1,
              "evidence_key":f"{item['from']}->{item['to']}","observation":f"Switching from {item['from']} to {item['to']} retained {round(item['retention']*100)}% of the preceding audience on average.",
              "sample_size":item["changes"],"confidence":"moderate" if item["changes"] < 6 else "strong",
              "supporting_stream_ids":item["stream_ids"],"supporting_dates":item["dates"],"confounding_factors":["Stream age and switch time may contribute to the decline"],
              "suggested_experiment":f"Test {item['to']} on a separate stream rather than after {item['from']}.","re_evaluate_after_streams":3})
    for item in finishes:
        if item["streams"] >= 3 and item["momentum"] < .75:
            recommendations.append({"rule_id":"FINISHING_MOMENTUM_DROP","rule_version":1,
              "evidence_key":f"{item['day']}-{item['hour']}","observation":f"The final 30 minutes retained {round(item['momentum']*100)}% of the preceding half-hour across {item['streams']} streams in this finish window.",
              "sample_size":item["streams"],"confidence":"moderate" if item["streams"] < 6 else "strong",
              "supporting_stream_ids":item["stream_ids"],"supporting_dates":item["dates"],"confounding_factors":["Planned endings and outgoing raids can reduce closing CCV"],
              "suggested_experiment":"Test ending 30 minutes earlier on three comparable broadcasts.","re_evaluate_after_streams":3})
    return {"schedule": schedule, "best_start": best_start, "clock_windows":clock_windows,"dayparts":dayparts,"timing_fallback":timing_fallback,"finish_windows": finishes, "best_finish": best_finish,
            "transitions": sorted(transitions, key=lambda item: (-item["changes"], -item["retention"])),
            "raid_affinity": sorted(raid_affinity, key=lambda item: (-(item["retention_30"] or -1), -item["raids"])),
            "chatter_velocity": sorted(chatter, key=lambda item: -(item["messages_per_minute_per_viewer"] or -1))[:8],
            "energy": energy, "recommendations":recommendations,"completed_streams": len(completed), "recommendation_threshold": 3}


@app.get(f"{PREFIX}/")
def home():
    return render_template("index.html")


@app.route(f"{PREFIX}/login", methods=["GET", "POST"])
def login():
    if not admin_enabled():
        return render_template("login.html", unavailable=True), 503
    error = None
    if request.method == "POST":
        configured = admin_password_hash()
        supplied = request.form.get("password", "")
        valid = (configured.startswith("plain:") and secrets.compare_digest(supplied, configured[6:])) \
            or (not configured.startswith("plain:") and check_password_hash(configured, supplied))
        if valid:
            session.clear()
            session["admin_authenticated"] = True
            csrf_token()
            destination = request.args.get("next", "")
            if not destination.startswith(f"{PREFIX}/") or destination.startswith("//"):
                destination = f"{PREFIX}/"
            return redirect(destination)
        error = "That password was not accepted."
    return render_template("login.html", unavailable=False, error=error)


@app.post(f"{PREFIX}/logout")
def logout():
    session.clear()
    return redirect(f"{PREFIX}/login")


@app.route(f"{PREFIX}/settings", methods=["GET", "POST"])
def settings_page():
    if not admin_enabled():
        return render_template("login.html", unavailable=True), 503
    message = None
    error = None
    with connect() as db:
        if request.method == "POST":
            client_id = request.form.get("twitch_client_id", "").strip()
            steam_id = request.form.get("steam_id64", "").strip()
            steam_key = request.form.get("steam_api_key", "").strip()
            twitch_secret = request.form.get("twitch_client_secret", "").strip()
            timezone_name = request.form.get("timezone", "").strip() or "UTC"
            channel_url = request.form.get("channel_url", "").strip()
            try:
                ZoneInfo(timezone_name)
            except (KeyError, ValueError):
                error = "Enter a valid IANA timezone such as Europe/London or America/New_York."
            if steam_id and (not steam_id.isdigit() or len(steam_id) != 17):
                error = "Steam ID64 must contain exactly 17 digits."
            if steam_key and not re.fullmatch(r"[A-Fa-f0-9]{32}", steam_key):
                error = "Steam Web API keys contain 32 hexadecimal characters."
            if channel_url:
                parsed = urlparse(channel_url)
                if parsed.scheme != "https" or parsed.hostname not in ("twitch.tv", "www.twitch.tv"):
                    error = "The channel URL must be an HTTPS twitch.tv address."
            if not error:
                for key, value in (("twitch_client_id", client_id), ("steam_id64", steam_id),
                                   ("timezone", timezone_name), ("channel_url", channel_url)):
                    save_setting(db, key, value)
                if steam_key:
                    save_setting(db, "steam_api_key", steam_key)
                if twitch_secret:
                    save_setting(db, "twitch_client_secret", twitch_secret)
                message = "Settings saved. The collector will use them on its next cycle."
        values = {
            "twitch_client_id": configured_client_id(db),
            "twitch_client_id_from_environment": bool(CLIENT_ID),
            "twitch_secret_configured": bool(setting(db, "twitch_client_secret") or os.environ.get("TWITCH_CLIENT_SECRET")),
            "steam_id64": setting(db, "steam_id64", STEAM_ID64),
            "steam_key_configured": bool(setting(db, "steam_api_key") or STEAM_API_KEY),
            "timezone": setting(db, "timezone", DEFAULT_TIMEZONE),
            "channel_url": setting(db, "channel_url", DEFAULT_CHANNEL_URL),
            "prefix": PREFIX or "/",
            "database": DB,
        }
    return render_template("settings.html", values=values, message=message, error=error)


@app.get(f"{PREFIX}/game-finder")
def game_finder_page():
    return render_template("game_finder.html")


@app.get(f"{PREFIX}/streamline")
def streamline_page():
    return render_template("streamline.html", overlay=False)


@app.get(f"{PREFIX}/streamline/overlay")
def streamline_overlay():
    return render_template("streamline.html", overlay=True)


@app.get(f"{PREFIX}/stream-boss")
def stream_boss_page():
    return render_template("stream_boss.html", overlay=False, layout="desk")


@app.get(f"{PREFIX}/stream-boss/overlay")
def stream_boss_overlay():
    return render_template("stream_boss.html", overlay=True, layout="full")


@app.get(f"{PREFIX}/stream-boss/overlay/compact")
def stream_boss_compact_overlay():
    return render_template("stream_boss.html", overlay=True, layout="compact")


def streamline_event_label(event):
    name = event.get("user_name") or "Someone"
    kind = event["event_type"]
    amount = event.get("amount")
    try:
        detail = json.loads(event.get("detail") or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    if kind == "follow": return f"{name} joined the journey"
    if kind == "subscribe": return f"{name} subscribed"
    if kind == "subscription_gift": return f"{name} gifted {amount or 'some'} subs"
    if kind == "cheer": return f"{name} cheered {amount or 0} bits"
    if kind == "redemption":
        reward = detail.get("reward", {}).get("title")
        return f"{name}: {reward}" if reward else f"{name} redeemed a reward"
    return kind.replace("_", " ").title()


def build_streamline(db, requested_id=None):
    stream = current_or_latest_stream(db, requested_id)
    if not stream:
        return {"stream": None, "stations": [], "live": False, "updated_at": datetime.now(timezone.utc).isoformat()}
    stream = dict(stream)
    stations = [{"id": "start", "occurred_at": stream["started_at"], "type": "start",
                 "label": "Going Live", "detail": stream["category_name"] or stream["title"] or "The journey begins",
                 "importance": "terminus"}]
    seen_categories = {stream.get("category_name") or ""}
    for segment in rows(db, "SELECT * FROM stream_segments WHERE stream_id=? ORDER BY started_at", (stream["id"],)):
        category = segment.get("category_name") or ""
        if not category or category in seen_categories: continue
        seen_categories.add(category)
        stations.append({"id": f"segment-{segment['id']}", "occurred_at": segment["started_at"], "type": "category",
                         "label": category, "detail": "Changed game / category", "importance": "interchange"})
    event_types = ("follow", "subscribe", "subscription_gift", "cheer", "redemption")
    placeholders = ",".join("?" for _ in event_types)
    for event in rows(db, f"SELECT * FROM community_events WHERE stream_id=? AND event_type IN ({placeholders}) ORDER BY occurred_at",
                      (stream["id"], *event_types)):
        importance = "interchange" if event["event_type"] in ("subscription_gift", "cheer") else "stop"
        stations.append({"id": f"event-{event['id']}", "occurred_at": event["occurred_at"], "type": event["event_type"],
                         "label": streamline_event_label(event), "detail": event["event_type"].replace("_", " ").title(),
                         "importance": importance})
    for marker in rows(db, "SELECT * FROM timeline_markers WHERE stream_id=? ORDER BY occurred_at", (stream["id"],)):
        stations.append({"id": f"marker-{marker['id']}", "occurred_at": marker["occurred_at"], "type": marker["marker_type"],
                         "label": marker["label"], "detail": "Automatic event" if marker["source"] == "eventsub" else "Stream marker",
                         "importance": "interchange" if marker["marker_type"] in ("raid", "raid_outgoing", "hype_train", "boss") else "stop"})
    maximum = db.execute("SELECT max(viewer_count) FROM viewer_samples WHERE stream_id=?", (stream["id"],)).fetchone()[0] or 0
    for threshold in (10, 25, 50, 100, 250, 500, 1000):
        if maximum < threshold: continue
        crossed = db.execute("SELECT sampled_at FROM viewer_samples WHERE stream_id=? AND viewer_count>=? ORDER BY sampled_at LIMIT 1",
                             (stream["id"], threshold)).fetchone()
        if crossed:
            stations.append({"id": f"viewers-{threshold}", "occurred_at": crossed[0], "type": "milestone",
                             "label": f"{threshold} viewers", "detail": "Audience milestone", "importance": "interchange"})
    if stream.get("ended_at"):
        stations.append({"id": "end", "occurred_at": stream["ended_at"], "type": "end", "label": "Home Terminus",
                         "detail": "Journey complete", "importance": "terminus"})
    stations.sort(key=lambda station: station["occurred_at"])
    return {"stream": {key: stream.get(key) for key in ("id", "title", "category_name", "started_at", "ended_at", "peak_viewers")},
            "stations": stations, "live": stream.get("ended_at") is None,
            "updated_at": datetime.now(timezone.utc).isoformat()}


@app.get(f"{PREFIX}/api/streamline")
def api_streamline():
    requested_id = request.args.get("stream_id", type=int)
    with connect() as db:
        return jsonify(build_streamline(db, requested_id))


def active_boss(db):
    return db.execute("SELECT * FROM stream_boss_runs WHERE status IN ('active','paused') ORDER BY id DESC LIMIT 1").fetchone()


def boss_log(db, run_id, event_type, label, amount=0, occurred_at=None):
    db.execute("INSERT INTO stream_boss_events(run_id,occurred_at,event_type,label,amount) VALUES(?,?,?,?,?)",
               (run_id, occurred_at or datetime.now(timezone.utc).isoformat(), event_type, str(label)[:160], int(amount or 0)))


def boss_contribution(db, run_id, user_id, user_name, charge=0, damage=0, messages=0, specials=0):
    identity = str(user_id or user_name or "community")[:100]
    display = str(user_name or "The community")[:80]
    db.execute("""INSERT INTO stream_boss_contributions
      (run_id,user_id,user_name,charge_given,special_damage,messages,special_hits) VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(run_id,user_id) DO UPDATE SET user_name=excluded.user_name,
      charge_given=charge_given+excluded.charge_given,special_damage=special_damage+excluded.special_damage,
      messages=messages+excluded.messages,special_hits=special_hits+excluded.special_hits""",
      (run_id, identity, display, charge, damage, messages, specials))


def defeat_boss(db, run, winner_name):
    ended_at = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE stream_boss_runs SET hp=0,status='defeated',winner_name=?,ended_at=?,updated_at=? WHERE id=?",
               (winner_name, ended_at, ended_at, run["id"]))
    boss_log(db, run["id"], "victory", f"{run['boss_name']} defeated by {winner_name}", 0, ended_at)
    if run["stream_id"]:
        add_marker(db, f"Stream Boss defeated: {run['boss_name']}", "boss", "stream-boss",
                   {"run_id": run["id"], "winner": winner_name}, stream_id=run["stream_id"], occurred_at=ended_at)


def boss_damage(db, run, amount, label, event_type, winner_name="The community", occurred_at=None):
    amount = max(0, int(amount or 0))
    hp = max(0, run["hp"] - amount)
    now = occurred_at or datetime.now(timezone.utc).isoformat()
    boss_log(db, run["id"], event_type, label, min(amount, run["hp"]), now)
    if hp == 0 and run["status"] != "defeated":
        defeat_boss(db, run, winner_name)
        return 0
    phase = 3 if hp <= run["max_hp"] * .33 else 2 if hp <= run["max_hp"] * .66 else 1
    db.execute("UPDATE stream_boss_runs SET hp=?,phase=?,updated_at=? WHERE id=?", (hp, phase, now, run["id"]))
    return hp


def sync_stream_boss(db):
    db.execute("BEGIN IMMEDIATE")
    run = active_boss(db)
    if not run or run["status"] == "paused":
        return run
    if not run["stream_id"]:
        stream = db.execute("SELECT id FROM streams WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()
        if not stream:
            return run
        last_chat = db.execute("SELECT coalesce(max(id),0) FROM chat_activity").fetchone()[0]
        last_event = db.execute("SELECT coalesce(max(id),0) FROM community_events").fetchone()[0]
        db.execute("UPDATE stream_boss_runs SET stream_id=?,last_chat_id=?,last_event_id=? WHERE id=?",
                   (stream["id"], last_chat, last_event, run["id"]))
        run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
    run = dict(run)
    chat_rows = db.execute("""SELECT * FROM chat_activity WHERE id>? AND stream_id=?
      AND occurred_at>=? ORDER BY id""", (run["last_chat_id"], run["stream_id"], run["started_at"])).fetchall()
    last_chat_id = run["last_chat_id"]
    charge = run["charge"]
    hp = run["hp"]
    for chat in chat_rows:
        last_chat_id = chat["id"]
        user_id = chat["user_id"] or chat["user_name"] or f"anonymous-{chat['id']}"
        cooldown = db.execute("SELECT last_charge_at FROM stream_boss_cooldowns WHERE run_id=? AND user_id=?",
                              (run["id"], user_id)).fetchone()
        eligible = not cooldown or (parse_time(chat["occurred_at"]) - parse_time(cooldown[0])).total_seconds() >= 20
        boss_contribution(db, run["id"], user_id, chat["user_name"], messages=1, charge=12 if eligible else 0)
        if not eligible: continue
        db.execute("""INSERT INTO stream_boss_cooldowns(run_id,user_id,last_charge_at) VALUES(?,?,?)
          ON CONFLICT(run_id,user_id) DO UPDATE SET last_charge_at=excluded.last_charge_at""",
          (run["id"], user_id, chat["occurred_at"]))
        charge += 12
        if charge >= 100 and hp > 0:
            charge -= 100
            fresh = {**run, "hp": hp}
            hp = boss_damage(db, fresh, 140, f"Party attack charged by {chat['user_name'] or 'chat'}", "party_attack",
                             chat["user_name"] or "The community", chat["occurred_at"])
    last_event_id = run["last_event_id"]
    community = db.execute("""SELECT * FROM community_events WHERE id>? AND stream_id=?
      AND occurred_at>=? ORDER BY id""", (last_event_id, run["stream_id"], run["started_at"])).fetchall()
    special_damage = {"follow": 45, "subscribe": 180, "subscription_message": 180,
                      "subscription_gift": 240, "cheer": 60, "raid": 300, "redemption": 80}
    for event in community:
        last_event_id = event["id"]
        if event["event_type"] not in special_damage or hp <= 0: continue
        amount = special_damage[event["event_type"]]
        if event["event_type"] == "cheer": amount += min(440, int(event["amount"] or 0) // 2)
        if event["event_type"] == "raid": amount += min(700, int(event["amount"] or 0) * 10)
        if event["event_type"] == "subscription_gift": amount += min(400, int(event["amount"] or 0) * 20)
        name = event["user_name"] or "The community"
        label = f"{name} used {event['event_type'].replace('_', ' ')}"
        boss_contribution(db, run["id"], event["user_id"], name, damage=amount, specials=1)
        hp = boss_damage(db, {**run, "hp": hp}, amount, label, event["event_type"], name, event["occurred_at"])
    now = datetime.now(timezone.utc).isoformat()
    current = db.execute("SELECT status FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
    if current and current[0] != "defeated":
        phase = 3 if hp <= run["max_hp"] * .33 else 2 if hp <= run["max_hp"] * .66 else 1
        db.execute("UPDATE stream_boss_runs SET hp=?,charge=?,phase=?,last_chat_id=?,last_event_id=?,updated_at=? WHERE id=?",
                   (hp, charge, phase, last_chat_id, last_event_id, now, run["id"]))
    return db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()


def boss_payload(db, run):
    catalog = [{"key": key, **entry, "sprite_url": f"{PREFIX}/static/stream-boss/{entry['sprite']}"}
               for key, entry in BOSS_CATALOG.items()]
    if not run:
        return {"boss": None, "catalog": catalog, "leaders": [], "events": [], "updated_at": datetime.now(timezone.utc).isoformat()}
    run = dict(run)
    leaders = rows(db, """SELECT user_name,charge_given,special_damage,messages,special_hits,
      charge_given+special_damage score FROM stream_boss_contributions WHERE run_id=?
      ORDER BY score DESC,messages DESC LIMIT 5""", (run["id"],))
    events = rows(db, "SELECT event_type,label,amount,occurred_at FROM stream_boss_events WHERE run_id=? ORDER BY id DESC LIMIT 6",
                  (run["id"],))
    events.reverse()
    return {"boss": {key: run.get(key) for key in ("id", "stream_id", "boss_key", "boss_name", "max_hp", "hp", "charge", "phase", "status", "winner_name", "started_at", "ended_at")},
            "catalog": catalog, "leaders": leaders, "events": events, "updated_at": run["updated_at"]}


@app.get(f"{PREFIX}/api/stream-boss")
def api_stream_boss():
    with connect() as db:
        run = sync_stream_boss(db)
        return jsonify(boss_payload(db, run))


@app.post(f"{PREFIX}/api/stream-boss/control")
def stream_boss_control():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        run = active_boss(db)
        if action == "spawn":
            boss_key = str(payload.get("boss_key") or "signal-gremlin")
            if boss_key not in BOSS_CATALOG:
                return jsonify({"error": "Choose a boss from the available roster."}), 400
            if run: db.execute("UPDATE stream_boss_runs SET status='retired',ended_at=?,updated_at=? WHERE id=?", (now, now, run["id"]))
            stream = db.execute("SELECT * FROM streams WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()
            selected = BOSS_CATALOG[boss_key]
            max_hp = max(500, min(50000, int(payload.get("max_hp") or selected["default_hp"])))
            name = str(payload.get("boss_name") or selected["name"]).strip()[:60] or selected["name"]
            last_chat = db.execute("SELECT coalesce(max(id),0) FROM chat_activity").fetchone()[0]
            last_event = db.execute("SELECT coalesce(max(id),0) FROM community_events").fetchone()[0]
            run_id = db.execute("""INSERT INTO stream_boss_runs
              (stream_id,boss_key,boss_name,max_hp,hp,started_at,last_chat_id,last_event_id,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?)""", (stream["id"] if stream else None, boss_key, name, max_hp, max_hp, now, last_chat, last_event, now)).lastrowid
            boss_log(db, run_id, "spawn", f"{name} has disrupted the service")
            run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run_id,)).fetchone()
        elif not run:
            return jsonify({"error": "Summon a boss first."}), 409
        elif action in ("pause", "resume"):
            status = "paused" if action == "pause" else "active"
            db.execute("UPDATE stream_boss_runs SET status=?,updated_at=? WHERE id=?", (status, now, run["id"]))
            boss_log(db, run["id"], action, f"Battle {action}d")
            run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
        elif action == "damage":
            boss_damage(db, run, max(1, min(10000, int(payload.get("amount") or 250))), "Signal operator test strike", "manual", "Signal operator")
            run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
        elif action == "heal":
            hp = min(run["max_hp"], run["hp"] + max(1, min(10000, int(payload.get("amount") or 250))))
            phase = 3 if hp <= run["max_hp"] * .33 else 2 if hp <= run["max_hp"] * .66 else 1
            db.execute("UPDATE stream_boss_runs SET hp=?,phase=?,updated_at=? WHERE id=?", (hp, phase, now, run["id"]))
            boss_log(db, run["id"], "heal", f"{run['boss_name']} repaired the line", hp - run["hp"])
            run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
        elif action == "defeat":
            boss_damage(db, run, run["hp"], "Emergency signal override", "manual", "Signal operator")
            run = db.execute("SELECT * FROM stream_boss_runs WHERE id=?", (run["id"],)).fetchone()
        else:
            return jsonify({"error": "Unknown control action."}), 400
        return jsonify(boss_payload(db, run))


@app.get(f"{PREFIX}/api/status")
def api_status():
    with connect() as db:
        channel = setting(db, "broadcaster_name")
        last_sync = setting(db, "last_sync")
        has_client = bool(configured_client_id(db))
        has_token = bool(setting(db, "access_token") or os.environ.get("TWITCH_ACCESS_TOKEN"))
        expires_at = int(setting(db, "token_expires_at", "0") or 0)
        token_current = has_token and (not expires_at or expires_at > time.time())
        live = bool(db.execute("SELECT 1 FROM streams WHERE ended_at IS NULL LIMIT 1").fetchone())
    return jsonify({
        "connected": bool(channel and has_client and token_current),
        "channel": channel or None,
        "last_sync": last_sync or None,
        "credentials": {"client_id": has_client, "access_token": token_current},
        "authorization_expired": bool(has_token and not token_current),
        "live": live,
    })


@app.post(f"{PREFIX}/api/auth/start")
def auth_start():
    client_id = configured_client_id()
    if not client_id:
        return jsonify({"error": "The Twitch Client ID has not been configured"}), 503
    status, payload = form_post("https://id.twitch.tv/oauth2/device", {
        "client_id": client_id,
        "scopes": AUTH_SCOPES,
    })
    if status != 200:
        return jsonify({"error": payload.get("message", "Twitch rejected the device request")}), 502
    with connect() as db:
        save_setting(db, "device_code", payload["device_code"])
        save_setting(db, "device_expires_at", int(time.time()) + int(payload["expires_in"]))
        save_setting(db, "device_interval", payload.get("interval", 5))
    return jsonify({
        "user_code": payload["user_code"],
        "verification_uri": payload["verification_uri"],
        "expires_in": payload["expires_in"],
        "interval": payload.get("interval", 5),
    })


@app.post(f"{PREFIX}/api/auth/poll")
def auth_poll():
    with connect() as db:
        device_code = setting(db, "device_code")
        expires_at = int(setting(db, "device_expires_at", "0"))
    if not device_code or time.time() >= expires_at:
        return jsonify({"status": "expired", "error": "The authorization code has expired"}), 410
    status, payload = form_post("https://id.twitch.tv/oauth2/token", {
        "client_id": configured_client_id(),
        "scopes": AUTH_SCOPES,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if status == 400 and payload.get("message") == "authorization_pending":
        return jsonify({"status": "pending"}), 202
    if status != 200:
        return jsonify({"status": "error", "error": payload.get("message", "Authorization failed")}), 502
    token = payload["access_token"]
    validate = Request("https://id.twitch.tv/oauth2/validate", headers={"Authorization": f"Bearer {token}"})
    with urlopen(validate, timeout=20) as response:
        identity = json.load(response)
    user_request = Request("https://api.twitch.tv/helix/users",
                           headers={"Client-Id": configured_client_id(), "Authorization": f"Bearer {token}"})
    with urlopen(user_request, timeout=20) as response:
        users = json.load(response).get("data", [])
    channel_name = users[0]["display_name"] if users else identity.get("login", "")
    with connect() as db:
        save_setting(db, "access_token", token)
        save_setting(db, "refresh_token", payload.get("refresh_token", ""))
        save_setting(db, "token_expires_at", int(time.time()) + int(payload.get("expires_in", 0)))
        save_setting(db, "token_scopes", json.dumps(payload.get("scope", [])))
        save_setting(db, "broadcaster_id", identity.get("user_id", ""))
        save_setting(db, "broadcaster_login", identity.get("login", ""))
        save_setting(db, "broadcaster_name", channel_name)
        db.execute("DELETE FROM settings WHERE key IN ('device_code','device_expires_at','device_interval')")
    return jsonify({"status": "connected", "channel": channel_name})


@app.get(f"{PREFIX}/api/dashboard")
def api_dashboard():
    with connect() as db:
        totals = dict(db.execute("""SELECT count(*) streams,
          coalesce(round(avg(average_viewers),1),0) average_viewers,
          coalesce(round(avg(baseline_average_viewers),1),0) baseline_average_viewers,
          coalesce(max(peak_viewers),0) best_peak,
          coalesce(round(sum(viewer_minutes)/60.0,1),0) viewer_hours
          FROM streams WHERE ended_at IS NOT NULL""").fetchone())
        totals.update(dict(db.execute("""SELECT
          count(CASE WHEN event_type='follow' THEN 1 END) followers,
          count(CASE WHEN event_type IN ('subscribe','subscription_message') THEN 1 END) subscriptions,
          coalesce(sum(CASE WHEN event_type='cheer' THEN amount ELSE 0 END),0) bits,
          count(CASE WHEN event_type='raid' THEN 1 END) raids
          FROM community_events""").fetchone()))
        totals.update(dict(db.execute("""SELECT
          coalesce((SELECT sum(message_count) FROM stream_chat_summary),0) chat_messages,
          coalesce((SELECT count(DISTINCT user_id) FROM stream_chatters),0) unique_chatters""").fetchone()))
        recent = rows(db, """SELECT s.*,
          coalesce((SELECT c.unique_chatters FROM stream_chat_summary c WHERE c.stream_id=s.id),0) unique_chatters,
          coalesce((SELECT c.message_count FROM stream_chat_summary c WHERE c.stream_id=s.id),0) chat_messages,
          (SELECT count(*) FROM community_events e WHERE e.stream_id=s.id AND e.event_type='follow') follows
          FROM streams s ORDER BY s.started_at DESC LIMIT 8""")
        timeline = rows(db, """SELECT sampled_at,viewer_count FROM viewer_samples
          WHERE stream_id=(SELECT id FROM streams ORDER BY started_at DESC LIMIT 1)
          ORDER BY sampled_at""")
        event_mix = rows(db, """SELECT event_type,count(*) count FROM community_events
          GROUP BY event_type ORDER BY count DESC""")
        categories = rows(db, """SELECT category_name,count(*) streams,
          round(avg(average_viewers),1) average_viewers,max(peak_viewers) peak_viewers,
          round(sum(viewer_minutes)/60.0,1) viewer_hours
          FROM streams WHERE ended_at IS NOT NULL AND category_name<>''
          GROUP BY category_name ORDER BY average_viewers DESC LIMIT 8""")
        clips = rows(db, """SELECT twitch_id,title,created_at,view_count,creator_name,url
          FROM content_items WHERE kind='clip' ORDER BY view_count DESC,created_at DESC LIMIT 6""")
        latest = db.execute("SELECT id FROM streams ORDER BY started_at DESC LIMIT 1").fetchone()
        latest_id = latest["id"] if latest else None
        markers = rows(db, """SELECT id,stream_id,occurred_at,marker_type,label,source,detail
          FROM timeline_markers WHERE stream_id=? ORDER BY occurred_at""", (latest_id,)) if latest_id else []
        segments = rows(db, """SELECT id,started_at,ended_at,title,category_name,tags_json
          FROM stream_segments WHERE stream_id=? ORDER BY started_at""", (latest_id,)) if latest_id else []
        raids = rows(db, """SELECT id,stream_id,occurred_at,user_name,amount,detail
          FROM community_events WHERE event_type='raid' ORDER BY occurred_at DESC LIMIT 12""")
        for raid in raids:
            baseline = db.execute("""SELECT viewer_count FROM viewer_samples
              WHERE stream_id=? AND sampled_at<=? ORDER BY sampled_at DESC LIMIT 1""",
              (raid["stream_id"], raid["occurred_at"])).fetchone()
            raid["before"] = baseline[0] if baseline else None
            raid_time = datetime.fromisoformat(raid["occurred_at"].replace("Z", "+00:00"))
            for minutes in (5, 15, 30):
                target = (raid_time + timedelta(minutes=minutes)).isoformat()
                lower = (raid_time + timedelta(minutes=minutes - 3)).isoformat()
                upper = (raid_time + timedelta(minutes=minutes + 3)).isoformat()
                sample = db.execute("""SELECT viewer_count FROM viewer_samples WHERE stream_id=?
                  AND sampled_at BETWEEN ? AND ? ORDER BY abs(julianday(sampled_at)-julianday(?)) LIMIT 1""",
                  (raid["stream_id"], lower, upper, target)).fetchone()
                raid[f"after_{minutes}"] = sample[0] if sample else None
        notes = dict(db.execute("SELECT * FROM stream_notes WHERE stream_id=?", (latest_id,)).fetchone() or {}) if latest_id else {}
        channel = setting(db, "broadcaster_name")
        insights = build_insights(db)
        for item in insights["recommendations"]:
            db.execute("""INSERT INTO recommendations(rule_id,rule_version,evidence_key,generated_at,observation,
              sample_size,confidence,supporting_stream_ids,supporting_dates,confounding_factors,suggested_experiment,re_evaluate_after_streams)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule_id,rule_version,evidence_key) DO UPDATE SET
              generated_at=excluded.generated_at,observation=excluded.observation,sample_size=excluded.sample_size,
              confidence=excluded.confidence,supporting_stream_ids=excluded.supporting_stream_ids,supporting_dates=excluded.supporting_dates,confounding_factors=excluded.confounding_factors,
              suggested_experiment=excluded.suggested_experiment,re_evaluate_after_streams=excluded.re_evaluate_after_streams""",
              (item["rule_id"],item["rule_version"],item["evidence_key"],datetime.now(timezone.utc).isoformat(),
               item["observation"],item["sample_size"],item["confidence"],json.dumps(item["supporting_stream_ids"]),json.dumps(item["supporting_dates"]),
               json.dumps(item["confounding_factors"]),item["suggested_experiment"],item["re_evaluate_after_streams"]))
        campaign_links = rows(db, """SELECT l.*,
          (SELECT count(*) FROM campaign_clicks c WHERE c.slug=l.slug) clicks
          FROM campaign_links l ORDER BY l.created_at DESC""")
        campaign_clicks = rows(db, """SELECT b.bucket_start,b.slug,b.click_count,l.platform,l.campaign_name
          FROM campaign_click_buckets b JOIN campaign_links l ON l.slug=b.slug
          WHERE b.stream_id=? ORDER BY b.bucket_start""", (latest_id,)) if latest_id else []
    return jsonify({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel or None,
        "totals": totals,
        "recent_streams": recent,
        "viewer_timeline": timeline,
        "event_mix": event_mix,
        "categories": categories,
        "top_clips": clips,
        "markers": markers,
        "segments": segments,
        "raids": raids,
        "latest_stream_id": latest_id,
        "notes": notes,
        "insights": insights,
        "campaign_links": campaign_links,
        "campaign_clicks": campaign_clicks,
    })


@app.get(f"{PREFIX}/api/game-finder")
def api_game_finder():
    with connect() as db:
        data = game_recommendations(db)
        steam_id = setting(db, "steam_id64") or STEAM_ID64
        api_key = setting(db, "steam_api_key") or STEAM_API_KEY
        last_library_sync = setting(db, "steam_last_sync")
        last_error = setting(db, "game_finder_last_error")
    data["setup"] = {
        "steam_id": steam_id,
        "api_key_configured": bool(api_key),
        "ready": bool(steam_id and api_key),
        "last_library_sync": last_library_sync or None,
        "last_error": last_error or None,
    }
    return jsonify(data)


@app.put(f"{PREFIX}/api/game-finder/settings")
def save_game_finder_settings():
    payload = request.get_json(silent=True) or {}
    steam_id = str(payload.get("steam_id", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    if not steam_id.isdigit() or len(steam_id) != 17:
        return jsonify({"error": "Steam ID64 must contain 17 digits."}), 400
    if api_key and not re.fullmatch(r"[A-Fa-f0-9]{32}", api_key):
        return jsonify({"error": "Steam Web API keys contain 32 hexadecimal characters."}), 400
    with connect() as db:
        save_setting(db, "steam_id64", steam_id)
        if api_key:
            save_setting(db, "steam_api_key", api_key)
    return jsonify({"status": "saved", "steam_id": steam_id, "api_key_configured": bool(api_key or STEAM_API_KEY)})


@app.post(f"{PREFIX}/api/game-finder/refresh")
def refresh_game_finder():
    with connect() as db:
        steam_id = setting(db, "steam_id64") or STEAM_ID64
        api_key = setting(db, "steam_api_key") or STEAM_API_KEY
        token = setting(db, "access_token") or os.environ.get("TWITCH_ACCESS_TOKEN", "")
        if not steam_id or not api_key:
            return jsonify({"error": "Add your Steam ID64 and Web API key first."}), 409
        client_id = configured_client_id(db)
        if not client_id or not token:
            return jsonify({"error": "Connect Twitch before refreshing Game Finder."}), 409
        try:
            owned = sync_steam_games(db, api_key, steam_id)
            matched = match_twitch_categories(db, client_id, token)
            sampled, failed = sample_categories(db, client_id, token)
            global_sampled, global_failed = sample_global_categories(db, client_id, token)
            now = datetime.now(timezone.utc).isoformat()
            save_setting(db, "steam_last_sync", now)
            save_setting(db, "game_finder_last_error", "")
        except Exception as exc:
            message = str(exc)[:500]
            save_setting(db, "game_finder_last_error", message)
            return jsonify({"error": message}), 502
        data = game_recommendations(db)
    data["refresh"] = {"owned": owned, "new_matches": matched, "sampled": sampled, "failed": failed,
                       "global_sampled": global_sampled, "global_failed": global_failed}
    return jsonify(data)


@app.put(f"{PREFIX}/api/game-finder/games/<int:app_id>/match")
def update_game_match(app_id):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("twitch_name", "")).strip()[:300]
    if not name:
        return jsonify({"error": "Enter the exact Twitch category name."}), 400
    with connect() as db:
        token = setting(db, "access_token") or os.environ.get("TWITCH_ACCESS_TOKEN", "")
        client_id = configured_client_id(db)
        if not client_id or not token:
            return jsonify({"error": "Connect Twitch first."}), 409
        try:
            games = twitch_get(client_id, token, "games", [("name", name)]).get("data", [])
        except Exception as exc:
            return jsonify({"error": str(exc)[:300]}), 502
        if not games:
            return jsonify({"error": "Twitch did not find that exact category."}), 404
        game = games[0]
        updated = db.execute("UPDATE steam_games SET twitch_game_id=?,twitch_name=?,match_method='manual' WHERE app_id=?",
                             (game["id"], game["name"], app_id)).rowcount
        if not updated:
            return jsonify({"error": "Steam game not found."}), 404
    return jsonify({"status": "saved", "twitch_game_id": game["id"], "twitch_name": game["name"]})


def markdown_cell(value):
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def ratio(value, denominator, scale=1):
    if value is None or not denominator:
        return None
    return round(value / denominator * scale, 2)


def build_analysis_streams(db):
    stream_rows = rows(db, """SELECT s.*,
      coalesce(c.message_count,0) chat_messages,
      coalesce(c.unique_chatters,0) unique_chatters,
      coalesce(c.first_time_chatters,0) first_time_chatters,
      n.summary,n.promotion,n.went_well,n.improve,n.next_test,n.energy,
      n.discord_promoted,n.instagram_promoted,n.threads_promoted,n.promotion_at,
      n.guests,n.special_event_type,n.stream_plan
      FROM streams s
      LEFT JOIN stream_chat_summary c ON c.stream_id=s.id
      LEFT JOIN stream_notes n ON n.stream_id=s.id
      WHERE s.ended_at IS NOT NULL ORDER BY s.started_at""")
    result = []
    for stream in stream_rows:
        started = parse_time(stream["started_at"])
        ended = parse_time(stream["ended_at"])
        duration_hours = max(0, (ended - started).total_seconds() / 3600)
        samples = rows(db, """SELECT sampled_at,viewer_count,tags_json FROM viewer_samples
          WHERE stream_id=? ORDER BY sampled_at""", (stream["id"],))
        timed_samples = [(parse_time(item["sampled_at"]), item["viewer_count"]) for item in samples]

        def window_average(start_minutes, end_minutes=None):
            lower = started + timedelta(minutes=start_minutes)
            upper = ended if end_minutes is None else min(ended, started + timedelta(minutes=end_minutes))
            values = [value for moment, value in timed_samples if lower <= moment < upper]
            value = mean(values)
            return round(value, 2) if value is not None else None

        peak_sample = max(timed_samples, key=lambda item: item[1], default=None)
        event_counts = {row["event_type"]: row for row in rows(db, """SELECT event_type,count(*) count,
          coalesce(sum(amount),0) amount FROM community_events WHERE stream_id=? GROUP BY event_type""", (stream["id"],))}
        follows = event_counts.get("follow", {}).get("count", 0)
        subscriptions = sum(event_counts.get(kind, {}).get("count", 0) for kind in ("subscribe", "subscription_message", "subscription_gift"))
        bits = event_counts.get("cheer", {}).get("amount", 0)
        tags = []
        for sample in samples:
            try:
                for tag in json.loads(sample.get("tags_json") or "[]"):
                    if tag not in tags:
                        tags.append(tag)
            except (TypeError, json.JSONDecodeError):
                pass
        viewer_hours = round((stream.get("viewer_minutes") or 0) / 60, 2)
        first_hour = window_average(0, 60)
        remainder = window_average(60)
        context = {
            "discord_promoted": None if stream.get("discord_promoted") is None else bool(stream["discord_promoted"]),
            "instagram_promoted": None if stream.get("instagram_promoted") is None else bool(stream["instagram_promoted"]),
            "threads_promoted": None if stream.get("threads_promoted") is None else bool(stream["threads_promoted"]),
            "promotion_at": stream.get("promotion_at") or None,
            "guests": [value.strip() for value in (stream.get("guests") or "").split(",") if value.strip()],
            "special_event_type": stream.get("special_event_type") or None,
            "stream_plan": stream.get("stream_plan") or None,
            "promotion_notes": stream.get("promotion") or None,
        }
        result.append({
            "stream_id": stream["id"], "twitch_stream_id": stream.get("twitch_stream_id"),
            "started_at": stream["started_at"], "ended_at": stream["ended_at"],
            "duration_minutes": round(duration_hours * 60, 1), "title": stream.get("title") or None,
            "category_id": stream.get("category_id"), "category_name": stream.get("category_name") or None,
            "tags": tags, "average_viewers": stream.get("average_viewers"),
            "raid_adjusted_baseline_viewers": stream.get("baseline_average_viewers"),
            "peak_viewers": stream.get("peak_viewers"), "viewer_hours": viewer_hours,
            "follows": follows, "unique_chatters": stream.get("unique_chatters", 0),
            "first_time_chatters": stream.get("first_time_chatters", 0), "chat_messages": stream.get("chat_messages", 0),
            "incoming_raids": event_counts.get("raid", {}).get("count", 0),
            "outgoing_raids": event_counts.get("raid_outgoing", {}).get("count", 0),
            "subscriptions": subscriptions, "bits": bits,
            "first_15_minute_average": window_average(0, 15),
            "first_30_minute_average": window_average(0, 30),
            "first_60_minute_average": first_hour,
            "remainder_average": remainder,
            "first_hour_vs_remainder_ratio": ratio(first_hour, remainder),
            "time_to_peak_minutes": round((peak_sample[0] - started).total_seconds() / 60, 1) if peak_sample else None,
            "follows_per_hour": ratio(follows, duration_hours),
            "follows_per_100_viewer_hours": ratio(follows, viewer_hours, 100),
            "chat_messages_per_viewer_hour": ratio(stream.get("chat_messages", 0), viewer_hours),
            "unique_chatters_per_viewer_hour": ratio(stream.get("unique_chatters", 0), viewer_hours),
            "context": context,
            "journal": {key: stream.get(key) or None for key in ("summary", "went_well", "improve", "next_test", "energy")},
        })
    return result


def ai_markdown_report(data):
    totals = data["totals"]
    insights = data["insights"]
    lines = [
        "# Twitch Metrics — AI Analysis Brief", "", f"Generated: {data['generated_at']}",
        f"Channel: {data.get('channel') or 'Not recorded'}", "", "## Instructions for the AI", "",
        "Act as a careful Twitch growth analyst. Identify meaningful patterns, explain the evidence, and propose specific experiments for future streams. Separate observations from hypotheses. Do not imply causation from correlation, do not overstate small samples, and mention missing data that limits a conclusion. Prioritise three practical actions and explain how to measure whether each one worked.",
        "", "## Metric definitions and caveats", "",
        "- Average viewers is the mean reported across completed streams.",
        "- Baseline average viewers is raid-adjusted where that value is available.",
        "- Viewer hours are estimated from periodic viewer-count samples.",
        "- Timing, category-switch, raid-retention, and energy findings are observational associations—not proof of causation.",
        f"- Timing recommendations require at least {insights.get('recommendation_threshold', 3)} comparable streams; always consider the displayed sample size.",
        "- A dash means the value was unavailable or not recorded.", "", "## Overall snapshot", "",
        "| Completed streams | Avg viewers | Raid-adjusted baseline | Best peak | Viewer hours | Followers | Unique chatters | Chat messages | Subscriptions | Raids |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {totals['streams']} | {totals['average_viewers']} | {totals['baseline_average_viewers']} | {totals['best_peak']} | {totals['viewer_hours']} | {totals['followers']} | {totals['unique_chatters']} | {totals['chat_messages']} | {totals['subscriptions']} | {totals['raids']} |",
        "", "## Streams", "",
        "| Date | Title | Category | Duration (min) | Avg | Baseline | Peak | Viewer hours | Chatters | Messages | Follows |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stream in data["analysis_streams"]:
        values = [stream.get("started_at", "")[:10], stream.get("title"), stream.get("category_name"), stream.get("duration_minutes"),
                  stream.get("average_viewers"), stream.get("raid_adjusted_baseline_viewers"), stream.get("peak_viewers"),
                  stream.get("viewer_hours"), stream.get("unique_chatters"), stream.get("chat_messages"), stream.get("follows")]
        lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
    if not data["analysis_streams"]:
        lines.append("| — | No completed streams recorded | — | — | — | — | — | — | — | — | — |")

    lines += ["", "## Per-stream context and derived metrics", ""]
    if data["analysis_streams"]:
        for stream in data["analysis_streams"]:
            lines += [f"### {stream.get('started_at', '')[:10]} — {stream.get('title') or 'Untitled stream'}", "",
                      f"```json\n{json.dumps(stream, ensure_ascii=False, indent=2)}\n```", ""]
    else:
        lines += ["No completed streams recorded yet.", ""]

    lines += ["", "## Category performance", "", "| Category | Streams | Avg viewers | Peak | Viewer hours |", "|---|---:|---:|---:|---:|"]
    for item in data["categories"]:
        lines.append("| " + " | ".join(markdown_cell(item.get(key)) for key in ("category_name", "streams", "average_viewers", "peak_viewers", "viewer_hours")) + " |")
    if not data["categories"]:
        lines.append("| No category data | — | — | — | — |")

    best = insights.get("best_start") or insights.get("timing_fallback")
    finish = insights.get("best_finish")
    lines += ["", "## Observed timing", "",
              f"- Best supported start signal: `{json.dumps(best, ensure_ascii=False) if best else 'insufficient evidence'}`",
              f"- Strongest supported finish signal: `{json.dumps(finish, ensure_ascii=False) if finish else 'insufficient evidence'}`",
              "", "## Diagnostic observations", ""]
    diagnostic_groups = (("Category-switch retention", insights.get("transitions", [])),
                         ("Raid audience affinity", insights.get("raid_affinity", [])),
                         ("Chatter intensity", insights.get("chatter_velocity", [])),
                         ("Energy and performance", insights.get("energy", [])))
    for title, items in diagnostic_groups:
        lines += [f"### {title}", ""]
        lines.extend((f"- `{json.dumps(item, ensure_ascii=False)}`" for item in items),)
        if not items:
            lines.append("- No evidence recorded yet.")
        lines.append("")

    lines += ["## Rule-based recommendations already generated", ""]
    if insights.get("recommendations"):
        for item in insights["recommendations"]:
            lines += [f"### {item['suggested_experiment']}", "", f"- Observation: {item['observation']}",
                      f"- Evidence: {item['confidence']}; sample size {item['sample_size']}",
                      f"- Re-evaluate after: {item['re_evaluate_after_streams']} comparable streams",
                      f"- Caveats: {'; '.join(item['confounding_factors'])}", ""]
    else:
        lines += ["No rule-based recommendation has crossed its evidence threshold yet.", ""]

    lines += ["## Latest stream journal", ""]
    notes = data.get("notes") or {}
    for label, key in (("Summary", "summary"), ("Promotion", "promotion"), ("What worked", "went_well"),
                       ("What to improve", "improve"), ("Next experiment", "next_test"), ("Energy level", "energy")):
        lines.append(f"- **{label}:** {markdown_cell(notes.get(key))}")
    lines += ["", "## Community event totals", ""]
    if data["event_mix"]:
        lines.extend(f"- {item['event_type'].replace('_', ' ')}: {item['count']}" for item in data["event_mix"])
    else:
        lines.append("- No community events recorded yet.")
    lines += ["", "## Latest-stream markers and segments", "",
              "Markers: " + (json.dumps(data["markers"], ensure_ascii=False) if data["markers"] else "none recorded"), "",
              "Segments: " + (json.dumps(data["segments"], ensure_ascii=False) if data["segments"] else "none recorded"), "",
              "---", "For the complete machine-readable dashboard data, use the accompanying JSON export.", ""]
    return "\n".join(lines)


@app.get(f"{PREFIX}/export/ai-report.md")
def export_ai_report():
    data = api_dashboard().get_json()
    with connect() as db:
        data["analysis_streams"] = build_analysis_streams(db)
    stamp = datetime.now(local_timezone()).strftime("%Y-%m-%d")
    return Response(ai_markdown_report(data), content_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="twitch-metrics-ai-report-{stamp}.md"', "Cache-Control": "no-store"})


@app.get(f"{PREFIX}/export/data.json")
def export_json_data():
    data = api_dashboard().get_json()
    with connect() as db:
        data["analysis_streams"] = build_analysis_streams(db)
    export = {"export_version": 2,
              "purpose": "Portable Twitch metrics export for analysis by ChatGPT or another AI assistant",
              "analysis_guidance": "Treat patterns as observational, cite sample sizes, identify missing data, and separate evidence from hypotheses.",
              "field_policy": {
                  "automatically_collected": "Twitch identifiers, timestamps, title, category, tags, viewer samples, and observed community events are pre-filled when available.",
                  "optional_context": "Promotion channels/timestamp, guests, event type, stream plan, journal, and energy are optional; null means not recorded.",
                  "derived_values": "Derived metrics are null when their required samples or denominator are unavailable.",
              },
              "data": data}
    stamp = datetime.now(local_timezone()).strftime("%Y-%m-%d")
    return Response(json.dumps(export, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="twitch-metrics-data-{stamp}.json"', "Cache-Control": "no-store"})


@app.post(f"{PREFIX}/api/campaigns")
def create_campaign():
    payload = request.get_json(silent=True) or {}
    slug = re.sub(r"[^a-z0-9-]", "-", str(payload.get("slug", "")).lower()).strip("-")[:48]
    platform = str(payload.get("platform", "other")).strip().lower()[:32]
    name = str(payload.get("campaign_name", "")).strip()[:120]
    with connect() as db:
        default_target = setting(db, "channel_url", DEFAULT_CHANNEL_URL)
    target = str(payload.get("target_url", default_target)).strip()
    parsed = urlparse(target)
    if not slug or not name:
        return jsonify({"error":"A slug and campaign name are required"}), 400
    if parsed.scheme != "https" or parsed.hostname not in ("twitch.tv", "www.twitch.tv"):
        return jsonify({"error":"Campaign targets must be HTTPS Twitch URLs"}), 400
    try:
        with connect() as db:
            db.execute("INSERT INTO campaign_links(slug,platform,campaign_name,target_url) VALUES(?,?,?,?)",
                       (slug,platform,name,target))
    except sqlite3.IntegrityError:
        return jsonify({"error":"That campaign slug already exists"}), 409
    return jsonify({"status":"created","slug":slug,"path":f"{PREFIX}/r/{slug}"}), 201


@app.post(f"{PREFIX}/api/campaigns/<slug>/toggle")
def toggle_campaign(slug):
    with connect() as db:
        changed = db.execute("UPDATE campaign_links SET is_active=1-is_active WHERE slug=?", (slug,)).rowcount
    return (jsonify({"status":"updated"}), 200) if changed else (jsonify({"error":"Campaign not found"}), 404)


@app.get(f"{PREFIX}/r/<slug>")
def campaign_redirect(slug):
    with connect() as db:
        link = db.execute("SELECT * FROM campaign_links WHERE slug=? AND is_active=1", (slug,)).fetchone()
        if not link: return jsonify({"error":"Campaign link not found"}), 404
        agent = request.headers.get("User-Agent", "")
        lower_agent = agent.lower()
        is_preview = any(bot in lower_agent for bot in PREVIEW_BOTS)
        now = datetime.now(timezone.utc).isoformat()
        source = f"{request.remote_addr or 'unknown'}|{slug}".encode()
        fingerprint = hashlib.blake2s(source, key=CLICK_GUARD_SALT, digest_size=12).hexdigest()
        duplicate = False
        with CLICK_GUARD_LOCK:
            last = CLICK_GUARD.get(fingerprint, 0)
            duplicate = time.time() - last < 30
            CLICK_GUARD[fingerprint] = time.time()
            if len(CLICK_GUARD) > 2000:
                cutoff = time.time() - 3600
                recent = {key:value for key,value in CLICK_GUARD.items() if value >= cutoff}
                CLICK_GUARD.clear(); CLICK_GUARD.update(recent)
        if not is_preview and not duplicate:
            stream = db.execute("SELECT id FROM streams WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()
            stream_id = stream["id"] if stream else None
            bucket = floor_five_minutes(now)
            db.execute("INSERT INTO campaign_clicks(slug,clicked_at,stream_id,device_class) VALUES(?,?,?,?)",
                       (slug,now,stream_id,device_class(agent)))
            db.execute("""INSERT INTO campaign_click_buckets(slug,bucket_start,stream_id,click_count) VALUES(?,?,?,1)
              ON CONFLICT(slug,bucket_start) DO UPDATE SET click_count=click_count+1""", (slug,bucket,stream_id))
    response = redirect(link["target_url"], code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post(f"{PREFIX}/api/markers")
def create_marker():
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label", "")).strip()[:160]
    marker_type = str(payload.get("marker_type", "note")).strip()[:40] or "note"
    if not label:
        return jsonify({"error": "A marker label is required"}), 400
    with connect() as db:
        marker_id = add_marker(db, label, marker_type, "manual", stream_id=payload.get("stream_id"))
    if marker_id is None:
        return jsonify({"error": "No stream exists yet"}), 409
    return jsonify({"status": "created", "id": marker_id}), 201


@app.post(f"{PREFIX}/api/hooks/marker/<token>")
def marker_hook(token):
    payload = request.get_json(silent=True) or request.form.to_dict()
    with connect() as db:
        if not secrets.compare_digest(token, setting(db, "marker_hook_token")):
            return jsonify({"error": "Invalid hook token"}), 403
        label = str(payload.get("label", "Streamer.bot marker")).strip()[:160]
        marker_id = add_marker(db, label, str(payload.get("type", "note"))[:40], "streamer.bot", detail=payload)
    if marker_id is None:
        return jsonify({"error": "No stream exists yet"}), 409
    return jsonify({"status": "created", "id": marker_id}), 201


@app.get(f"{PREFIX}/api/integrations")
def integrations():
    with connect() as db:
        token = setting(db, "marker_hook_token")
    return jsonify({
        "marker_hook": f"{PREFIX}/api/hooks/marker/{token}",
        "method": "POST",
        "json_example": {"label": "Boss wipe", "type": "gameplay"},
    })


@app.post(f"{PREFIX}/api/streams/<int:stream_id>/notes")
def save_notes(stream_id):
    payload = request.get_json(silent=True) or {}
    energy = payload.get("energy")
    if energy not in (None, ""):
        try:
            energy = max(1, min(5, int(energy)))
        except (TypeError, ValueError):
            return jsonify({"error": "Energy must be from 1 to 5"}), 400
    else:
        energy = None
    def optional_boolean(key):
        value = payload.get(key)
        if value in (None, ""):
            return None
        if str(value).lower() in ("1", "true", "yes"):
            return 1
        if str(value).lower() in ("0", "false", "no"):
            return 0
        raise ValueError
    try:
        promotion_flags = [optional_boolean(key) for key in
                           ("discord_promoted", "instagram_promoted", "threads_promoted")]
    except ValueError:
        return jsonify({"error": "Promotion fields must be yes, no, or not recorded"}), 400
    stream_plan = str(payload.get("stream_plan", "")).strip().lower()
    if stream_plan not in ("", "planned", "spontaneous"):
        return jsonify({"error": "Stream plan must be planned, spontaneous, or not recorded"}), 400
    values = [str(payload.get(key, "")).strip()[:4000] for key in
              ("summary", "promotion", "went_well", "improve", "next_test")]
    promotion_at = str(payload.get("promotion_at", "")).strip()[:40] or None
    guests = str(payload.get("guests", "")).strip()[:500]
    special_event_type = str(payload.get("special_event_type", "")).strip()[:160]
    with connect() as db:
        if not db.execute("SELECT 1 FROM streams WHERE id=?", (stream_id,)).fetchone():
            return jsonify({"error": "Stream not found"}), 404
        db.execute("""INSERT INTO stream_notes(stream_id,summary,promotion,went_well,improve,next_test,energy,
          discord_promoted,instagram_promoted,threads_promoted,promotion_at,guests,special_event_type,stream_plan,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(stream_id) DO UPDATE SET
          summary=excluded.summary,promotion=excluded.promotion,went_well=excluded.went_well,
          improve=excluded.improve,next_test=excluded.next_test,energy=excluded.energy,
          discord_promoted=excluded.discord_promoted,instagram_promoted=excluded.instagram_promoted,
          threads_promoted=excluded.threads_promoted,promotion_at=excluded.promotion_at,guests=excluded.guests,
          special_event_type=excluded.special_event_type,stream_plan=excluded.stream_plan,updated_at=CURRENT_TIMESTAMP""",
          (stream_id, *values, energy, *promotion_flags, promotion_at, guests, special_event_type, stream_plan))
    return jsonify({"status": "saved"})


@app.get(f"{PREFIX}/health")
def health():
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5120")))
