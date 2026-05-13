-- Files known to the system.
CREATE TABLE IF NOT EXISTS files (
    id              BIGSERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    media_type      TEXT NOT NULL CHECK (media_type IN ('video', 'audio')),
    size            BIGINT NOT NULL,
    mtime           TIMESTAMPTZ NOT NULL,
    sha256          TEXT,

    -- ffprobe-derived metadata
    duration        DOUBLE PRECISION,
    width           INTEGER,
    height          INTEGER,
    video_codec     TEXT,
    audio_codec     TEXT,
    bitrate         INTEGER,
    sample_rate     INTEGER,
    channels        INTEGER,

    -- video fingerprint: array of 64-bit perceptual hashes (one per sampled frame)
    phashes         BIGINT[],

    -- audio fingerprint: chromaprint compressed string + reference duration
    chromaprint     TEXT,
    chromaprint_dur DOUBLE PRECISION,

    -- processing state
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    processing_started_at TIMESTAMPTZ,
    fingerprinted_at TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processing','done','failed','missing','deleted')),
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,

    -- which source root this file was discovered under (FK, but the actual
    -- ALTER TABLE ADD CONSTRAINT lives in app/db.py migrations for graceful
    -- handling on existing installs).
    source_id       BIGINT
);

CREATE INDEX IF NOT EXISTS idx_files_status      ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_media_type  ON files(media_type);
CREATE INDEX IF NOT EXISTS idx_files_size        ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_sha256      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_source_id   ON files(source_id);

-- Configured scan roots. Each source is a named subdirectory under /media
-- inside the containers, corresponding to a host bind mount (managed
-- externally via fstab/autofs).
CREATE TABLE IF NOT EXISTS sources (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    path        TEXT UNIQUE NOT NULL,
    media_type  TEXT NOT NULL CHECK (media_type IN ('video', 'audio', 'both')),
    priority    INTEGER NOT NULL DEFAULT 0,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled);

-- A duplicate group (one row per cluster of files that match each other).
CREATE TABLE IF NOT EXISTS dup_groups (
    id              BIGSERIAL PRIMARY KEY,
    media_type      TEXT NOT NULL CHECK (media_type IN ('video', 'audio')),
    match_type      TEXT NOT NULL CHECK (match_type IN ('exact', 'perceptual', 'chromaprint', 'filename', 'llm')),
    similarity      DOUBLE PRECISION,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    reviewed        BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_groups_media_type ON dup_groups(media_type);
CREATE INDEX IF NOT EXISTS idx_groups_reviewed   ON dup_groups(reviewed);
CREATE INDEX IF NOT EXISTS idx_groups_match_type ON dup_groups(match_type);

CREATE TABLE IF NOT EXISTS dup_members (
    group_id        BIGINT NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    file_id         BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    is_keeper       BOOLEAN NOT NULL DEFAULT FALSE,
    action          TEXT CHECK (action IN ('keep','delete','ignore')),
    PRIMARY KEY (group_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_members_file_id ON dup_members(file_id);

-- Audit log of destructive actions.
CREATE TABLE IF NOT EXISTS action_log (
    id              BIGSERIAL PRIMARY KEY,
    file_id         BIGINT,
    path            TEXT NOT NULL,
    action          TEXT NOT NULL,
    succeeded       BOOLEAN NOT NULL,
    error           TEXT,
    bytes_freed     BIGINT,
    performed_at    TIMESTAMPTZ DEFAULT NOW()
);
