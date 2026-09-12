-- Киллер: начальная схема.
-- Источник правды по ходу игры — таблица event; текущие цели лежат в
-- participant.target_id и являются производной, пересчитываемой при каждом
-- применённом событии.

CREATE TABLE user (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    last_name             TEXT    NOT NULL,
    first_name            TEXT    NOT NULL,
    middle_name           TEXT    NOT NULL DEFAULT '',
    qualifier             TEXT    NOT NULL DEFAULT '',
    name_normalized       TEXT    NOT NULL UNIQUE,
    password_hash         TEXT    NOT NULL,
    role                  TEXT    NOT NULL DEFAULT 'user',      -- user | sysadmin
    status                TEXT    NOT NULL DEFAULT 'active',    -- active | blocked
    telegram              TEXT    NOT NULL DEFAULT '',
    avatar_emoji          TEXT    NOT NULL DEFAULT '🕵',
    department            TEXT    NOT NULL DEFAULT '',
    created_at            TEXT    NOT NULL,
    last_seen_at          TEXT
);

CREATE TABLE session (
    id                    TEXT    PRIMARY KEY,          -- случайный токен (хеш)
    user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    user_agent            TEXT    NOT NULL DEFAULT '',
    created_at            TEXT    NOT NULL,
    last_seen_at          TEXT    NOT NULL,
    revoked_at            TEXT
);
CREATE INDEX idx_session_user ON session(user_id);

CREATE TABLE game (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT    NOT NULL,
    description           TEXT    NOT NULL DEFAULT '',
    status                TEXT    NOT NULL DEFAULT 'draft',  -- draft|recruiting|running|paused|finished|void
    visibility            TEXT    NOT NULL DEFAULT 'open',   -- open | private
    color                 TEXT    NOT NULL DEFAULT '#D9A441',
    capacity              INTEGER,
    reveal_after_finish   INTEGER NOT NULL DEFAULT 0,
    admin_user_id         INTEGER NOT NULL REFERENCES user(id),
    rules_json            TEXT    NOT NULL DEFAULT '{}',
    winner_participant_id INTEGER,
    created_at            TEXT    NOT NULL,
    started_at            TEXT,
    deadline_at           TEXT,
    finished_at           TEXT
);
CREATE INDEX idx_game_status ON game(status);

CREATE TABLE participant (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               INTEGER NOT NULL REFERENCES game(id) ON DELETE CASCADE,
    user_id               INTEGER REFERENCES user(id) ON DELETE SET NULL,
    display_name_snapshot TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'invited', -- invited|joined|declined|alive|dead|withdrawn
    target_id             INTEGER REFERENCES participant(id),
    kills_count           INTEGER NOT NULL DEFAULT 0,
    place                 INTEGER,
    invited_at            TEXT    NOT NULL,
    joined_at             TEXT,
    died_at               TEXT,
    UNIQUE (game_id, user_id)
);
CREATE INDEX idx_participant_game ON participant(game_id, status);

CREATE TABLE event (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               INTEGER NOT NULL REFERENCES game(id) ON DELETE CASCADE,
    type                  TEXT    NOT NULL,
    actor_participant_id  INTEGER,
    subject_participant_id INTEGER,
    payload_json          TEXT    NOT NULL DEFAULT '{}',
    created_at            TEXT    NOT NULL,
    reverted_at           TEXT
);
CREATE INDEX idx_event_game ON event(game_id, id);

CREATE TABLE kill_claim (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               INTEGER NOT NULL REFERENCES game(id) ON DELETE CASCADE,
    killer_id             INTEGER NOT NULL REFERENCES participant(id),
    victim_id             INTEGER NOT NULL REFERENCES participant(id),
    chain_version         INTEGER NOT NULL DEFAULT 0,
    status                TEXT    NOT NULL DEFAULT 'pending', -- pending|confirmed|cancelled
    created_by            INTEGER NOT NULL REFERENCES participant(id),
    cancel_reason         TEXT,
    created_at            TEXT    NOT NULL,
    resolved_at           TEXT
);
CREATE INDEX idx_claim_game ON kill_claim(game_id, status);
CREATE INDEX idx_claim_victim ON kill_claim(victim_id, status);

CREATE TABLE edge_snapshot (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id               INTEGER NOT NULL REFERENCES game(id) ON DELETE CASCADE,
    hunter_id             INTEGER NOT NULL,
    target_id             INTEGER NOT NULL,
    valid_from_event_id   INTEGER NOT NULL,
    valid_to_event_id     INTEGER
);
CREATE INDEX idx_edge_game ON edge_snapshot(game_id, valid_to_event_id);

CREATE TABLE support_request (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    code                  TEXT    NOT NULL UNIQUE,
    user_id               INTEGER REFERENCES user(id) ON DELETE SET NULL,
    claimed_name          TEXT    NOT NULL DEFAULT '',
    game_id               INTEGER REFERENCES game(id) ON DELETE SET NULL,
    subject_participant_id INTEGER,
    type                  TEXT    NOT NULL,   -- password|name_conflict|withdraw|other
    status                TEXT    NOT NULL DEFAULT 'open',  -- open|resolved|rejected
    message               TEXT    NOT NULL DEFAULT '',
    created_at            TEXT    NOT NULL,
    resolved_at           TEXT,
    resolved_by           INTEGER
);
CREATE INDEX idx_support_status ON support_request(status, created_at);

CREATE TABLE reset_code (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    code_hash             TEXT    NOT NULL,
    issued_by             INTEGER,
    expires_at            TEXT    NOT NULL,
    used_at               TEXT
);

CREATE TABLE device_code (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    source_session_id     TEXT    NOT NULL,
    code_hash             TEXT    NOT NULL,
    attempts              INTEGER NOT NULL DEFAULT 0,
    expires_at            TEXT    NOT NULL,
    used_at               TEXT
);

CREATE TABLE notification (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    type                  TEXT    NOT NULL,
    game_id               INTEGER REFERENCES game(id) ON DELETE CASCADE,
    text                  TEXT    NOT NULL,
    read_at               TEXT,
    created_at            TEXT    NOT NULL
);
CREATE INDEX idx_notification_user ON notification(user_id, read_at);

CREATE TABLE achievement (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    code                  TEXT    NOT NULL,
    game_id               INTEGER REFERENCES game(id) ON DELETE SET NULL,
    awarded_at            TEXT    NOT NULL,
    UNIQUE (user_id, code, game_id)
);

CREATE TABLE audit_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id         INTEGER,
    action                TEXT    NOT NULL,
    target_type           TEXT    NOT NULL DEFAULT '',
    target_id             INTEGER,
    payload_json          TEXT    NOT NULL DEFAULT '{}',
    created_at            TEXT    NOT NULL
);
CREATE INDEX idx_audit_created ON audit_log(created_at);

CREATE TABLE setting (
    key                   TEXT    PRIMARY KEY,
    value_json            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    updated_by            INTEGER
);

CREATE TABLE rate_hit (
    bucket                TEXT    NOT NULL,
    created_at            TEXT    NOT NULL
);
CREATE INDEX idx_rate_bucket ON rate_hit(bucket, created_at);
