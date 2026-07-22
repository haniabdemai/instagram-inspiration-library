"""
Epic 2 V2: Full Transcription Pipeline
=======================================
Processes all downloaded videos through the validated two-stage pipeline:
  Stage 1 (local): Silero VAD + energy analysis → audio classification
  Stage 2 (cloud): AssemblyAI → transcription + all enrichment features

Stores results per data-storage-approach.md:
  - Raw API response → assemblyai_raw_json
  - Canonical JSON → transcript_json
  - Plain text → transcript_text
  - Summary → summary
  - Tags (topics, entities, key phrases) → tags / video_tags
  - Flat fields → audio_type, transcript_language, etc.

Incremental: only processes videos with transcription_status='pending'.
Resumable: pick up where you left off after interruption.
Error-tolerant: failed videos are logged, not fatal.

Usage:
  .venv-ml/bin/python transcribe.py --api-key YOUR_KEY
  .venv-ml/bin/python transcribe.py --status
  .venv-ml/bin/python transcribe.py --limit 20
  .venv-ml/bin/python transcribe.py --classify-only  # skip API calls
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import assemblyai as aai
import torch

# ─── Configuration ───────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent
DB_PATH = PROJECT_ROOT / "library.db"
VIDEOS_DIR = Path(os.environ.get("INSPO_VIDEOS_DIR", PROJECT_ROOT / "videos"))

# Confidence threshold: below this, the transcript is flagged as unreliable.
# Based on POC findings: <25% = likely singing/gibberish, not speech.
LOW_CONFIDENCE_THRESHOLD = 0.25


# ─── Database ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_pending_videos(conn, limit=None):
    """Get downloaded videos that haven't been transcribed yet."""
    sql = """
        SELECT v.id, v.shortcode, v.local_path, v.username,
               GROUP_CONCAT(c.name, ', ') as collections
        FROM videos v
        LEFT JOIN video_collections vc ON v.id = vc.video_id
        LEFT JOIN collections c ON vc.collection_id = c.id
        WHERE v.download_status = 'downloaded'
          AND v.transcription_status = 'pending'
        GROUP BY v.id
        ORDER BY v.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def get_status(conn):
    """Print transcription pipeline status."""
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    downloaded = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE download_status = 'downloaded'"
    ).fetchone()[0]

    statuses = conn.execute("""
        SELECT transcription_status, COUNT(*) as cnt
        FROM videos
        WHERE download_status = 'downloaded'
        GROUP BY transcription_status
    """).fetchall()
    status_map = {r["transcription_status"]: r["cnt"] for r in statuses}

    audio_types = conn.execute("""
        SELECT audio_type, COUNT(*) as cnt
        FROM videos
        WHERE audio_type IS NOT NULL
        GROUP BY audio_type
    """).fetchall()

    tags_count = conn.execute(
        "SELECT COUNT(*) FROM video_tags WHERE generator = 'assemblyai'"
    ).fetchone()[0]

    print("=" * 55)
    print("  Transcription Pipeline Status")
    print("=" * 55)
    print()
    print(f"  Total videos:     {total}")
    print(f"  Downloaded:       {downloaded}")
    print()
    print(f"  Transcription status (of downloaded):")
    print(f"    Pending:        {status_map.get('pending', 0)}")
    print(f"    Done:           {status_map.get('done', 0)}")
    print(f"    Failed:         {status_map.get('failed', 0)}")
    print(f"    Not applicable: {status_map.get('not_applicable', 0)}")
    print()
    if audio_types:
        print(f"  Audio classification:")
        for r in audio_types:
            print(f"    {r['audio_type']:<24} {r['cnt']}")
        print()
    print(f"  Auto-generated tags:  {tags_count}")
    print()
    print("=" * 55)


# ─── Stage 1: Local Audio Classification ─────────────────────────

def load_vad_model():
    """Load Silero VAD model (tiny, runs in milliseconds).

    Pinned to a tagged release: torch.hub executes code from the fetched
    repo, so tracking the default branch would mean running whatever it
    points at tomorrow. First run downloads the model (needs network).
    """
    model, utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad:v5.1",
        model="silero_vad",
        trust_repo=True,
    )
    get_speech_timestamps = utils[0]
    read_audio = utils[2]
    return model, get_speech_timestamps, read_audio


def extract_audio(video_path, tmp_dir):
    """Extract audio from video as 16kHz mono WAV."""
    audio_path = Path(tmp_dir) / f"{video_path.stem}.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-ar", "16000", "-ac", "1", "-f", "wav",
            str(audio_path),
        ],
        capture_output=True,
    )
    return audio_path


def classify_audio(audio_path, vad_model, get_speech_timestamps, read_audio):
    """Classify audio type using Silero VAD + energy analysis."""
    sample_rate = 16000
    waveform = read_audio(str(audio_path), sampling_rate=sample_rate)
    duration = len(waveform) / sample_rate

    rms = torch.sqrt(torch.mean(waveform**2)).item()
    if rms < 0.001:
        return {
            "audio_type": "no_audio",
            "duration_seconds": round(duration, 1),
            "rms_energy": round(rms, 6),
            "speech_ratio": 0.0,
            "speech_seconds": 0.0,
            "has_meaningful_audio": False,
        }

    speech_timestamps = get_speech_timestamps(
        waveform,
        vad_model,
        sampling_rate=sample_rate,
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=100,
    )

    speech_duration = sum(
        (ts["end"] - ts["start"]) / sample_rate for ts in speech_timestamps
    )
    speech_ratio = speech_duration / duration if duration > 0 else 0

    if speech_ratio > 0.4:
        audio_type = "speech"
    elif speech_ratio > 0.05:
        audio_type = "voiceover_with_music"
    else:
        audio_type = "music"

    return {
        "audio_type": audio_type,
        "duration_seconds": round(duration, 1),
        "rms_energy": round(rms, 6),
        "speech_ratio": round(speech_ratio, 3),
        "speech_seconds": round(speech_duration, 1),
        "has_meaningful_audio": True,
    }


# ─── Stage 2: AssemblyAI Transcription ──────────────────────────

def transcribe_with_assemblyai(audio_path):
    """Send the extracted audio to AssemblyAI with all enrichment features.

    Deliberately uploads the 16kHz mono WAV, never the video file: the
    visual content stays on your machine and strictly less data goes to
    the API for the same transcript.

    Returns the full Transcript object (for raw JSON access) and status.
    """
    config = aai.TranscriptionConfig(
        speech_models=["universal-3-pro"],
        speaker_labels=True,
        language_detection=True,
        punctuate=True,
        format_text=True,
        # Enrichment features (all free, included in API call)
        # Note: auto_chapters and summarization are mutually exclusive.
        # We use auto_chapters because it gives timeline-bound data AND
        # per-chapter summaries. The flat summary field is derived from
        # chapter summaries.
        auto_chapters=True,
        entity_detection=True,
        iab_categories=True,
        sentiment_analysis=True,
        # auto_highlights disabled: SDK 0.58.0 has a parsing bug where
        # some highlight results lack timestamps, causing validation errors.
        # Key phrases are still captured via IAB topics and entities.
        content_safety=True,
    )

    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(str(audio_path), config=config)
    return transcript


def normalise_transcript(transcript):
    """Convert AssemblyAI response to canonical JSON shape.

    Follows the schema in data-storage-approach.md:
    - All timestamps in seconds (float), converted from ms
    - Speaker labels preserved as-is
    - Word-level detail preserved
    - Sentiment and chapters included (timeline-bound)
    """
    canonical = {"utterances": [], "sentiment": [], "chapters": []}

    # Utterances with word-level detail
    if transcript.utterances:
        for utt in transcript.utterances:
            words = []
            if utt.words:
                for w in utt.words:
                    words.append({
                        "word": w.text,
                        "start": round(w.start / 1000, 2),
                        "end": round(w.end / 1000, 2),
                        "confidence": round(w.confidence, 3),
                        "speaker": w.speaker,
                    })

            canonical["utterances"].append({
                "start": round(utt.start / 1000, 2),
                "end": round(utt.end / 1000, 2),
                "speaker": utt.speaker,
                "text": utt.text,
                "confidence": round(utt.confidence, 3),
                "words": words,
            })

    # Sentiment analysis (timeline-bound)
    if transcript.sentiment_analysis:
        for s in transcript.sentiment_analysis:
            canonical["sentiment"].append({
                "start": round(s.start / 1000, 2),
                "end": round(s.end / 1000, 2),
                "text": s.text,
                "sentiment": s.sentiment.value,
                "confidence": round(s.confidence, 3),
            })

    # Auto chapters (timeline-bound)
    if transcript.chapters:
        for ch in transcript.chapters:
            canonical["chapters"].append({
                "start": round(ch.start / 1000, 2),
                "end": round(ch.end / 1000, 2),
                "headline": ch.headline,
                "summary": ch.summary,
            })

    return canonical


def extract_and_store_tags(conn, video_id, transcript):
    """Extract topics, entities, and key phrases from AssemblyAI response.

    Stores them in tags/video_tags tables with generator='assemblyai'.
    Idempotent: deletes existing assemblyai tags for this video first.
    """
    # Clear previous assemblyai tags for this video
    conn.execute(
        "DELETE FROM video_tags WHERE video_id = ? AND generator = 'assemblyai'",
        (video_id,),
    )

    tags_to_insert = []

    # IAB topics
    if transcript.iab_categories and transcript.iab_categories.results:
        seen_topics = set()
        for result in transcript.iab_categories.results:
            if result.labels:
                for label in result.labels:
                    topic = label.label
                    if topic not in seen_topics:
                        seen_topics.add(topic)
                        tags_to_insert.append(
                            ("topic", topic, label.relevance)
                        )

    # Entities
    if transcript.entities:
        seen_entities = set()
        for entity in transcript.entities:
            entity_type = entity.entity_type.value if hasattr(entity.entity_type, 'value') else str(entity.entity_type)
            # Map to our category naming
            if "location" in entity_type.lower() or "place" in entity_type.lower():
                category = "entity:place"
            elif "person" in entity_type.lower():
                category = "entity:person"
            elif "organization" in entity_type.lower() or "org" in entity_type.lower():
                category = "entity:org"
            else:
                category = f"entity:{entity_type.lower()}"

            key = (category, entity.text)
            if key not in seen_entities:
                seen_entities.add(key)
                tags_to_insert.append((category, entity.text, None))

    # Insert tags
    for category, name, confidence in tags_to_insert:
        # Get or create tag
        existing = conn.execute(
            "SELECT id FROM tags WHERE name = ? AND category = ?",
            (name, category),
        ).fetchone()

        if existing:
            tag_id = existing[0]
        else:
            cursor = conn.execute(
                "INSERT INTO tags (name, category) VALUES (?, ?)",
                (name, category),
            )
            tag_id = cursor.lastrowid

        # Link to video
        conn.execute(
            """INSERT OR REPLACE INTO video_tags
               (video_id, tag_id, source, confidence, generator)
               VALUES (?, ?, 'auto', ?, 'assemblyai')""",
            (video_id, tag_id, confidence),
        )

    return len(tags_to_insert)


def get_avg_word_confidence(transcript):
    """Calculate average word confidence from transcript."""
    if not transcript.words:
        return 0.0
    confidences = [w.confidence for w in transcript.words]
    return sum(confidences) / len(confidences) if confidences else 0.0


# ─── Pipeline ────────────────────────────────────────────────────

def store_classification_only(conn, video_id, classification,
                               keep_pending=False):
    """Store audio classification for videos that won't be transcribed.

    If keep_pending=True, leaves transcription_status as 'pending' so the
    video will be picked up for transcription in a future run. Used in
    --classify-only mode for speech videos.
    """
    audio_type = classification["audio_type"]
    transcription_status = "pending" if keep_pending else "not_applicable"

    conn.execute(
        """UPDATE videos SET
            audio_type = ?,
            has_meaningful_audio = ?,
            duration = ?,
            transcription_status = ?
        WHERE id = ?""",
        (
            audio_type,
            classification["has_meaningful_audio"],
            classification["duration_seconds"],
            transcription_status,
            video_id,
        ),
    )


def store_transcription_results(conn, video_id, classification, transcript):
    """Store full transcription results in the database."""
    canonical = normalise_transcript(transcript)
    avg_confidence = get_avg_word_confidence(transcript)

    # Determine audio_type: override VAD if AssemblyAI found no speech
    audio_type = classification["audio_type"]
    if not transcript.words or len(transcript.words) == 0:
        # VAD said speech but AssemblyAI found nothing: likely music
        audio_type = "music"

    # Low confidence flag
    low_confidence = avg_confidence < LOW_CONFIDENCE_THRESHOLD and avg_confidence > 0

    # Derive summary from auto_chapters (since summarization can't run alongside)
    summary_text = None
    if transcript.chapters:
        summary_parts = [ch.summary for ch in transcript.chapters if ch.summary]
        if summary_parts:
            summary_text = " ".join(summary_parts)

    # Language
    language = transcript.json_response.get("language_code", None)

    # Raw API response (verbatim, for reprocessing)
    raw_json = json.dumps(transcript.json_response, ensure_ascii=False)

    conn.execute(
        """UPDATE videos SET
            audio_type = ?,
            has_meaningful_audio = ?,
            duration = ?,
            transcript_json = ?,
            transcript_text = ?,
            transcript_source = 'assemblyai',
            transcript_language = ?,
            transcription_status = 'done',
            assemblyai_raw_json = ?,
            summary = ?
        WHERE id = ?""",
        (
            audio_type,
            classification["has_meaningful_audio"],
            classification["duration_seconds"],
            json.dumps(canonical, ensure_ascii=False),
            transcript.text or "",
            language,
            raw_json,
            summary_text,
            video_id,
        ),
    )

    # Extract and store tags
    tag_count = extract_and_store_tags(conn, video_id, transcript)

    return {
        "audio_type": audio_type,
        "language": language,
        "speakers": len(set(
            u.speaker for u in (transcript.utterances or [])
        )),
        "avg_confidence": round(avg_confidence, 3),
        "low_confidence": low_confidence,
        "tags_created": tag_count,
        "has_summary": summary_text is not None,
        "chapters": len(canonical["chapters"]),
    }


def process_video(video_row, vad_model, get_speech_timestamps, read_audio,
                   tmp_dir, classify_only=False):
    """Process a single video through the full pipeline.

    Returns a result dict with status information.
    """
    video_id = video_row["id"]
    shortcode = video_row["shortcode"]
    video_path = VIDEOS_DIR / f"{shortcode}.mp4"

    if not video_path.exists():
        return {"status": "error", "error": "file not found", "shortcode": shortcode}

    # Stage 1: Audio classification
    audio_path = extract_audio(video_path, tmp_dir)
    classification = classify_audio(
        audio_path, vad_model, get_speech_timestamps, read_audio
    )

    conn = get_db()
    should_transcribe = classification["audio_type"] in ("speech", "voiceover_with_music")

    if not should_transcribe:
        audio_path.unlink(missing_ok=True)
        store_classification_only(conn, video_id, classification)
        conn.commit()
        conn.close()
        return {
            "status": "classified",
            "shortcode": shortcode,
            "audio_type": classification["audio_type"],
            "duration": classification["duration_seconds"],
        }

    if classify_only:
        # Speech videos stay pending so they're picked up for transcription later
        audio_path.unlink(missing_ok=True)
        store_classification_only(conn, video_id, classification, keep_pending=True)
        conn.commit()
        conn.close()
        return {
            "status": "classified_only",
            "shortcode": shortcode,
            "audio_type": classification["audio_type"],
        }

    # Stage 2: AssemblyAI transcription (uploads the extracted audio only)
    try:
        transcript = transcribe_with_assemblyai(audio_path)
    except Exception as e:
        conn.execute(
            "UPDATE videos SET transcription_status = 'failed' WHERE id = ?",
            (video_id,),
        )
        conn.commit()
        conn.close()
        return {"status": "error", "shortcode": shortcode, "error": str(e)}
    finally:
        audio_path.unlink(missing_ok=True)

    if transcript.status == aai.TranscriptStatus.error:
        conn.execute(
            "UPDATE videos SET transcription_status = 'failed' WHERE id = ?",
            (video_id,),
        )
        conn.commit()
        conn.close()
        return {
            "status": "error",
            "shortcode": shortcode,
            "error": transcript.error,
        }

    # Store everything
    result_info = store_transcription_results(
        conn, video_id, classification, transcript
    )
    conn.commit()
    conn.close()

    return {
        "status": "transcribed",
        "shortcode": shortcode,
        **result_info,
    }


# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Epic 2 V2: Full Transcription Pipeline"
    )
    parser.add_argument(
        "--api-key", type=str,
        help="AssemblyAI API key (prefer the ASSEMBLYAI_API_KEY env var: "
             "CLI arguments are visible in shell history and ps output)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show pipeline status and exit",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N videos",
    )
    parser.add_argument(
        "--classify-only", action="store_true",
        help="Only run audio classification, skip API calls",
    )
    args = parser.parse_args()

    conn = get_db()

    if args.status:
        get_status(conn)
        conn.close()
        return

    # Resolve API key
    api_key = args.api_key or os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key and not args.classify_only:
        print("ERROR: AssemblyAI API key required.")
        print("  Pass --api-key KEY or set ASSEMBLYAI_API_KEY env var")
        print("  Or use --classify-only to test audio classification only")
        sys.exit(1)

    if api_key:
        aai.settings.api_key = api_key

    # Get pending videos
    pending = get_pending_videos(conn, limit=args.limit)
    conn.close()

    if not pending:
        print("No pending videos to process.")
        print("Run --status to see current state.")
        return

    print(f"Processing {len(pending)} videos...")
    if args.classify_only:
        print("(classify-only mode: no API calls)")
    print()

    # Load VAD model
    print("Loading Silero VAD model...")
    vad_model, get_speech_timestamps, read_audio = load_vad_model()

    # Process videos
    stats = {"transcribed": 0, "classified": 0, "errors": 0, "tags": 0}
    start_time = time.time()

    with tempfile.TemporaryDirectory(prefix="inspo-audio-") as tmp_dir:
        for i, video in enumerate(pending):
            shortcode = video["shortcode"]
            username = video["username"]
            collections = video["collections"] or ""

            progress = f"[{i + 1}/{len(pending)}]"
            print(f"{progress} {shortcode} (@{username})", end="")

            if collections:
                # Truncate long collection lists
                coll_display = collections[:40] + "..." if len(collections) > 40 else collections
                print(f" [{coll_display}]", end="")

            try:
                result = process_video(
                    video, vad_model, get_speech_timestamps, read_audio,
                    tmp_dir, classify_only=args.classify_only,
                )
            except Exception as e:
                print(f" ERROR: {e}")
                stats["errors"] += 1
                # Mark as failed so we don't retry endlessly
                err_conn = get_db()
                err_conn.execute(
                    "UPDATE videos SET transcription_status = 'failed' WHERE id = ?",
                    (video["id"],),
                )
                err_conn.commit()
                err_conn.close()
                continue

            status = result["status"]
            if status == "transcribed":
                stats["transcribed"] += 1
                stats["tags"] += result.get("tags_created", 0)
                conf_str = f"{result['avg_confidence']:.0%}"
                flag = " [LOW CONF]" if result.get("low_confidence") else ""
                print(
                    f" -> {result['audio_type']} | "
                    f"{result['language']} | "
                    f"{result['speakers']}spk | "
                    f"{conf_str}{flag} | "
                    f"{result['tags_created']} tags"
                )
            elif status in ("classified", "classified_only"):
                stats["classified"] += 1
                print(f" -> {result['audio_type']} (skip transcription)")
            elif status == "error":
                stats["errors"] += 1
                print(f" ERROR: {result.get('error', 'unknown')}")

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print()
    print("=" * 55)
    print(f"  Pipeline complete ({minutes}m {seconds}s)")
    print("=" * 55)
    print(f"  Transcribed:      {stats['transcribed']}")
    print(f"  Classified only:  {stats['classified']}")
    print(f"  Errors:           {stats['errors']}")
    print(f"  Tags created:     {stats['tags']}")
    print("=" * 55)

    # Show updated status
    print()
    conn = get_db()
    get_status(conn)
    conn.close()


if __name__ == "__main__":
    main()
