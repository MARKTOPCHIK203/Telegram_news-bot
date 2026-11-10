"""Best-effort publisher URL resolution for Google News RSS items.

Uses Google News' unofficial batchexecute protocol. Fail closed (return None)
rather than guessing at article identity; caller can fall back to a text card.
No copyrighted images are downloaded or re-uploaded.
"""
import ipaddress
import json
import re
from urllib.parse import urlparse

import requests

_GOOGLE_NEWS_HOST = "news.google.com"
_ARTICLE_PATH = re.compile(r"^/(?:rss/)?articles/([A-Za-z0-9_-]{12,2048})/?$|^/read/([A-Za-z0-9_-]{12,2048})/?$")
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7",
}


def google_article_id(link):
    try:
        parsed = urlparse(link or "")
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != _GOOGLE_NEWS_HOST:
        return None
    match = _ARTICLE_PATH.fullmatch(parsed.path)
    return next((part for part in match.groups() if part), None) if match else None


def safe_publisher_url(link):
    """Accept ordinary public HTTPS article URLs, not internal addresses."""
    try:
        parsed = urlparse(link or "")
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return False
        if not host or "." not in host or host == _GOOGLE_NEWS_HOST:
            return False
        if host.endswith((".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def parse_google_response(body):
    """Return a publisher URL from one Google Fbv4je RPC response."""
    data = body.split("\n\n", 1)[-1] if "\n\n" in body else body.lstrip(")]}'\r\n ")
    frames = json.loads(data)
    for frame in frames:
        if not isinstance(frame, list) or len(frame) < 3:
            continue
        if frame[0] == "wrb.fr" and frame[1] == "Fbv4je" and frame[2]:
            result = json.loads(frame[2])
            if isinstance(result, list) and len(result) > 1 and result[0] == "garturlres":
                url = result[1]
                return url if isinstance(url, str) and safe_publisher_url(url) else None
    return None


def resolve_publisher_url(link, session=None):
    """Decode Google News RSS link in at most two HTTP calls.

    Google may block or change this unofficial endpoint; never fail a post.
    """
    article_id = google_article_id(link)
    if not article_id:
        return None
    http = session if session is not None else requests.Session()
    try:
        article_page = http.get(
            "https://news.google.com/rss/articles/" + article_id,
            headers={**_BROWSER_HEADERS, "Cookie": "CONSENT=PENDING+987"},
            timeout=9,
        )
        article_page.raise_for_status()
        signature = re.search(r'data-n-a-sg="([^"]+)"', article_page.text)
        timestamp = re.search(r'data-n-a-ts="(\d+)"', article_page.text)
        if not signature or not timestamp:
            return None
        inner = [
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en",
              None, 1, None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            article_id, int(timestamp.group(1)), signature.group(1),
        ]
        outer = [[["Fbv4je", json.dumps(inner, separators=(",", ":"))]]]
        response = http.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={
                **_BROWSER_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://news.google.com",
                "Referer": "https://news.google.com/",
                "X-Same-Domain": "1",
            },
            data={"f.req": json.dumps(outer, separators=(",", ":"))},
            timeout=9,
        )
        response.raise_for_status()
        return parse_google_response(response.text)
    except (requests.RequestException, ValueError, TypeError, IndexError, KeyError) as exc:
        print(f"Предпросмотр Google News недоступен: {type(exc).__name__}")
        return None
