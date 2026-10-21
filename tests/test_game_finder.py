import sqlite3

from game_finder import normalise_title, recommendations, score_game


def test_title_normalisation_handles_common_steam_editions():
    assert normalise_title("Control™ Ultimate Edition") == "control"
    assert normalise_title("The Witcher 3: GOTY Edition") == "witcher 3"


def test_score_penalises_a_dominant_top_channel():
    base = {"total_viewers": 400, "active_channels": 20, "channels_above_typical": 5,
            "personal_streams": 0, "personal_average": None, "snapshot_count": 1,
            "playtime_minutes": 600}
    distributed = score_game({**base, "top_share": .2}, typical_viewers=8, max_playtime=1000)[0]
    concentrated = score_game({**base, "top_share": .9}, typical_viewers=8, max_playtime=1000)[0]
    assert distributed > concentrated + 8


def test_recommendations_include_personal_evidence(tmp_path):
    database = tmp_path / "finder.db"
    schema = open("schema.sql", encoding="utf-8").read()
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.executescript(schema)
        db.execute("""INSERT INTO steam_games(app_id,name,playtime_minutes,recent_playtime_minutes,
          twitch_game_id,twitch_name,synced_at) VALUES(1,'Test Game',600,20,'tw-1','Test Game','2026-09-24')""")
        db.execute("""INSERT INTO twitch_category_snapshots(steam_app_id,twitch_game_id,sampled_at,
          total_viewers,active_channels,top_viewers,median_viewers,top_share,pages_sampled,truncated)
          VALUES(1,'tw-1','2026-09-24T12:00:00+00:00',300,15,60,8,.2,1,0)""")
        for index in range(3):
            db.execute("""INSERT INTO streams(twitch_stream_id,title,category_name,started_at,ended_at,
              average_viewers,baseline_average_viewers) VALUES(?,?,?,?,?,?,?)""",
              (f"stream-{index}", "Test", "Test Game", f"2026-09-{20+index}T18:00:00+00:00",
               f"2026-09-{20+index}T20:00:00+00:00", 12, 10))
        data = recommendations(db)
    assert data["games"][0]["personal_streams"] == 3
    assert data["games"][0]["confidence"] == "high"
    assert data["games"][0]["score"] > 50
