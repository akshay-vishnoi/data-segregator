from __future__ import annotations

import sqlite3
from pathlib import Path

from .project import project_dir


def database_path(project_name: str) -> Path:
    return project_dir(project_name) / "database" / "media.sqlite"


def get_connection(project_name: str) -> sqlite3.Connection:
    path = database_path(project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(project_name: str) -> None:
    conn = get_connection(project_name)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            source_label TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            added_at TEXT NOT NULL,
            last_seen_at TEXT
        );

        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            source_label TEXT NOT NULL,
            source_relative_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            media_type TEXT NOT NULL,
            confidence TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            modified_epoch REAL NOT NULL,
            modified_iso TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            sha256 TEXT,
            hash_error TEXT,
            export_status TEXT NOT NULL DEFAULT 'not_exported',
            review_status TEXT NOT NULL DEFAULT 'not_reviewed',
            UNIQUE(source_id, source_relative_path),
            FOREIGN KEY(source_id) REFERENCES sources(source_id)
        );

        CREATE INDEX IF NOT EXISTS idx_media_size ON media_files(size_bytes);
        CREATE INDEX IF NOT EXISTS idx_media_type ON media_files(media_type);
        CREATE INDEX IF NOT EXISTS idx_media_extension ON media_files(extension);
        CREATE INDEX IF NOT EXISTS idx_media_hash ON media_files(sha256);
    """)
    conn.commit()
    conn.close()
