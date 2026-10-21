PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS streams (
  id INTEGER PRIMARY KEY,
  twitch_stream_id TEXT UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  category_id TEXT,
  category_name TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  offline_pending_at TEXT,
  peak_viewers INTEGER NOT NULL DEFAULT 0,
  average_viewers REAL,
  baseline_average_viewers REAL,
  viewer_minutes REAL NOT NULL DEFAULT 0,
  sample_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS viewer_samples (
  id INTEGER PRIMARY KEY,
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  sampled_at TEXT NOT NULL,
  viewer_count INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  category_name TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]',
  is_boosted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(stream_id, sampled_at)
);

CREATE TABLE IF NOT EXISTS community_events (
  id INTEGER PRIMARY KEY,
  twitch_event_id TEXT UNIQUE,
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  user_id TEXT,
  user_name TEXT,
  amount INTEGER,
  detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chat_activity (
  id INTEGER PRIMARY KEY,
  twitch_message_id TEXT UNIQUE,
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  occurred_at TEXT NOT NULL,
  user_id TEXT,
  user_name TEXT,
  message_length INTEGER NOT NULL DEFAULT 0,
  is_first_message INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_buckets (
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  bucket_start TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  unique_chatters INTEGER NOT NULL DEFAULT 0,
  first_time_chatters INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(stream_id, bucket_start)
);

CREATE TABLE IF NOT EXISTS chat_bucket_users (
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  bucket_start TEXT NOT NULL,
  user_id TEXT NOT NULL,
  PRIMARY KEY(stream_id, bucket_start, user_id)
);

CREATE TABLE IF NOT EXISTS stream_chat_summary (
  stream_id INTEGER PRIMARY KEY REFERENCES streams(id) ON DELETE CASCADE,
  message_count INTEGER NOT NULL DEFAULT 0,
  unique_chatters INTEGER NOT NULL DEFAULT 0,
  first_time_chatters INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stream_chatters (
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  PRIMARY KEY(stream_id, user_id)
);

CREATE TABLE IF NOT EXISTS content_items (
  id INTEGER PRIMARY KEY,
  twitch_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('video','clip')),
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  view_count INTEGER NOT NULL DEFAULT 0,
  creator_name TEXT,
  url TEXT,
  UNIQUE(kind, twitch_id)
);

CREATE TABLE IF NOT EXISTS stream_segments (
  id INTEGER PRIMARY KEY,
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  title TEXT NOT NULL DEFAULT '',
  category_id TEXT,
  category_name TEXT NOT NULL DEFAULT '',
  tags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS timeline_markers (
  id INTEGER PRIMARY KEY,
  stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
  occurred_at TEXT NOT NULL,
  marker_type TEXT NOT NULL DEFAULT 'note',
  label TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stream_notes (
  stream_id INTEGER PRIMARY KEY REFERENCES streams(id) ON DELETE CASCADE,
  summary TEXT NOT NULL DEFAULT '',
  promotion TEXT NOT NULL DEFAULT '',
  went_well TEXT NOT NULL DEFAULT '',
  improve TEXT NOT NULL DEFAULT '',
  next_test TEXT NOT NULL DEFAULT '',
  energy INTEGER,
  discord_promoted INTEGER,
  instagram_promoted INTEGER,
  threads_promoted INTEGER,
  promotion_at TEXT,
  guests TEXT NOT NULL DEFAULT '',
  special_event_type TEXT NOT NULL DEFAULT '',
  stream_plan TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign_links (
  slug TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  campaign_name TEXT NOT NULL,
  target_url TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign_clicks (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL REFERENCES campaign_links(slug) ON DELETE CASCADE,
  clicked_at TEXT NOT NULL,
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  device_class TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS campaign_click_buckets (
  slug TEXT NOT NULL REFERENCES campaign_links(slug) ON DELETE CASCADE,
  bucket_start TEXT NOT NULL,
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  click_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(slug,bucket_start)
);

CREATE TABLE IF NOT EXISTS stream_boss_runs (
  id INTEGER PRIMARY KEY,
  stream_id INTEGER REFERENCES streams(id) ON DELETE SET NULL,
  boss_key TEXT NOT NULL DEFAULT 'signal-gremlin',
  boss_name TEXT NOT NULL DEFAULT 'The Signal Gremlin',
  max_hp INTEGER NOT NULL DEFAULT 2500,
  hp INTEGER NOT NULL DEFAULT 2500,
  charge INTEGER NOT NULL DEFAULT 0,
  phase INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','defeated','retired')),
  winner_name TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  last_chat_id INTEGER NOT NULL DEFAULT 0,
  last_event_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_boss_contributions (
  run_id INTEGER NOT NULL REFERENCES stream_boss_runs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL,
  charge_given INTEGER NOT NULL DEFAULT 0,
  special_damage INTEGER NOT NULL DEFAULT 0,
  messages INTEGER NOT NULL DEFAULT 0,
  special_hits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id,user_id)
);

CREATE TABLE IF NOT EXISTS stream_boss_cooldowns (
  run_id INTEGER NOT NULL REFERENCES stream_boss_runs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  last_charge_at TEXT NOT NULL,
  PRIMARY KEY(run_id,user_id)
);

CREATE TABLE IF NOT EXISTS stream_boss_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES stream_boss_runs(id) ON DELETE CASCADE,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  label TEXT NOT NULL,
  amount INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY,
  rule_id TEXT NOT NULL,
  rule_version INTEGER NOT NULL,
  evidence_key TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  observation TEXT NOT NULL,
  sample_size INTEGER NOT NULL,
  confidence TEXT NOT NULL,
  supporting_stream_ids TEXT NOT NULL DEFAULT '[]',
  supporting_dates TEXT NOT NULL DEFAULT '[]',
  confounding_factors TEXT NOT NULL DEFAULT '[]',
  suggested_experiment TEXT NOT NULL,
  re_evaluate_after_streams INTEGER NOT NULL DEFAULT 3,
  status TEXT NOT NULL DEFAULT 'new',
  UNIQUE(rule_id,rule_version,evidence_key)
);

CREATE TABLE IF NOT EXISTS steam_games (
  app_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  playtime_minutes INTEGER NOT NULL DEFAULT 0,
  recent_playtime_minutes INTEGER NOT NULL DEFAULT 0,
  icon_url TEXT,
  twitch_game_id TEXT,
  twitch_name TEXT,
  match_method TEXT,
  ignored INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS twitch_category_snapshots (
  id INTEGER PRIMARY KEY,
  steam_app_id INTEGER NOT NULL REFERENCES steam_games(app_id) ON DELETE CASCADE,
  twitch_game_id TEXT NOT NULL,
  sampled_at TEXT NOT NULL,
  total_viewers INTEGER NOT NULL DEFAULT 0,
  active_channels INTEGER NOT NULL DEFAULT 0,
  top_viewers INTEGER NOT NULL DEFAULT 0,
  median_viewers REAL NOT NULL DEFAULT 0,
  top_share REAL NOT NULL DEFAULT 0,
  channels_above_typical INTEGER NOT NULL DEFAULT 0,
  pages_sampled INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  UNIQUE(steam_app_id, sampled_at)
);

CREATE TABLE IF NOT EXISTS twitch_global_snapshots (
  id INTEGER PRIMARY KEY,
  twitch_game_id TEXT NOT NULL,
  twitch_name TEXT NOT NULL,
  box_art_url TEXT,
  sampled_at TEXT NOT NULL,
  total_viewers INTEGER NOT NULL DEFAULT 0,
  active_channels INTEGER NOT NULL DEFAULT 0,
  top_viewers INTEGER NOT NULL DEFAULT 0,
  median_viewers REAL NOT NULL DEFAULT 0,
  top_share REAL NOT NULL DEFAULT 0,
  channels_above_typical INTEGER NOT NULL DEFAULT 0,
  pages_sampled INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  UNIQUE(twitch_game_id, sampled_at)
);

CREATE INDEX IF NOT EXISTS idx_samples_stream_time ON viewer_samples(stream_id, sampled_at);
CREATE INDEX IF NOT EXISTS idx_events_stream_time ON community_events(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_chat_stream_time ON chat_activity(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_chat_time ON chat_activity(occurred_at);
CREATE INDEX IF NOT EXISTS idx_chat_buckets_time ON chat_buckets(bucket_start);
CREATE INDEX IF NOT EXISTS idx_content_created ON content_items(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_segments_stream_time ON stream_segments(stream_id, started_at);
CREATE INDEX IF NOT EXISTS idx_markers_stream_time ON timeline_markers(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_campaign_clicks_time ON campaign_clicks(clicked_at);
CREATE INDEX IF NOT EXISTS idx_campaign_clicks_stream_time ON campaign_clicks(stream_id,clicked_at);
CREATE INDEX IF NOT EXISTS idx_boss_events_run_time ON stream_boss_events(run_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_steam_games_twitch ON steam_games(twitch_game_id);
CREATE INDEX IF NOT EXISTS idx_category_snapshots_game_time ON twitch_category_snapshots(steam_app_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_global_snapshots_time ON twitch_global_snapshots(sampled_at);
