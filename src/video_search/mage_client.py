from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from video_search.analysis import (
    InvalidAnalyzerOutputError,
    ShotAnalysis,
    TransientAnalyzerError,
    adaptive_frame_count,
)
from video_search.media import Segment, probe_video


COMPACT_RETRY_INSTRUCTION = """

上一次输出未通过结构或内容质量校验。请将多帧合并为一个连续镜头，不要逐帧重复描述。
这次必须输出紧凑 JSON：events 最多 3 项，objects 最多 8 项，总长度不超过 1800 个中文字符。
who、events、objects 必须是 JSON 数组，数组每一项必须是 JSON 对象，禁止使用字符串、嵌套数组或 null。
如果画面没有人物，who 可以为空数组，但仍要描述环境变化和摄影机运动。
"""

DEFAULT_MAGE_MAX_LONG_EDGE = 896
ANALYSIS_SUMMARY_PLACEHOLDERS = (
    "一到两句完整描述，必须包含主要人物、场景和动态内容",
)
ANALYSIS_UNKNOWN_VALUES = frozenset(
    {"无法判断", "未知", "不确定", "unknown", "n/a", "na"}
)


def extract_uniform_frames(
    video_path: Path,
    requested_count: int,
    output_directory: Path,
    *,
    segment: Segment | None = None,
    max_long_edge: int = DEFAULT_MAGE_MAX_LONG_EDGE,
    timeout_seconds: float = 300,
) -> list[Path]:
    if requested_count < 1:
        raise ValueError("requested_count must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if segment is None:
        segment = Segment(
            0,
            probe_video(video_path, timeout_seconds=timeout_seconds).duration_ms,
        )
    duration_ms = segment.end_ms - segment.start_ms
    if duration_ms <= 0:
        raise ValueError("segment end must be after start")
    count = requested_count
    frame_rate = count * 1_000 / duration_ms
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_pattern = output_directory / "frame-%03d.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{segment.start_ms / 1_000:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration_ms / 1_000:.3f}",
            "-vf",
            (
                f"fps={frame_rate:.12g},"
                f"scale='if(gte(iw,ih),min({max_long_edge},iw),-2)':"
                f"'if(gte(iw,ih),-2,min({max_long_edge},ih))'"
            ),
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(count),
            "-q:v",
            "2",
            "-y",
            str(output_pattern),
        ],
        check=True,
        timeout=timeout_seconds,
    )
    frames = sorted(output_directory.glob("frame-*.jpg"))
    if len(frames) != count:
        raise ValueError(f"expected {count} sampled frames, got {len(frames)}")
    return frames


def _analysis_payload(content: str) -> dict[str, object]:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Mage-VL response did not contain a JSON object")
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError("Mage-VL response contained invalid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Mage-VL analysis must be a JSON object")
    return payload


