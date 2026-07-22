# Data Storage Approach: Enrichment Pipeline

**Date:** 2026-03-19
**Status:** Approved: schema changes required before pipeline runs
**Context:** Defines how enrichment data from all sources (AssemblyAI, Twelve Labs, Essentia, Claude) is stored in the database. Applies across all enrichment epics (2, 3, 4, 5). Supersedes nothing: this is net new.

---

## Principles

**1. Time is the universal axis.** Every piece of enrichment data: what's said, what's shown, what music plays, what the camera does, happens at a specific moment in the video. All timestamps are stored as **seconds from video start, as floats** (e.g. `12.35`), regardless of what format the source API uses. This is what allows "what was being said during that tracking shot" to be answered.

**2. Store data in the shape it'll be queried.** Three query modes exist in the system, and each has its own storage form:
- **Structured filtering** (SQL on tables): tags, collections, audio type, language, creator. Labels that users filter by go in the `tags`/`video_tags` tables.
- **Text search** (FTS5): keyword matching across transcripts, notes, analysis. Plain text goes in flat fields that feed the `videos_fts` virtual table.
- **Semantic search** (sqlite-vec): meaning-based matching via embeddings. Vectors go in the `video_embeddings` virtual table.

If data serves filtering, it goes in a table. If it serves text search, it goes in a flat field. If it serves timeline alignment, it goes in JSON. Data should not be buried in a JSON blob if users will want to filter or search by it.

**3. Each enrichment layer adds without restructuring.** The schema was designed with all enrichment fields from day one (nullable, populated progressively). Transcription fills `transcript_json` and related fields. Visual analysis fills `analysis_json`. Audio intelligence fills `music_json`. Claude fills `claude_insight`. Each layer slots in without altering what came before. The tags system absorbs labels from any source through a common interface.

**4. Raw API responses are always preserved.** Every external API response is stored verbatim. The structured storage (tags, JSON, flat fields) is our interpretation of the raw data: a decision made at pipeline-write time. If that decision turns out to be wrong, or new features need data we didn't extract, the raw response lets us re-derive without paying to re-process.

**5. Cross-layer alignment uses tolerance, not exact matching.** Audio timestamps (from AssemblyAI) and video timestamps (from Twelve Labs) may drift by up to a few hundred milliseconds due to Instagram's re-encoding. Any query that joins data across layers should use a tolerance window (0.5s recommended) rather than exact timestamp equality.

---

## Data Flow: From API Response to Database

Every enrichment source follows the same four-step flow:

```
1. Raw response → stored verbatim (insurance, never lose data)
2. Timeline data → normalised into canonical JSON shape (seconds, documented schema)
3. Labels → extracted into tags/video_tags (filterable, with generator attribution)
4. Plain text → written to flat fields (FTS5 searchable)
```

---

## Transcription Data (AssemblyAI: Epic 2)

### Step 1: Raw Response

| Field | Type | Description |
|---|---|---|
| `assemblyai_raw_json` | TEXT | **New field.** Complete AssemblyAI API response, stored verbatim as JSON. ~50-200 KB per video. For 730 videos: ~36-146 MB total. Not queried directly: exists for reprocessing. |

### Step 2: Timeline Data: `transcript_json`

Canonical JSON shape. AssemblyAI's response is normalised into this structure at write time. If we ever use Deepgram or another service, the normalisation layer changes but this shape stays the same. Downstream code only knows this shape.

```json
{
  "utterances": [
    {
      "start": 1.25,
      "end": 4.82,
      "speaker": "A",
      "text": "The trick with sourdough is patience, not flour...",
      "confidence": 0.68,
      "words": [
        {"word": "The", "start": 1.25, "end": 1.52, "confidence": 0.92, "speaker": "A"},
        {"word": "trick", "start": 1.53, "end": 1.71, "confidence": 0.88, "speaker": "A"}
      ]
    }
  ],
  "sentiment": [
    {
      "start": 1.25,
      "end": 4.82,
      "text": "The trick with sourdough...",
      "sentiment": "positive",
      "confidence": 0.74
    }
  ],
  "chapters": [
    {
      "start": 0.0,
      "end": 45.3,
      "headline": "Why starter timing beats flour choice",
      "summary": "Letting the starter peak matters more than which flour you buy."
    }
  ]
}
```

**Rules:**
- All timestamps in seconds (float). AssemblyAI returns milliseconds (int): divide by 1000 at write time.
- Speaker labels preserved as-is from AssemblyAI (`"A"`, `"B"`, etc.).
- Word-level detail preserved for alignment with visual analysis and for precise search highlighting.
- Sentiment and chapters are timeline-bound: they reference specific moments, so they live here, not in tables.

### Step 3: Labels: Tags System

AssemblyAI returns topics (IAB taxonomy), entities (typed: place, person, organisation), and key phrases. These are filterable labels: they go in the `tags`/`video_tags` tables.

| AssemblyAI output | `tags.category` | `tags.name` | `video_tags.generator` | Example |
|---|---|---|---|---|
| Topic | `topic` | IAB taxonomy path | `assemblyai` | `"Technology & Computing > Software"` |
| Entity (location) | `entity:place` | Entity text | `assemblyai` | `"Central Park"` |
| Entity (person) | `entity:person` | Entity text | `assemblyai` | `"Marie Curie"` |
| Entity (organisation) | `entity:org` | Entity text | `assemblyai` | `"NASA"` |
| Key phrase | `key_phrase` | Phrase text | `assemblyai` | `"golden hour"` |

