import unittest

from datetime import datetime, timezone

from news_service import _articles_from_response, classify_news_articles


class NewsRiskTests(unittest.TestCase):
    def test_dangerous_headline_blocks_candidate(self) -> None:
        result = classify_news_articles(
            ticker="TEST",
            articles=[
                {
                    "headline": "TEST announces public offering",
                    "summary": "The company plans to issue shares.",
                    "source": "Example Wire",
                    "created_at": "2026-08-01T10:00:00Z",
                }
            ],
        )

        self.assertEqual(result.status, "BLOCK")
        self.assertIn("public offering", result.blocking_matches)

    def test_ordinary_headline_requires_review(self) -> None:
        result = classify_news_articles(
            ticker="TEST",
            articles=[
                {
                    "headline": "TEST opens a new distribution center",
                    "summary": "Operations begin next month.",
                    "source": "Example Wire",
                    "created_at": "2026-08-01T10:00:00Z",
                }
            ],
        )

        self.assertEqual(result.status, "REVIEW")
        self.assertFalse(result.blocking_matches)

    def test_no_recent_headlines_passes_risk_check(self) -> None:
        result = classify_news_articles(
            ticker="TEST",
            articles=[],
        )

        self.assertEqual(result.status, "PASS")
        self.assertFalse(result.headlines)
        self.assertFalse(result.provider_query_succeeded)

    def test_successful_empty_query_has_explicit_provenance(self) -> None:
        queried_at = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
        result = classify_news_articles(
            "TEST", [], provider_query_succeeded=True, queried_at=queried_at
        )
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.provider_query_succeeded)
        self.assertEqual(result.queried_at, queried_at)

    def test_malformed_or_ambiguous_provider_response_is_not_empty_success(self) -> None:
        for response in (None, {}, {"data": {}}, object()):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    _articles_from_response(response)


if __name__ == "__main__":
    unittest.main()
