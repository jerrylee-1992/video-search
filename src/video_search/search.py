from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import Protocol

from video_search.database import Database


class TextEmbedder(Protocol):
    version: str

    def embed_query(self, text: str) -> list[float]: ...


class VisualEmbedder(Protocol):
    version: str

    def embed_text(self, text: str) -> list[float]: ...


FILTER_PATTERN = re.compile(
    r"(?P<name>when|period|lighting|environment|where|venue|who|action):(?P<value>[^\s]+)",
    re.IGNORECASE,
)

DEFAULT_EVENT_WEIGHT = 0.15
COMMON_ACTION_MIN_DOCUMENTS = 3
COMMON_ACTION_DOCUMENT_RATIO = 0.2


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _lexical_score(query: str, text: str) -> float:
    terms = re.findall(r"[\w\u3400-\u9fff]+", query.casefold())
    if not terms:
        return 0.0
    compact_text = re.sub(r"\s+", "", text.casefold())
    matches = sum(
        1
        for term in terms
        if term in text.casefold() or re.sub(r"\s+", "", term) in compact_text
    )
    return matches / len(terms)


def _compact_search_text(text: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]+", text.casefold()))


def _character_ngrams(text: str, *, size: int = 2) -> set[str]:
    compact = _compact_search_text(text)
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _event_score(
    query: str,
    events: list[dict[str, object]],
    *,
    action_document_frequency: Mapping[str, int] | None = None,
    total_documents: int = 0,
) -> float:
    compact_query = _compact_search_text(query)
    if not compact_query or not events:
        return 0.0

    ordered_events = sorted(events, key=lambda item: int(item.get("sequence", 0)))
    matched_actions: list[tuple[str, int]] = []
    event_text_parts: list[str] = []
    seen_actions: set[str] = set()
    for event in ordered_events:
        action = _compact_search_text(str(event.get("action") or ""))
        description = str(event.get("description") or "")
        event_text_parts.extend((action, description))
        if len(action) >= 2 and action in compact_query and action not in seen_actions:
            matched_actions.append((action, compact_query.index(action)))
            seen_actions.add(action)

    if not matched_actions:
        return 0.0

    if len(matched_actions) == 1 and action_document_frequency is not None:
        action = matched_actions[0][0]
        document_frequency = action_document_frequency.get(action, 0)
        if (
            total_documents > 0
            and document_frequency >= COMMON_ACTION_MIN_DOCUMENTS
            and document_frequency / total_documents >= COMMON_ACTION_DOCUMENT_RATIO
        ):
            return 0.0

    matched_characters = sum(len(action) for action, _ in matched_actions)
    action_score = min(1.0, matched_characters / 6)

    query_ngrams = _character_ngrams(compact_query)
    event_ngrams = _character_ngrams(" ".join(event_text_parts))
    context_score = (
        len(query_ngrams & event_ngrams) / len(query_ngrams)
        if query_ngrams
        else 0.0
    )

    order_score = 0.0
    if len(matched_actions) >= 2:
        query_positions = [position for _, position in matched_actions]
        if query_positions == sorted(query_positions):
            order_score = 1.0

    return 0.7 * action_score + 0.2 * context_score + 0.1 * order_score