def _analysis_text_values(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [
            text
            for child in value.values()
            for text in _analysis_text_values(child)
        ]
    if isinstance(value, list):
        return [text for child in value for text in _analysis_text_values(child)]
    return []


def _validate_analysis_quality(payload: dict[str, object]) -> None:
    summary = payload.get("summary")
    if isinstance(summary, str) and any(
        placeholder in summary for placeholder in ANALYSIS_SUMMARY_PLACEHOLDERS
    ):
        raise ValueError("Mage-VL response echoed an analysis prompt placeholder")

    text_values = _analysis_text_values(payload)
    normalized_values = {
        text.strip("。.!！ ").casefold()
        for text in text_values
        if text.strip("。.!！ ")
    }
    if normalized_values and normalized_values <= ANALYSIS_UNKNOWN_VALUES:
        raise ValueError("Mage-VL response contained no meaningful visual description")


def analyze_clip(
    *,
    frame_paths: list[Path],
    prompt: str,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    max_tokens: int = 2_400,
) -> ShotAnalysis:
    if not frame_paths:
        raise ValueError("at least one frame is required")
    content: list[dict[str, object]] = []
    for path in frame_paths:
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    content.append({"type": "text", "text": prompt})
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(endpoint, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:
        response_payload = json.load(response)
    try:
        answer = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Mage-VL service returned an unexpected response") from error
    if not isinstance(answer, str):
        raise ValueError("Mage-VL response content must be text")
    try:
        payload = _analysis_payload(answer)
        analysis = ShotAnalysis.from_dict(payload)
        _validate_analysis_quality(payload)
    except ValueError as error:
        raise InvalidAnalyzerOutputError(
            str(error),
            attempts=[{"error": str(error), "raw_response": answer}],
        ) from error
    return analysis


class MageServiceAnalyzer:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        prompt: str,
        version: str,
        timeout_seconds: float = 300,
        max_tokens: int = 2_400,
        max_long_edge: int = DEFAULT_MAGE_MAX_LONG_EDGE,
        ffmpeg_timeout_seconds: float = 300,
        retry_delays: tuple[float, ...] = (10, 30, 90),
    ) -> None:
        if any(delay < 0 for delay in retry_delays):
            raise ValueError("retry delays cannot be negative")
        if max_long_edge <= 0:
            raise ValueError("max_long_edge must be positive")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.prompt = prompt
        self.version = f"{version}+max-edge-{max_long_edge}"
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.max_long_edge = max_long_edge
        self.ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self.retry_delays = tuple(retry_delays)

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        analysis, _ = self._analyze(path, segment, thumbnail_destination=None)
        return analysis

    def analyze_with_thumbnail(
        self, path: Path, segment: Segment, destination: Path
    ) -> tuple[ShotAnalysis, int]:
        return self._analyze(path, segment, thumbnail_destination=destination)

    def _analyze(
        self,
        path: Path,
        segment: Segment,
        *,
        thumbnail_destination: Path | None,
    ) -> tuple[ShotAnalysis, int | None]:
        duration_ms = segment.end_ms - segment.start_ms
        with tempfile.TemporaryDirectory(prefix="video-search-mage-frames-") as directory:
            root = Path(directory)
            frames = extract_uniform_frames(
                path,
                adaptive_frame_count(duration_ms),
                root / "primary",
                segment=segment,
                max_long_edge=self.max_long_edge,
                timeout_seconds=self.ffmpeg_timeout_seconds,
            )
            try:
                analysis = self._analyze_frames(frames, self.prompt)
            except InvalidAnalyzerOutputError as primary_error:
                compact_frames = extract_uniform_frames(
                    path,
                    4,
                    root / "compact-retry",
                    segment=segment,
                    max_long_edge=self.max_long_edge,
                    timeout_seconds=self.ffmpeg_timeout_seconds,
                )
                try:
                    analysis = self._analyze_frames(
                        compact_frames, self.prompt + COMPACT_RETRY_INSTRUCTION
                    )
                except InvalidAnalyzerOutputError as compact_error:
                    attempts = [
                        {**attempt, "mode": "primary"}
                        for attempt in primary_error.attempts
                    ]
                    attempts.extend(
                        {**attempt, "mode": "compact"}
                        for attempt in compact_error.attempts
                    )
                    raise InvalidAnalyzerOutputError(
                        "Mage-VL primary and compact responses were invalid",
                        attempts=attempts,
                    ) from compact_error
            timestamp_ms: int | None = None
            if thumbnail_destination is not None:
                representative_index = len(frames) // 2
                thumbnail_destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(frames[representative_index], thumbnail_destination)
                timestamp_ms = segment.start_ms + round(
                    (representative_index + 0.5) * duration_ms / len(frames)
                )
            return analysis, timestamp_ms

    def _analyze_frames(
        self, frame_paths: list[Path], prompt: str
    ) -> ShotAnalysis:
        last_error: BaseException | None = None
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                time.sleep(self.retry_delays[attempt - 1])
            try:
                return analyze_clip(
                    frame_paths=frame_paths,
                    prompt=prompt,
                    base_url=self.base_url,
                    model=self.model,
                    api_key=self.api_key,
                    timeout_seconds=self.timeout_seconds,
                    max_tokens=self.max_tokens,
                )
            except Exception as error:
                transient = self._is_transient_service_error(error)
                if isinstance(error, HTTPError):
                    error.close()
                if not transient:
                    raise
                last_error = error
        assert last_error is not None
        raise TransientAnalyzerError(
            f"Mage-VL service unavailable after {len(self.retry_delays) + 1} attempts: "
            f"{last_error}"
        ) from last_error

    @staticmethod
    def _is_transient_service_error(error: BaseException) -> bool:
        if isinstance(error, HTTPError):
            return error.code == 429 or 500 <= error.code < 600
        return isinstance(error, (URLError, TimeoutError, ConnectionError))
