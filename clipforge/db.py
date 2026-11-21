"""SQLite storage. WAL mode lets the dashboard and the worker share one file."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from clipforge.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,            -- youtube | tiktok | instagram | facebook
    handle TEXT,
    niche TEXT NOT NULL,
    show_credit INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS creators (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT,                     -- twitch | youtube | kick | other
    channel_url TEXT,
    niche TEXT NOT NULL,
    crop_mode TEXT,                    -- overrides the niche default
    facecam TEXT,                      -- JSON {x,y,w,h} as 0..1 fractions of the source frame
    crop_x REAL NOT NULL DEFAULT 0.5,  -- horizontal focus for center crops
    auto_discover INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY,
    creator_id INTEGER NOT NULL REFERENCES creators(id),
    granted_by TEXT NOT NULL,          -- who said yes (name / handle)
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    scope TEXT NOT NULL DEFAULT 'clips',
    allows_monetization INTEGER NOT NULL DEFAULT 1,
    evidence TEXT,                     -- link / file path to the written permission
    notes TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    creator_id INTEGER REFERENCES creators(id),
    own_content INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL,
    external_id TEXT,
    title TEXT,
    niche TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT,
    progress REAL,
    error TEXT,
    duration REAL,
    video_path TEXT,
    audio_path TEXT,
    transcript_path TEXT,
    loudness_path TEXT,
    media_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sources_ext ON sources(external_id);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    rank INTEGER,
    start REAL NOT NULL,
    end REAL NOT NULL,
    peak REAL,
    score REAL NOT NULL,
    heuristic_score REAL,
    llm_score REAL,
    llm_reason TEXT,
    features TEXT,
    transcript TEXT,
    title TEXT,
    hook TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new | rendering | rendered | approved | rejected | failed
    clip_path TEXT,
    thumb_path TEXT,
    review_notes TEXT,
    reviewed_at TEXT,
    rendered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_candidates_status ON candidates(status);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,                -- ingest | render | discover | cleanup
    source_id INTEGER,
    candidate_id INTEGER,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, priority, id);

CREATE TABLE IF NOT EXISTS publish_queue (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    scheduled_at TEXT,
    caption TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | exported | posted | cancelled
    exported_path TEXT,
    post_url TEXT,
    posted_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_stats (
    id INTEGER PRIMARY KEY,
    publish_id INTEGER NOT NULL REFERENCES publish_queue(id),
    recorded_at TEXT NOT NULL,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    avg_view_pct REAL,
    followers_gained INTEGER,
    revenue REAL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def insert(conn: sqlite3.Connection, table: str, **values: Any) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, row_id: int, **values: Any) -> None:
    if table == "sources":
        values.setdefault("updated_at", now())
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))


def one(conn: sqlite3.Connection, sql: str, *args: Any) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def all_(conn: sqlite3.Connection, sql: str, *args: Any) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = one(conn, "SELECT value FROM kv WHERE key = ?", key)
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def enqueue(conn: sqlite3.Connection, job_type: str, *, source_id: int | None = None,
            candidate_id: int | None = None, payload: dict | None = None, priority: int = 0) -> int:
    return insert(conn, "jobs", type=job_type, source_id=source_id, candidate_id=candidate_id,
                  payload=json.dumps(payload or {}), priority=priority, created_at=now())


def claim_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with tx(conn):
        row = one(conn, "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority DESC, id LIMIT 1")
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
                     (now(), row["id"]))
    return one(conn, "SELECT * FROM jobs WHERE id = ?", row["id"])
