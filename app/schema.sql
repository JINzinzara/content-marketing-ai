-- 사용자·제작권·불변 스타일 버전·이미지 작업과 게시 관측 저장
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    style TEXT NOT NULL DEFAULT '', style_proposal TEXT NOT NULL DEFAULT '', reference_notes TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#354f43', ig_user_id TEXT UNIQUE, ig_username TEXT,
    token BLOB, token_issued_at INTEGER, token_expires_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    reference TEXT NOT NULL, amount INTEGER NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(owner_id, reference)
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    product_name TEXT NOT NULL, facts TEXT NOT NULL, price TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL, original_file TEXT NOT NULL,
    title TEXT NOT NULL, caption TEXT NOT NULL, hashtags TEXT NOT NULL DEFAULT '',
    layout TEXT NOT NULL, aspect TEXT NOT NULL, color TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual', revision INTEGER NOT NULL DEFAULT 1,
    rendered_revision INTEGER, finished_file TEXT, finalized_at INTEGER,
    publish_state TEXT NOT NULL DEFAULT 'not_requested', container_id TEXT,
    ig_media_id TEXT UNIQUE, published_at TEXT, published_snapshot TEXT,
    publish_message TEXT NOT NULL DEFAULT '',
    promotion TEXT NOT NULL DEFAULT 'unknown', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY, owner_id TEXT, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS media_links (
    token_hash TEXT PRIMARY KEY, filename TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    owner_id TEXT NOT NULL REFERENCES accounts(id), media_id TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '', media_type TEXT, media_url TEXT,
    thumbnail_url TEXT, published_at TEXT, permalink TEXT,
    PRIMARY KEY(owner_id, media_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    media_id TEXT NOT NULL, observed_at TEXT NOT NULL, published_at TEXT,
    metrics TEXT NOT NULL, UNIQUE(owner_id, media_id, observed_at)
);
CREATE TABLE IF NOT EXISTS ai_calls (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    post_id TEXT REFERENCES posts(id), purpose TEXT NOT NULL, model TEXT NOT NULL,
    state TEXT NOT NULL, reserved_microusd INTEGER NOT NULL,
    cost_microusd INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    response_id TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    evidence_hash TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(owner_id, evidence_hash)
);
CREATE TABLE IF NOT EXISTS style_references (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL, caption TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS style_profiles (
    id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposal','confirmed')),
    content TEXT NOT NULL, reference_ids TEXT NOT NULL, created_at INTEGER NOT NULL,
    UNIQUE(owner_id, version)
);
CREATE TABLE IF NOT EXISTS image_generations (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES accounts(id),
    post_id TEXT NOT NULL REFERENCES posts(id), call_id TEXT UNIQUE NOT NULL REFERENCES ai_calls(id),
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','unknown')),
    style_snapshot TEXT NOT NULL, mask_points TEXT NOT NULL, prompt TEXT NOT NULL,
    parameters TEXT NOT NULL DEFAULT '{}',
    filename TEXT, message TEXT NOT NULL DEFAULT '', usage TEXT,
    created_at INTEGER NOT NULL, completed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS image_one_active ON image_generations(post_id)
    WHERE state IN ('queued','running','unknown');