**Entity:place tags get special treatment.** Feature 18 (Location Recognition) and Epic 8 (Place Saving) need structured location data eventually (coordinates, Place IDs). For now, place entities are stored as tags like everything else, but with a distinct category (`entity:place`) so they can be promoted to structured locations later without re-processing all transcripts.

**Idempotent re-runs.** If the transcription pipeline is re-run for a video, all tags with `generator='assemblyai'` for that video are deleted before new ones are written. The `generator` column makes this safe: manual tags and tags from other sources are untouched.

### Step 4: Flat Fields

| Field | Content | Purpose |
|---|---|---|
| `transcript_text` | Plain text, no timestamps, no speaker labels | FTS5 search |
| `summary` | AssemblyAI auto-summary, plain text | Human-readable, quick reference, Claude input |
| `audio_type` | `'speech'` / `'music'` / `'voiceover_with_music'` / `'no_audio'` | Filtering, pipeline routing |
| `transcript_language` | ISO 639-1 code (`'en'`, `'fr'`) | Filtering |
| `transcript_source` | `'assemblyai'` | Provenance |
| `transcription_status` | `'done'` / `'failed'` / `'not_applicable'` | Pipeline tracking |
| `has_meaningful_audio` | Boolean | Filtering |
| `has_burned_captions` | Boolean (deferred, not tested in POC yet) | Filtering |

---

## Future Enrichment Layers (Same Pattern)

### Visual Analysis (Twelve Labs: Epic 4)

```
Raw response    → new field (e.g. `twelvelabs_raw_json`)
Timeline data   → `analysis_json` (canonical shape: scenes with timestamps in seconds)
Labels          → tags/video_tags with generator='twelve_labs', categories: mood, technique, colour, etc.
Plain text      → analysis descriptions feed into FTS5 (mechanism TBD)
Embeddings      → `video_embeddings` virtual table (sqlite-vec, 512-dim Marengo vectors)
```

### Audio Intelligence (Epic 3)

```
Raw response    → stored (field or file, TBD)
Timeline data   → `music_json` (song segments with timestamps, genre, mood, energy)
Labels          → tags/video_tags with generator from relevant tool
```

### Claude Insights (Epic 5)

```
No raw response: Claude IS the processor
Output          → `claude_insight` on videos table (video-level)
                → `insights` table (collection-level, cross-collection)
Labels          → tags/video_tags with generator='claude'
```

---

## Schema Changes Required

These must be applied before the enrichment pipeline writes any data.

### New fields on `videos`

```sql
ALTER TABLE videos ADD COLUMN assemblyai_raw_json TEXT;
ALTER TABLE videos ADD COLUMN summary TEXT;
```

### Constraint fixes

```sql
-- transcript_source: add 'assemblyai'
-- This requires recreating the table or using a workaround since SQLite
-- does not support ALTER COLUMN. Simplest: drop and recreate the CHECK
-- by recreating the column. Since no enrichment data has been written yet,
-- this is safe to do now via a migration script.
```

The `transcript_source` CHECK must include `'assemblyai'`. The `video_tags.source` CHECK must accommodate the new `generator` approach.

### New column on `video_tags`

```sql
ALTER TABLE video_tags ADD COLUMN generator TEXT;
```

This records which tool produced the tag (`'assemblyai'`, `'twelve_labs'`, `'claude'`, NULL for manual). Enables idempotent re-runs (delete all tags from a specific generator, regenerate) and UI filtering (show/hide auto-generated tags by source).

### FTS5 sync triggers

```sql
-- These are critical. Without them, text search silently returns nothing
-- for videos that have been enriched after the FTS table was created.

CREATE TRIGGER IF NOT EXISTS videos_fts_insert AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
    VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_update AFTER UPDATE OF transcript_text, notes, claude_insight ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
    VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
    INSERT INTO videos_fts(rowid, transcript_text, notes, claude_insight)
    VALUES (new.id, new.transcript_text, new.notes, new.claude_insight);
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_delete AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, transcript_text, notes, claude_insight)
    VALUES ('delete', old.id, old.transcript_text, old.notes, old.claude_insight);
END;
```

---

## What This Spec Does NOT Cover

- **The canonical JSON shape for `analysis_json`** (Twelve Labs): defined when Epic 4 POC is complete and we know what Pegasus returns.
- **The canonical JSON shape for `music_json`**: defined when Epic 3 POC is complete.
- **Structured location data** (coordinates, Place IDs): deferred to Epic 8. Place entities are stored as tags for now.
- **Structured annotation UX** (Feature 10): the exact form of "what dimension is interesting" is an open design question. The tags system can accommodate it.
- **The `video_embeddings` virtual table creation**: deferred until sqlite-vec is installed and embeddings are generated (Epic 4 V2).

---

## Relationship to Other Documents

- **`database-schema-design.md`**: the original schema. This document describes changes required to that schema. The schema spec should be updated when the changes are applied.
- **`epic2-transcription-approach.md`**: the transcription approach. This document defines how that approach's output is stored.

(These docs are excerpts from a larger private planning corpus; the strategic
vision and feature-discovery documents they were written alongside are not
part of this repo.)
