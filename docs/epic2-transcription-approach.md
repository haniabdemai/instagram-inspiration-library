# Epic 2: Transcription & Audio Classification: Approach

**Date:** 2026-03-19 (updated with POC results)
**Status:** POC complete. Approach validated. Ready for full pipeline build.
**Context:** Supersedes an earlier WhisperX-primary approach from the private planning corpus; this document records the *how* for transcription as actually shipped.

---

## What Changed and Why

The original plan positioned **WhisperX as the primary transcription tool** (free, local, open source) with **Deepgram as a fallback** for difficult audio. The reasoning was cost: WhisperX is free, Deepgram costs money.

During implementation planning, several practical problems emerged:

1. **Machine impact.** WhisperX runs on CPU (Apple Silicon MPS is not supported by its backend, ctranslate2). Processing 730 videos locally means hours of heavy CPU load: the machine becomes sluggish, fans spin, battery drains. This isn't background work like the download script (which is mostly idle between requests); transcription is continuous computation.

2. **Processing time.** Even with the `small` model (which is adequate for English), the full library would take 1.5–3 hours. With `large-v3` (needed for French accuracy), 12–36 hours. Adding speaker diarisation roughly doubles these times.

3. **Setup friction.** WhisperX's speaker diarisation depends on pyannote, which requires a Hugging Face account, accepting gated model agreements, and configuring an access token. Not expensive (it's free), but fiddly.

4. **The "free" comparison is misleading.** Cloud APIs cost $1.50–$3.40 for the entire library, and both Deepgram ($200 free credit) and AssemblyAI ($50 free credit) cover the project many times over on free tier alone. WhisperX's cost advantage disappears when the alternative is effectively also free.

---

## Approach: Cloud API with Local Audio Classification

### Transcription: AssemblyAI (primary), Deepgram (backup)

**AssemblyAI** is the primary choice for transcription and speaker diarisation. Reasons:

- **Diarisation quality on short audio.** Recent model update (2025): 64% fewer speaker counting errors, 19% improvement in speaker count prediction for audio under 2 minutes. This is directly relevant: most videos are 8–180 seconds.
- **French optimisation.** Universal-3-Pro model is specifically optimised for French (among other European languages). Pyannote's diarisation team is French (CNRS/INRIA), but AssemblyAI's diarisation is competitive and doesn't require separate setup.
- **Built-in features.** Speaker diarisation, word-level timestamps, per-word confidence scores, automatic language detection: all in a single API call. No need to chain together separate tools.
- **No machine impact.** Audio is sent to their servers; results come back. The local machine does nothing.

**Deepgram** remains available as a backup if AssemblyAI underperforms on specific content. $200 free credit with no expiry provides enormous headroom.

**WhisperX** is not ruled out: it remains installed (`.venv-ml/`) and could be useful for:
- Offline processing if needed
- A/B comparison on specific videos where API results seem wrong
- Any future scenario where sending audio to a cloud API is undesirable

### Cost

| Service | Rate (with diarisation) | Cost for 9 hours | Free credit | Runs covered by free tier |
|---|---|---|---|---|
| AssemblyAI | $0.17–$0.29/hr | $1.53–$2.61 | $50 | ~19x (worst case) to ~33x (best case) |
| Deepgram | ~$0.38/hr | ~$3.40 | $200 | ~58x |

Either service covers the entire library on free credits alone. A hybrid approach (e.g. AssemblyAI for most, Deepgram for specific re-runs) is possible but unnecessary for cost reasons: it would only make sense if one service proved better at specific content types.

### Audio Classification: Local pre-processing step

**No cloud transcription service offers built-in audio classification** (speech vs music vs silence). This was verified across Deepgram, AssemblyAI, Rev.ai, Google Cloud Speech, Amazon Transcribe, and Azure Speech Services. It's a gap across the entire market.

We handle this ourselves as a lightweight local step before sending audio to the transcription API:

1. **Silero VAD (Voice Activity Detection)**: tiny model, runs locally in milliseconds, detects whether and where human voice is present in audio. No machine impact.
2. **Energy analysis**: basic RMS energy check to detect silence vs audio present.
3. **Classification logic:**
   - Silent/near-silent audio → `no_audio`, skip transcription
   - High VAD speech ratio → `speech`, send to transcription API
   - Low VAD speech ratio but audio energy present → `music` (likely), send a sample to transcription API with low expectations
   - Mixed VAD (speech present but with gaps of non-speech audio) → `voiceover_with_music`, send to transcription API

