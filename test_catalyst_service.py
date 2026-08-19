import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from catalyst_service import evaluate_catalyst_watch


NOW = datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)


def article(minutes_old, headline="AAPL announces strategic partnership", source="Wire"):
    return {
        "created_at": (NOW - timedelta(minutes=minutes_old)).isoformat(),
        "headline": headline,
        "source": source,
    }


class CatalystWatchTests(unittest.TestCase):
    def test_freshness_buckets(self):
        with patch("catalyst_service._fetch_articles", side_effect=[
            [article(90)], [article(91)], [article(18 * 60)], [article(18 * 60 + 1)],
        ]):
            results = evaluate_catalyst_watch(["A", "B", "C", "D"], now=NOW)

        self.assertEqual(results[0].headlines[0].freshness, "BREAKING")
        self.assertEqual(results[1].headlines[0].freshness, "RECENT")
        self.assertEqual(results[2].headlines[0].freshness, "RECENT")
        self.assertEqual(results[3].headlines[0].freshness, "STALE")

    def test_timezone_aware_timestamp_is_normalized_to_utc(self):
        timestamp = "2026-08-19T11:00:00-04:00"
        with patch("catalyst_service._fetch_articles", return_value=[{
            "created_at": timestamp,
            "headline": "AAPL wins contract",
            "source": "Wire",
        }]):
            result = evaluate_catalyst_watch(["$aapl"], now=NOW)[0]

        self.assertEqual(result.headlines[0].created_at.tzinfo, timezone.utc)
        self.assertEqual(result.headlines[0].ticker, "AAPL")

    def test_missing_or_malformed_timestamps_are_unavailable(self):
        with patch("catalyst_service._fetch_articles", return_value=[
            {"headline": "Missing timestamp"},
            {"created_at": "bad", "headline": "Malformed timestamp"},
        ]):
            results = evaluate_catalyst_watch(["AAPL"], now=NOW)

        self.assertEqual(results[0].status, "UNAVAILABLE")

    def test_provider_error_and_empty_response_are_unavailable(self):
        with patch("catalyst_service._fetch_articles", side_effect=[RuntimeError("down"), []]):
            results = evaluate_catalyst_watch(["AAPL", "MSFT"], now=NOW)

        self.assertEqual(results[0].status, "UNAVAILABLE")
        self.assertEqual(results[1].status, "UNAVAILABLE")

    def test_symbols_are_normalized_and_duplicates_removed(self):
        with patch("catalyst_service._fetch_articles", return_value=[]):
            results = evaluate_catalyst_watch([" aapl ", "$AAPL", "", "bad symbol"], now=NOW)

        self.assertEqual([result.ticker for result in results], ["AAPL"])

    def test_multi_symbol_article_is_safe(self):
        with patch("catalyst_service._fetch_articles", return_value=[{
            "created_at": NOW.isoformat(),
            "headline": "AAPL and MSFT sign contract",
            "symbols": ["AAPL", "MSFT"],
            "source": "Wire",
        }]):
            results = evaluate_catalyst_watch(["AAPL", "MSFT"], now=NOW)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[1].headlines[0].event_type, "contract/deal")

    def test_duplicate_articles_are_suppressed(self):
        duplicate = article(10)
        with patch("catalyst_service._fetch_articles", return_value=[duplicate, duplicate.copy()]):
            result = evaluate_catalyst_watch(["AAPL"], now=NOW)[0]

        self.assertEqual(len(result.headlines), 1)

    def test_favorable_adverse_and_informational_classification(self):
        with patch("catalyst_service._fetch_articles", return_value=[
            article(1, "AAPL raises guidance"),
            article(2, "AAPL faces SEC investigation"),
            article(3, "AAPL opens a new office"),
        ]):
            headlines = evaluate_catalyst_watch(["AAPL"], now=NOW)[0].headlines

        classifications = {item.headline: item.classification for item in headlines}
        self.assertEqual(classifications["AAPL raises guidance"], "FAVORABLE")
        self.assertEqual(classifications["AAPL faces SEC investigation"], "ADVERSE")
        self.assertEqual(classifications["AAPL opens a new office"], "INFORMATIONAL")

    def test_shares_headline_is_not_offering_or_dilution(self):
        with patch("catalyst_service._fetch_articles", return_value=[article(
            1,
            "Institutional Investors Added Over 70 Million RIVN Shares",
        )]):
            item = evaluate_catalyst_watch(["RIVN"], now=NOW)[0].headlines[0]

        self.assertNotEqual(item.event_type, "offering/dilution")
        self.assertEqual(item.classification, "INFORMATIONAL")
        self.assertFalse(item.is_material)

    def test_roundup_is_broad_informational_and_non_material(self):
        headlines = [
            "12 Health Care Stocks Moving In Wednesday's Pre-Market Session",
            "Stock Market Today",
        ]
        with patch("catalyst_service._fetch_articles", return_value=[
            article(1, headline) | {"symbols": ["AAPL", "MSFT", "RIVN", "NVDA"]}
            for headline in headlines
        ]):
            result = evaluate_catalyst_watch(["RIVN"], now=NOW)[0]

        self.assertEqual(result.status, "NO MATERIAL CATALYST")
        self.assertEqual(result.suppressed_count, 2)
        self.assertTrue(all(item.relevance == "BROAD/MULTI_SYMBOL" for item in result.headlines))
        self.assertTrue(all(item.classification == "INFORMATIONAL" for item in result.headlines))
        self.assertTrue(all(not item.is_material for item in result.headlines))

    def test_fresh_informational_news_does_not_elevate_ticker(self):
        with patch("catalyst_service._fetch_articles", return_value=[article(
            1,
            "AAPL opens a new office",
        )]):
            result = evaluate_catalyst_watch(["AAPL"], now=NOW)[0]

        self.assertEqual(result.headlines[0].freshness, "BREAKING")
        self.assertEqual(result.status, "NO MATERIAL CATALYST")

    def test_direct_fresh_material_events_remain_material(self):
        headlines = [
            "FDA decision clears AAPL treatment",
            "AAPL wins contract award",
            "AAPL reports earnings result",
            "AAPL announces merger",
            "AAPL announces registered direct offering",
        ]
        with patch("catalyst_service._fetch_articles", return_value=[
            article(index + 1, headline)
            for index, headline in enumerate(headlines)
        ]):
            result = evaluate_catalyst_watch(["AAPL"], now=NOW)[0]

        self.assertEqual(result.status, "MATERIAL BREAKING")
        self.assertTrue(all(item.is_material for item in result.headlines))

    def test_catalyst_metadata_does_not_create_technical_eligibility(self):
        with patch("catalyst_service._fetch_articles", return_value=[article(1)]):
            result = evaluate_catalyst_watch(["AAPL"], now=NOW)[0]

        self.assertEqual(result.headlines[0].classification, "FAVORABLE")
        self.assertFalse(hasattr(result, "automatic_paper_eligible"))

    def test_no_order_or_execution_function_is_called(self):
        with patch("catalyst_service._fetch_articles", return_value=[article(1)]), \
             patch("paper_execution_service.submit_paper_entry") as submit, \
             patch("paper_execution_service.paper_execution_enabled") as enabled:
            evaluate_catalyst_watch(["AAPL"], now=NOW)

        submit.assert_not_called()
        enabled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
