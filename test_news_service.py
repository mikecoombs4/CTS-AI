import unittest

from news_service import classify_news_articles


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


if __name__ == "__main__":
    unittest.main()
