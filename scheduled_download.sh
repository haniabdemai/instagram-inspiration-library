#!/bin/bash
# Scheduled Instagram download session
# - Checks if Instagram is reachable (e.g. blocked by a distraction blocker), retries after 30 min if not
# - Daily cap varies 65-95 (avg ~80), weekly cap 350 (rolling 7-day)
# - Adjusts batch size automatically based on what's already been done
# - If a session is missed/skipped, the next one catches up within safe limits
# - Plists use date-sorted names (dl-0323a) so alphabetical = chronological

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv/bin/python"
SCRIPT="$PROJECT_DIR/download_videos.py"
LOG="$PROJECT_DIR/download_log.txt"
DEFAULT_LIMIT=${1:-60}
LABEL="${2:-}"
# Optional: set NTFY_TOPIC (ntfy.sh) to get session results pushed to your phone.
# ntfy topics are unauthenticated: pick a long random topic name and treat it
# like a password, since anyone who knows it can read or send messages.
NTFY_TOPIC="${NTFY_TOPIC:-}"
PLIST_GLOB="$HOME/Library/LaunchAgents/com.inspo-library.dl-"

# Send push notification to phone
push_notify() {
    [ -z "$NTFY_TOPIC" ] && return 0
    curl -s -o /dev/null -H "Title: $1" -d "$2" "https://ntfy.sh/$NTFY_TOPIC" 2>/dev/null
}

# Validate that a value is a non-negative integer. Returns 1 if not.
is_int() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

# Crash handler: if something unexpected happens, say so honestly
crash_notify() {
    local MSG="$1"
    echo "ERROR: $MSG" >> "$LOG"
    echo "========================================" >> "$LOG"
    push_notify "Instagram Downloads ERROR" "$MSG Check download_log.txt and scheduled_download_stderr.log."
    self_clean
    exit 1
}

cd "$PROJECT_DIR"

# Self-cleanup function: removes this agent's plist so it never fires again
self_clean() {
    if [ -n "$LABEL" ]; then
        PLIST_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
        if [ -f "$PLIST_FILE" ]; then
            rm -f "$PLIST_FILE"
            echo "Self-cleaned: removed $PLIST_FILE" >> "$LOG"
        fi
    fi
}

# Find next scheduled session (skips current agent's plist).
# With date-sorted names (dl-0323a, dl-0323b, dl-0324a...) the first
# non-self match is chronologically next.
find_next_session() {
    for f in "${PLIST_GLOB}"*.plist; do
        [ -f "$f" ] || continue
        BASENAME=$(basename "$f" .plist)
        [ "$BASENAME" = "$LABEL" ] && continue
        local ND=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Day" "$f" 2>/dev/null)
        local NM=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Month" "$f" 2>/dev/null)
        local NH=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Hour" "$f" 2>/dev/null)
        local NI=$(/usr/libexec/PlistBuddy -c "Print :StartCalendarInterval:Minute" "$f" 2>/dev/null)
        if [ -n "$ND" ]; then
            local MONTHS=(January February March April May June July August September October November December)
            local MONTH_NAME="${MONTHS[$(( ${NM:-$(date +%-m)} - 1 ))]}"
            echo "$MONTH_NAME $ND at $(printf '%02d:%02d' "$NH" "$NI")"
            return
        fi
    done
}

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "Scheduled session starting at $(date)" >> "$LOG"
echo "Requested limit: $DEFAULT_LIMIT" >> "$LOG"

