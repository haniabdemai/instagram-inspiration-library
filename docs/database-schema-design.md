# Design Spec: Database Schema & Export Parsing

**Date:** 2026-03-16
**Status:** Draft: awaiting approval
**Source:** feature-discovery and project-vision documents from the private planning corpus (not part of this repo)

---

## Goal

Parse the Instagram export data into a SQLite database that supports all four phases of the project (V1–V4). Create all tables now: populated or empty, so the schema is complete from day one and no migrations are needed as features are added.

---

## Approach

**Approach A (temporary):** A single `parse_export.py` script using Python's standard library `sqlite3` module. No ORM, no package structure. This will be replaced with a proper Python package (Approach B) before V2 begins.

---

## Conventions

**Timestamp convention:** Instagram-sourced timestamps are stored as epoch integers (the format Instagram exports them in). Application-generated timestamps (when we add a record to our database, when we update it) use ISO 8601 TEXT via SQLite's `datetime('now')`. This is a deliberate choice: epoch integers preserve the original data exactly, while application timestamps benefit from human-readable ISO format.

**Foreign keys:** All foreign keys use `ON DELETE CASCADE`. SQLite does not enforce foreign keys by default, so the parsing script (and any future code) must issue `PRAGMA foreign_keys = ON` before any operations.

---

## Database Schema

### Table: `collections`

Covers Instagram collections, system collections ("Uncollected"), user-created ad hoc groups (V3), and auto-generated clusters (V3). Note: collection names are NOT unique: Instagram allows duplicate names (e.g. two separate "Funny" collections exist in this export).

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT | NOT NULL | Display name (emoji-decoded). Not unique: duplicates possible |
| `type` | TEXT | NOT NULL, CHECK IN ('instagram', 'system', 'adhoc', 'cluster') | What kind of collection |
| `parent_collection_id` | INTEGER | REFERENCES collections(id) ON DELETE CASCADE, NULLABLE | For clusters, which collection this is a sub-cluster of |
| `description` | TEXT | NULLABLE | User or auto-generated description |
| `created_at` | INTEGER | NULLABLE | Instagram creation timestamp (epoch) |
| `updated_at` | INTEGER | NULLABLE | Instagram update timestamp (epoch) |

### Table: `videos`

Central table. All enrichment fields are nullable: populated progressively across V1–V3.

**V1 fields (populated during export parsing + download):**

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `instagram_url` | TEXT | NOT NULL UNIQUE | Full Instagram URL |
| `shortcode` | TEXT | NOT NULL UNIQUE | Extracted from URL (e.g. "Cxample1abc"): the unique identifier on Instagram. Primary deduplication key for V4 Chrome Extension |
| `username` | TEXT | NOT NULL | Instagram username of the creator |
| `content_type` | TEXT | NOT NULL, CHECK IN ('reel', 'post', 'unknown') | Derived from URL pattern. 'unknown' for URLs that don't match `/reel/` or `/p/` |
| `saved_at` | INTEGER | NULLABLE | Global save timestamp from `saved_posts.json` (field: `string_map_data["Saved on"]["timestamp"]`). NULL if collections_only. This is distinct from `video_collections.added_at` which records when a video was added to a specific collection |
| `source` | TEXT | NOT NULL, CHECK IN ('both', 'collections_only', 'saved_only') | Data provenance, which export file(s) this appeared in |
| `local_path` | TEXT | NULLABLE | Path to downloaded video file |
| `thumbnail_path` | TEXT | NULLABLE | Path to extracted keyframe image |
| `duration` | REAL | NULLABLE | Video duration in seconds |
| `download_status` | TEXT | NOT NULL DEFAULT 'pending', CHECK IN ('pending', 'downloaded', 'failed', 'unavailable') | |
| `notes` | TEXT | NULLABLE | User's freeform notes: first-class, searchable, included in embeddings |
| `created_at` | TEXT | NOT NULL DEFAULT (datetime('now')) | When added to our database |
| `updated_at` | TEXT | NULLABLE | Last modification timestamp |

**V2 fields (populated during enrichment pipeline):**

