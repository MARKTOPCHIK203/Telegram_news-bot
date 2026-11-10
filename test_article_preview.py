"""Offline checks for Google News publisher previews (no external HTTP needed)."""
import json
import unittest
from unittest.mock import Mock

from article_preview import (
    google_article_id, parse_google_response, resolve_publisher_url,
    safe_publisher_url,
)


class ArticlePreviewTests(unittest.TestCase):
    def test_only_google_news_articles(self):
        self.assertEqual(
            google_article_id("https://news.google.com/rss/articles/CBMiEXAMPLE123?oc=5"),
            "CBMiEXAMPLE123",
        )
        self.assertIsNone(google_article_id("https://news.google.com/rss/search?q=other"))
        self.assertIsNone(google_article_id("https://evil.example/articles/CBMiEXAMPLE123"))
        self.assertIsNone(google_article_id("http://news.google.com/rss/articles/CBMiEXAMPLE123"))

    def test_safe_publisher_urls(self):
        self.assertTrue(safe_publisher_url("https://www.bbc.com/russian/articles/abc"))
        self.assertFalse(safe_publisher_url("https://news.google.com/rss/articles/abc"))
        self.assertFalse(safe_publisher_url("http://www.bbc.com/news"))
        self.assertFalse(safe_publisher_url("https://127.0.0.1/private"))
        self.assertFalse(safe_publisher_url("https://localhost/private"))

    def test_google_rpc_reply(self):
        inner = json.dumps(["garturlres", "https://www.bbc.com/russian/articles/abc"])
        body = ")]}'\n\n" + json.dumps([
            ["wrb.fr", "Fbv4je", inner, None, None],
            ["di", 16],
        ])
        self.assertEqual(
            parse_google_response(body),
            "https://www.bbc.com/russian/articles/abc",
        )
        self.assertIsNone(parse_google_response(")]}'\n\n[[\"wrb.fr\",\"Fbv4je\",null]]"))

    def test_resolve_constructs_google_request(self):
        inner = json.dumps(["garturlres", "https://news.example.org/story/7"])
        page = Mock(text='<div data-n-a-sg="mysignature" data-n-a-ts="1720000000">')
        page.raise_for_status = Mock()
        rpc = Mock(text=")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]]))
        rpc.raise_for_status = Mock()
        http = Mock()
        http.get.return_value = page
        http.post.return_value = rpc
        url = resolve_publisher_url(
            "https://news.google.com/rss/articles/CBMiEXAMPLE123?oc=5", http
        )
        self.assertEqual(url, "https://news.example.org/story/7")
        self.assertEqual(http.get.call_count, 1)
        self.assertEqual(http.post.call_count, 1)
        encoded = http.post.call_args.kwargs["data"]["f.req"]
        self.assertIn("CBMiEXAMPLE123", encoded)
        self.assertIn("mysignature", encoded)

    def test_fallback_when_no_signature(self):
        page = Mock(text="<html>Consent required</html>")
        page.raise_for_status = Mock()
        http = Mock()
        http.get.return_value = page
        self.assertIsNone(resolve_publisher_url(
            "https://news.google.com/rss/articles/CBMiEXAMPLE123", http
        ))
        http.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