# Check if Instagram is reachable
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://www.instagram.com/" 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    echo "Instagram not reachable (HTTP $HTTP_CODE): blocker or no connection." >> "$LOG"
    echo "Retrying in 30 minutes..." >> "$LOG"
    osascript -e 'display notification "Instagram unreachable, retrying in 30 min" with title "Instagram Downloader"' 2>/dev/null
    sleep 1800

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://www.instagram.com/" 2>/dev/null)
    if [ "$HTTP_CODE" != "200" ]; then
        echo "Still unreachable after retry. Session skipped: next session will catch up." >> "$LOG"
        osascript -e 'display notification "Still unreachable: session skipped, will catch up next time" with title "Instagram Downloader"' 2>/dev/null
        NEXT=$(find_next_session)
        if [ -n "$NEXT" ]; then
            push_notify "Instagram Downloads" "Session skipped, Instagram unreachable. Next session: $NEXT (will catch up)."
        else
            push_notify "Instagram Downloads" "Session skipped, Instagram unreachable. No more sessions scheduled."
        fi
        self_clean
        exit 0
    fi
    echo "Instagram reachable now. Continuing." >> "$LOG"
fi

# Get current stats via JSON (no fragile text parsing)
STATS_JSON=$("$VENV" "$SCRIPT" --status-json 2>&1)
STATUS_EXIT=$?
if [ "$STATUS_EXIT" -ne 0 ]; then
    echo "Status command failed (exit $STATUS_EXIT). Output:" >> "$LOG"
    echo "$STATS_JSON" >> "$LOG"
    crash_notify "download_videos.py --status-json failed (exit $STATUS_EXIT)."
fi

# Parse JSON with Python: single invocation, no grep/awk fragility
read TODAY WEEK PENDING <<< $(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d['today'], d['week'], d['pending'])
except Exception as e:
    print(f'PARSE_ERROR PARSE_ERROR PARSE_ERROR', file=sys.stderr)
    sys.exit(1)
" "$STATS_JSON" 2>>"$LOG")

if ! is_int "$TODAY" || ! is_int "$WEEK" || ! is_int "$PENDING"; then
    echo "JSON parse failed. Raw: $STATS_JSON" >> "$LOG"
    crash_notify "Could not parse status JSON. Raw output: $STATS_JSON"
fi

echo "Stats: today $TODAY, week $WEEK, pending $PENDING" >> "$LOG"

# Safety: skip if thresholds hit
if [ "${WEEK:-0}" -ge 350 ]; then
    echo "Weekly limit reached ($WEEK/350). Skipping." >> "$LOG"
    osascript -e 'display notification "Weekly limit reached, session skipped" with title "Instagram Downloader"' 2>/dev/null
    NEXT=$(find_next_session)
    if [ -n "$NEXT" ]; then
        push_notify "Instagram Downloads" "Session skipped, weekly limit reached ($WEEK/350). Next session: $NEXT (weekly limit resets first)."
    else
        push_notify "Instagram Downloads" "Session skipped, weekly limit reached ($WEEK/350). No more sessions scheduled."
    fi
    self_clean
    exit 0
fi

