# instagram-inspiration-library

**Your Instagram saved posts are a write-only archive: hundreds of ideas you
filed away and can never find again.** This pipeline turns your official
Instagram data export into a local, searchable, tagged library: every saved
video downloaded, transcribed, auto-tagged, and full-text searchable in a
single SQLite file on your machine. The library, your videos, and your login
session all stay local; the one exception is transcription, where clips
classified as speech have their extracted audio (never the video) sent to
AssemblyAI. Use `--classify-only` if you want to stay fully offline.

```
 Instagram data export (official Meta download)
        │
        ▼
 parse_export.py ─────── saved posts + collections → SQLite
        │
        ▼
 download_videos.py ──── scheduled, rate-capped, human-paced browser
        │                sessions fetch each saved video
        ▼
 transcribe.py ───────── Silero VAD classifies speech vs music locally,
        │                then AssemblyAI transcribes only what has speech;
        │                topics / entities / key phrases become tags
        ▼
 SQLite + FTS5 ───────── one portable file: transcripts, tags, notes,
                         full-text search with sync triggers
```

## The engineering

**The pipeline is incremental by construction.** Every stage writes a status
column (`pending` / `done` / `failed`) and only ever pulls its own pending
work, so any stage can be re-run, interrupted, or scheduled without
re-processing a single item. Tag generation is idempotent per generator:
re-running enrichment replaces its own output and touches nothing else.

**Classification is free; transcription costs money, so classify first.**
Roughly a third of saved reels are music with no speech. Silero VAD runs
locally in milliseconds and gates every clip (speech / voiceover-with-music /
music / no audio) before a single API call is made; music-only clips are
skipped with a proper status rather than paid for. A word-confidence check
flags the remaining singing-classified-as-speech cases in the run output.
Only the extracted 16kHz mono audio is uploaded for transcription, so the
visual content never leaves your machine. AssemblyAI's data-retention terms
apply to what you send; read them before transcribing anything sensitive.

**Downloads behave like a person, not a crawler.** A real Chrome instance
with a persistent logged-in session, stealth patches, randomised viewport,
Gaussian-distributed delays, simulated mouse movement and scroll, warm-up
browsing, batch pauses, and daily (65–95) plus rolling-weekly caps. An
adaptive session engine records every run in SQLite and varies the next run's
batch size, pacing, and order based on history, so no two sessions look
alike. Scheduling is handled by self-cleaning, date-sorted launchd agents
(macOS) that skip when the network blocks Instagram and catch up safely
later.

**One SQLite file is the whole backend.** Schema in `schema.py`: videos,
collections, tags, per-generator tag provenance, session history, and an
external-content FTS5 index over transcripts/notes/insights kept in sync by
triggers. Vector search (sqlite-vec, 512-dim embeddings) is designed in
[docs/database-schema-design.md](docs/database-schema-design.md) but not yet
shipped: search today is FTS5 keyword search.

## Getting started

Prerequisites: Python 3.11+, ffmpeg on PATH, and your
[Instagram data export](https://accountscenter.instagram.com/info_and_permissions/dyi/)
(JSON format: request it from Meta, arrives within a day or two).

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python parse_export.py path/to/export/your_instagram_activity/saved/
.venv/bin/python download_videos.py --limit 20                 # first session
```

`parse_export.py` wants the folder that directly contains
`saved_collections.json` and `saved_posts.json`; in a standard Meta export
that is `your_instagram_activity/saved/`.

For transcription (separate venv keeps torch out of the download env):

```bash
python -m venv .venv-ml && .venv-ml/bin/pip install -r requirements-ml.txt
export ASSEMBLYAI_API_KEY=your-key
.venv-ml/bin/python transcribe.py                # classify + transcribe + tag
.venv-ml/bin/python transcribe.py --classify-only   # free: VAD only, no API
```

The first download session opens a browser window for you to log in;
the session persists after that. Transcription downloads the Silero VAD
model on first run (needs network once).

**Be clear-eyed about Instagram's terms before you use this.** It only
touches *your own saved posts*, via the official export plus your own
logged-in session, for personal archival. But automated access of any kind
is against Instagram's Terms of Use, and accounts have been restricted or
banned for it; the human pacing and caps reduce the risk, they do not
remove it. Use a secondary account and keep the caps conservative.

## Configuration

| What | How |
|---|---|
| Videos folder | `INSPO_VIDEOS_DIR` (default `./videos`) |
| Browser locale/timezone | `INSPO_LOCALE`, `INSPO_TIMEZONE`: match your account's usual fingerprint |
| Transcription key | `ASSEMBLYAI_API_KEY` or `--api-key` |
| Scheduled sessions (macOS) | `scheduled_download.sh`: caps and pacing at the top of the file |
| Push notifications | optional `NTFY_TOPIC` (ntfy.sh) for session results on your phone; topics are unauthenticated, so use a long random name |

## Scheduling sessions (macOS)

`scheduled_download.sh` is driven by one launchd agent per session:
copy `com.inspo-library.dl-example.plist` into `~/Library/LaunchAgents/`
per the instructions inside it (name each agent `dl-<MMDD><letter>` so
alphabetical order is chronological), and each agent self-removes after it
fires. Scheduling and `--use-chrome` are macOS-only; the core
parse/download/transcribe pipeline is cross-platform.

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/pytest
```

The suite covers export parsing, schema/FTS5 triggers, and helpers against
synthetic fixtures: no real data, no network.

## Status

Working pipeline, personal production use since March 2026 (~700-video
library; the VAD gate classified ~40% of clips as music and skipped them
before any API spend). Shipped: parse → download → transcribe → tag → FTS5.
Designed, not yet shipped: vector/hybrid search and a browsing UI; design
docs in [docs/](docs/).

## Why not just yt-dlp?

Fetchers like `yt-dlp` or `instaloader` get you files. This builds a
*library*: it starts from the official export (so it knows your collections
and everything you ever saved, not just what a scraper can see), keeps
every stage incremental in one SQLite file, and gates paid transcription
behind a free local classifier so you only pay for clips that actually
contain speech. The result is searchable and tagged, not a folder of mp4s.

## Built with

[Silero VAD](https://github.com/snakers4/silero-vad) (local speech
detection) · [AssemblyAI](https://www.assemblyai.com/) (transcription) ·
[Playwright](https://playwright.dev/python/) +
[playwright-stealth](https://github.com/AtuboDad/playwright_stealth) ·
[SQLite FTS5](https://www.sqlite.org/fts5.html) · ffmpeg · launchd. The
models and browser tooling are their authors' work; this repo is the
pipeline that makes them one library.

## Licence

[MIT](LICENSE)