| Column | Type | Constraints | Description |
|---|---|---|---|
| `audio_type` | TEXT | NULLABLE, CHECK IN ('speech', 'music', 'voiceover_with_music', 'no_audio', 'mixed') | Audio classification: routing decision for downstream pipelines |
| `has_burned_captions` | BOOLEAN | NULLABLE | Whether on-screen captions were detected |
| `has_meaningful_audio` | BOOLEAN | NULLABLE | FALSE for purely visual videos with no speech content |
| `transcript_json` | TEXT | NULLABLE | Structured JSON: speaker labels, word-level timestamps, confidence scores, segments |
| `transcript_text` | TEXT | NULLABLE | Plain text version for full-text search (FTS5) |
| `transcript_source` | TEXT | NULLABLE, CHECK IN ('assemblyai', 'deepgram', 'captions') | Which tool produced the transcript |
| `transcript_language` | TEXT | NULLABLE | ISO 639-1 code ('en', 'fr') |
| `music_json` | TEXT | NULLABLE | Flexible JSON: shape depends on identification success. See decisions.md |
| `analysis_json` | TEXT | NULLABLE | Twelve Labs Pegasus structured output. Queryable via json_extract() |
| `transcription_status` | TEXT | NOT NULL DEFAULT 'pending', CHECK IN ('pending', 'done', 'failed', 'not_applicable') | |
| `analysis_status` | TEXT | NOT NULL DEFAULT 'pending', CHECK IN ('pending', 'done', 'failed') | |
| `embedding_status` | TEXT | NOT NULL DEFAULT 'pending', CHECK IN ('pending', 'done', 'failed') | |

**V3 fields (populated by Claude insight layer):**

| Column | Type | Constraints | Description |
|---|---|---|---|
| `claude_insight` | TEXT | NULLABLE | Video-level interpretation. Context-aware based on collection membership |

### Table: `video_collections`

Many-to-many junction. A video in three collections has three rows.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `video_id` | INTEGER | NOT NULL REFERENCES videos(id) ON DELETE CASCADE | |
| `collection_id` | INTEGER | NOT NULL REFERENCES collections(id) ON DELETE CASCADE | |
| `added_at` | INTEGER | NULLABLE | Epoch timestamp from `saved_collections.json` (field: `string_map_data["Added Time"]["timestamp"]`) |
| | | PRIMARY KEY (video_id, collection_id) | |

### Table: `tags`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `name` | TEXT | NOT NULL | Tag display name |
| `category` | TEXT | NULLABLE | e.g. 'mood', 'technique', 'subject', 'colour', 'location', 'genre' |
| | | UNIQUE (name, category) | Prevent duplicate tag+category pairs |

### Table: `video_tags`

| Column | Type | Constraints | Description |
|---|---|---|---|
| `video_id` | INTEGER | NOT NULL REFERENCES videos(id) ON DELETE CASCADE | |
| `tag_id` | INTEGER | NOT NULL REFERENCES tags(id) ON DELETE CASCADE | |
| `source` | TEXT | NOT NULL, CHECK IN ('auto', 'manual') | Was this auto-generated or user-applied? |
| `confidence` | REAL | NULLABLE | For auto-generated tags: how confident the model was |
| | | PRIMARY KEY (video_id, tag_id) | |

### Table: `insights`

Claude's analysis at levels above individual videos.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| `level` | TEXT | NOT NULL, CHECK IN ('collection', 'cross_collection', 'cluster') | What level this insight covers |
| `reference_id` | INTEGER | NULLABLE | collection.id for collection/cluster level. NULL for cross-collection |
| `content` | TEXT | NOT NULL | The insight text |
| `created_at` | TEXT | NOT NULL DEFAULT (datetime('now')) | |
| `updated_at` | TEXT | NULLABLE | |

### Virtual table: sqlite-vec embeddings (V2)

```sql
-- Created when sqlite-vec is installed and embeddings are generated
CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
    video_id INTEGER PRIMARY KEY,
    embedding FLOAT[512]
);
```

### Virtual table: FTS5 full-text search (V2)

```sql
-- Created when transcripts and analysis are populated (V2).
-- claude_insight is included now to avoid recreating the FTS table in V3;
-- it will be NULL until V3 populates it, which FTS5 handles gracefully.
CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    transcript_text,
    notes,
    claude_insight,
    content='videos',
    content_rowid='id'
);
```

---

## Parsing Logic

### Input files

1. **`saved_collections.json`**: primary source. A flat JSON array under the key `saved_saved_collections`. Contains collection headers (entries with `title: "Collection"`) followed by their member videos. **36 collections** (note: "Funny" appears twice as separate collections with different creation dates and different videos), **712 video entries**, **704 unique URLs**. Total array length: 748.
2. **`saved_posts.json`**: secondary source. A flat JSON array under the key `saved_saved_media`. 595 saved items with timestamps but no collection info.