# Vary daily cap: 65-95 (averages ~80), seeded by day-of-year so both sessions
# on the same day see the same cap
DAY_SEED=$((10#$(date +%j)))
DAILY_CAP=$((65 + (DAY_SEED * 17) % 31))

if [ "${TODAY:-0}" -ge "$DAILY_CAP" ]; then
    echo "Daily limit reached ($TODAY/$DAILY_CAP). Skipping." >> "$LOG"
    osascript -e 'display notification "Daily limit reached, session skipped" with title "Instagram Downloader"' 2>/dev/null
    NEXT=$(find_next_session)
    if [ -n "$NEXT" ]; then
        push_notify "Instagram Downloads" "Session skipped, daily limit reached ($TODAY/$DAILY_CAP). Next session: $NEXT (will catch up)."
    else
        push_notify "Instagram Downloads" "Session skipped, daily limit reached ($TODAY/$DAILY_CAP). No more sessions scheduled."
    fi
    self_clean
    exit 0
fi

# Calculate safe limit
DAILY_ROOM=$(($DAILY_CAP - $TODAY))
WEEKLY_ROOM=$((350 - $WEEK))
LIMIT=$DEFAULT_LIMIT
[ "$LIMIT" -gt "$DAILY_ROOM" ] && LIMIT=$DAILY_ROOM
[ "$LIMIT" -gt "$WEEKLY_ROOM" ] && LIMIT=$WEEKLY_ROOM
[ "$PENDING" -gt 0 ] && [ "$LIMIT" -gt "$PENDING" ] && LIMIT=$PENDING

echo "Limit calc: daily_cap $DAILY_CAP, daily_room $DAILY_ROOM, weekly_room $WEEKLY_ROOM, limit $LIMIT" >> "$LOG"

if [ "$LIMIT" -lt 1 ]; then
    if [ "$PENDING" -eq 0 ]; then
        echo "All downloads genuinely complete." >> "$LOG"
        push_notify "Instagram Downloads" "All downloads complete! Nothing left to download."
    else
        # LIMIT is 0 but videos are pending: something is wrong
        crash_notify "Limit calculated as $LIMIT but $PENDING videos still pending. daily_cap=$DAILY_CAP daily_room=$DAILY_ROOM weekly_room=$WEEKLY_ROOM. Script bug, needs investigation."
    fi
    self_clean
    exit 0
fi

echo "Downloading $LIMIT videos (adjusted from $DEFAULT_LIMIT)" >> "$LOG"

# Capture starting total so we can compute actual downloaded count
START_TOTAL=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['total_downloaded'])" "$STATS_JSON" 2>/dev/null)
is_int "$START_TOTAL" || START_TOTAL=0

# Run. Redirect order matters: stdout to file first, then stderr to same fd.
# Previously was 2>&1 >> "$LOG" which sent stderr to the void (2026-03-30 crash
# lost its traceback because of this).
"$VENV" "$SCRIPT" --limit "$LIMIT" --yes >> "$LOG" 2>&1
DOWNLOAD_EXIT=$?

echo "Session ended at $(date) (exit code: $DOWNLOAD_EXIT)" >> "$LOG"

if [ "$DOWNLOAD_EXIT" -ne 0 ]; then
    echo "Download script exited with error code $DOWNLOAD_EXIT" >> "$LOG"
    push_notify "Instagram Downloads ERROR" "Download script crashed (exit $DOWNLOAD_EXIT). $LIMIT videos were requested. Check download_log.txt."
    # Don't exit: still try to get final stats and report
fi

# Get final stats for notification
END_JSON=$("$VENV" "$SCRIPT" --status-json 2>/dev/null)
DOWNLOADED=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['total_downloaded'])" "$END_JSON" 2>/dev/null)
STILL_PENDING=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['pending'])" "$END_JSON" 2>/dev/null)
is_int "$DOWNLOADED" || DOWNLOADED=0
is_int "$STILL_PENDING" || STILL_PENDING=0

NEXT_SESSION=$(find_next_session)

SESSION_COUNT=$(( ${DOWNLOADED:-0} - ${START_TOTAL:-0} ))
MSG="Session done: $SESSION_COUNT videos downloaded. Total: ${DOWNLOADED:-?}, remaining: ${STILL_PENDING:-?}."
if [ -n "$NEXT_SESSION" ]; then
    MSG="$MSG Next session: $NEXT_SESSION."
else
    MSG="$MSG No more sessions scheduled."
fi
push_notify "Instagram Downloads" "$MSG"

self_clean

# If all downloads are done, clean up any remaining scheduled plists
REMAINING="${STILL_PENDING:-1}"
if [ "${REMAINING}" -eq 0 ]; then
    echo "All downloads complete: cleaning up remaining scheduled agents." >> "$LOG"
    push_notify "Instagram Downloads" "All downloads complete! $DOWNLOADED videos in the library."
    for f in "${PLIST_GLOB}"*.plist; do
        [ -f "$f" ] && rm -f "$f" && echo "  Removed $f" >> "$LOG"
    done
fi

echo "========================================" >> "$LOG"

osascript -e "display notification \"Download session complete\" with title \"Instagram Downloader\" sound name \"Glass\"" 2>/dev/null
