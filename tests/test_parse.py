from parse_export import parse_collections, parse_saved_posts, merge_and_insert


def test_parse_collections_creates_correct_count(sample_collections_json):
    collections, _ = parse_collections(sample_collections_json)
    assert len(collections) == 4


def test_parse_collections_duplicate_names_preserved(sample_collections_json):
    collections, _ = parse_collections(sample_collections_json)
    names = [c["name"] for c in collections]
    assert names.count("Funny") == 2


def test_parse_collections_video_count(sample_collections_json):
    _, videos_by_url = parse_collections(sample_collections_json)
    assert len(videos_by_url) == 5


def test_parse_collections_multi_collection_video(sample_collections_json):
    _, videos_by_url = parse_collections(sample_collections_json)
    abc_entry = videos_by_url["https://www.instagram.com/reel/ABC123/"]
    assert len(abc_entry["collection_indices"]) == 2


def test_parse_collections_extracts_shortcode(sample_collections_json):
    _, videos_by_url = parse_collections(sample_collections_json)
    entry = videos_by_url["https://www.instagram.com/reel/ABC123/"]
    assert entry["shortcode"] == "ABC123"


def test_parse_collections_detects_content_type(sample_collections_json):
    _, videos_by_url = parse_collections(sample_collections_json)
    assert videos_by_url["https://www.instagram.com/reel/ABC123/"]["content_type"] == "reel"
    assert videos_by_url["https://www.instagram.com/p/DEF456/"]["content_type"] == "post"


def test_parse_collections_malformed_url_has_none_shortcode(sample_collections_json):
    _, videos_by_url = parse_collections(sample_collections_json)
    entry = videos_by_url["https://www.instagram.com/stories/someone/123/"]
    assert entry["shortcode"] is None
    assert entry["content_type"] == "unknown"


def test_parse_saved_posts_count(sample_saved_posts_json):
    saved = parse_saved_posts(sample_saved_posts_json)
    assert len(saved) == 2


def test_parse_saved_posts_extracts_fields(sample_saved_posts_json):
    saved = parse_saved_posts(sample_saved_posts_json)
    url = "https://www.instagram.com/reel/ABC123/"
    assert url in saved
    assert saved[url]["username"] == "creator_one"
    assert saved[url]["saved_at"] == 1700500000


def test_merge_source_both(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT source FROM videos WHERE shortcode='ABC123'")
    assert cursor.fetchone()[0] == "both"


def test_merge_source_collections_only(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT source FROM videos WHERE shortcode='DEF456'")
    assert cursor.fetchone()[0] == "collections_only"


def test_merge_source_saved_only(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT source FROM videos WHERE shortcode='MNO345'")
    assert cursor.fetchone()[0] == "saved_only"


def test_merge_creates_uncollected_collection(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT id, type FROM collections WHERE name='Uncollected'")
    row = cursor.fetchone()
    assert row is not None
    assert row[1] == "system"


def test_merge_saved_only_in_uncollected(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("""
        SELECT v.shortcode FROM videos v
        JOIN video_collections vc ON v.id = vc.video_id
        JOIN collections c ON vc.collection_id = c.id
        WHERE c.name = 'Uncollected'
    """)
    assert cursor.fetchone()[0] == "MNO345"


def test_merge_video_in_multiple_collections(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("""
        SELECT COUNT(*) FROM video_collections vc
        JOIN videos v ON vc.video_id = v.id
        WHERE v.shortcode = 'ABC123'
    """)
    assert cursor.fetchone()[0] == 2


def test_merge_saved_at_populated_for_both(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT saved_at FROM videos WHERE shortcode='ABC123'")
    assert cursor.fetchone()[0] == 1700500000


def test_merge_malformed_url_inserted_with_warning(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    stats = merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT content_type FROM videos WHERE instagram_url LIKE '%stories%'")
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "unknown"
    assert any("stories" in w for w in stats["parse_warnings"])


def test_merge_empty_collection_exists(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("""
        SELECT c.name, COUNT(vc.video_id) FROM collections c
        LEFT JOIN video_collections vc ON c.id = vc.collection_id
        WHERE c.name = 'Empty'
        GROUP BY c.id
    """)
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "Empty"
    assert row[1] == 0


def test_merge_duplicate_collection_names_in_db(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    merge_and_insert(db, collections, coll_videos, saved)
    cursor = db.execute("SELECT COUNT(*) FROM collections WHERE name='Funny'")
    assert cursor.fetchone()[0] == 2


def test_merge_total_counts(db, sample_collections_json, sample_saved_posts_json):
    collections, coll_videos = parse_collections(sample_collections_json)
    saved = parse_saved_posts(sample_saved_posts_json)
    stats = merge_and_insert(db, collections, coll_videos, saved)
    assert stats["total_videos"] == 6
    assert stats["total_collections"] == 5
    assert stats["source_both"] == 1
    assert stats["source_collections_only"] == 4
    assert stats["source_saved_only"] == 1