### JSON field paths

**`saved_collections.json`: collection header:**
- Identified by: `item["title"] == "Collection"`
- Collection name: `item["string_map_data"]["Name"]["value"]`
- Created: `item["string_map_data"]["Creation Time"]["timestamp"]`
- Updated: `item["string_map_data"]["Update Time"]["timestamp"]`

**`saved_collections.json`: video entry (no `title` field):**
- Instagram URL: `item["string_map_data"]["Name"]["href"]`
- Username: `item["string_map_data"]["Name"]["value"]`
- Added to collection: `item["string_map_data"]["Added Time"]["timestamp"]`

**`saved_posts.json`: video entry:**
- Username: `item["title"]` (note: different location from collections file)
- Instagram URL: `item["string_map_data"]["Saved on"]["href"]`
- Saved timestamp: `item["string_map_data"]["Saved on"]["timestamp"]`

### Processing steps

1. Read `saved_collections.json`. Walk the flat array:
   - Entry with `title == "Collection"` → create collection record. Do NOT merge duplicate names: two collections called "Funny" stay as two separate records, distinguished by `id` and `created_at`
   - Entry without `title` → add to current collection, extract URL + username + added_at using the field paths above
2. Fix double-encoded UTF-8 in collection names: `name.encode('latin-1').decode('utf-8')`
3. Extract shortcode from each URL (the path segment after `/reel/` or `/p/`, stripping trailing slash)
4. Determine content_type from URL pattern (`/reel/` → 'reel', `/p/` → 'post')
5. Read `saved_posts.json`. For each entry, extract URL (from `string_map_data["Saved on"]["href"]`), username (from `title`), and saved_at timestamp (from `string_map_data["Saved on"]["timestamp"]`)
6. Merge:
   - URLs in both files → `source = 'both'`, carry `saved_at` from saved_posts
   - URLs only in collections → `source = 'collections_only'`, `saved_at = NULL`
   - URLs only in saved_posts → `source = 'saved_only'`, add to "Uncollected" system collection
7. Create "Uncollected" system collection for the ~26 saved-only items
8. Insert all data with foreign keys

### Validation

After parsing, print summary stats:
- Total videos, collections, video-collection links
- Breakdown by source (both / collections_only / saved_only)
- Breakdown by content type (reel / post)
- Videos in multiple collections
- Any URLs that failed to parse

---

## Indexes

```sql
CREATE INDEX idx_videos_shortcode ON videos(shortcode);
CREATE INDEX idx_videos_username ON videos(username);
CREATE INDEX idx_videos_source ON videos(source);
CREATE INDEX idx_videos_download_status ON videos(download_status);
CREATE INDEX idx_videos_content_type ON videos(content_type);
CREATE INDEX idx_video_collections_collection_id ON video_collections(collection_id);
CREATE INDEX idx_video_collections_video_id ON video_collections(video_id);
CREATE INDEX idx_video_tags_tag_id ON video_tags(tag_id);
CREATE INDEX idx_video_tags_video_id ON video_tags(video_id);
CREATE INDEX idx_insights_level ON insights(level);
CREATE INDEX idx_insights_reference_id ON insights(reference_id);
```

---

## What this spec does NOT cover

- **Video downloading strategy**: how to fetch videos from Instagram URLs (yt-dlp, instaloader, etc.). Separate spec needed.
- **Thumbnail extraction**: ffmpeg frame extraction. Implementation detail for V1.
- **Frontend**: web UI tech stack and architecture. Separate spec needed.
- **Enrichment pipeline**: AssemblyAI, Twelve Labs, Claude integration. See `docs/epic2-transcription-approach.md` and `docs/data-storage-approach.md`.
- **Conversation history**: deferred. Design when V3 scope is clearer.
- **V4 `source` values**: the CHECK constraint on `videos.source` currently only covers export-related values ('both', 'collections_only', 'saved_only'). V4's Chrome Extension will need additional values (e.g. 'extension'). The constraint can be altered at that point.

---

## Temporary nature of Approach A

This implementation uses a single Python script (`parse_export.py`) with the standard library `sqlite3` module. Before V2 begins, this must be refactored into a proper Python package (`src/inspo_library/`) with:
- Separate modules for database, parsing, and pipeline stages
- An ORM or migration tool for schema management
- Configuration management (paths, API keys)
- Proper error handling and logging

The database file and schema will carry forward: only the code that interacts with it changes.
