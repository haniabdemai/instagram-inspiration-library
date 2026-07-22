import json
import sqlite3
import pytest
from schema import create_schema


@pytest.fixture
def db():
    """In-memory SQLite database with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_collections_json(tmp_path):
    """Minimal saved_collections.json with:
    - 4 collections (Aesthetics, Funny, Funny duplicate, Empty)
    - 5 video entries (one in two collections, one with malformed URL)
    - Empty collection (no videos)
    """
    data = {
        "saved_saved_collections": [
            {
                "title": "Collection",
                "string_map_data": {
                    "Name": {"value": "Aesthetics"},
                    "Creation Time": {"timestamp": 1700000000},
                    "Update Time": {"timestamp": 1700000001}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/reel/ABC123/",
                        "value": "creator_one"
                    },
                    "Added Time": {"timestamp": 1700000100}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/p/DEF456/",
                        "value": "creator_two"
                    },
                    "Added Time": {"timestamp": 1700000200}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/stories/someone/123/",
                        "value": "creator_malformed"
                    },
                    "Added Time": {"timestamp": 1700000300}
                }
            },
            {
                "title": "Collection",
                "string_map_data": {
                    "Name": {"value": "Funny"},
                    "Creation Time": {"timestamp": 1701000000},
                    "Update Time": {"timestamp": 1701000001}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/reel/GHI789/",
                        "value": "creator_three"
                    },
                    "Added Time": {"timestamp": 1701000100}
                }
            },
            {
                "title": "Collection",
                "string_map_data": {
                    "Name": {"value": "Funny"},
                    "Creation Time": {"timestamp": 1702000000},
                    "Update Time": {"timestamp": 1702000001}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/reel/JKL012/",
                        "value": "creator_four"
                    },
                    "Added Time": {"timestamp": 1702000100}
                }
            },
            {
                "string_map_data": {
                    "Name": {
                        "href": "https://www.instagram.com/reel/ABC123/",
                        "value": "creator_one"
                    },
                    "Added Time": {"timestamp": 1702000200}
                }
            },
            {
                "title": "Collection",
                "string_map_data": {
                    "Name": {"value": "Empty"},
                    "Creation Time": {"timestamp": 1703000000},
                    "Update Time": {"timestamp": 1703000001}
                }
            }
        ]
    }
    path = tmp_path / "saved_collections.json"
    path.write_text(json.dumps(data))
    return str(path)


@pytest.fixture
def sample_saved_posts_json(tmp_path):
    """Minimal saved_posts.json: overlaps with collections + one unique."""
    data = {
        "saved_saved_media": [
            {
                "title": "creator_one",
                "string_map_data": {
                    "Saved on": {
                        "href": "https://www.instagram.com/reel/ABC123/",
                        "timestamp": 1700500000
                    }
                }
            },
            {
                "title": "creator_five",
                "string_map_data": {
                    "Saved on": {
                        "href": "https://www.instagram.com/reel/MNO345/",
                        "timestamp": 1700600000
                    }
                }
            }
        ]
    }
    path = tmp_path / "saved_posts.json"
    path.write_text(json.dumps(data))
    return str(path)
