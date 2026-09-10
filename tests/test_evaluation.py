import unittest

from video_search.evaluation import evaluate_suite


class StaticSearcher:
    def __init__(self, results: dict[str, list[dict[str, object]]]) -> None:
        self.results = results

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, object]]:
        return self.results[query][:limit]


class EvaluationTest(unittest.TestCase):
    def test_reports_top_one_top_three_mrr_and_failure_details(self) -> None:
        target = {
            "video_path": "/videos/wedding-a.mp4",
            "shot_index": 4,
            "start_ms": 10_000,
            "end_ms": 15_000,
            "summary": "新人在湖边拥抱",
            "score": 0.8,
        }
        other = {
            "video_path": "/videos/wedding-b.mp4",
            "shot_index": 1,
            "start_ms": 2_000,
            "end_ms": 5_000,
            "summary": "宾客进入宴会厅",
            "score": 0.9,
        }
        suite = {
            "version": "wedding-eval-v1",
            "cases": [
                {
                    "id": "embrace-first",
                    "category": "action",
                    "query": "湖边拥抱",
                    "expected": [
                        {
                            "video": "wedding-a.mp4",
                            "start_ms": 10_000,
                            "end_ms": 15_000,
                        }
                    ],
                },
                {
                    "id": "embrace-second",
                    "category": "action",
                    "query": "浪漫互动",
                    "expected": [
                        {
                            "video": "wedding-a.mp4",
                            "start_ms": 10_000,
                            "end_ms": 15_000,
                        }
                    ],
                },
                {
                    "id": "missing-lake",
                    "category": "where",
                    "query": "湖边仪式",
                    "expected": [
                        {
                            "video": "wedding-a.mp4",
                            "start_ms": 20_000,
                            "end_ms": 25_000,
                        }
                    ],
                },
            ],
        }
        searcher = StaticSearcher(
            {
                "湖边拥抱": [target, other],
                "浪漫互动": [other, target],
                "湖边仪式": [other],
            }
        )

        report = evaluate_suite(searcher, suite)

        self.assertEqual("wedding-eval-v1", report["version"])
        self.assertEqual(3, report["total"])
        self.assertEqual(1, report["top1_hits"])
        self.assertEqual(2, report["top3_hits"])
        self.assertEqual(0.333333, report["top1_accuracy"])
        self.assertEqual(0.666667, report["top3_accuracy"])
        self.assertEqual(0.5, report["mrr"])
        self.assertEqual(
            {"total": 2, "top1_hits": 1, "top3_hits": 2},
            report["by_category"]["action"],
        )
        self.assertEqual(
            ["embrace-second", "missing-lake"],
            [item["id"] for item in report["top1_failures"]],
        )
        self.assertEqual(
            ["missing-lake"],
            [item["id"] for item in report["top3_failures"]],
        )
        self.assertEqual([1, 2, None], [item["rank"] for item in report["results"]])


if __name__ == "__main__":
    unittest.main()
