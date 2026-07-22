import sqlite3 as sq
import pytest


def test_collections_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='collections'")
    assert cursor.fetchone() is not None

def test_videos_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='videos'")
    assert cursor.fetchone() is not None

def test_video_collections_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='video_collections'")
    assert cursor.fetchone() is not None

def test_tags_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tags'")
    assert cursor.fetchone() is not None

def test_video_tags_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='video_tags'")
    assert cursor.fetchone() is not None

def test_insights_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='insights'")
    assert cursor.fetchone() is not None

def test_fts5_table_exists(db):
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='videos_fts'")
    assert cursor.fetchone() is not None

def test_collections_allows_duplicate_names(db):
    db.execute("INSERT INTO collections (name, type) VALUES ('Funny', 'instagram')")
    db.execute("INSERT INTO collections (name, type) VALUES ('Funny', 'instagram')")
    db.commit()
    cursor = db.execute("SELECT COUNT(*) FROM collections WHERE name='Funny'")
    assert cursor.fetchone()[0] == 2

def test_videos_unique_url(db):
    db.execute("""INSERT INTO videos (instagram_url, shortcode, username, content_type, source)
                  VALUES ('https://instagram.com/reel/ABC/', 'ABC', 'user1', 'reel', 'both')""")
    db.commit()
    with pytest.raises(sq.IntegrityError):
        db.execute("""INSERT INTO videos (instagram_url, shortcode, username, content_type, source)
                      VALUES ('https://instagram.com/reel/ABC/', 'ABC', 'user1', 'reel', 'both')""")

def test_videos_accepts_unknown_content_type(db):
    db.execute("""INSERT INTO videos (instagram_url, shortcode, username, content_type, source)
                  VALUES ('https://instagram.com/x/Z/', 'Z', 'user1', 'unknown', 'both')""")
    db.commit()
    cursor = db.execute("SELECT content_type FROM videos WHERE shortcode='Z'")
    assert cursor.fetchone()[0] == "unknown"

def test_video_collections_cascade_delete(db):
    db.execute("INSERT INTO collections (name, type) VALUES ('Test', 'instagram')")
    db.execute("""INSERT INTO videos (instagram_url, shortcode, username, content_type, source)
                  VALUES ('https://instagram.com/reel/X/', 'X', 'u', 'reel', 'both')""")
    db.execute("INSERT INTO video_collections (video_id, collection_id) VALUES (1, 1)")
    db.commit()
    db.execute("DELETE FROM videos WHERE id = 1")
    db.commit()
    cursor = db.execute("SELECT COUNT(*) FROM video_collections")
    assert cursor.fetchone()[0] == 0

def test_foreign_keys_enforced(db):
    with pytest.raises(sq.IntegrityError):
        db.execute("INSERT INTO video_collections (video_id, collection_id) VALUES (999, 999)")