class HybridSearcher:
    def __init__(
        self,
        database: Database,
        *,
        text_embedder: TextEmbedder | None = None,
        visual_embedder: VisualEmbedder | None = None,
        event_weight: float = DEFAULT_EVENT_WEIGHT,
        semantic_min_score: float | None = None,
    ) -> None:
        self.database = database
        self.text_embedder = text_embedder
        self.visual_embedder = visual_embedder
        self.event_weight = event_weight
        if semantic_min_score is None:
            version = getattr(text_embedder, "version", "")
            semantic_min_score = 0.4 if version.startswith("fastembed-v1:BAAI/bge-small-zh-v1.5") else 0.0
        if not -1 <= semantic_min_score <= 1:
            raise ValueError("semantic_min_score must be between -1 and 1")
        self.semantic_min_score = semantic_min_score

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, object]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        filters = [
            (match.group("name").lower(), match.group("value").casefold())
            for match in FILTER_PATTERN.finditer(query)
        ]
        natural_query = FILTER_PATTERN.sub(" ", query).strip()
        query_vector = (
            self.text_embedder.embed_query(natural_query or query)
            if self.text_embedder is not None
            else None
        )
        visual_query_vector = (
            self.visual_embedder.embed_text(natural_query or query)
            if self.visual_embedder is not None
            else None
        )

        records = self.database.list_search_shots(
            text_embedding_version=(self.text_embedder.version if self.text_embedder else None),
            visual_embedding_version=(self.visual_embedder.version if self.visual_embedder else None),
        )
        shots_with_details = [(record["shot"], record["details"]) for record in records]
        action_document_frequency: Counter[str] = Counter()
        for _, details in shots_with_details:
            actions = {
                _compact_search_text(str(event.get("action") or ""))
                for event in details["events"]
            }
            action_document_frequency.update(action for action in actions if action)

        ranked: list[dict[str, object]] = []
        for record in records:
            shot, details = record["shot"], record["details"]
            if not self._matches_filters(shot, details, filters):
                continue
            lexical_document = " ".join(
                str(value)
                for value in (
                    shot.get("search_text"), shot.get("metadata_search_text")
                )
                if value
            )
            lexical = _lexical_score(natural_query, lexical_document)
            semantic = 0.0
            has_text_vector = False
            if query_vector is not None and self.text_embedder is not None:
                shot_vector = record["text_vector"] or []
                if record["text_vector"] is not None:
                    has_text_vector = True
                semantic = _cosine(query_vector, shot_vector)
            visual = 0.0
            has_visual_vector = False
            if visual_query_vector is not None and self.visual_embedder is not None:
                visual_scores = []
                for frame in details["frames"]:
                    frame_vector = record["visual_vectors"].get(int(frame["id"]))
                    if frame_vector is None:
                        continue
                    has_visual_vector = True
                    visual_scores.append(_cosine(visual_query_vector, frame_vector))
                visual = max(visual_scores, default=0.0)
            event = _event_score(
                natural_query,
                details["events"],
                action_document_frequency=action_document_frequency,
                total_documents=len(shots_with_details),
            )
            if (
                query_vector is None
                and visual_query_vector is None
                and natural_query
                and lexical == 0
                and event == 0
            ):
                continue
            if natural_query and lexical == 0 and event == 0:
                semantic_match = has_text_vector and semantic > self.semantic_min_score
                visual_match = has_visual_vector and visual > self.semantic_min_score
                if not semantic_match and not visual_match:
                    continue
            if query_vector is not None and visual_query_vector is not None:
                score = 0.5 * semantic + 0.3 * visual + 0.2 * lexical
            elif query_vector is not None:
                score = 0.75 * semantic + 0.25 * lexical
            elif visual_query_vector is not None:
                score = 0.75 * visual + 0.25 * lexical
            else:
                score = lexical
            score = (1 - self.event_weight) * score + self.event_weight * event
            thumbnail_path = next(
                (
                    frame["path"]
                    for frame in details["frames"]
                    if frame.get("kind") == "thumbnail"
                ),
                None,
            )
            who = []
            for role in details["roles"]:
                person: dict[str, object] = {"role": str(role["role"])}
                if role.get("count") is not None:
                    person["count"] = int(role["count"])
                who.append(person)
            actions = list(
                dict.fromkeys(
                    str(event["action"])
                    for event in sorted(
                        details["events"], key=lambda item: int(item.get("sequence", 0))
                    )
                    if event.get("action")
                )
            )
            ranked.append(
                {
                    "shot_id": int(shot["id"]),
                    "shot_token": str(shot["identity_token"]),
                    "video_path": shot["video_path"],
                    "source_root": shot.get("source_root"),
                    "relative_path": shot.get("relative_path"),
                    "project_name": shot.get("project_name"),
                    "event_date": shot.get("event_date"),
                    "media_type": shot.get("media_type"),
                    "edit_version": shot.get("edit_version"),
                    "shot_index": int(shot["shot_index"]),
                    "start_ms": int(shot["start_ms"]),
                    "end_ms": int(shot["end_ms"]),
                    "start_seconds": int(shot["start_ms"]) / 1_000,
                    "end_seconds": int(shot["end_ms"]) / 1_000,
                    "summary": shot["summary"],
                    "when_period": shot["when_period"],
                    "lighting": shot["lighting"],
                    "environment": shot["environment"],
                    "venue": shot["venue"],
                    "who": who,
                    "actions": actions,
                    "thumbnail_path": thumbnail_path,
                    "score": round(score, 6),
                }
            )
        ranked.sort(key=lambda item: (-float(item["score"]), int(item["shot_id"])))
        return ranked[:limit]

    @staticmethod
    def _matches_filters(
        shot: dict[str, object],
        details: dict[str, list[dict[str, object]]],
        filters: list[tuple[str, str]],
    ) -> bool:
        for name, expected in filters:
            if name in {"when", "period"}:
                values = [shot.get("when_period")]
            elif name == "lighting":
                values = [shot.get("lighting")]
            elif name == "environment":
                values = [shot.get("environment")]
            elif name == "venue":
                values = [shot.get("venue")]
            elif name == "where":
                values = [shot.get("environment"), shot.get("venue")]
            elif name == "who":
                values = [item.get("role") for item in details["roles"]]
            else:
                values = [item.get("action") for item in details["events"]]
            if not any(
                expected in str(value).casefold() for value in values if value is not None
            ):
                return False
        return True
