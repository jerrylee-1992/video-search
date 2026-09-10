from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".webm",
    ".mts",
    ".m2ts",
}

ESTIMATED_CACHE_BYTES_PER_SHOT = 512 * 1_024


@dataclass(frozen=True)
class VideoMetadata:
    duration_ms: int
    width: int
    height: int


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int


def discover_videos(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def fingerprint(path: Path) -> str:
    stat = Path(path).stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def probe_video(path: Path, *, timeout_seconds: float = 120) -> VideoMetadata:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    return VideoMetadata(
        duration_ms=round(float(payload["format"]["duration"]) * 1_000),
        width=int(stream["width"]),
        height=int(stream["height"]),
    )


def preflight_folder(
    root: Path,
    *,
    probe: Callable[[Path], VideoMetadata] | None = None,
    timeout_seconds: float = 120,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if probe is None:
        probe = lambda path: probe_video(path, timeout_seconds=timeout_seconds)
    ignored_sidecars = sum(
        1
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and path.name.startswith("._")
    )
    videos = discover_videos(root)
    errors: list[dict[str, str]] = []
    readable = 0
    total_bytes = 0
    total_duration_ms = 0
    estimated_shots_low = 0
    estimated_shots_high = 0
    resolutions: dict[str, int] = {}
    for path in videos:
        try:
            metadata = probe(path)
        except Exception as error:
            errors.append({"path": str(path), "error": str(error)})
            continue
        readable += 1
        total_bytes += path.stat().st_size
        total_duration_ms += metadata.duration_ms
        estimated_shots_low += math.ceil(metadata.duration_ms / 30_000)
        estimated_shots_high += math.ceil(metadata.duration_ms / 3_000)
        resolution = f"{metadata.width}x{metadata.height}"
        resolutions[resolution] = resolutions.get(resolution, 0) + 1
    disk_root = Path(cache_root) if cache_root is not None else root
    while not disk_root.exists() and disk_root != disk_root.parent:
        disk_root = disk_root.parent
    return {
        "folder": str(root),
        "discovered": len(videos),
        "readable": readable,
        "failed": len(errors),
        "ignored_sidecars": ignored_sidecars,
        "total_bytes": total_bytes,
        "total_duration_ms": total_duration_ms,
        "estimated_shots_low": estimated_shots_low,
        "estimated_shots_high": estimated_shots_high,
        "estimated_cache_bytes_low": (
            estimated_shots_low * ESTIMATED_CACHE_BYTES_PER_SHOT
        ),
        "estimated_cache_bytes_high": (
            estimated_shots_high * ESTIMATED_CACHE_BYTES_PER_SHOT
        ),
        "cache_bytes_per_shot_assumption": ESTIMATED_CACHE_BYTES_PER_SHOT,
        "cache_free_bytes": shutil.disk_usage(disk_root).free,
        "resolutions": resolutions,
        "errors": errors,
    }


class FFmpegSceneSegmenter:
    def __init__(
        self,
        *,
        threshold: float = 0.32,
        min_scene_ms: int = 500,
        max_scene_ms: int = 30_000,
        timeout_seconds: float = 3_600,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        if min_scene_ms < 1:
            raise ValueError("min_scene_ms must be positive")
        if max_scene_ms < min_scene_ms:
            raise ValueError("max_scene_ms must be at least min_scene_ms")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.threshold = threshold
        self.min_scene_ms = min_scene_ms
        self.max_scene_ms = max_scene_ms
        self.timeout_seconds = timeout_seconds
        self.version = (
            f"ffmpeg-scene-v2:threshold={threshold!r}:min_scene_ms={min_scene_ms}:"
            f"max_scene_ms={max_scene_ms}"
        )

    def segment(self, path: Path, duration_ms: int) -> list[Segment]:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(path),
                "-vf",
                f"select='gt(scene,{self.threshold})',showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        candidates = [
            round(float(value) * 1_000)
            for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)
        ]
        cuts: list[int] = []
        previous = 0
        for cut in candidates:
            if cut - previous < self.min_scene_ms:
                continue
            if duration_ms - cut < self.min_scene_ms:
                continue
            cuts.append(cut)
            previous = cut
        scene_boundaries = [0, *cuts, duration_ms]
        boundaries = [0]
        for start, end in zip(scene_boundaries, scene_boundaries[1:]):
            duration = end - start
            pieces = max(1, (duration + self.max_scene_ms - 1) // self.max_scene_ms)
            boundaries.extend(
                start + round(duration * index / pieces)
                for index in range(1, pieces + 1)
            )
        return [Segment(start, end) for start, end in zip(boundaries, boundaries[1:])]


class FFmpegThumbnailer:
    def __init__(self, *, timeout_seconds: float = 300) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def extract(self, path: Path, segment: Segment, destination: Path) -> int:
        if segment.end_ms <= segment.start_ms:
            raise ValueError("segment end must be after start")
        timestamp_ms = segment.start_ms + (segment.end_ms - segment.start_ms) // 2
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp_ms / 1_000:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(destination),
            ],
            check=True,
            timeout=self.timeout_seconds,
        )
        return timestamp_ms
