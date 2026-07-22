"""
Download Instagram videos using Playwright browser automation.

Uses a real browser with stealth patches, Gaussian-distributed delays,
human-like mouse/scroll simulation, random break periods, and adaptive
session profiling. Each session is logged; future sessions automatically
vary their behaviour based on history to avoid detection patterns.

First run: a browser window opens. Log into Instagram manually if needed.
The session is saved to .browser-session/ and persists across runs.

Usage:
    .venv/bin/python download_videos.py [options]
    .venv/bin/python download_videos.py --status       # View download history
    .venv/bin/python download_videos.py --dry-run      # Preview without downloading
"""

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser-session")

# Where downloaded videos land. Override with INSPO_VIDEOS_DIR (e.g. an iCloud
# or synced folder); defaults to ./videos next to this script.
DEFAULT_VIDEOS_DIR = os.environ.get(
    "INSPO_VIDEOS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos"),
)

# Safety thresholds: warnings, not blocks
DAILY_WARN = 80
WEEKLY_WARN = 350


# ── Session log ───────────────────────────────────────────────────

def ensure_session_table(db_path: str):
    """Create the download_sessions table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS download_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            videos_attempted INTEGER DEFAULT 0,
            videos_downloaded INTEGER DEFAULT 0,
            videos_failed INTEGER DEFAULT 0,
            videos_unavailable INTEGER DEFAULT 0,
            mean_delay_used REAL,
            batch_size_used INTEGER,
            video_order TEXT,
            duration_seconds REAL
        )
    """)
    conn.commit()
    conn.close()


