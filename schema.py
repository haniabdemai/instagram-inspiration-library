def create_schema(conn):
    """Create all tables, indexes, and constraints for the inspiration library."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('instagram', 'system', 'adhoc', 'cluster')),
            parent_collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
            description TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS videos (
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

        CREATE TABLE IF NOT EXISTS video_collections (
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            added_at INTEGER,
            PRIMARY KEY (video_id, collection_id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            UNIQUE(name, category)
        );

        CREATE TABLE IF NOT EXISTS video_tags (
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
            confidence REAL,
            generator TEXT,
            PRIMARY KEY (video_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL CHECK(level IN ('collection', 'cross_collection', 'cluster')),
            reference_id INTEGER,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
            transcript_text,
            notes,
            claude_insight,
            content='videos',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS videos_fts_insert AFTER INSERT ON videos BEGIN
            INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
            VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
        END;

        CREATE TRIGGER IF NOT EXISTS videos_fts_update
        AFTER UPDATE OF transcript_text, notes, claude_insight ON videos BEGIN
            INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
            VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
            INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
            VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
        END;

        CREATE TRIGGER IF NOT EXISTS videos_fts_delete AFTER DELETE ON videos BEGIN
            INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
            VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
        END;

        CREATE INDEX IF NOT EXISTS idx_videos_shortcode ON videos(shortcode);
        CREATE INDEX IF NOT EXISTS idx_videos_username ON videos(username);
        CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source);
        CREATE INDEX IF NOT EXISTS idx_videos_download_status ON videos(download_status);
        CREATE INDEX IF NOT EXISTS idx_videos_content_type ON videos(content_type);
        CREATE INDEX IF NOT EXISTS idx_video_collections_collection_id ON video_collections(collection_id);
        CREATE INDEX IF NOT EXISTS idx_video_collections_video_id ON video_collections(video_id);
        CREATE INDEX IF NOT EXISTS idx_video_tags_tag_id ON video_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_video_tags_video_id ON video_tags(video_id);
        CREATE INDEX IF NOT EXISTS idx_insights_level ON insights(level);
        CREATE INDEX IF NOT EXISTS idx_insights_reference_id ON insights(reference_id);
    """)
    conn.commit()
