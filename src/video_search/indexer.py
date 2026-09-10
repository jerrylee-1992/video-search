from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
import uuid

from video_search.analysis import ShotAnalysis, TransientAnalyzerError
from video_search.database import Database
from video_search.media import (
    FFmpegSceneSegmenter,
    Segment,
    VideoMetadata,
    discover_videos,
    fingerprint,
    probe_video,
)
from video_search.source_metadata import infer_source_metadata


class Analyzer(Protocol):
    version: str

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis: ...


class Segmenter(Protocol):
    version: str

    def segment(self, path: Path, duration_ms: int) -> list[Segment]: ...


class TextEmbedder(Protocol):
    version: str

    def embed_document(self, text: str) -> list[float]: ...


class VisualEmbedder(Protocol):
    version: str

    def embed_image(self, path: Path) -> list[float]: ...


class Thumbnailer(Protocol):
    def extract(self, path: Path, segment: Segment, destination: Path) -> int: ...


class Indexer:
    def __init__(
        self,
        *,
        database: Database,
        analyzer: Analyzer,
        segmenter: Segmenter | None = None,
        probe: Callable[[Path], VideoMetadata] = probe_video,
        text_embedder: TextEmbedder | None = None,
        visual_embedder: VisualEmbedder | None = None,
        thumbnailer: Thumbnailer | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.database = database
        self.analyzer = analyzer
        self.segmenter = segmenter or FFmpegSceneSegmenter()
        self.probe = probe
        self.text_embedder = text_embedder
        self.visual_embedder = visual_embedder
        self.thumbnailer = thumbnailer
        self.cache_dir = cache_dir or database.path.parent / "frames"

    def index_folder(
        self,
        root: Path,
        *,
        job_id: int | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, int | bool]:
        self.database.initialize()
        root = Path(root)
        return self.index_paths(
            discover_videos(root), job_id=job_id, progress=progress,
            source_root=root,
        )

    def index_paths(
        self,
        videos: list[Path],
        *,
        job_id: int | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
        source_root: Path | None = None,
    ) -> dict[str, int | bool]:
        self.database.initialize()
        result: dict[str, int | bool] = {
            "discovered": len(videos),
            "indexed": 0,
            "skipped": 0,
            "failed": 0,
            "shots": 0,
        }
        completed_files = 0
        total_shots = 0
        completed_shots = 0
        if job_id is not None:
            result["paused"] = False
            self._update_job(
                job_id, progress, status="running", total_files=len(videos),
                completed_files=0, total_shots=0, completed_shots=0,
                current_stage="discover", error=None,
            )
        for path in videos:
            path = Path(path)
            source_metadata = (
                infer_source_metadata(source_root, path).as_database_values()
                if source_root is not None
                else None
            )
            metadata_search_text = str(
                (source_metadata or {}).get("metadata_search_text") or ""
            )
            if self._pause_if_requested(job_id, progress, result):
                return result
            video_id: int | None = None
            try:
                self._update_job(
                    job_id, progress, current_path=str(path), current_stage="probe",
                    current_shot_index=None,
                )
                current_fingerprint = fingerprint(path)
                existing = self.database.get_video_by_path(str(path))
                if existing is not None and source_metadata is not None:
                    self.database.update_video_source_metadata(
                        int(existing["id"]), source_metadata
                    )
                source_changed_early = (
                    existing is not None and existing["fingerprint"] != current_fingerprint
                )
                if source_changed_early:
                    video_id = int(existing["id"])
                    self.database.set_video_shots_status(video_id, "stale")
                    self.database.set_video_status(video_id, "processing")
                metadata = self.probe(path)
                same_source_and_segments = (
                    existing is not None
                    and existing["fingerprint"] == current_fingerprint
                    and existing["segmentation_version"] == self.segmenter.version
                )
                analysis_current = (
                    existing is not None
                    and self.database.video_has_analysis_version(
                        int(existing["id"]), self.analyzer.version
                    )
                )
                if (
                    existing is not None and same_source_and_segments
                    and analysis_current and existing["status"] == "ready"
                ):
                    changed = self._backfill_assets(path, int(existing["id"]))
                    result["indexed" if changed else "skipped"] += 1
                    ready_count = len(self.database.list_video_shots(int(existing["id"])))
                    total_shots += ready_count
                    completed_shots += ready_count
                    if changed:
                        result["shots"] += ready_count
                    completed_files += 1
                    self._update_job(
                        job_id, progress, completed_files=completed_files,
                        total_shots=total_shots, completed_shots=completed_shots,
                        current_stage="file-complete",
                    )
                    continue

                has_existing_shots = bool(
                    existing is not None
                    and self.database.list_video_shots(int(existing["id"]))
                )
                if existing is not None and has_existing_shots and (
                    not same_source_and_segments or not analysis_current
                ):
                    source_changed = existing["fingerprint"] != current_fingerprint
                    staged_files: list[Path] = []
                    try:
                        self._update_job(job_id, progress, current_stage="segment")
                        segments = self.segmenter.segment(path, metadata.duration_ms)
                        total_shots += len(segments)
                        self._update_job(job_id, progress, total_shots=total_shots)
                        staged = []
                        for shot_index, segment in enumerate(segments):
                            if self._pause_if_requested(job_id, progress, result):
                                for staged_file in staged_files:
                                    staged_file.unlink(missing_ok=True)
                                return result
                            self._update_job(
                                job_id, progress, current_stage="analyze",
                                current_shot_index=shot_index,
                            )
                            staged.append(
                                self._stage_shot(
                                    path, shot_index, segment, staged_files,
                                    metadata_search_text=metadata_search_text,
                                )
                            )
                            completed_shots += 1
                            self._update_job(
                                job_id, progress, completed_shots=completed_shots,
                            )
                        video_id = self.database.replace_video_analysis(
                            path=str(path), fingerprint=current_fingerprint,
                            duration_ms=metadata.duration_ms,
                            segmentation_version=self.segmenter.version, shots=staged,
                            source_metadata=source_metadata,
                        )
                    except Exception as rebuild_error:
                        for staged_file in staged_files:
                            staged_file.unlink(missing_ok=True)
                        if source_changed:
                            self.database.set_video_status(int(existing["id"]), "failed", str(rebuild_error))
                        else:
                            self.database.set_video_status(
                                int(existing["id"]),
                                str(existing["status"]),
                                f"refresh failed: {rebuild_error}",
                            )
                        raise
                    result["indexed"] += 1
                    result["shots"] += len(segments)
                    completed_files += 1
                    self._update_job(
                        job_id, progress, completed_files=completed_files,
                        current_stage="file-complete",
                    )
                    continue

                can_resume = (
                    existing is not None
                    and same_source_and_segments
                    and existing["status"] in {"failed", "processing"}
                    and self.database.video_has_analysis_version(
                        existing["id"], self.analyzer.version
                    )
                )
                video_id = self.database.upsert_video(
                    path=str(path),
                    fingerprint=current_fingerprint,
                    duration_ms=metadata.duration_ms,
                    segmentation_version=self.segmenter.version,
                    source_metadata=source_metadata,
                )
                self.database.prepare_video_index(
                    video_id, preserve_shots=can_resume
                )
                self._update_job(job_id, progress, current_stage="segment")
                segments = self.segmenter.segment(path, metadata.duration_ms)
                total_shots += len(segments)
                self._update_job(job_id, progress, total_shots=total_shots)
                for shot_index, segment in enumerate(segments):
                    if self._pause_if_requested(job_id, progress, result):
                        return result
                    self._update_job(
                        job_id, progress, current_stage="analyze",
                        current_shot_index=shot_index,
                    )
                    if can_resume:
                        reusable_id = self.database.prepare_shot_index(
                            video_id=video_id, shot_index=shot_index,
                            start_ms=segment.start_ms, end_ms=segment.end_ms,
                            analysis_version=self.analyzer.version,
                        )
                        if reusable_id is not None:
                            reusable = next(
                                shot for shot in self.database.list_video_shots(video_id)
                                if int(shot["id"]) == reusable_id
                            )
                            self._backfill_shot_assets(path, reusable)
                            completed_shots += 1
                            self._update_job(
                                job_id, progress, completed_shots=completed_shots,
                            )
                            continue
                    analysis, analyzed_frame = self._analyze_with_optional_thumbnail(
                        path, segment
                    )
                    enriched_search_text = self._enriched_search_text(
                        analysis.search_text, metadata_search_text
                    )
                    shot_id = self.database.insert_shot(
                        video_id=video_id,
                        shot_index=shot_index,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        summary=analysis.summary,
                        search_text=enriched_search_text,
                        when_period=analysis.when_period,
                        lighting=analysis.lighting,
                        environment=analysis.environment,
                        venue=analysis.venue,
                        analysis_json=analysis.raw,
                        analysis_version=self.analyzer.version,
                        status="processing",
                    )
                    self.database.replace_shot_details(
                        shot_id,
                        roles=analysis.roles,
                        events=analysis.events,
                        objects=analysis.objects,
                    )
                    if self.text_embedder is not None:
                        self.database.put_text_vector(
                            shot_id=shot_id,
                            embedding_version=self.text_embedder.version,
                            values=self.text_embedder.embed_document(enriched_search_text),
                        )
                    if analyzed_frame is not None:
                        frame_path = Path(str(analyzed_frame["path"]))
                        timestamp_ms = int(analyzed_frame["timestamp_ms"])
                        frame_id = self.database.add_shot_frame(
                            shot_id=shot_id,
                            timestamp_ms=timestamp_ms,
                            path=str(frame_path),
                            kind="thumbnail",
                            quality_score=None,
                        )
                        if self.visual_embedder is not None:
                            self.database.put_visual_vector(
                                frame_id=frame_id,
                                embedding_version=self.visual_embedder.version,
                                values=self.visual_embedder.embed_image(frame_path),
                            )
                    self.database.set_shot_status(shot_id, "ready")
                    completed_shots += 1
                    self._update_job(
                        job_id, progress, completed_shots=completed_shots,
                    )
                self.database.set_video_status(video_id, "ready")
                result["indexed"] += 1
                result["shots"] += len(segments)
                completed_files += 1
                self._update_job(
                    job_id, progress, completed_files=completed_files,
                    current_stage="file-complete",
                )
            except TransientAnalyzerError as error:
                if video_id is not None:
                    self.database.set_video_status(video_id, "failed", str(error))
                result["paused"] = True
                self._update_job(
                    job_id,
                    progress,
                    status="paused",
                    current_stage="service-unavailable",
                    error=str(error),
                )
                return result
            except Exception as error:
                if video_id is not None:
                    self.database.set_video_status(video_id, "failed", str(error))
                result["failed"] += 1
                completed_files += 1
                self._update_job(
                    job_id, progress, completed_files=completed_files,
                    current_stage="file-failed", error=str(error),
                )
        self._update_job(
            job_id,
            progress,
            status="completed" if result["failed"] == 0 else "failed",
            current_path=None,
            current_shot_index=None,
            current_stage="complete",
            error=None if result["failed"] == 0 else f"{result['failed']} file(s) failed",
        )
        return result

    def _update_job(
        self,
        job_id: int | None,
        progress: Callable[[dict[str, object]], None] | None,
        **fields: object,
    ) -> None:
        if job_id is None:
            return
        job = self.database.update_index_job(job_id, **fields)
        if progress is not None:
            progress(job)

    def _pause_if_requested(
        self,
        job_id: int | None,
        progress: Callable[[dict[str, object]], None] | None,
        result: dict[str, int | bool],
    ) -> bool:
        if job_id is None or not self.database.get_index_job(job_id)["stop_requested"]:
            return False
        result["paused"] = True
        self._update_job(
            job_id,
            progress,
            status="paused",
            current_stage="paused",
            current_shot_index=None,
        )
        return True

    def _new_thumbnail_path(self) -> Path:
        return self.cache_dir / "assets" / f"{uuid.uuid4().hex}.jpg"

    def _analyze_with_optional_thumbnail(
        self, path: Path, segment: Segment
    ) -> tuple[ShotAnalysis, dict[str, object] | None]:
        if self.thumbnailer is not None:
            analyze_with_thumbnail = getattr(self.analyzer, "analyze_with_thumbnail", None)
            if callable(analyze_with_thumbnail):
                frame_path = self._new_thumbnail_path()
                try:
                    analysis, timestamp_ms = analyze_with_thumbnail(
                        path, segment, frame_path
                    )
                except Exception:
                    frame_path.unlink(missing_ok=True)
                    raise
                return analysis, {"timestamp_ms": timestamp_ms, "path": str(frame_path)}
        analysis = self.analyzer.analyze(path, segment)
        if self.thumbnailer is None:
            return analysis, None
        frame_path = self._new_thumbnail_path()
        try:
            timestamp_ms = self.thumbnailer.extract(path, segment, frame_path)
        except Exception:
            frame_path.unlink(missing_ok=True)
            raise
        return analysis, {"timestamp_ms": timestamp_ms, "path": str(frame_path)}

    def _stage_shot(
        self,
        path: Path,
        shot_index: int,
        segment: Segment,
        staged_files: list[Path],
        *,
        metadata_search_text: str = "",
    ) -> dict[str, object]:
        analysis, frame = self._analyze_with_optional_thumbnail(path, segment)
        enriched_search_text = self._enriched_search_text(
            analysis.search_text, metadata_search_text
        )
        item: dict[str, object] = {
            "shot_index": shot_index, "segment": segment, "analysis": analysis,
            "analysis_version": self.analyzer.version,
            "search_text": enriched_search_text,
        }
        if self.text_embedder is not None:
            item["text_vector"] = (
                self.text_embedder.version,
                self.text_embedder.embed_document(enriched_search_text),
            )
        if frame is not None:
            frame_path = Path(str(frame["path"]))
            staged_files.append(frame_path)
            if self.visual_embedder is not None:
                frame["visual_vector"] = (
                    self.visual_embedder.version,
                    self.visual_embedder.embed_image(frame_path),
                )
            item["frame"] = frame
        return item

    @staticmethod
    def _enriched_search_text(analysis_text: str, metadata_text: str) -> str:
        return " ".join(value.strip() for value in (analysis_text, metadata_text) if value.strip())

    def _backfill_assets(self, path: Path, video_id: int) -> bool:
        changed = False
        for shot in self.database.list_video_shots(video_id):
            changed = self._backfill_shot_assets(path, shot) or changed
        return changed

    def _backfill_shot_assets(self, path: Path, shot: dict[str, object]) -> bool:
        changed = False
        shot_id = int(shot["id"])
        if self.text_embedder is not None:
            try:
                self.database.get_text_vector(shot_id, self.text_embedder.version)
            except KeyError:
                self.database.put_text_vector(
                    shot_id=shot_id, embedding_version=self.text_embedder.version,
                    values=self.text_embedder.embed_document(str(shot["search_text"])),
                )
                changed = True
        details = self.database.get_shot_details(shot_id)
        thumbnail = next((frame for frame in details["frames"] if frame["kind"] == "thumbnail"), None)
        if thumbnail is not None and not Path(str(thumbnail["path"])).is_file():
            self.database.delete_shot_frame(int(thumbnail["id"]))
            thumbnail = None
            changed = True
        if thumbnail is None and self.thumbnailer is not None:
            destination = self._new_thumbnail_path()
            segment = Segment(int(shot["start_ms"]), int(shot["end_ms"]))
            try:
                timestamp_ms = self.thumbnailer.extract(path, segment, destination)
                frame_id = self.database.add_shot_frame(
                    shot_id=shot_id, timestamp_ms=timestamp_ms, path=str(destination),
                    kind="thumbnail", quality_score=None,
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            thumbnail = {"id": frame_id, "path": str(destination)}
            changed = True
        if self.visual_embedder is not None and thumbnail is not None:
            try:
                self.database.get_visual_vector(int(thumbnail["id"]), self.visual_embedder.version)
            except KeyError:
                self.database.put_visual_vector(
                    frame_id=int(thumbnail["id"]), embedding_version=self.visual_embedder.version,
                    values=self.visual_embedder.embed_image(Path(str(thumbnail["path"]))),
                )
                changed = True
        return changed