This saves API calls (don't waste credits on music-only videos) and produces the audio classification that downstream pipelines need (Epic 3: Audio Intelligence depends on this routing).

**Important:** This classification is a heuristic, not ground truth. The POC will validate whether it's reliable enough, and edge cases (e.g. speech over loud music where VAD struggles) will be documented for refinement.

---

## What the POC Tests

Ten videos selected to cover edge cases. For each video, the POC evaluates:

| Dimension | What we're checking | Ground truth |
|---|---|---|
| **Audio classification** | Does the local classifier correctly identify speech / music / mixed / silence? | User watches the video |
| **Speaker diarisation** | How many speakers detected? Are segments attributed to the right person? | User watches the video |
| **Language detection** | Does it correctly identify English vs French? | User watches the video |
| **Transcription accuracy** | Are the actual words correct? | User watches the video |
| **Edge cases** | Very short clips, heavy background music, accents, overlapping audio | User watches the video |

The user will watch all 10 videos and compare against the automated results, marking each dimension as correct / wrong / partially correct.

### Test Videos

| Video | Duration | Expected challenge |
|---|---|---|
| video-01 | 180s | Long, likely French speech |
| video-02 | 23s | Short English speech |
| video-03 | 179s | French cultural piece |
| video-04 | 8s | Very short, likely music-only |
| video-05 | ? | Tutorial, possibly non-English |
| video-06 | 21s | Travel narration over music |
| video-07 | 34s | Music-focused |
| video-08 | 13s | Music content |
| video-09 | 81s | English speech, possibly with music |
| video-10 | 46s | Likely French speech |

Note: some of the "expected challenge" guesses may be wrong: the user noted this, and discovering where assumptions are wrong is itself valuable POC data.

### POC Outputs

- Per-video JSON with full detail (segments, word timestamps, confidence scores, speaker labels)
- Human-readable summary report for the user to review against their own viewing
- Assessment of audio classification accuracy
- Assessment of diarisation accuracy
- Recommendations for the full-library pipeline based on findings

---

## Full-Library Pipeline (Post-POC)

Once the POC validates the approach, the full pipeline for 730 videos will be:

```
For each video:
  1. Extract audio (ffmpeg, fast, local)
  2. Audio classification (Silero VAD + energy analysis, fast, local)
     → no_audio: mark in database, skip transcription
     → music: mark in database, route to Epic 3 (Audio Intelligence)
     → speech / voiceover_with_music: continue to step 3
  3. Send to AssemblyAI (speaker diarisation + language detection enabled)
  4. Store results in database:
     - audio_type
     - transcript_json (structured: segments, speakers, word timestamps, confidence)
     - transcript_text (plain text for FTS5 search)
     - transcript_source ('assemblyai')
     - transcript_language (detected language code)
     - has_meaningful_audio
     - transcription_status
```

Processing time for 730 videos: minutes (API processing) rather than hours (local CPU). No machine impact. Cost: $1.50–$2.61 against $50 free credit.

The pipeline must be:
- **Incremental**: process only videos not yet transcribed
- **Resumable**: pick up where it left off after interruption
- **Status-tracked**: `transcription_status` in database shows progress
- **Error-tolerant**: failed videos are logged and retried, not fatal

---

## Services Evaluated

Full comparison of six services was conducted. Summary:

| Service | Cost (9 hrs + diarisation) | Free tier | Diarisation | French | Short clip handling | Verdict |
|---|---|---|---|---|---|---|
| **AssemblyAI** | $1.53–$2.61 | $50 | Best (recent upgrade for short audio) | Optimised (Universal-3-Pro) | Specifically improved | **Primary choice** |
| **Deepgram** | ~$3.40 | $200 | Works, some community-reported issues | Supported | Good | **Backup** |
| Rev.ai | $1.62–$2.70 | 5 hrs | Good, but weaker on short utterances | Supported | Caveated | Not chosen |
| Google Cloud | ~$8.64+ | 60 min/month | Available (Chirp 3 batch only) | Supported | Good | Too expensive |
| Amazon Transcribe | ~$12.96 | 60 min/month | Included | Supported | Good | Too expensive |
| Azure Speech | ~$3.24 | 5 hrs/month | Included in batch | Supported | Good | Mid-tier, no advantage |

**Key gap across all services:** None offer built-in audio classification (speech vs music vs silence). This must be handled locally as a pre-processing step.

---

## POC Results (2026-03-19)

14 videos tested (10 original edge-case set + 4 multi-speaker). User watched all videos and validated results. (The raw POC result files live in the private planning corpus, not this repo.)

### What Works

- **Transcription accuracy:** Excellent for genuine speech in both English and French. Every video with clear speech got an accurate transcript.
- **Language detection:** Reliable. French correctly identified at 99%+ confidence in all French videos.
- **English diarisation:** Correctly identified 2 speakers and attributed text in English multi-speaker videos.
- **Audio classification (Silero VAD):** Correctly identified music-only videos and skipped them. Correctly identified speech-dominant videos.
- **Short clips:** Works well even on 8–15 second videos.

### Known Limitations (Accepted)

1. **French speaker diarisation is imperfect.** Both AssemblyAI and Deepgram were tested on a French video with two clearly distinct voices (man and woman). Both got the words mostly right but confused who said what. This is a hard problem across all services, not vendor-specific. **Decision: accept this. The words are right, which is what matters for search and downstream analysis. Speaker attribution in French is a nice-to-have, not critical.**

2. **Singing/rapping is detected as speech by the VAD.** The VAD detects human voice, not speech specifically: it can't distinguish talking from singing. A rap track registered as 96% speech. **Decision: use word confidence as a secondary check. Voice present + word confidence under ~25-30% = likely singing, flag as music rather than speech. The user noted the ideal behaviour is flagging "this is a song/artist singing" rather than transcribing it as speech.**

3. **False positive speech detection in pure music.** The VAD occasionally detects speech where there is none (~13% speech ratio on a pure music track). However, AssemblyAI self-corrects by returning zero speakers and no transcript. **Decision: harmless: wastes an API call but produces the correct result. The two-stage pipeline catches its own mistakes.**

4. **"voiceover_with_music" label is imprecise.** Covers both "narration over background music" and "brief on-camera speech in mostly non-verbal video." These are functionally different but the label works for pipeline routing purposes. **Decision: keep the label. Refine display names in the UI if needed later.**

5. **Theatre/gibberish/non-language audio.** The system fails completely on non-language vocalisation (theatre performance with gibberish singing). Low word confidence (<12%) is a reliable signal. **Decision: expected failure. Low-confidence transcripts will be flagged for review.**

### Confidence Scores as Signal

Clear pattern across all 14 videos:
- **>40% avg word confidence:** genuine, accurate speech transcription
- **25-40%:** speech present but noisy or accented: transcript mostly accurate
- **<25%:** likely not speech (singing, gibberish, music misidentified): transcript unreliable

This threshold can be used in the pipeline to flag questionable results.

### Deepgram Comparison

Deepgram (Nova-3) was tested on the French multi-speaker video for comparison. Results were comparable to AssemblyAI: words mostly right, speaker attribution similarly imperfect. No clear winner for French diarisation. **Decision: stick with AssemblyAI as primary. Deepgram remains available ($200 credit) if needed.**

### AssemblyAI Audio Intelligence Features

AssemblyAI offers additional features beyond transcription, all operating on the transcript text (not raw audio):
- **Topic Detection** (IAB taxonomy): useful for auto-tagging
- **Entity Detection** (people, places, organisations): useful for location recognition
- **Key Phrases**: useful for search/tagging
- **Auto Chapters**: useful for longer videos
- **Summarisation**: useful for insights

These are all included in the API call at no extra cost. **Enable all of them in the full pipeline**: capture everything AssemblyAI can give us. Even if some features turn out less useful, there's no reason not to store the data when it's free. Sentiment analysis and content moderation are also available and should be captured.

No service offers audio-level queries (e.g. "is there music at timestamp X?" or "what instrument is playing?"). That remains in Epic 3 (Audio Intelligence) scope.

**ElevenLabs (noted for future reference):** ElevenLabs' Scribe v2 transcription API has an `tag_audio_events` feature that tags non-speech sounds (laughter, applause, footsteps) inline with the transcript. This is not available in AssemblyAI. Not a must-have for the current pipeline, but worth revisiting when building richer audio understanding: especially for videos where audience reactions, laughter, or ambient sounds carry meaning. Pricing is comparable (~$0.30-0.40/hr). Full research in session notes, 2026-03-19.

---

## Relationship to Other Documents

- **`database-schema-design.md`**: all fields referenced here (`audio_type`, `transcript_json`, etc.) already exist in the schema.
- The strategic vision and feature-discovery documents this approach was
  written against are part of a private planning corpus and not in this repo.
