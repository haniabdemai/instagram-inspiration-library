import re


def extract_shortcode(url: str) -> str | None:
    """Extract the shortcode from an Instagram URL (reel or post)."""
    match = re.search(r'/(?:reel|p)/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None


def detect_content_type(url: str) -> str:
    """Detect 'reel' or 'post' from an Instagram URL pattern."""
    if '/reel/' in url:
        return 'reel'
    if '/p/' in url:
        return 'post'
    return 'unknown'


def fix_emoji_encoding(text: str) -> str:
    """Fix Instagram's double-encoded UTF-8 in collection names."""
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text
