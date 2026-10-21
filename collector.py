#!/usr/bin/env python3
"""Hybrid Twitch collector: EventSub for events and Helix polling for CCV."""
import argparse
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import websocket
from game_finder import match_twitch_categories, sample_categories, sample_global_categories, sync_steam_games

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TWITCH_GROWTH_DB", os.path.join(BASE, "twitch_growth.db"))
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TOKEN = os.environ.get("TWITCH_ACCESS_TOKEN", "")
LOGIN = os.environ.get("TWITCH_BROADCASTER_LOGIN", "")
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
OFFLINE_GRACE_SECONDS = 180
RAW_CHAT_RETENTION_DAYS = 90
GAME_FINDER_INTERVAL_SECONDS = 1800
PRINT_LOCK = threading.Lock()
TOKEN_REFRESH_LOCK = threading.Lock()


def log(message):
    with PRINT_LOCK:
        print(f"{utc_now()}: {message}", flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(db, key, value):
    db.execute("""INSERT INTO settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
      ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""", (key, str(value)))


def configured_client_id(db=None):
    if CLIENT_ID:
        return CLIENT_ID
    if db is not None:
        return setting(db, "twitch_client_id")
    with connect() as connection:
        return setting(connection, "twitch_client_id")


def configured_client_secret(db=None):
    secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
    if secret:
        return secret
    if db is not None:
        return setting(db, "twitch_client_secret")
    with connect() as connection:
        return setting(connection, "twitch_client_secret")


def access_token():
    with connect() as db:
        return setting(db, "access_token") or TOKEN


def refresh_access_token(failed_token=None):
    if not TOKEN_REFRESH_LOCK.acquire(timeout=10):
        with connect() as db:
            current = setting(db, "access_token")
        if failed_token and current and current != failed_token:
            return current
        raise RuntimeError("Timed out waiting 10 seconds for the Twitch token refresh lock")
    try:
        with connect() as db:
            current = setting(db, "access_token")
            refresh = setting(db, "refresh_token")
        if failed_token and current and current != failed_token:
            return current
        if not refresh:
            raise RuntimeError("Twitch authorization has expired; reconnect from the dashboard")
        values = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": configured_client_id()}
        secret = configured_client_secret()
        if secret:
            values["client_secret"] = secret
        req = Request("https://id.twitch.tv/oauth2/token", data=urlencode(values).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(req, timeout=20) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Twitch token refresh failed: {error.read().decode('utf-8', 'replace')}") from error
        with connect() as db:
            save_setting(db, "access_token", payload["access_token"])
            save_setting(db, "refresh_token", payload.get("refresh_token", refresh))
            save_setting(db, "token_expires_at", int(time.time()) + int(payload.get("expires_in", 0)))
        log("Twitch access token refreshed")
        return payload["access_token"]
    finally:
        TOKEN_REFRESH_LOCK.release()


def twitch(path, method="GET", body=None, retry=True, **params):
    token = access_token()
    client_id = configured_client_id()
    if not client_id or not token:
        raise RuntimeError("Connect Twitch from the Growth dashboard first")
    url = "https://api.twitch.tv/helix/" + path
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401 and retry:
            refresh_access_token(token)
            return twitch(path, method=method, body=body, retry=False, **params)
        raise RuntimeError(f"Twitch {method} {path} returned HTTP {error.code}: "
                           f"{error.read().decode('utf-8', 'replace')}") from error


def validate_token():
    token = access_token()
    req = Request("https://id.twitch.tv/oauth2/validate",
                  headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401:
            refresh_access_token(token)
            return validate_token()
        raise


def identify(force=False):
    with connect() as db:
        cached_id = setting(db, "broadcaster_id")
        cached_login = setting(db, "broadcaster_login")
        cached_name = setting(db, "broadcaster_name")
    if cached_id and not force:
        return {"id": cached_id, "login": cached_login, "display_name": cached_name}
    params = {"login": LOGIN} if LOGIN else {}
    users = twitch("users", **params).get("data", [])
    if not users:
        raise RuntimeError("The authorized Twitch broadcaster could not be resolved")
    user = users[0]
    with connect() as db:
        save_setting(db, "broadcaster_id", user["id"])
        save_setting(db, "broadcaster_login", user["login"])
        save_setting(db, "broadcaster_name", user["display_name"])
    return user


def active_stream(db):
    return db.execute("SELECT * FROM streams WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def floor_five_minutes(value):
    moment = parse_time(value)
    return moment.replace(minute=(moment.minute // 5) * 5, second=0, microsecond=0).isoformat()


def update_aggregate(db, stream_id):
    samples = db.execute("SELECT sampled_at,viewer_count,is_boosted FROM viewer_samples WHERE stream_id=? ORDER BY sampled_at", (stream_id,)).fetchall()
    if not samples:
        return
    viewer_minutes = 0.0
    for previous, current in zip(samples, samples[1:]):
        start = datetime.fromisoformat(previous["sampled_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(current["sampled_at"].replace("Z", "+00:00"))
        viewer_minutes += previous["viewer_count"] * min(5.0, max(0.0, (end - start).total_seconds() / 60.0))
    baseline = [row["viewer_count"] for row in samples if not row["is_boosted"]]
    db.execute("""UPDATE streams SET peak_viewers=?,average_viewers=?,baseline_average_viewers=?,
      viewer_minutes=?,sample_count=? WHERE id=?""",
      (max(row["viewer_count"] for row in samples),
       sum(row["viewer_count"] for row in samples) / len(samples),
       (sum(baseline) / len(baseline)) if baseline else None,
       round(viewer_minutes, 2), len(samples), stream_id))


def update_segment(db, stream_id, sampled_at, title, category_id, category_name, tags_json):
    segment = db.execute("SELECT * FROM stream_segments WHERE stream_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (stream_id,)).fetchone()
    changed = not segment or any((segment["title"] != title, segment["category_id"] != category_id,
                                  segment["category_name"] != category_name, segment["tags_json"] != tags_json))
    if not changed:
        return
    if segment:
        db.execute("UPDATE stream_segments SET ended_at=? WHERE id=?", (sampled_at, segment["id"]))
    db.execute("""INSERT INTO stream_segments(stream_id,started_at,title,category_id,category_name,tags_json)
      VALUES(?,?,?,?,?,?)""", (stream_id, sampled_at, title, category_id, category_name, tags_json))
    if segment:
        changes = []
        if segment["title"] != title: changes.append("title")
        if segment["category_name"] != category_name: changes.append("category")
        if segment["tags_json"] != tags_json: changes.append("tags")
        db.execute("""INSERT INTO timeline_markers(stream_id,occurred_at,marker_type,label,source,detail)
          VALUES(?,?,?,?,?,?)""", (stream_id, sampled_at, "metadata", "Changed " + ", ".join(changes),
          "collector", json.dumps({"title": title, "category": category_name, "tags": json.loads(tags_json)})))


def sample_once():
    user = identify()
    live_streams = twitch("streams", user_id=user["id"]).get("data", [])
    sampled_at = utc_now()
    with connect() as db:
        if live_streams:
            live = live_streams[0]
            tags_json = json.dumps(live.get("tags", []), ensure_ascii=False)
            pending = active_stream(db)
            if pending and pending["offline_pending_at"]:
                elapsed = (parse_time(sampled_at) - parse_time(pending["offline_pending_at"])).total_seconds()
                if elapsed <= OFFLINE_GRACE_SECONDS:
                    old_twitch_id = pending["twitch_stream_id"]
                    if old_twitch_id != live["id"]:
                        db.execute("UPDATE streams SET twitch_stream_id=? WHERE id=?", (live["id"], pending["id"]))
                    db.execute("UPDATE streams SET offline_pending_at=NULL WHERE id=?", (pending["id"],))
                    db.execute("""INSERT INTO timeline_markers(stream_id,occurred_at,marker_type,label,source,detail)
                      VALUES(?,?,?,?,?,?)""", (pending["id"], sampled_at, "reconnect",
                      "Stream resumed after a brief disconnect", "collector",
                      json.dumps({"offline_seconds": round(elapsed), "previous_twitch_stream_id": old_twitch_id})))
            db.execute("""INSERT INTO streams(twitch_stream_id,title,category_id,category_name,started_at)
              VALUES(?,?,?,?,?) ON CONFLICT(twitch_stream_id) DO UPDATE SET
              title=excluded.title,category_id=excluded.category_id,category_name=excluded.category_name,
              offline_pending_at=NULL""",
              (live["id"], live.get("title", ""), live.get("game_id"), live.get("game_name", ""), live["started_at"]))
            stream = db.execute("SELECT id FROM streams WHERE twitch_stream_id=?", (live["id"],)).fetchone()
            raid_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            boosted = bool(db.execute("""SELECT 1 FROM community_events WHERE stream_id=? AND event_type='raid'
              AND occurred_at>=? LIMIT 1""", (stream["id"], raid_cutoff)).fetchone())
            db.execute("""INSERT OR IGNORE INTO viewer_samples
              (stream_id,sampled_at,viewer_count,title,category_name,tags_json,is_boosted)
              VALUES(?,?,?,?,?,?,?)""", (stream["id"], sampled_at, live.get("viewer_count", 0),
              live.get("title", ""), live.get("game_name", ""), tags_json, int(boosted)))
            update_segment(db, stream["id"], sampled_at, live.get("title", ""), live.get("game_id"),
                           live.get("game_name", ""), tags_json)
            update_aggregate(db, stream["id"])
            state = f"live with {live.get('viewer_count', 0)} viewers"
        else:
            open_streams = db.execute("SELECT id,offline_pending_at FROM streams WHERE ended_at IS NULL").fetchall()
            for stream in open_streams:
                if not stream["offline_pending_at"]:
                    db.execute("UPDATE streams SET offline_pending_at=? WHERE id=?", (sampled_at, stream["id"]))
                elif (parse_time(sampled_at) - parse_time(stream["offline_pending_at"])).total_seconds() >= OFFLINE_GRACE_SECONDS:
                    ended_at = stream["offline_pending_at"]
                    db.execute("UPDATE streams SET ended_at=?,offline_pending_at=NULL WHERE id=?", (ended_at, stream["id"]))
                    db.execute("UPDATE stream_segments SET ended_at=? WHERE stream_id=? AND ended_at IS NULL", (ended_at, stream["id"]))
                    update_aggregate(db, stream["id"])
            state = "offline (within disconnect grace)" if open_streams else "offline"
        save_setting(db, "last_sync", sampled_at)
    return user, state


def sync_clips(broadcaster_id):
    payload = twitch("clips", broadcaster_id=broadcaster_id, first=100)
    with connect() as db:
        for clip in payload.get("data", []):
            db.execute("""INSERT INTO content_items(twitch_id,kind,title,created_at,view_count,creator_name,url)
              VALUES(?,'clip',?,?,?,?,?) ON CONFLICT(kind,twitch_id) DO UPDATE SET
              title=excluded.title,view_count=excluded.view_count,creator_name=excluded.creator_name,url=excluded.url""",
              (clip["id"], clip.get("title", ""), clip["created_at"], clip.get("view_count", 0),
               clip.get("creator_name"), clip.get("url")))


SUBSCRIPTIONS = [
    ("stream.online", "1", "broadcaster"), ("stream.offline", "1", "broadcaster"),
    ("channel.update", "2", "broadcaster"), ("channel.follow", "2", "follow"),
    ("channel.chat.message", "1", "chat"), ("channel.subscribe", "1", "broadcaster"),
    ("channel.subscription.end", "1", "broadcaster"), ("channel.subscription.gift", "1", "broadcaster"),
    ("channel.subscription.message", "1", "broadcaster"), ("channel.cheer", "1", "broadcaster"),
    ("channel.channel_points_custom_reward_redemption.add", "1", "broadcaster"),
    ("channel.hype_train.begin", "2", "broadcaster"), ("channel.hype_train.progress", "2", "broadcaster"),
    ("channel.hype_train.end", "2", "broadcaster"), ("channel.poll.end", "1", "broadcaster"),
    ("channel.prediction.end", "1", "broadcaster"),
]


def condition(kind, broadcaster_id):
    if kind == "follow":
        return {"broadcaster_user_id": broadcaster_id, "moderator_user_id": broadcaster_id}
    if kind == "chat":
        return {"broadcaster_user_id": broadcaster_id, "user_id": broadcaster_id}
    return {"broadcaster_user_id": broadcaster_id}


def create_subscriptions(session_id, broadcaster_id):
    created = 0
    for event_type, version, kind in SUBSCRIPTIONS:
        try:
            twitch("eventsub/subscriptions", method="POST", body={
                "type": event_type, "version": version,
                "condition": condition(kind, broadcaster_id),
                "transport": {"method": "websocket", "session_id": session_id},
            })
            created += 1
        except Exception as error:
            log(f"EventSub subscription {event_type} failed: {error}")
    for raid_condition in ({"to_broadcaster_user_id": broadcaster_id}, {"from_broadcaster_user_id": broadcaster_id}):
        try:
            twitch("eventsub/subscriptions", method="POST", body={
                "type": "channel.raid", "version": "1", "condition": raid_condition,
                "transport": {"method": "websocket", "session_id": session_id},
            })
            created += 1
        except Exception as error:
            log(f"EventSub subscription channel.raid failed: {error}")
    log(f"EventSub connected with {created} subscriptions")


def event_time(message):
    return message.get("metadata", {}).get("message_timestamp") or utc_now()


def store_event(message):
    payload = message.get("payload", {})
    subscription = payload.get("subscription", {})
    event = payload.get("event", {})
    event_type = subscription.get("type", "unknown")
    occurred_at = event_time(message)
    with connect() as db:
        stream = active_stream(db)
        stream_id = stream["id"] if stream else None
        if event_type == "channel.chat.message":
            inserted = db.execute("""INSERT OR IGNORE INTO chat_activity
              (twitch_message_id,stream_id,occurred_at,user_id,user_name,message_length,is_first_message)
              VALUES(?,?,?,?,?,?,?)""", (event.get("message_id"), stream_id, occurred_at,
              event.get("chatter_user_id"), event.get("chatter_user_name"),
              len(event.get("message", {}).get("text", "")), int(event.get("first_message", False)))).rowcount
            if inserted and stream_id and event.get("chatter_user_id"):
                user_id = event["chatter_user_id"]
                bucket = floor_five_minutes(occurred_at)
                db.execute("""INSERT INTO chat_buckets(stream_id,bucket_start,message_count,first_time_chatters)
                  VALUES(?,?,1,?) ON CONFLICT(stream_id,bucket_start) DO UPDATE SET
                  message_count=message_count+1,first_time_chatters=first_time_chatters+excluded.first_time_chatters""",
                  (stream_id, bucket, int(event.get("first_message", False))))
                db.execute("INSERT OR IGNORE INTO chat_bucket_users(stream_id,bucket_start,user_id) VALUES(?,?,?)",
                           (stream_id, bucket, user_id))
                unique_bucket = db.execute("SELECT count(*) FROM chat_bucket_users WHERE stream_id=? AND bucket_start=?",
                                           (stream_id, bucket)).fetchone()[0]
                db.execute("UPDATE chat_buckets SET unique_chatters=? WHERE stream_id=? AND bucket_start=?",
                           (unique_bucket, stream_id, bucket))
                new_stream_chatter = db.execute("INSERT OR IGNORE INTO stream_chatters(stream_id,user_id) VALUES(?,?)",
                                                (stream_id, user_id)).rowcount
                db.execute("""INSERT INTO stream_chat_summary(stream_id,message_count,unique_chatters,first_time_chatters)
                  VALUES(?,1,?,?) ON CONFLICT(stream_id) DO UPDATE SET message_count=message_count+1,
                  unique_chatters=unique_chatters+excluded.unique_chatters,
                  first_time_chatters=first_time_chatters+excluded.first_time_chatters""",
                  (stream_id, new_stream_chatter, int(event.get("first_message", False))))
            return
        short_type = {
            "channel.follow": "follow", "channel.subscribe": "subscribe",
            "channel.subscription.end": "subscription_end", "channel.subscription.gift": "subscription_gift",
            "channel.subscription.message": "subscription_message", "channel.cheer": "cheer",
            "channel.channel_points_custom_reward_redemption.add": "redemption",
            "channel.raid": "raid", "channel.poll.end": "poll", "channel.prediction.end": "prediction",
            "channel.hype_train.begin": "hype_train", "channel.hype_train.progress": "hype_train_progress",
            "channel.hype_train.end": "hype_train_end", "stream.online": "stream_online",
            "stream.offline": "stream_offline", "channel.update": "channel_update",
        }.get(event_type, event_type)
        if event_type == "channel.raid" and event.get("from_broadcaster_user_id") == identify()["id"]:
            short_type = "raid_outgoing"
        user_id = event.get("user_id") or event.get("from_broadcaster_user_id")
        user_name = event.get("user_name") or event.get("from_broadcaster_user_name")
        amount = event.get("bits") or event.get("viewers") or event.get("total")
        event_id = message.get("metadata", {}).get("message_id")
        db.execute("""INSERT OR IGNORE INTO community_events
          (twitch_event_id,stream_id,event_type,occurred_at,user_id,user_name,amount,detail)
          VALUES(?,?,?,?,?,?,?,?)""", (event_id, stream_id, short_type, occurred_at,
          user_id, user_name, amount, json.dumps(event, ensure_ascii=False)))
        if short_type in ("raid", "raid_outgoing", "stream_online", "stream_offline", "channel_update", "hype_train", "poll", "prediction") and stream_id:
            if short_type in ("raid", "raid_outgoing"):
                label = ("Raid sent to " + str(event.get("to_broadcaster_user_name"))) if short_type == "raid_outgoing" else \
                        ("Raid from " + str(event.get("from_broadcaster_user_name")) + f" ({event.get('viewers', 0)})")
            else:
                label = short_type.replace("_", " ").title()
            db.execute("""INSERT INTO timeline_markers(stream_id,occurred_at,marker_type,label,source,detail)
              VALUES(?,?,?,?,?,?)""", (stream_id, occurred_at, short_type, label, "eventsub", json.dumps(event)))
    if short_type in ("stream_online", "stream_offline", "channel_update"):
        try:
            sample_once()
        except Exception as error:
            log(f"Immediate snapshot after {short_type} failed: {error}")


def receive_welcome(url):
    socket = websocket.create_connection(url, timeout=90)
    message = json.loads(socket.recv())
    if message.get("metadata", {}).get("message_type") != "session_welcome":
        socket.close()
        raise RuntimeError("EventSub did not welcome the replacement connection")
    return socket, message["payload"]["session"]


def eventsub_loop(stop_event):
    broadcaster_id = identify()["id"]
    while not stop_event.is_set():
        socket = None
        try:
            validate_token()
            socket, session = receive_welcome(EVENTSUB_URL)
            create_subscriptions(session["id"], broadcaster_id)
            while not stop_event.is_set():
                message = json.loads(socket.recv())
                message_type = message.get("metadata", {}).get("message_type")
                if message_type == "notification":
                    store_event(message)
                elif message_type == "session_reconnect":
                    reconnect_url = message["payload"]["session"]["reconnect_url"]
                    replacement, replacement_session = receive_welcome(reconnect_url)
                    old_socket = socket
                    socket = replacement
                    old_socket.close()
                    log(f"EventSub connection handed over seamlessly to {replacement_session['id']}")
                elif message_type == "revocation":
                    log(f"EventSub revoked: {message['payload'].get('subscription', {}).get('type')}")
        except Exception as error:
            log(f"EventSub connection error: {error}; retrying")
            stop_event.wait(10)
        finally:
            if socket:
                socket.close()


def maintain_database():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RAW_CHAT_RETENTION_DAYS)).isoformat()
    with connect() as db:
        removed = db.execute("DELETE FROM chat_activity WHERE occurred_at<?", (cutoff,)).rowcount
        campaign_removed = db.execute("DELETE FROM campaign_clicks WHERE clicked_at<?", (cutoff,)).rowcount
        db.execute("PRAGMA optimize")
        save_setting(db, "last_database_maintenance", utc_now())
    if removed:
        log(f"Pruned {removed} raw chat events older than {RAW_CHAT_RETENTION_DAYS} days")
    if campaign_removed:
        log(f"Pruned {campaign_removed} raw campaign clicks older than {RAW_CHAT_RETENTION_DAYS} days")


def refresh_game_finder():
    with connect() as db:
        steam_id = setting(db, "steam_id64") or os.environ.get("STEAM_ID64", "")
        api_key = setting(db, "steam_api_key") or os.environ.get("STEAM_API_KEY", "")
        if not steam_id or not api_key:
            return False
        last_sync = setting(db, "steam_last_sync")
        library_stale = not last_sync or (datetime.now(timezone.utc) - parse_time(last_sync)).total_seconds() >= 86400
        if library_stale:
            owned = sync_steam_games(db, api_key, steam_id)
            client_id = configured_client_id(db)
            matched = match_twitch_categories(db, client_id, access_token())
            save_setting(db, "steam_last_sync", utc_now())
            log(f"Game Finder synced {owned} Steam games and added {matched} Twitch matches")
        client_id = configured_client_id(db)
        sampled, failed = sample_categories(db, client_id, access_token())
        global_sampled, global_failed = sample_global_categories(db, client_id, access_token())
        save_setting(db, "game_finder_last_error", "")
        log(f"Game Finder sampled {sampled} owned and {global_sampled} Twitch-wide categories ({failed + global_failed} unavailable)")
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run EventSub and poll CCV continuously")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if not args.loop:
        user, state = sample_once()
        sync_clips(user["id"])
        log(f"{user['display_name']} is {state}")
        return
    stop_event = threading.Event()
    event_thread = threading.Thread(target=eventsub_loop, args=(stop_event,), name="eventsub", daemon=True)
    event_thread.start()
    last_clip_sync = 0
    last_validation = 0
    last_maintenance = 0
    last_game_finder = 0
    try:
        while True:
            try:
                if time.time() - last_validation >= 3600:
                    validate_token()
                    last_validation = time.time()
                user, state = sample_once()
                if time.time() - last_clip_sync >= 3600:
                    sync_clips(user["id"])
                    last_clip_sync = time.time()
                if time.time() - last_maintenance >= 86400:
                    maintain_database()
                    last_maintenance = time.time()
                if time.time() - last_game_finder >= GAME_FINDER_INTERVAL_SECONDS:
                    try:
                        if refresh_game_finder():
                            last_game_finder = time.time()
                    except Exception as error:
                        with connect() as db:
                            save_setting(db, "game_finder_last_error", str(error)[:500])
                        log(f"Game Finder refresh failed: {error}")
                        last_game_finder = time.time()
                log(f"{user['display_name']} is {state}")
            except Exception as error:
                log(f"collection error: {error}")
            time.sleep(max(30, args.interval))
    except KeyboardInterrupt:
        stop_event.set()
        event_thread.join(timeout=5)


if __name__ == "__main__":
    main()
