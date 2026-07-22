import argparse
import json
import os
import sqlite3
import sys
from helpers import extract_shortcode, detect_content_type, fix_emoji_encoding
from schema import create_schema


def parse_collections(collections_path: str) -> tuple[list[dict], dict]:
    """
    Parse saved_collections.json.

    Returns:
        collections: list of collection dicts (name, created_at, updated_at)
        videos_by_url: dict mapping URL -> {username, shortcode, content_type,
                        collection_indices, added_at_by_collection}
    """
    with open(collections_path, 'r') as f:
        data = json.load(f)

    items = data["saved_saved_collections"]
    collections = []
    videos_by_url = {}
    current_collection_index = None

    for item in items:
        if item.get("title") == "Collection":
            name = fix_emoji_encoding(item["string_map_data"]["Name"]["value"])
            collections.append({
                "name": name,
                "created_at": item["string_map_data"].get("Creation Time", {}).get("timestamp"),
                "updated_at": item["string_map_data"].get("Update Time", {}).get("timestamp"),
            })
            current_collection_index = len(collections) - 1
        elif current_collection_index is not None:
            smd = item.get("string_map_data", {})
            url = smd.get("Name", {}).get("href", "")
            if not url:
                continue
            username = smd.get("Name", {}).get("value", "")
            added_at = smd.get("Added Time", {}).get("timestamp")
            shortcode = extract_shortcode(url)
            content_type = detect_content_type(url)

            if url not in videos_by_url:
                videos_by_url[url] = {
                    "username": username,
                    "shortcode": shortcode,
                    "content_type": content_type,
                    "collection_indices": [],
                    "added_at_by_collection": {},
                }
            videos_by_url[url]["collection_indices"].append(current_collection_index)
            videos_by_url[url]["added_at_by_collection"][current_collection_index] = added_at

    return collections, videos_by_url


def parse_saved_posts(saved_posts_path: str) -> dict:
    """
    Parse saved_posts.json.

    Returns:
        dict mapping URL -> {username, saved_at, shortcode, content_type}
    """
    with open(saved_posts_path, 'r') as f:
        data = json.load(f)

    saved = {}
    for item in data.get("saved_saved_media", []):
        username = item.get("title", "")
        smd = item.get("string_map_data", {})
        saved_on = smd.get("Saved on", {})
        url = saved_on.get("href", "")
        if not url:
            continue
        saved_at = saved_on.get("timestamp")
        saved[url] = {
            "username": username,
            "saved_at": saved_at,
            "shortcode": extract_shortcode(url),
            "content_type": detect_content_type(url),
        }
    return saved


