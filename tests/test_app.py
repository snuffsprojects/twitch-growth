import importlib.util
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITCH_GROWTH_DB", str(tmp_path / "test.db"))
    path = Path(__file__).parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("twitch_growth_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def test_health_and_empty_dashboard(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    assert client.get("/intranet/twitch-growth/health").json == {"status": "ok"}
    dashboard = client.get("/intranet/twitch-growth/api/dashboard").json
    assert dashboard["totals"]["streams"] == 0
    assert dashboard["recent_streams"] == []
    assert len(dashboard["insights"]["schedule"]) == 56
    assert dashboard["insights"]["timing_fallback"] is None


def test_campaign_redirect_and_privacy_guards(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    created = client.post("/intranet/twitch-growth/api/campaigns", json={
        "slug": "YT Guide", "platform": "youtube", "campaign_name": "Guide",
        "target_url": "https://www.twitch.tv/example_channel",
    })
    assert created.status_code == 201
    assert created.json["slug"] == "yt-guide"
    assert client.post("/intranet/twitch-growth/api/campaigns", json={
        "slug": "bad", "campaign_name": "Bad", "target_url": "https://example.com",
    }).status_code == 400
    redirect = client.get("/intranet/twitch-growth/r/yt-guide", headers={"User-Agent": "Mozilla/5.0"})
    assert redirect.status_code == 302
    assert redirect.location == "https://www.twitch.tv/example_channel"
    dashboard = client.get("/intranet/twitch-growth/api/dashboard").json
    assert dashboard["campaign_links"][0]["clicks"] == 1
    client.get("/intranet/twitch-growth/r/yt-guide", headers={"User-Agent": "Discordbot"})
    dashboard = client.get("/intranet/twitch-growth/api/dashboard").json
    assert dashboard["campaign_links"][0]["clicks"] == 1


def test_disconnected_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_ACCESS_TOKEN", raising=False)
    app = load_app(tmp_path, monkeypatch)
    status = app.test_client().get("/intranet/twitch-growth/api/status").json
    assert status["connected"] is False


def test_admin_login_csrf_and_masked_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITCH_GROWTH_ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "never-render-this-secret")
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    assert client.get("/intranet/twitch-growth/").status_code == 302
    assert client.get("/intranet/twitch-growth/api/dashboard").status_code == 401
    assert client.post("/intranet/twitch-growth/login", data={"password": "wrong"}).status_code == 200
    signed_in = client.post("/intranet/twitch-growth/login", data={"password": "correct horse battery staple"})
    assert signed_in.status_code == 302
    settings = client.get("/intranet/twitch-growth/settings")
    assert settings.status_code == 200
    assert b"never-render-this-secret" not in settings.data
    assert client.post("/intranet/twitch-growth/settings", data={"timezone": "UTC"}).status_code == 403
    with client.session_transaction() as user_session:
        token = user_session["csrf_token"]
    saved = client.post("/intranet/twitch-growth/settings", data={
        "_csrf_token": token, "timezone": "America/New_York",
        "channel_url": "https://www.twitch.tv/example", "steam_id64": "",
    })
    assert saved.status_code == 200
    with sqlite3.connect(tmp_path / "test.db") as database:
        assert database.execute("SELECT value FROM settings WHERE key='timezone'").fetchone()[0] == "America/New_York"


def test_configurable_route_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITCH_GROWTH_PREFIX", "/twitch")
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    assert client.get("/twitch/health").json == {"status": "ok"}
    assert client.get("/twitch/").status_code == 200
    assert b'"base": "/twitch"' in client.get("/twitch/").data


def test_streamline_builds_a_chronological_journey(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    database = tmp_path / "test.db"
    with sqlite3.connect(database) as db:
        stream_id = db.execute("""INSERT INTO streams(twitch_stream_id,title,category_name,started_at,peak_viewers)
          VALUES('journey-1','Test Journey','Final Fantasy XIV','2026-10-15T18:00:00+00:00',28)""").lastrowid
        db.execute("""INSERT INTO community_events(stream_id,event_type,occurred_at,user_name)
          VALUES(?,?,?,?)""", (stream_id, "follow", "2026-10-15T18:10:00+00:00", "NewViewer"))
        db.execute("""INSERT INTO timeline_markers(stream_id,occurred_at,marker_type,label,source)
          VALUES(?,?,?,?,?)""", (stream_id, "2026-10-15T18:20:00+00:00", "boss", "Boss defeated", "manual"))
        db.execute("""INSERT INTO viewer_samples(stream_id,sampled_at,viewer_count)
          VALUES(?,?,?)""", (stream_id, "2026-10-15T18:15:00+00:00", 25))
    client = app.test_client()
    assert client.get("/intranet/twitch-growth/streamline").status_code == 200
    assert client.get("/intranet/twitch-growth/streamline/overlay").status_code == 200
    journey = client.get("/intranet/twitch-growth/api/streamline").json
    assert journey["live"] is True
    assert journey["stream"]["title"] == "Test Journey"
    assert [station["label"] for station in journey["stations"]] == [
        "Going Live", "NewViewer joined the journey", "10 viewers", "25 viewers", "Boss defeated"
    ]


def test_stream_boss_uses_unique_chat_charge_and_special_damage(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    database = tmp_path / "test.db"
    with sqlite3.connect(database) as db:
        stream_id = db.execute("""INSERT INTO streams(twitch_stream_id,title,category_name,started_at)
          VALUES('boss-1','Boss Test','Just Chatting','2026-10-15T18:00:00+00:00')""").lastrowid
    spawned = client.post("/intranet/twitch-growth/api/stream-boss/control", json={
        "action": "spawn", "boss_name": "Test Gremlin", "max_hp": 1000,
    })
    assert spawned.status_code == 200
    assert spawned.json["boss"]["stream_id"] == stream_id
    assert spawned.json["boss"]["boss_key"] == "signal-gremlin"
    assert len(spawned.json["catalog"]) == 6
    started_at = datetime.fromisoformat(spawned.json["boss"]["started_at"])
    with sqlite3.connect(database) as db:
        for index in range(9):
            db.execute("""INSERT INTO chat_activity
              (twitch_message_id,stream_id,occurred_at,user_id,user_name,message_length)
              VALUES(?,?,?,?,?,?)""", (f"message-{index}", stream_id, (started_at + timedelta(seconds=index + 1)).isoformat(),
              f"user-{index}", f"Viewer {index}", 10))
        db.execute("""INSERT INTO community_events
          (twitch_event_id,stream_id,event_type,occurred_at,user_id,user_name)
          VALUES(?,?,?,?,?,?)""", ("follow-1", stream_id, "follow", (started_at + timedelta(seconds=20)).isoformat(), "follower", "New Friend"))
    battle = client.get("/intranet/twitch-growth/api/stream-boss").json
    assert battle["boss"]["hp"] == 815
    assert battle["boss"]["charge"] == 8
    assert battle["leaders"][0]["user_name"] in {"New Friend", *{f"Viewer {index}" for index in range(9)}}
    assert any(event["event_type"] == "party_attack" for event in battle["events"])
    assert client.get("/intranet/twitch-growth/stream-boss").status_code == 200
    full_overlay = client.get("/intranet/twitch-growth/stream-boss/overlay")
    assert full_overlay.status_code == 200
    assert b"Send a message to add 12% charge" in full_overlay.data
    compact_overlay = client.get("/intranet/twitch-growth/stream-boss/overlay/compact")
    assert compact_overlay.status_code == 200
    assert b"Send a message to add 12% charge" not in compact_overlay.data
    dragon = client.post("/intranet/twitch-growth/api/stream-boss/control", json={"action": "spawn", "boss_key": "delay-dragon"})
    assert dragon.status_code == 200
    assert dragon.json["boss"]["boss_name"] == "The Delay Dragon"
    assert dragon.json["boss"]["max_hp"] == 4000
    knight = client.post("/intranet/twitch-growth/api/stream-boss/control", json={"action": "spawn", "boss_key": "neon-golden-knight"})
    assert knight.status_code == 200
    assert knight.json["boss"]["boss_name"] == "The Neon Golden Knight"
    assert knight.json["boss"]["max_hp"] == 4500
    assert client.post("/intranet/twitch-growth/api/stream-boss/control", json={"action": "spawn", "boss_key": "unknown"}).status_code == 400


def test_game_finder_setup_does_not_return_api_key(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    invalid = client.put("/intranet/twitch-growth/api/game-finder/settings", json={
        "steam_id": "123", "api_key": "bad",
    })
    assert invalid.status_code == 400
    saved = client.put("/intranet/twitch-growth/api/game-finder/settings", json={
        "steam_id": "76561198000000000", "api_key": "a" * 32,
    })
    assert saved.status_code == 200
    finder = client.get("/intranet/twitch-growth/api/game-finder").json
    assert finder["setup"]["ready"] is True
    assert finder["setup"]["steam_id"] == "76561198000000000"
    assert "api_key" not in finder["setup"]
    assert finder["games"] == []


def test_portable_ai_exports(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    client = app.test_client()
    report = client.get("/intranet/twitch-growth/export/ai-report.md")
    assert report.status_code == 200
    assert report.mimetype == "text/markdown"
    assert "attachment;" in report.headers["Content-Disposition"]
    assert "# Twitch Metrics — AI Analysis Brief" in report.get_data(as_text=True)
    assert "Do not imply causation" in report.get_data(as_text=True)
    raw = client.get("/intranet/twitch-growth/export/data.json")
    assert raw.status_code == 200
    assert raw.mimetype == "application/json"
    assert raw.json["export_version"] == 2
    assert raw.json["data"]["totals"]["streams"] == 0


def test_analysis_stream_fields_and_optional_context(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch)
    database = tmp_path / "test.db"
    with sqlite3.connect(database) as db:
        stream_id = db.execute("""INSERT INTO streams(twitch_stream_id,title,category_id,category_name,
          started_at,ended_at,peak_viewers,average_viewers,baseline_average_viewers,viewer_minutes,sample_count)
          VALUES('tw-123','Test stream','game-1','Test Game','2026-09-20T18:00:00+00:00',
          '2026-09-20T20:00:00+00:00',20,10,8,1200,3)""").lastrowid
        db.executemany("INSERT INTO viewer_samples(stream_id,sampled_at,viewer_count,tags_json) VALUES(?,?,?,?)", [
            (stream_id, "2026-09-20T18:05:00+00:00", 8, '["English"]'),
            (stream_id, "2026-09-20T18:30:00+00:00", 20, '["English","Casual"]'),
            (stream_id, "2026-09-20T19:30:00+00:00", 12, '["Casual"]'),
        ])
        db.execute("INSERT INTO stream_chat_summary(stream_id,message_count,unique_chatters,first_time_chatters) VALUES(?,?,?,?)",
                   (stream_id, 60, 8, 2))
        db.execute("INSERT INTO community_events(stream_id,event_type,occurred_at,amount) VALUES(?,?,?,?)",
                   (stream_id, "follow", "2026-09-20T18:40:00+00:00", None))
    client = app.test_client()
    saved = client.post(f"/intranet/twitch-growth/api/streams/{stream_id}/notes", json={
        "discord_promoted": "1", "instagram_promoted": "", "threads_promoted": "0",
        "stream_plan": "planned", "guests": "Alice, Bob", "special_event_type": "Charity",
    })
    assert saved.status_code == 200
    exported = client.get("/intranet/twitch-growth/export/data.json").json
    stream = exported["data"]["analysis_streams"][0]
    assert stream["twitch_stream_id"] == "tw-123"
    assert stream["tags"] == ["English", "Casual"]
    assert stream["first_30_minute_average"] == 8
    assert stream["time_to_peak_minutes"] == 30
    assert stream["follows_per_hour"] == .5
    assert stream["context"]["discord_promoted"] is True
    assert stream["context"]["instagram_promoted"] is None
    assert stream["context"]["threads_promoted"] is False
    assert stream["context"]["guests"] == ["Alice", "Bob"]
