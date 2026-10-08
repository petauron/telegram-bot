from __future__ import annotations

import unittest
from urllib.parse import urlparse

import httpx

from app.hacker_news import (
    HN_API_BASE,
    HackerNewsFetcher,
    parse_hacker_news_stories,
    validate_hacker_news_settings,
)


def story(story_id: int, *, title: str, score: int = 180) -> dict:
    return {
        "id": story_id,
        "type": "story",
        "title": title,
        "score": score,
        "descendants": 42,
        "time": 1_786_426_800,
        "url": f"https://example.com/articles/{story_id}",
    }


class HackerNewsTests(unittest.IsolatedAsyncioTestCase):
    def test_local_heat_topic_and_story_type_filters(self) -> None:
        feed = parse_hacker_news_stories(
            [
                story(1, title="Open source AI database is released"),
                story(2, title="Ordinary entertainment story"),
                story(3, title="Cloud security update", score=30),
                {**story(4, title="AI comment"), "type": "comment"},
            ],
            settings={"minimum_score": 100, "story_list": "best", "keywords": []},
        )
        self.assertEqual([entry.external_id for entry in feed.entries], ["hn-story:1"])
        self.assertIn("180 分", feed.entries[0].body)

    def test_custom_keywords_replace_defaults(self) -> None:
        feed = parse_hacker_news_stories(
            [story(5, title="A niche compiler release")],
            settings={"minimum_score": 100, "story_list": "top", "keywords": ["compiler"]},
        )
        self.assertEqual(len(feed.entries), 1)
        self.assertEqual(feed.entries[0].url, "https://example.com/articles/5")

    async def test_fetch_uses_bounded_official_story_endpoints_and_no_comments(self) -> None:
        requests: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            path = urlparse(str(request.url)).path
            if path.endswith("/beststories.json"):
                return httpx.Response(200, json=[101, 102])
            if path.endswith("/item/101.json"):
                return httpx.Response(200, json=story(101, title="AI developer platform"))
            if path.endswith("/item/102.json"):
                return httpx.Response(200, json=story(102, title="Unrelated topic"))
            return httpx.Response(404)

        fetcher = HackerNewsFetcher(transport=httpx.MockTransport(handler))
        try:
            result = await fetcher.fetch(
                {"settings": {"story_list": "best", "minimum_score": 100, "keywords": []}}
            )
            self.assertEqual(len(result.feed.entries), 1)
            self.assertEqual(len(requests), 3)
            self.assertTrue(all(url.startswith(HN_API_BASE) for url in requests))
            self.assertFalse(any("comment" in url for url in requests))
        finally:
            await fetcher.close()

    def test_settings_are_strict_and_bounded(self) -> None:
        with self.assertRaises(ValueError):
            validate_hacker_news_settings({"story_list": "new", "minimum_score": 100})
        with self.assertRaises(ValueError):
            validate_hacker_news_settings({"story_list": "best", "minimum_score": 10})


if __name__ == "__main__":
    unittest.main()
