from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Searcher(Protocol):
    def search(self, query: str, *, limit: int = 20) -> list[dict[str, object]]: ...


def _matches_expected(
    result: dict[str, object], expected: list[dict[str, object]]
) -> bool:
    video_name = Path(str(result["video_path"])).name
    return any(
        video_name == item["video"]
        and result["start_ms"] == item["start_ms"]
        and result["end_ms"] == item["end_ms"]
        for item in expected
    )


def _result_summary(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "video_path",
            "shot_index",
            "start_ms",
            "end_ms",
            "summary",
            "score",
        )
    }


def evaluate_suite(
    searcher: Searcher,
    suite: dict[str, object],
) -> dict[str, object]:
    version = suite.get("version")
    cases = suite.get("cases")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("evaluation suite version is required")
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation suite must contain cases")

    results: list[dict[str, object]] = []
    by_category: dict[str, dict[str, int]] = {}
    top1_hits = 0
    top3_hits = 0
    reciprocal_rank_total = 0.0
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("evaluation cases must be objects")
        case_id = case.get("id")
        category = case.get("category")
        query = case.get("query")
        expected = case.get("expected")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (case_id, category, query)
        ):
            raise ValueError("each evaluation case requires id, category and query")
        if not isinstance(expected, list) or not expected or any(
            not isinstance(item, dict) for item in expected
        ):
            raise ValueError("each evaluation case requires expected shots")

        ranked = searcher.search(str(query), limit=3)
        rank = next(
            (
                index
                for index, result in enumerate(ranked, start=1)
                if _matches_expected(result, expected)
            ),
            None,
        )
        top1_hit = rank == 1
        top3_hit = rank is not None
        top1_hits += int(top1_hit)
        top3_hits += int(top3_hit)
        reciprocal_rank_total += 1 / rank if rank is not None else 0.0
        category_totals = by_category.setdefault(
            str(category), {"total": 0, "top1_hits": 0, "top3_hits": 0}
        )
        category_totals["total"] += 1
        category_totals["top1_hits"] += int(top1_hit)
        category_totals["top3_hits"] += int(top3_hit)
        results.append(
            {
                "id": case_id,
                "category": category,
                "query": query,
                "rank": rank,
                "expected": expected,
                "top_results": [_result_summary(result) for result in ranked],
            }
        )

    total = len(results)
    return {
        "version": version,
        "total": total,
        "top1_hits": top1_hits,
        "top3_hits": top3_hits,
        "top1_accuracy": round(top1_hits / total, 6),
        "top3_accuracy": round(top3_hits / total, 6),
        "mrr": round(reciprocal_rank_total / total, 6),
        "by_category": by_category,
        "results": results,
        "top1_failures": [item for item in results if item["rank"] != 1],
        "top3_failures": [item for item in results if item["rank"] is None],
    }
