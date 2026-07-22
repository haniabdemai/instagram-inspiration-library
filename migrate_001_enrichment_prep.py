"""Migration 001: Prepare schema for enrichment pipelines.

Changes:
  1. Add assemblyai_raw_json TEXT and summary TEXT columns to videos
  2. Fix transcript_source CHECK to include 'assemblyai' (requires table recreation)
  3. Add generator TEXT column to video_tags
  4. Create FTS5 sync triggers (INSERT, UPDATE, DELETE)

Safe to run multiple times: checks for already-applied changes.
Creates a backup before any destructive operations.

Only needed for databases created by early versions of schema.py: fresh
databases already include all of this, so new users can ignore this file.

Reference: docs/data-storage-approach.md
"""

import shutil
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "library.db"


def check_column_exists(conn, table, column):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == column for c in cols)


def check_trigger_exists(conn, name):
    result = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    return result[0] > 0


def check_transcript_source_allows_assemblyai(conn):
    """Check if the CHECK constraint on transcript_source includes 'assemblyai'."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()[0]
    return "'assemblyai'" in sql


def backup_database():
    backup_path = DB_PATH.with_suffix(".db.pre-migration-001")
    if backup_path.exists():
        print(f"  Backup already exists at {backup_path.name}")
        return backup_path
    shutil.copy2(DB_PATH, backup_path)
    print(f"  Backup created: {backup_path.name}")
    return backup_path


def add_new_columns(conn):
    """Step 1: Add assemblyai_raw_json and summary to videos."""
    changed = False

    if not check_column_exists(conn, "videos", "assemblyai_raw_json"):
        conn.execute("ALTER TABLE videos ADD COLUMN assemblyai_raw_json TEXT")
        print("  Added: videos.assemblyai_raw_json")
        changed = True
    else:
        print("  Skipped: videos.assemblyai_raw_json (already exists)")

    if not check_column_exists(conn, "videos", "summary"):
        conn.execute("ALTER TABLE videos ADD COLUMN summary TEXT")
        print("  Added: videos.summary")
        changed = True
    else:
        print("  Skipped: videos.summary (already exists)")

    return changed


def fix_transcript_source_check(conn):
    """Step 2: Recreate videos table with corrected CHECK constraint.

    SQLite doesn't support ALTER COLUMN, so we recreate the table.
    This preserves all data: enrichment columns are all NULL at this point.
    """
    if check_transcript_source_allows_assemblyai(conn):
        print("  Skipped: transcript_source CHECK (already includes 'assemblyai')")
        return False

    print("  Recreating videos table with corrected transcript_source CHECK...")

    conn.execute("PRAGMA foreign_keys = OFF")

    conn.executescript("""
        CREATE TABLE videos_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instagram_url TEXT NOT NULL UNIQUE,
            shortcode TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            content_type TEXT NOT NULL CHECK(content_type IN ('reel', 'post', 'unknown')),
            saved_at INTEGER,
            source TEXT NOT NULL CHECK(source IN ('both', 'collections_only', 'saved_only')),
            local_path TEXT,
            thumbnail_path TEXT,
            duration REAL,
            download_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(download_status IN ('pending', 'downloaded', 'failed', 'unavailable')),
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT,
            audio_type TEXT CHECK(audio_type IN ('speech', 'music', 'voiceover_with_music', 'no_audio', 'mixed')),
            has_burned_captions BOOLEAN,
            has_meaningful_audio BOOLEAN,
            transcript_json TEXT,
            transcript_text TEXT,
            transcript_source TEXT CHECK(transcript_source IN ('assemblyai', 'deepgram', 'captions', 'whisperx')),
            transcript_language TEXT,
            music_json TEXT,
            analysis_json TEXT,
            transcription_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(transcription_status IN ('pending', 'done', 'failed', 'not_applicable')),
            analysis_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(analysis_status IN ('pending', 'done', 'failed')),
            embedding_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(embedding_status IN ('pending', 'done', 'failed')),
            claude_insight TEXT,
            assemblyai_raw_json TEXT,
            summary TEXT
        );

        INSERT INTO videos_new SELECT
            id, instagram_url, shortcode, username, content_type, saved_at, source,
            local_path, thumbnail_path, duration, download_status, notes, created_at,
            updated_at, audio_type, has_burned_captions, has_meaningful_audio,
            transcript_json, transcript_text, transcript_source, transcript_language,
            music_json, analysis_json, transcription_status, analysis_status,
            embedding_status, claude_insight, assemblyai_raw_json, summary
        FROM videos;

        DROP TABLE videos;
        ALTER TABLE videos_new RENAME TO videos;

        -- Recreate indexes
        CREATE INDEX idx_videos_shortcode ON videos(shortcode);
        CREATE INDEX idx_videos_username ON videos(username);
        CREATE INDEX idx_videos_source ON videos(source);
        CREATE INDEX idx_videos_download_status ON videos(download_status);
        CREATE INDEX idx_videos_content_type ON videos(content_type);
    """)

    conn.execute("PRAGMA foreign_keys = ON")
    print("  Done: transcript_source CHECK now includes 'assemblyai'")
    return True


def add_generator_column(conn):
    """Step 3: Add generator column to video_tags."""
    if check_column_exists(conn, "video_tags", "generator"):
        print("  Skipped: video_tags.generator (already exists)")
        return False

    conn.execute("ALTER TABLE video_tags ADD COLUMN generator TEXT")
    print("  Added: video_tags.generator")
    return True


def create_fts_triggers(conn):
    """Step 4: Create FTS5 sync triggers."""
    triggers = {
        "videos_fts_insert": """
            CREATE TRIGGER videos_fts_insert AFTER INSERT ON videos BEGIN
                INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
                VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
            END;
        """,
        "videos_fts_update": """
            CREATE TRIGGER videos_fts_update
            AFTER UPDATE OF transcript_text, notes, claude_insight ON videos BEGIN
                INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
                VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
                INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
                VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
            END;
        """,
        "videos_fts_delete": """
            CREATE TRIGGER videos_fts_delete AFTER DELETE ON videos BEGIN
                INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
                VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
            END;
        """,
    }

    changed = False
    for name, sql in triggers.items():
        if check_trigger_exists(conn, name):
            print(f"  Skipped: {name} (already exists)")
        else:
            conn.execute(sql)
            print(f"  Created: {name}")
            changed = True

    return changed


def rebuild_fts_index(conn):
    """Rebuild FTS index to sync with current videos table contents.

    After table recreation, the FTS content table reference may be stale.
    The 'rebuild' command re-reads all rows from the content table.
    """
    conn.execute("INSERT INTO videos_fts(videos_fts) VALUES ('rebuild')")
    print("  Rebuilt FTS index")


def verify(conn):
    """Verify migration was applied correctly."""
    errors = []

    if not check_column_exists(conn, "videos", "assemblyai_raw_json"):
        errors.append("videos.assemblyai_raw_json missing")
    if not check_column_exists(conn, "videos", "summary"):
        errors.append("videos.summary missing")
    if not check_transcript_source_allows_assemblyai(conn):
        errors.append("transcript_source CHECK doesn't include 'assemblyai'")
    if not check_column_exists(conn, "video_tags", "generator"):
        errors.append("video_tags.generator missing")
    for trigger in ("videos_fts_insert", "videos_fts_update", "videos_fts_delete"):
        if not check_trigger_exists(conn, trigger):
            errors.append(f"trigger {trigger} missing")

    row_count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

    if errors:
        print(f"\n  VERIFICATION FAILED:")
        for e in errors:
            print(f"    - {e}")
        return False

    print(f"\n  Verification passed. {row_count} videos intact.")
    return True


def main():
    if not DB_PATH.exists():
        print(f"Error: database not found at {DB_PATH}")
        sys.exit(1)

    print(f"Migration 001: Enrichment prep")
    print(f"Database: {DB_PATH}")
    print()

    print("Backing up...")
    backup_database()
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    any_changes = False

    print("Step 1: Add new columns to videos...")
    any_changes |= add_new_columns(conn)
    conn.commit()

    print("Step 2: Fix transcript_source CHECK constraint...")
    any_changes |= fix_transcript_source_check(conn)
    conn.commit()

    print("Step 3: Add generator column to video_tags...")
    any_changes |= add_generator_column(conn)
    conn.commit()

    print("Step 4: Create FTS5 sync triggers...")
    any_changes |= create_fts_triggers(conn)
    conn.commit()

    if any_changes:
        print("\nRebuilding FTS index...")
        rebuild_fts_index(conn)
        conn.commit()

    print("\nVerifying...")
    if verify(conn):
        print("\nMigration 001 complete.")
    else:
        print("\nMigration failed: restore from backup.")
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
