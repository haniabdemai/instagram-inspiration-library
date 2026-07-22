from helpers import extract_shortcode, detect_content_type, fix_emoji_encoding


def test_extract_shortcode_from_reel():
    assert extract_shortcode("https://www.instagram.com/reel/Cxample1abc/") == "Cxample1abc"

def test_extract_shortcode_from_post():
    assert extract_shortcode("https://www.instagram.com/p/Cxample2def/") == "Cxample2def"

def test_extract_shortcode_no_trailing_slash():
    assert extract_shortcode("https://www.instagram.com/reel/Cxample1abc") == "Cxample1abc"

def test_extract_shortcode_unknown_pattern():
    assert extract_shortcode("https://www.instagram.com/stories/someone/123/") is None

def test_detect_content_type_reel():
    assert detect_content_type("https://www.instagram.com/reel/Cxample1abc/") == "reel"

def test_detect_content_type_post():
    assert detect_content_type("https://www.instagram.com/p/Cxample2def/") == "post"

def test_detect_content_type_unknown():
    assert detect_content_type("https://www.instagram.com/stories/foo/") == "unknown"

def test_fix_emoji_encoding_bucket():
    broken = "Travel and \u00f0\u009f\u00aa\u00a3"
    assert fix_emoji_encoding(broken) == "Travel and 🪣"

def test_fix_emoji_encoding_plain_text_unchanged():
    assert fix_emoji_encoding("Funny") == "Funny"

def test_fix_emoji_encoding_already_correct():
    assert fix_emoji_encoding("Already 🪣 fine") == "Already 🪣 fine"