def start_session(db_path: str, mean_delay: float, batch_size: int, video_order: str) -> int:
    """Log the start of a download session. Returns session ID."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """INSERT INTO download_sessions (started_at, mean_delay_used, batch_size_used, video_order)
           VALUES (datetime('now'), ?, ?, ?)""",
        (mean_delay, batch_size, video_order),
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id


def end_session(db_path: str, session_id: int, attempted: int, downloaded: int,
                failed: int, unavailable: int, duration: float):
    """Log the end of a download session."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE download_sessions
           SET ended_at=datetime('now'), videos_attempted=?, videos_downloaded=?,
               videos_failed=?, videos_unavailable=?, duration_seconds=?
           WHERE id=?""",
        (attempted, downloaded, failed, unavailable, duration, session_id),
    )
    conn.commit()
    conn.close()


def get_session_history(db_path: str) -> dict:
    """Get download activity stats for safety checks and profile advice."""
    ensure_session_table(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0).isoformat()
    week_start = (now - timedelta(days=7)).isoformat()

    # Today's downloads
    row = conn.execute(
        "SELECT COALESCE(SUM(videos_downloaded), 0) as total FROM download_sessions WHERE started_at >= ?",
        (today_start,),
    ).fetchone()
    today_count = row["total"]

    # This week's downloads
    row = conn.execute(
        "SELECT COALESCE(SUM(videos_downloaded), 0) as total FROM download_sessions WHERE started_at >= ?",
        (week_start,),
    ).fetchone()
    week_count = row["total"]

    # All time
    row = conn.execute("SELECT COALESCE(SUM(videos_downloaded), 0) as total FROM download_sessions").fetchone()
    all_time = row["total"]

    # Recent sessions for profile advice
    recent = conn.execute(
        """SELECT started_at, videos_downloaded, mean_delay_used, batch_size_used, video_order
           FROM download_sessions WHERE started_at >= ? ORDER BY started_at DESC LIMIT 10""",
        (week_start,),
    ).fetchall()

    # Pending videos count
    pending_row = conn.execute("SELECT COUNT(*) as c FROM videos WHERE download_status = 'pending'").fetchone()
    pending = pending_row["c"]

    # Total downloaded
    dl_row = conn.execute("SELECT COUNT(*) as c FROM videos WHERE download_status = 'downloaded'").fetchone()
    total_downloaded = dl_row["c"]

    conn.close()

    return {
        "today": today_count,
        "week": week_count,
        "all_time": all_time,
        "pending": pending,
        "total_downloaded": total_downloaded,
        "recent_sessions": [dict(r) for r in recent],
    }


def print_status(db_path: str):
    """Print download status summary."""
    history = get_session_history(db_path)

    print("\n" + "=" * 55)
    print("  Instagram Download Status")
    print("=" * 55)
    print(f"\n  Total downloaded:  {history['total_downloaded']}")
    print(f"  Still pending:     {history['pending']}")
    print(f"\n  Today:             {history['today']} videos")
    print(f"  This week:         {history['week']} videos")
    print(f"  All time:          {history['all_time']} videos")

    # Safety assessment
    print(f"\n  Daily threshold:   {history['today']}/{DAILY_WARN}")
    print(f"  Weekly threshold:  {history['week']}/{WEEKLY_WARN}")

    if history["today"] >= DAILY_WARN:
        print(f"\n  ** Above daily threshold: consider waiting until tomorrow **")
    elif history["week"] >= WEEKLY_WARN:
        print(f"\n  ** Above weekly threshold: consider slowing down **")
    else:
        remaining_today = DAILY_WARN - history["today"]
        print(f"\n  Room today: ~{remaining_today} more videos")

    # Recent session details
    if history["recent_sessions"]:
        print(f"\n  Recent sessions:")
        for s in history["recent_sessions"][:5]:
            ts = s["started_at"][:16].replace("T", " ")
            delay = s["mean_delay_used"]
            batch = s["batch_size_used"]
            count = s["videos_downloaded"]
            order = s["video_order"] or "?"
            print(f"    {ts}  |  {count} videos  |  delay={delay:.0f}s  |  batch={batch}  |  order={order}")

    print("\n" + "=" * 55)


# ── Profile advisor ───────────────────────────────────────────────

def advise_session_params(history: dict, user_limit: int, user_delay: float) -> dict:
    """
    Based on past session history, recommend varied parameters for this session.
    Returns dict with: limit, mean_delay, video_order, warnings.
    """
    recent = history["recent_sessions"]
    warnings = []

    # Safety warnings
    if history["today"] >= DAILY_WARN:
        warnings.append(f"You've already downloaded {history['today']} videos today (threshold: {DAILY_WARN}). Consider waiting.")
    if history["week"] >= WEEKLY_WARN:
        warnings.append(f"You've downloaded {history['week']} videos this week (threshold: {WEEKLY_WARN}). Consider slowing down.")

    # Vary batch size for natural-looking patterns (not aggressive reduction)
    recent_batches = [s["batch_size_used"] for s in recent if s["batch_size_used"]]
    if recent_batches:
        avg_batch = sum(recent_batches) / len(recent_batches)
        # Light variance: +/- 15% from requested limit, nudged away from recent average
        if avg_batch > user_limit * 0.9:
            suggested_limit = max(20, int(user_limit * random.uniform(0.75, 0.95)))
        elif avg_batch < user_limit * 0.6:
            suggested_limit = int(user_limit * random.uniform(0.95, 1.15))
        else:
            suggested_limit = int(user_limit * random.uniform(0.85, 1.1))
    else:
        suggested_limit = user_limit

    # Cap at remaining daily allowance
    remaining_today = max(10, DAILY_WARN - history["today"])
    suggested_limit = min(suggested_limit, remaining_today)

    # Vary delay (don't always use the same speed)
    recent_delays = [s["mean_delay_used"] for s in recent if s["mean_delay_used"]]
    if recent_delays:
        avg_delay = sum(recent_delays) / len(recent_delays)
        # Shift away from recent average
        if avg_delay < 20:
            suggested_delay = user_delay * random.uniform(1.1, 1.4)
        elif avg_delay > 30:
            suggested_delay = user_delay * random.uniform(0.8, 1.0)
        else:
            suggested_delay = user_delay * random.uniform(0.85, 1.25)
    else:
        suggested_delay = user_delay

    # Vary video order
    recent_orders = [s["video_order"] for s in recent if s["video_order"]]
    order_options = ["sequential", "random", "by_collection"]
    if recent_orders:
        # Avoid repeating the same order as last session
        last_order = recent_orders[0]
        order_options = [o for o in order_options if o != last_order] or order_options
    suggested_order = random.choice(order_options)

    # Time-of-day advice
    recent_hours = []
    for s in recent:
        try:
            h = int(s["started_at"][11:13])
            recent_hours.append(h)
        except (ValueError, IndexError):
            pass
    if recent_hours:
        avg_hour = sum(recent_hours) / len(recent_hours)
        current_hour = datetime.now().hour
        if abs(current_hour - avg_hour) < 2:
            warnings.append(f"Your recent sessions were around {int(avg_hour)}:00. Try running at a different time for variety.")

    return {
        "limit": suggested_limit,
        "mean_delay": round(suggested_delay, 1),
        "video_order": suggested_order,
        "warnings": warnings,
    }


# ── Human-like timing ─────────────────────────────────────────────

def gaussian_delay(mean: float, std_ratio: float = 0.4, floor: float = 5.0, ceiling: float = 90.0) -> float:
    """Gaussian-distributed delay clipped to [floor, ceiling]."""
    std = mean * std_ratio
    delay = random.gauss(mean, std)
    return max(floor, min(ceiling, delay))


def maybe_take_break() -> float:
    """Occasionally simulate the user walking away."""
    roll = random.random()
    if roll < 0.03:
        pause = random.uniform(120, 480)
        print(f"    [Break: pausing {pause:.0f}s]")
        return pause
    elif roll < 0.11:
        pause = random.uniform(30, 90)
        print(f"    [Short pause: {pause:.0f}s]")
        return pause
    return 0


# ── Human-like mouse and scroll ───────────────────────────────────

def simulate_human_browsing(page):
    """Random mouse movements, scrolling, and dwell time."""
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    w, h = viewport["width"], viewport["height"]

    for _ in range(random.randint(2, 4)):
        x = random.randint(100, w - 100)
        y = random.randint(100, h - 100)
        try:
            page.mouse.move(x, y, steps=random.randint(8, 25))
        except Exception:
            pass
        time.sleep(random.uniform(0.1, 0.5))

    if random.random() < 0.6:
        scroll_amount = random.randint(100, 400)
        try:
            page.mouse.wheel(0, scroll_amount)
            time.sleep(random.uniform(0.5, 1.5))
            if random.random() < 0.4:
                page.mouse.wheel(0, -random.randint(50, scroll_amount))
                time.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    time.sleep(random.uniform(1.0, 4.0))


# ── Database ──────────────────────────────────────────────────────

def get_pending_videos(db_path: str, limit: int, order: str = "sequential") -> list[dict]:
    """Get videos that haven't been downloaded yet, in the specified order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if order == "random":
        rows = conn.execute(
            """SELECT id, instagram_url, shortcode, username
               FROM videos WHERE download_status = 'pending'
               ORDER BY RANDOM() LIMIT ?""",
            (limit,),
        ).fetchall()
    elif order == "by_collection":
        # Pick a random collection and download from it first, then fill with others
        rows = conn.execute(
            """SELECT v.id, v.instagram_url, v.shortcode, v.username
               FROM videos v
               JOIN video_collections vc ON v.id = vc.video_id
               JOIN collections c ON vc.collection_id = c.id
               WHERE v.download_status = 'pending'
               ORDER BY c.id, RANDOM()
               LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, instagram_url, shortcode, username
               FROM videos WHERE download_status = 'pending'
               ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def update_video_status(db_path: str, video_id: int, status: str,
                        local_path: str = None, duration: float = None):
    """Update a video's download status."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    if local_path:
        conn.execute(
            "UPDATE videos SET download_status=?, local_path=?, duration=?, updated_at=datetime('now') WHERE id=?",
            (status, local_path, duration, video_id),
        )
    else:
        conn.execute(
            "UPDATE videos SET download_status=?, updated_at=datetime('now') WHERE id=?",
            (status, video_id),
        )
    conn.commit()
    conn.close()


# ── Video capture ─────────────────────────────────────────────────

def capture_video_url(page, url: str, timeout_ms: int = 30000) -> str | None:
    """
    Navigate to an Instagram URL and extract the full video URL from page data.

    Instagram embeds video URLs in a `video_versions` JSON array within the page
    source. This gives us the complete MP4 URL, not streaming fragments.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_selector("video", timeout=10000)
        except Exception:
            pass

        # Human-like browsing while the page loads
        simulate_human_browsing(page)
        page.wait_for_timeout(random.randint(2000, 4000))

        # Extract video URL from video_versions in page source
        content = page.content()
        versions = re.findall(r'"video_versions":\[([^\]]+)\]', content)
        if versions:
            urls = re.findall(r'"url":"([^"]+)"', versions[0])
            if urls:
                # Unescape the URL (Instagram escapes slashes and ampersands)
                video_url = urls[0].replace(r'\/', '/').replace('\\u0026', '&')
                if 'cdninstagram.com' in video_url:
                    return video_url

        # Fallback: try og:video meta tag
        og_url = page.evaluate("""() => {
            const m = document.querySelector('meta[property="og:video"]');
            return m ? m.content : null;
        }""")
        if og_url and 'cdninstagram.com' in og_url:
            return og_url

        # Fallback: try clicking play and checking again
        try:
            video_el = page.query_selector("video")
            if video_el:
                box = video_el.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2 + random.uniform(-20, 20)
                    y = box["y"] + box["height"] / 2 + random.uniform(-20, 20)
                    page.mouse.click(x, y)
                    page.wait_for_timeout(random.randint(2000, 4000))
                    # Check page source again after click
                    content = page.content()
                    versions = re.findall(r'"video_versions":\[([^\]]+)\]', content)
                    if versions:
                        urls = re.findall(r'"url":"([^"]+)"', versions[0])
                        if urls:
                            video_url = urls[0].replace(r'\/', '/').replace('\\u0026', '&')
                            if 'cdninstagram.com' in video_url:
                                return video_url
        except Exception:
            pass

    except Exception as e:
        print(f"    Navigation error: {e}")
        return None

    return None


# ── File download ─────────────────────────────────────────────────

def download_file(url: str, dest_path: str) -> bool:
    """Download a file from a CDN URL to disk."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": "https://www.instagram.com/",
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        })
        with urllib.request.urlopen(req, timeout=120) as response:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"    Download error: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def get_video_duration(file_path: str) -> float | None:
    """Get video duration using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


# ── Browser setup ─────────────────────────────────────────────────

def find_chrome_path() -> str | None:
    """Find the installed Chrome binary on macOS."""
    paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def launch_browser(p, use_chrome: bool):
    """Launch a browser with stealth settings and persistent session."""
    chrome_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    chrome_path = find_chrome_path() if use_chrome else None
    if use_chrome and not chrome_path:
        print("Chrome not found. Falling back to Playwright Chromium.")

    return p.chromium.launch_persistent_context(
        SESSION_DIR,
        executable_path=chrome_path,
        headless=False,
        viewport={"width": random.randint(1240, 1380), "height": random.randint(850, 950)},
        args=chrome_args,
        ignore_default_args=["--enable-automation"],
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        # Match these to the locale your Instagram account normally browses
        # from: a mismatch between account history and fingerprint is itself
        # a signal. Override via INSPO_LOCALE / INSPO_TIMEZONE.
        locale=os.environ.get("INSPO_LOCALE", "en-GB"),
        timezone_id=os.environ.get("INSPO_TIMEZONE", "Europe/London"),
    )


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download Instagram videos via Playwright")
    parser.add_argument("--limit", type=int, default=50, help="Max videos this run (default: 50)")
    parser.add_argument("--mean-delay", type=float, default=22, help="Mean seconds between downloads (default: 22)")
    parser.add_argument("--batch-pause", type=float, default=300, help="Mean pause every 20-30 videos (default: 300)")
    parser.add_argument("--videos-dir", default=DEFAULT_VIDEOS_DIR, help="Where to save videos")
    parser.add_argument("--db", default="library.db", help="Database path")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded")
    parser.add_argument("--use-chrome", action="store_true", help="Use installed Chrome instead of Chromium")
    parser.add_argument("--status", action="store_true", help="Show download history and exit")
    parser.add_argument("--status-json", action="store_true", help="Output status as JSON (for scheduled_download.sh)")
    parser.add_argument("--no-adapt", action="store_true", help="Disable adaptive parameter adjustment")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip all confirmation prompts (for background/automated runs)")
    args = parser.parse_args()

    ensure_session_table(args.db)

    # Status mode: just print stats and exit
    if args.status:
        print_status(args.db)
        return

    # Machine-readable status for shell scripts
    if args.status_json:
        import json
        history = get_session_history(args.db)
        json.dump({
            "today": history["today"],
            "week": history["week"],
            "pending": history["pending"],
            "total_downloaded": history["total_downloaded"],
        }, sys.stdout)
        return

    os.makedirs(args.videos_dir, exist_ok=True)

    # Get history and compute adaptive parameters
    history = get_session_history(args.db)

    if args.no_adapt:
        session_limit = args.limit
        session_delay = args.mean_delay
        session_order = "sequential"
        advice_warnings = []
    else:
        advice = advise_session_params(history, args.limit, args.mean_delay)
        session_limit = advice["limit"]
        session_delay = advice["mean_delay"]
        session_order = advice["video_order"]
        advice_warnings = advice["warnings"]

    pending = get_pending_videos(args.db, session_limit, session_order)
    if not pending:
        print("No pending videos to download.")
        return

    next_batch_pause_at = random.randint(18, 30)

    print(f"\n{'=' * 55}")
    print(f"  Session plan")
    print(f"{'=' * 55}")
    print(f"  Videos:      {len(pending)} (of {history['pending']} pending)")
    print(f"  Order:       {session_order}")
    print(f"  Mean delay:  {session_delay}s")
    print(f"  Batch pause: ~{args.batch_pause:.0f}s every ~{next_batch_pause_at} videos")
    print(f"  Today so far: {history['today']} downloaded")
    print(f"  This week:    {history['week']} downloaded")
    print(f"  Stealth:     {'yes' if HAS_STEALTH else 'no (pip install playwright-stealth)'}")
    print(f"  Browser:     {'Chrome' if args.use_chrome else 'Chromium'}")

    if not args.no_adapt and history["recent_sessions"]:
        print(f"\n  Adapted from history: limit {args.limit}->{session_limit}, "
              f"delay {args.mean_delay}->{session_delay}s, order={session_order}")

    for w in advice_warnings:
        print(f"\n  WARNING: {w}")

    print(f"{'=' * 55}")

    if advice_warnings and not args.yes:
        print("\n  Press Enter to continue anyway, or Ctrl+C to abort...")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for v in pending:
            print(f"  {v['instagram_url']} -> {v['shortcode']}.mp4")
        return

    # Start session log
    session_id = start_session(args.db, session_delay, len(pending), session_order)
    session_start = time.time()

    downloaded = 0
    failed = 0
    unavailable = 0

    # Browser launch and login check can fail transiently (Chromium startup race,
    # session lock, resource contention). Retry once before giving up: a wasted
    # scheduled session is expensive because the plist self-cleans.
    MAX_LAUNCH_ATTEMPTS = 2
    RETRY_DELAY = 20  # seconds

    pw = None
    context = None
    page = None

    for attempt in range(1, MAX_LAUNCH_ATTEMPTS + 1):
        try:
            pw = sync_playwright().start()

            if HAS_STEALTH:
                stealth = Stealth()
                stealth.use_sync(pw)

            context = launch_browser(pw, args.use_chrome)
            page = context.pages[0] if context.pages else context.new_page()

            print("\nChecking Instagram login...")
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2, 4))

            is_logged_in = page.evaluate("""() => {
                return !document.querySelector('input[name="username"]');
            }""")
            break  # success: exit retry loop

        except Exception as e:
            # Clean up this failed attempt before retrying
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            pw = None
            context = None
            page = None

            if attempt < MAX_LAUNCH_ATTEMPTS:
                print(f"\nBrowser launch failed (attempt {attempt}/{MAX_LAUNCH_ATTEMPTS}): {e}")
                print(f"Retrying in {RETRY_DELAY}s...", flush=True)
                time.sleep(RETRY_DELAY)
            else:
                print(f"\nFATAL: Browser launch failed after {MAX_LAUNCH_ATTEMPTS} attempts: {e}", flush=True)
                import traceback
                traceback.print_exc(file=sys.stdout)
                sys.exit(1)

    # Download phase: browser is up, pw/context/page are set.
    # Wrap in try/finally so Playwright is always cleaned up.
    try:
        if not is_logged_in:
            print("\n" + "=" * 55)
            print("  Log into Instagram in the browser window.")
            print("  Press Enter here when done...")
            print("=" * 55)
            try:
                input()
            except EOFError:
                print("  (Running in background: waiting 30s for login...)")
                time.sleep(30)

        print("Warming up...")
        simulate_human_browsing(page)
        time.sleep(random.uniform(3, 7))

        print(f"\nStarting downloads ({len(pending)} videos)...\n")

        for i, video in enumerate(pending):
            if i > 0 and i % next_batch_pause_at == 0:
                pause = gaussian_delay(args.batch_pause, std_ratio=0.3, floor=120, ceiling=600)
                print(f"\n--- Batch pause ({pause:.0f}s) after {i} videos ---")
                print(f"    Progress: {downloaded} ok, {failed} failed, {unavailable} unavailable")
                time.sleep(pause)
                next_batch_pause_at = random.randint(18, 30)
                print(f"--- Resuming (next pause in ~{next_batch_pause_at} videos) ---\n")

            shortcode = video["shortcode"]
            url = video["instagram_url"]
            dest_path = os.path.join(args.videos_dir, f"{shortcode}.mp4")
            rel_path = dest_path  # honours --videos-dir / INSPO_VIDEOS_DIR

            print(f"[{i+1}/{len(pending)}] @{video['username']}: {shortcode}")

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                duration = get_video_duration(dest_path)
                update_video_status(args.db, video["id"], "downloaded", rel_path, duration)
                dur_str = f" ({duration:.1f}s)" if duration else ""
                print(f"    Already on disk{dur_str}")
                downloaded += 1
                continue

            cdn_url = capture_video_url(page, url)

            if not cdn_url:
                print(f"    No video URL found: marking unavailable")
                update_video_status(args.db, video["id"], "unavailable")
                unavailable += 1
            else:
                print(f"    Downloading...")
                if download_file(cdn_url, dest_path):
                    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
                    duration = get_video_duration(dest_path)
                    update_video_status(args.db, video["id"], "downloaded", rel_path, duration)
                    dur_str = f", {duration:.1f}s" if duration else ""
                    print(f"    OK: {shortcode}.mp4 ({size_mb:.1f}MB{dur_str})")
                    downloaded += 1
                else:
                    print(f"    FAILED")
                    update_video_status(args.db, video["id"], "failed")
                    failed += 1

            if i < len(pending) - 1:
                delay = gaussian_delay(session_delay)
                delay += maybe_take_break()
                print(f"    Next in {delay:.0f}s...")
                time.sleep(delay)

        context.close()
    except Exception as e:
        print(f"\nFATAL: Unexpected error during download session: {e}", flush=True)
        import traceback
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    # End session log
    session_duration = time.time() - session_start
    end_session(args.db, session_id, len(pending), downloaded, failed, unavailable, session_duration)

    # Summary
    print(f"\n{'=' * 55}")
    print(f"  Session Complete")
    print(f"{'=' * 55}")
    print(f"  Downloaded:   {downloaded}")
    print(f"  Failed:       {failed}")
    print(f"  Unavailable:  {unavailable}")
    print(f"  Duration:     {session_duration/60:.1f} minutes")

    conn = sqlite3.connect(args.db)
    remaining = conn.execute("SELECT COUNT(*) FROM videos WHERE download_status = 'pending'").fetchone()[0]
    conn.close()
    print(f"\n  Still pending: {remaining}")
    if remaining > 0:
        print(f"  Run again to continue. Use --status to check your history first.")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