def merge_and_insert(conn, collections: list[dict], coll_videos: dict, saved: dict) -> dict:
    """
    Merge collections and saved posts data, insert into database.

    Returns stats dict with counts.
    """
    cursor = conn.cursor()

    # Insert Instagram collections
    collection_id_map = {}
    for i, coll in enumerate(collections):
        cursor.execute(
            "INSERT INTO collections (name, type, created_at, updated_at) VALUES (?, 'instagram', ?, ?)",
            (coll["name"], coll["created_at"], coll["updated_at"])
        )
        collection_id_map[i] = cursor.lastrowid

    # Create Uncollected system collection
    cursor.execute("INSERT INTO collections (name, type) VALUES ('Uncollected', 'system')")
    uncollected_id = cursor.lastrowid

    # Determine all unique URLs and their source
    all_urls = set(coll_videos.keys()) | set(saved.keys())
    stats = {
        "total_videos": 0, "total_collections": len(collections) + 1,
        "total_links": 0, "source_both": 0, "source_collections_only": 0,
        "source_saved_only": 0, "type_reel": 0, "type_post": 0,
        "type_unknown": 0, "multi_collection": 0, "parse_warnings": [],
    }

    for url in all_urls:
        in_colls = url in coll_videos
        in_saved = url in saved

        if in_colls and in_saved:
            source = "both"
            stats["source_both"] += 1
        elif in_colls:
            source = "collections_only"
            stats["source_collections_only"] += 1
        else:
            source = "saved_only"
            stats["source_saved_only"] += 1

        v = coll_videos[url] if in_colls else saved[url]

        shortcode = v["shortcode"]
        if shortcode is None:
            stats["parse_warnings"].append(f"Could not extract shortcode from {url}")
            fallback = url.rstrip("/").split("/")[-1]
            shortcode = f"unknown_{fallback}"

        content_type = v["content_type"]
        username = v["username"]
        saved_at = saved[url]["saved_at"] if in_saved else None

        stats[f"type_{content_type}"] = stats.get(f"type_{content_type}", 0) + 1

        try:
            cursor.execute("""
                INSERT INTO videos (instagram_url, shortcode, username, content_type, saved_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (url, shortcode, username, content_type, saved_at, source))
        except sqlite3.IntegrityError as e:
            stats["parse_warnings"].append(f"Skipped duplicate: {url} ({e})")
            continue

        video_id = cursor.lastrowid
        stats["total_videos"] += 1

        if in_colls:
            coll_indices = coll_videos[url]["collection_indices"]
            if len(coll_indices) > 1:
                stats["multi_collection"] += 1
            for ci in coll_indices:
                db_coll_id = collection_id_map[ci]
                added_at = coll_videos[url]["added_at_by_collection"].get(ci)
                cursor.execute(
                    "INSERT INTO video_collections (video_id, collection_id, added_at) VALUES (?, ?, ?)",
                    (video_id, db_coll_id, added_at)
                )
                stats["total_links"] += 1
        else:
            cursor.execute(
                "INSERT INTO video_collections (video_id, collection_id) VALUES (?, ?)",
                (video_id, uncollected_id)
            )
            stats["total_links"] += 1

    conn.commit()
    return stats


def print_stats(stats: dict):
    """Print human-readable summary of parsing results."""
    print("\n" + "=" * 55)
    print("  Instagram Inspiration Library: Parse Summary")
    print("=" * 55)
    print(f"\n  Videos:      {stats['total_videos']}")
    print(f"  Collections: {stats['total_collections']}")
    print(f"  Links:       {stats['total_links']}")
    print(f"\n  Source breakdown:")
    print(f"    In both files:    {stats['source_both']}")
    print(f"    Collections only: {stats['source_collections_only']}")
    print(f"    Saved only:       {stats['source_saved_only']}")
    print(f"\n  Content type:")
    print(f"    Reels: {stats.get('type_reel', 0)}")
    print(f"    Posts: {stats.get('type_post', 0)}")
    if stats.get('type_unknown', 0):
        print(f"    Unknown: {stats['type_unknown']}")
    print(f"\n  Videos in multiple collections: {stats['multi_collection']}")
    if stats["parse_warnings"]:
        print(f"\n  Warnings ({len(stats['parse_warnings'])}):")
        for w in stats["parse_warnings"]:
            print(f"    - {w}")
    print("\n" + "=" * 55)


def main():
    parser = argparse.ArgumentParser(description="Parse Instagram export into SQLite database")
    parser.add_argument("export_dir", help="Path to the saved/ directory in the Instagram export")
    parser.add_argument("-o", "--output", default="library.db", help="Output database path (default: library.db)")
    args = parser.parse_args()

    collections_path = os.path.join(args.export_dir, "saved_collections.json")
    saved_posts_path = os.path.join(args.export_dir, "saved_posts.json")

    for path in [collections_path, saved_posts_path]:
        if not os.path.exists(path):
            print(f"Error: {path} not found")
            sys.exit(1)

    conn = sqlite3.connect(args.output)
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)

    print(f"Parsing collections from {collections_path}...")
    collections, coll_videos = parse_collections(collections_path)
    print(f"  Found {len(collections)} collections, {len(coll_videos)} unique videos")

    print(f"Parsing saved posts from {saved_posts_path}...")
    saved = parse_saved_posts(saved_posts_path)
    print(f"  Found {len(saved)} saved posts")

    print("Merging and inserting...")
    stats = merge_and_insert(conn, collections, coll_videos, saved)

    print_stats(stats)
    conn.close()
    print(f"\nDatabase written to: {args.output}")


if __name__ == "__main__":
    main()
