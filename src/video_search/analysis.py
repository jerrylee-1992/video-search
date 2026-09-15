from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_search.media import Segment


class TransientAnalyzerError(RuntimeError):
    """The analyzer is temporarily unavailable and the job may be resumed."""


class InvalidAnalyzerOutputError(ValueError):
    """The analyzer replied, but its result cannot be stored as a valid analysis."""

    def __init__(
        self,
        message: str,
        *,
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = list(attempts or [])

    def as_record(self) -> dict[str, Any]:
        return {"error": str(self), "attempts": self.attempts}


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    return value or None


def _collect_text(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_collect_text(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_collect_text(child))
        return result
    return []


def _required_text(value: Any, field: str) -> str:
    result = _optional_text(value, field)
    if result is None:
        raise ValueError(f"{field} is required")
    return result


def _optional_confidence(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{field} must be a finite number between zero and one")
    return result


def adaptive_frame_count(duration_ms: int) -> int:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if duration_ms <= 8_000:
        return 8
    if duration_ms <= 20_000:
        return 16
    return 24


@dataclass(frozen=True)
class ShotAnalysis:
    summary: str
    roles: list[dict[str, Any]]
    events: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    when_period: str | None
    lighting: str | None
    environment: str | None
    venue: str | None
    search_text: str
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ShotAnalysis":
        if not isinstance(payload, dict):
            raise ValueError("analysis must be an object")
        summary = _optional_text(payload.get("summary"), "summary")
        if summary is None:
            raise ValueError("summary is required")

        roles = payload.get("who", [])
        events = payload.get("events", [])
        objects = payload.get("objects", [])
        where = payload.get("where", {})
        when = payload.get("when", {})
        camera = payload.get("camera", {})
        if not isinstance(roles, list):
            raise ValueError("who must be a list")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        if not isinstance(objects, list):
            raise ValueError("objects must be a list")
        if not isinstance(where, dict) or not isinstance(when, dict):
            raise ValueError("where and when must be objects")
        if not isinstance(camera, dict):
            raise ValueError("camera must be an object")
        if any(not isinstance(item, dict) for item in [*roles, *events, *objects]):
            raise ValueError("who, events and objects must contain objects")

        normalized_roles = []
        for item in roles:
            normalized = dict(item)
            normalized["role"] = _required_text(item.get("role"), "who.role")
            if "appearance" in item:
                normalized["appearance"] = _optional_text(item.get("appearance"), "who.appearance")
            count = item.get("count")
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                raise ValueError("who.count must be a non-negative integer")
            normalized["confidence"] = _optional_confidence(
                item.get("confidence"), "who.confidence"
            )
            normalized_roles.append(normalized)

        sequences = [event.get("sequence") for event in events]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in sequences):
            raise ValueError("event.sequence must be an integer")
        if sequences != list(range(len(events))):
            raise ValueError("event sequence must start at zero and be contiguous")
        normalized_events = []
        for event in events:
            normalized = dict(event)
            normalized["action"] = _required_text(event.get("action"), "event.action")
            normalized["description"] = _required_text(
                event.get("description"), "event.description"
            )
            for name in ("subject_role", "object", "target"):
                if name in event:
                    normalized[name] = _optional_text(event.get(name), f"event.{name}")
            normalized["confidence"] = _optional_confidence(
                event.get("confidence"), "event.confidence"
            )
            normalized_events.append(normalized)

        normalized_objects = []
        for item in objects:
            normalized = dict(item)
            normalized["name"] = _required_text(item.get("name"), "objects.name")
            normalized["confidence"] = _optional_confidence(
                item.get("confidence"), "objects.confidence"
            )
            normalized_objects.append(normalized)

        for container_name, container, fields in (
            ("where", where, ("environment", "venue", "background")),
            ("when", when, ("period", "lighting")),
            ("camera", camera, ("shot_size", "movement", "viewpoint")),
        ):
            for field in fields:
                if field in container:
                    _optional_text(container.get(field), f"{container_name}.{field}")

        text_parts = _collect_text(
            {
                "summary": summary,
                "who": normalized_roles,
                "where": where,
                "when": when,
                "events": normalized_events,
                "objects": normalized_objects,
                "camera": camera,
            }
        )
        search_text = " ".join(dict.fromkeys(text_parts))
        return cls(
            summary=summary,
            roles=normalized_roles,
            events=normalized_events,
            objects=normalized_objects,
            when_period=_optional_text(when.get("period"), "when.period"),
            lighting=_optional_text(when.get("lighting"), "when.lighting"),
            environment=_optional_text(where.get("environment"), "where.environment"),
            venue=_optional_text(where.get("venue"), "where.venue"),
            search_text=search_text,
            raw=dict(payload),
        )


class CommandAnalyzer:
    def __init__(
        self,
        command: list[str],
        *,
        version: str,
        timeout_seconds: float = 3_600,
    ) -> None:
        if not command:
            raise ValueError("analyzer command is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.command = list(command)
        self.version = version
        self.timeout_seconds = timeout_seconds

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        duration_ms = segment.end_ms - segment.start_ms
        with temporary_clip(
            path, segment, timeout_seconds=self.timeout_seconds
        ) as clip_path:
            request = {
                "clip_path": str(clip_path),
                "source_path": str(path),
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "sample_frames": adaptive_frame_count(duration_ms),
            }
            completed = subprocess.run(
                self.command,
                input=json.dumps(request, ensure_ascii=False),
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise ValueError("analyzer command did not return valid JSON") from error
            return ShotAnalysis.from_dict(payload)


@contextmanager
def temporary_clip(
    path: Path,
    segment: Segment,
    *,
    timeout_seconds: float = 300,
) -> Iterator[Path]:
    duration_ms = segment.end_ms - segment.start_ms
    if duration_ms <= 0:
        raise ValueError("segment end must be after start")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    with tempfile.TemporaryDirectory(prefix="video-search-shot-") as directory:
        clip_path = Path(directory) / "shot.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{segment.start_ms / 1_000:.3f}",
                "-i",
                str(path),
                "-t",
                f"{duration_ms / 1_000:.3f}",
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-y",
                str(clip_path),
            ],
            check=True,
            timeout=timeout_seconds,
        )
        yield clip_path
