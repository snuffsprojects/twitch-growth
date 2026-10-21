"""Steam-library and Twitch-category opportunity analysis."""
import json
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalise_title(value):
    value = "".join(char.lower() if char.isalnum() else " " for char in str(value))
    words = [word for word in value.split() if word not in {
        "edition", "game", "of", "the", "goty", "deluxe", "complete", "ultimate",
    }]
    return " ".join(words)


def get_json(url, headers=None, timeout=30):
    request = Request(url, headers=headers or {"User-Agent": "SnuffsProjects-GameFinder/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Remote service returned HTTP {error.code}: {detail}") from error


def fetch_steam_library(api_key, steam_id):
    params = urlencode({
        "key": api_key, "steamid": steam_id, "include_appinfo": "true",
        "include_played_free_games": "true", "format": "json",
    })
    payload = get_json(f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?{params}")
    games = payload.get("response", {}).get("games", [])
    if not games:
        raise RuntimeError("Steam returned no visible games. Check the Steam ID and Game details privacy setting.")
    return games


def twitch_get(client_id, token, path, params=()):
    query = urlencode(list(params), doseq=True)
    url = f"https://api.twitch.tv/helix/{path}" + (f"?{query}" if query else "")
    return get_json(url, {"Client-Id": client_id, "Authorization": f"Bearer {token}",
                          "User-Agent": "SnuffsProjects-GameFinder/1.0"})


def sync_steam_games(db, api_key, steam_id):
    games = fetch_steam_library(api_key, steam_id)
    synced_at = utc_now()
    for game in games:
        app_id = int(game["appid"])
        icon_hash = game.get("img_icon_url") or ""
        icon = f"https://media.steampowered.com/steamcommunity/public/images/apps/{app_id}/{icon_hash}.jpg" if icon_hash else None
        db.execute("""INSERT INTO steam_games(app_id,name,playtime_minutes,recent_playtime_minutes,icon_url,synced_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET name=excluded.name,
          playtime_minutes=excluded.playtime_minutes,recent_playtime_minutes=excluded.recent_playtime_minutes,
          icon_url=excluded.icon_url,synced_at=excluded.synced_at""",
          (app_id, str(game.get("name") or f"Steam app {app_id}")[:300], int(game.get("playtime_forever") or 0),
           int(game.get("playtime_2weeks") or 0), icon, synced_at))
    return len(games)


def match_twitch_categories(db, client_id, token):
    games = db.execute("SELECT app_id,name FROM steam_games WHERE ignored=0 AND twitch_game_id IS NULL ORDER BY app_id").fetchall()
    matched = 0
    for offset in range(0, len(games), 30):
        batch = games[offset:offset + 30]
        payload = twitch_get(client_id, token, "games", [("name", row["name"]) for row in batch])
        exact = {normalise_title(item["name"]): item for item in payload.get("data", [])}
        for row in batch:
            item = exact.get(normalise_title(row["name"]))
            if item:
                db.execute("UPDATE steam_games SET twitch_game_id=?,twitch_name=?,match_method='title' WHERE app_id=?",
                           (item["id"], item["name"], row["app_id"]))
                matched += 1
    return matched


def candidate_games(db, limit=45):
    historical = {normalise_title(row[0]) for row in db.execute(
        "SELECT DISTINCT category_name FROM streams WHERE category_name<>''")}
    all_games = [dict(row) for row in db.execute("""SELECT * FROM steam_games
      WHERE ignored=0 AND twitch_game_id IS NOT NULL ORDER BY recent_playtime_minutes DESC,playtime_minutes DESC""")]
    selected = []
    selected_ids = set()
    def add(game):
        if game["app_id"] not in selected_ids and len(selected) < limit:
            selected.append(game); selected_ids.add(game["app_id"])
    for game in all_games:
        if game["recent_playtime_minutes"] or normalise_title(game["twitch_name"]) in historical: add(game)
    for game in all_games[:30]: add(game)
    rotation = datetime.now(timezone.utc).toordinal()
    remaining = [game for game in all_games if game["app_id"] not in selected_ids]
    remaining.sort(key=lambda game: ((game["app_id"] * 2654435761 + rotation) & 0xffffffff))
    for game in remaining: add(game)
    return selected


def sample_one_category(game, client_id, token, typical_viewers=0, max_pages=3):
    streams, cursor, pages = [], None, 0
    while pages < max_pages:
        params = [("game_id", game["twitch_game_id"]), ("first", "100"), ("language", "en")]
        if cursor: params.append(("after", cursor))
        payload = twitch_get(client_id, token, "streams", params)
        streams.extend(payload.get("data", [])); pages += 1
        cursor = payload.get("pagination", {}).get("cursor")
        if not cursor: break
    viewers = sorted((int(stream.get("viewer_count") or 0) for stream in streams), reverse=True)
    total = sum(viewers)
    return {
        "steam_app_id": game["app_id"], "twitch_game_id": game["twitch_game_id"],
        "total_viewers": total, "active_channels": len(viewers), "top_viewers": viewers[0] if viewers else 0,
        "median_viewers": round(statistics.median(viewers), 1) if viewers else 0,
        "top_share": round(viewers[0] / total, 4) if total else 0,
        "channels_above_typical": sum(viewer > typical_viewers for viewer in viewers),
        "pages_sampled": pages, "truncated": int(bool(cursor)),
    }


def sample_categories(db, client_id, token):
    games = candidate_games(db)
    typical = db.execute("SELECT coalesce(avg(coalesce(baseline_average_viewers,average_viewers)),0) FROM streams WHERE ended_at IS NOT NULL").fetchone()[0] or 0
    sampled_at = utc_now()
    results, failures = [], 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(sample_one_category, game, client_id, token, typical): game for game in games}
        for future in as_completed(futures):
            try: results.append(future.result())
            except Exception: failures += 1
    for result in results:
        db.execute("""INSERT INTO twitch_category_snapshots(steam_app_id,twitch_game_id,sampled_at,
          total_viewers,active_channels,top_viewers,median_viewers,top_share,channels_above_typical,pages_sampled,truncated)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (result["steam_app_id"], result["twitch_game_id"], sampled_at,
          result["total_viewers"], result["active_channels"], result["top_viewers"], result["median_viewers"],
          result["top_share"], result["channels_above_typical"], result["pages_sampled"], result["truncated"]))
    return len(results), failures


def sample_global_categories(db, client_id, token, limit=40):
    top = twitch_get(client_id, token, "games/top", [("first", str(min(100, limit)))]).get("data", [])
    typical = db.execute("SELECT coalesce(avg(coalesce(baseline_average_viewers,average_viewers)),0) FROM streams WHERE ended_at IS NOT NULL").fetchone()[0] or 0
    games = [{"app_id": None, "twitch_game_id": item["id"], "name": item["name"],
              "box_art_url": item.get("box_art_url")} for item in top]
    sampled_at, results, failures = utc_now(), [], 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(sample_one_category, game, client_id, token, typical, 1): game for game in games}
        for future in as_completed(futures):
            game = futures[future]
            try:
                result = future.result(); result.update({"name": game["name"], "box_art_url": game["box_art_url"]}); results.append(result)
            except Exception: failures += 1
    for result in results:
        db.execute("""INSERT INTO twitch_global_snapshots(twitch_game_id,twitch_name,box_art_url,sampled_at,
          total_viewers,active_channels,top_viewers,median_viewers,top_share,channels_above_typical,pages_sampled,truncated)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (result["twitch_game_id"], result["name"], result["box_art_url"], sampled_at,
          result["total_viewers"], result["active_channels"], result["top_viewers"], result["median_viewers"],
          result["top_share"], result["channels_above_typical"], result["pages_sampled"], result["truncated"]))
    return len(results), failures


def opportunity_band(score):
    if score >= 65: return "strong"
    if score >= 48: return "possible"
    return "difficult"


def global_recommendations(db):
    latest = db.execute("SELECT max(sampled_at) FROM twitch_global_snapshots").fetchone()[0]
    if not latest: return []
    games = [dict(row) for row in db.execute("SELECT * FROM twitch_global_snapshots WHERE sampled_at=?", (latest,))]
    for game in games:
        demand = min(1, math.log1p(game["total_viewers"]) / math.log(1501))
        competition = 1 - min(1, game["active_channels"] / 80)
        distribution = 1 - game["top_share"] if game["total_viewers"] else 0
        visibility = 1 - min(game["channels_above_typical"], 50) / 50
        game["score"] = round(100 * (.38*demand + .24*competition + .23*distribution + .15*visibility), 1)
        game["band"] = opportunity_band(game["score"])
        game["box_art_url"] = (game.get("box_art_url") or "").replace("{width}", "144").replace("{height}", "192")
    games = [game for game in games if game["total_viewers"] >= 25]
    games.sort(key=lambda game: (-game["score"], -game["total_viewers"]))
    return games[:5]


def score_game(game, typical_viewers=0, max_playtime=1):
    viewers, channels = game["total_viewers"], game["active_channels"]
    demand = min(1, math.log1p(viewers) / math.log(1501))
    competition = 1 - min(1, channels / 80)
    distribution = 1 - game["top_share"] if viewers else 0
    opportunity = .42 * demand + .28 * competition + .30 * distribution if viewers else 0
    position = 1 + game.get("channels_above_typical", 0)
    visibility = 1 - min(position - 1, 50) / 50
    personal_streams = game.get("personal_streams", 0)
    personal_average = game.get("personal_average")
    if personal_average is None:
        personal, personal_confidence = .5, 0
    else:
        baseline = max(1, typical_viewers)
        personal = min(1, max(0, personal_average / (baseline * 1.5)))
        personal_confidence = personal_streams / (personal_streams + 3)
    familiarity = math.log1p(game["playtime_minutes"]) / max(1, math.log1p(max_playtime))
    sample_count = game.get("snapshot_count", 1)
    evidence = min(1, sample_count / 8)
    score = 100 * (.42 * opportunity + .18 * visibility + .18 * (personal * personal_confidence + .5 * (1-personal_confidence)) + .12 * familiarity + .10 * evidence)
    return round(score, 1), round(opportunity * 100, 1), position


def recommendations(db):
    latest = db.execute("SELECT max(sampled_at) FROM twitch_category_snapshots").fetchone()[0]
    total_owned = db.execute("SELECT count(*) FROM steam_games").fetchone()[0]
    matched = db.execute("SELECT count(*) FROM steam_games WHERE twitch_game_id IS NOT NULL").fetchone()[0]
    if not latest:
        return {"games": [], "twitch_top": global_recommendations(db), "total_owned": total_owned, "matched": matched, "sampled_at": None}
    typical = db.execute("SELECT coalesce(avg(coalesce(baseline_average_viewers,average_viewers)),0) FROM streams WHERE ended_at IS NOT NULL").fetchone()[0] or 0
    max_playtime = db.execute("SELECT coalesce(max(playtime_minutes),1) FROM steam_games").fetchone()[0]
    games = [dict(row) for row in db.execute("""SELECT g.*,s.total_viewers,s.active_channels,s.top_viewers,
      s.median_viewers,s.top_share,s.channels_above_typical,s.pages_sampled,s.truncated,
      (SELECT count(*) FROM twitch_category_snapshots x WHERE x.steam_app_id=g.app_id) snapshot_count,
      (SELECT count(*) FROM streams st WHERE lower(st.category_name)=lower(g.twitch_name) AND st.ended_at IS NOT NULL) personal_streams,
      (SELECT avg(coalesce(st.baseline_average_viewers,st.average_viewers)) FROM streams st
        WHERE lower(st.category_name)=lower(g.twitch_name) AND st.ended_at IS NOT NULL) personal_average
      FROM steam_games g JOIN twitch_category_snapshots s ON s.steam_app_id=g.app_id
      WHERE s.sampled_at=? AND g.ignored=0""", (latest,))]
    for game in games:
        game["score"], game["opportunity"], game["estimated_position"] = score_game(game, typical, max_playtime)
        game["band"] = opportunity_band(game["score"])
        game["confidence"] = "high" if game["snapshot_count"] >= 8 or game["personal_streams"] >= 3 else "medium" if game["snapshot_count"] >= 3 or game["personal_streams"] else "early"
        game["playtime_hours"] = round(game["playtime_minutes"] / 60, 1)
        reasons = []
        if game["total_viewers"] == 0: reasons.append("No English-language viewers live in the sampled category")
        elif game["top_share"] > .65: reasons.append("One channel currently holds most of the audience")
        elif game["active_channels"] <= 40: reasons.append("A browseable number of live English channels")
        if game["personal_streams"] >= 3: reasons.append(f"Backed by {game['personal_streams']} of your completed streams")
        elif game["personal_streams"]: reasons.append("Some personal channel evidence, but still low confidence")
        if game["recent_playtime_minutes"]: reasons.append("Played on Steam in the last two weeks")
        game["reasons"] = reasons[:3]
    games.sort(key=lambda game: (-game["score"], -game["opportunity"], game["name"]))
    return {"games": games, "twitch_top": global_recommendations(db), "total_owned": total_owned, "matched": matched, "sampled_at": latest,
            "typical_viewers": round(typical, 1), "candidate_count": len(games)}
