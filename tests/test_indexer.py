import tempfile
import unittest
from pathlib import Path

from video_search.analysis import ShotAnalysis, TransientAnalyzerError
from video_search.database import Database
from video_search.indexer import Indexer
from video_search.media import Segment, VideoMetadata


class FixedSegmenter:
    version = "fixed-segmenter-v1"

    def segment(self, path: Path, duration_ms: int) -> list[Segment]:
        return [Segment(0, 4_000), Segment(4_000, duration_ms)]


class RecordingAnalyzer:
    version = "mage-vl+prompt-v1+schema-v1"

    def __init__(self) -> None:
        self.calls: list[Segment] = []

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        self.calls.append(segment)
        action = "走向仪式区" if segment.start_ms == 0 else "拥抱"
        return ShotAnalysis.from_dict(
            {
                "summary": f"新人在户外草坪{action}",
                "who": [{"role": "新人", "count": 2, "confidence": 0.9}],
                "where": {"environment": "户外草坪", "venue": "湖边庄园"},
                "when": {"period": "傍晚", "lighting": "自然逆光"},
                "events": [
                    {
                        "sequence": 0,
                        "subject_role": "新人",
                        "action": action,
                        "description": f"新人{action}",
                        "confidence": 0.9,
                    }
                ],
                "objects": [],
                "camera": {"movement": "手持跟随"},
            }
        )


class FailingAnalyzer(RecordingAnalyzer):
    def __init__(self, fail_at_start_ms: int) -> None:
        super().__init__()
        self.fail_at_start_ms = fail_at_start_ms

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        if segment.start_ms == self.fail_at_start_ms:
            self.calls.append(segment)
            raise ValueError("invalid model JSON")
        return super().analyze(path, segment)


class FixedTextEmbedder:
    version = "text-embedding-v1"

    def embed_document(self, text: str) -> list[float]:
        return [1.0, 0.0]


class RecordingTextEmbedder(FixedTextEmbedder):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_document(self, text: str) -> list[float]:
        self.texts.append(text)
        return super().embed_document(text)


class FixedVisualEmbedder:
    version = "visual-embedding-v1"

    def embed_image(self, path: Path) -> list[float]:
        self.last_path = path
        return [0.0, 1.0]


class WritingThumbnailer:
    def extract(self, path: Path, segment: Segment, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"jpeg")
        return segment.start_ms + (segment.end_ms - segment.start_ms) // 2


class FailingThumbnailer(WritingThumbnailer):
    def __init__(self, fail_at_start_ms: int) -> None:
        self.fail_at_start_ms = fail_at_start_ms

    def extract(self, path: Path, segment: Segment, destination: Path) -> int:
        if segment.start_ms == self.fail_at_start_ms:
            raise ValueError("thumbnail failed")
        return super().extract(path, segment, destination)


class ArtifactAnalyzer(RecordingAnalyzer):
    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        raise AssertionError("plain analysis should not be used")

    def analyze_with_thumbnail(
        self, path: Path, segment: Segment, destination: Path
    ) -> tuple[ShotAnalysis, int]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"sampled-frame")
        analysis = RecordingAnalyzer.analyze(self, path, segment)
        return analysis, segment.start_ms + 123


class StopAfterFirstAnalyzer(RecordingAnalyzer):
    def __init__(self, database: Database, job_id: int) -> None:
        super().__init__()
        self.database = database
        self.job_id = job_id

    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        analysis = super().analyze(path, segment)
        if len(self.calls) == 1:
            self.database.request_index_job_stop(self.job_id)
        return analysis


class TemporarilyUnavailableAnalyzer(RecordingAnalyzer):
    def analyze(self, path: Path, segment: Segment) -> ShotAnalysis:
        self.calls.append(segment)
        raise TransientAnalyzerError("Mage-VL service unavailable")


class IndexerTest(unittest.TestCase):
    def test_transient_analyzer_failure_pauses_before_later_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.mp4"
            second = root / "b.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            database = Database(root / "index.sqlite3")
            database.initialize()
            job_id = database.create_index_job(
                folder=str(root),
                analysis_version=RecordingAnalyzer.version,
                segmentation_version=FixedSegmenter.version,
            )
            analyzer = TemporarilyUnavailableAnalyzer()

            result = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=analyzer,
                probe=lambda _: VideoMetadata(10_000, 1, 1),
            ).index_folder(root, job_id=job_id)

            job = database.get_index_job(job_id)
            self.assertTrue(result["paused"])
            self.assertEqual(0, result["failed"])
            self.assertEqual("paused", job["status"])
            self.assertEqual("service-unavailable", job["current_stage"])
            self.assertEqual(0, job["completed_files"])
            self.assertEqual([Segment(0, 4_000)], analyzer.calls)
            self.assertIsNone(database.get_video_by_path(str(second)))

    def test_index_folder_persists_and_embeds_source_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "素材库"
            project = root / "250208 Min & Sam Trelawn Wedding"
            project.mkdir(parents=True)
            video = project / "v2_Ceremony_MinAndSam.mp4"
            video.write_bytes(b"video")
            database = Database(Path(directory) / "index.sqlite3")
            embedder = RecordingTextEmbedder()

            Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=RecordingAnalyzer(),
                probe=lambda _: VideoMetadata(10_000, 1, 1),
                text_embedder=embedder,
            ).index_folder(root)

            stored = database.get_video_by_path(str(video))
            self.assertEqual("250208 Min & Sam Trelawn Wedding", stored["project_name"])
            self.assertEqual("2025-02-08", stored["event_date"])
            self.assertEqual("ceremony", stored["media_type"])
            self.assertEqual("v2", stored["edit_version"])
            self.assertTrue(all("Min & Sam" in text for text in embedder.texts))

    def test_job_pauses_between_shots_and_resume_reuses_completed_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "wedding.mp4"
            video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            database.initialize()
            job_id = database.create_index_job(
                folder=str(root),
                analysis_version=RecordingAnalyzer.version,
                segmentation_version=FixedSegmenter.version,
            )

            first = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=StopAfterFirstAnalyzer(database, job_id),
                probe=lambda _: VideoMetadata(10_000, 1, 1),
            ).index_folder(root, job_id=job_id)

            self.assertTrue(first["paused"])
            self.assertEqual("paused", database.get_index_job(job_id)["status"])
            self.assertEqual(1, len(database.list_shots()))

            database.resume_index_job(
                job_id,
                folder=str(root),
                analysis_version=RecordingAnalyzer.version,
                segmentation_version=FixedSegmenter.version,
            )
            resumed_analyzer = RecordingAnalyzer()
            second = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=resumed_analyzer,
                probe=lambda _: VideoMetadata(10_000, 1, 1),
            ).index_folder(root, job_id=job_id)

            self.assertFalse(second["paused"])
            self.assertEqual([Segment(4_000, 10_000)], resumed_analyzer.calls)
            self.assertEqual("completed", database.get_index_job(job_id)["status"])

    def test_reuses_analyzer_sample_as_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "a.mp4"
            video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")

            result = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=ArtifactAnalyzer(),
                probe=lambda _: VideoMetadata(10_000, 1, 1),
                thumbnailer=FailingThumbnailer(0),
                cache_dir=root / "cache",
            ).index_folder(root)

            self.assertEqual(1, result["indexed"])
            first = database.list_shots()[0]
            frame = database.get_shot_details(first["id"])["frames"][0]
            self.assertEqual(123, frame["timestamp_ms"])
            self.assertEqual(b"sampled-frame", Path(frame["path"]).read_bytes())

    def test_fingerprint_failure_is_isolated_to_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad, good = root / "a.mp4", root / "b.mp4"
            bad.write_bytes(b"bad")
            good.write_bytes(b"good")
            original_stat = Path.stat
            def probe(path: Path) -> VideoMetadata:
                return VideoMetadata(duration_ms=10_000, width=1, height=1)
            bad.unlink()
            result = Indexer(database=Database(root / "index.sqlite3"), segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=probe).index_paths([bad, good])
            self.assertEqual(1, result["failed"])
            self.assertEqual(1, result["indexed"])

    def test_unchanged_ready_shots_backfill_assets_without_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            first_analyzer = RecordingAnalyzer()
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=first_analyzer, probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            second_analyzer = RecordingAnalyzer()
            result = Indexer(database=database, segmenter=FixedSegmenter(), analyzer=second_analyzer, probe=lambda _: VideoMetadata(10_000, 1, 1), text_embedder=FixedTextEmbedder(), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache").index_folder(root)
            self.assertEqual([], second_analyzer.calls)
            self.assertEqual(1, result["indexed"])
            for shot in database.list_shots():
                self.assertEqual([1.0, 0.0], database.get_text_vector(shot["id"], "text-embedding-v1"))
                self.assertEqual(1, len(database.get_shot_details(shot["id"])["frames"]))

    def test_missing_thumbnail_file_is_regenerated_without_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: VideoMetadata(10_000, 1, 1), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache").index_folder(root)
            shot = database.list_shots()[0]
            old_frame = database.get_shot_details(shot["id"])["frames"][0]
            Path(old_frame["path"]).unlink()
            analyzer = RecordingAnalyzer()
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=analyzer, probe=lambda _: VideoMetadata(10_000, 1, 1), thumbnailer=WritingThumbnailer(), visual_embedder=FixedVisualEmbedder(), cache_dir=root / "cache").index_folder(root)
            frame = database.get_shot_details(shot["id"])["frames"][0]
            self.assertEqual([], analyzer.calls)
            self.assertNotEqual(old_frame["id"], frame["id"])
            self.assertTrue(Path(frame["path"]).is_file())
            self.assertEqual([0.0, 1.0], database.get_visual_vector(frame["id"], "visual-embedding-v1"))

    def test_changed_analysis_failure_preserves_ready_rows_vectors_and_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: VideoMetadata(10_000, 1, 1), text_embedder=FixedTextEmbedder(), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache").index_folder(root)
            before = database.list_shots(); old_frame = Path(database.get_shot_details(before[0]["id"])["frames"][0]["path"]); old_bytes = old_frame.read_bytes()
            failed = Indexer(database=database, segmenter=FixedSegmenter(), analyzer=FailingAnalyzer(0), probe=lambda _: VideoMetadata(10_000, 1, 1), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache")
            failed.analyzer.version = "analysis-v2"
            result = failed.index_folder(root)
            self.assertEqual(1, result["failed"])
            self.assertEqual([x["id"] for x in before], [x["id"] for x in database.list_shots()])
            self.assertEqual(old_bytes, old_frame.read_bytes())
            self.assertEqual([1.0, 0.0], database.get_text_vector(before[0]["id"], "text-embedding-v1"))

    def test_source_change_failure_preserves_data_but_makes_old_shots_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"old")
            database = Database(root / "index.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: VideoMetadata(10_000, 1, 1), text_embedder=FixedTextEmbedder(), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache").index_folder(root)
            before_video = database.get_video_by_path(str(video)); before = database.list_shots()
            old_frame = Path(database.get_shot_details(before[0]["id"])["frames"][0]["path"]); old_bytes = old_frame.read_bytes()
            video.write_bytes(b"new source bytes")
            failed = Indexer(database=database, segmenter=FixedSegmenter(), analyzer=FailingAnalyzer(0), probe=lambda _: VideoMetadata(10_000, 1, 1), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache")
            result = failed.index_folder(root)
            self.assertEqual(1, result["failed"])
            self.assertEqual(before_video["fingerprint"], database.get_video_by_path(str(video))["fingerprint"])
            self.assertEqual(len(before), len(database.list_shots()))
            self.assertEqual([], database.list_shots(ready_only=True))
            self.assertEqual(old_bytes, old_frame.read_bytes())

    def test_successful_version_rebuild_replaces_all_shots_and_expires_old_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            old = database.list_shots()[0]
            session = database.create_search_session(query="x", results=[{"shot_id": old["id"], "shot_token": old["identity_token"], "video_path": str(video), "start_ms": old["start_ms"], "end_ms": old["end_ms"]}])
            analyzer = RecordingAnalyzer(); analyzer.version = "analysis-v2"
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=analyzer, probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            current = database.list_shots()[0]
            self.assertNotEqual(old["identity_token"], current["identity_token"])
            loaded = database.get_search_session(session["session_id"])
            self.assertEqual(1, loaded["expired_count"])
            self.assertTrue(loaded["results"][0]["expired"])

    def test_failed_version_refresh_restores_failed_state_for_original_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "db.sqlite3")
            first = FailingAnalyzer(4_000)
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=first, probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            self.assertEqual("failed", database.get_video_by_path(str(video))["status"])
            second = FailingAnalyzer(0); second.version = "analysis-v2"
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=second, probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            self.assertEqual("failed", database.get_video_by_path(str(video))["status"])
            resumed = RecordingAnalyzer()
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=resumed, probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            self.assertEqual([Segment(4_000, 10_000)], resumed.calls)
            self.assertEqual([0, 1], [shot["shot_index"] for shot in database.list_shots()])
    def test_retries_a_shot_whose_thumbnail_pipeline_did_not_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "wedding.mp4"
            video.write_bytes(b"test video fingerprint")
            database = Database(root / "index.sqlite3")
            first_analyzer = RecordingAnalyzer()
            first = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=first_analyzer,
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
                thumbnailer=FailingThumbnailer(fail_at_start_ms=4_000),
                cache_dir=root / "cache",
            ).index_folder(root)

            resumed_analyzer = RecordingAnalyzer()
            second = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=resumed_analyzer,
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
                thumbnailer=WritingThumbnailer(),
                cache_dir=root / "cache",
            ).index_folder(root)

            self.assertEqual(1, first["failed"])
            self.assertEqual([Segment(4_000, 10_000)], resumed_analyzer.calls)
            self.assertEqual(1, second["indexed"])
            shots = database.list_shots()
            self.assertEqual(["ready", "ready"], [shot["status"] for shot in shots])
            self.assertEqual(
                [1, 1],
                [len(database.get_shot_details(shot["id"])["frames"]) for shot in shots],
            )

    def test_resumes_a_failed_video_without_reanalyzing_completed_shots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "wedding.mp4"
            video.write_bytes(b"test video fingerprint")
            database = Database(root / "index.sqlite3")
            failing_analyzer = FailingAnalyzer(fail_at_start_ms=4_000)
            first_indexer = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=failing_analyzer,
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
                text_embedder=FixedTextEmbedder(),
            )

            first = first_indexer.index_folder(root)
            completed_shot = database.list_shots()[0]
            completed_vector = database.get_text_vector(
                completed_shot["id"], "text-embedding-v1"
            )

            resumed_analyzer = RecordingAnalyzer()
            second = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=resumed_analyzer,
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
            ).index_folder(root)

            self.assertEqual(1, first["failed"])
            self.assertEqual([Segment(0, 4_000), Segment(4_000, 10_000)], failing_analyzer.calls)
            self.assertEqual([Segment(4_000, 10_000)], resumed_analyzer.calls)
            self.assertEqual(1, second["indexed"])
            self.assertEqual(2, second["shots"])
            shots = database.list_shots()
            self.assertEqual([0, 1], [shot["shot_index"] for shot in shots])
            self.assertEqual(completed_shot["id"], shots[0]["id"])
            self.assertEqual(
                completed_vector,
                database.get_text_vector(shots[0]["id"], "text-embedding-v1"),
            )
            self.assertEqual("ready", database.get_video_by_path(str(video))["status"])

    def test_resume_backfills_assets_for_reused_ready_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"video")
            database = Database(root / "db.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=FailingAnalyzer(4_000), probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            analyzer = RecordingAnalyzer()
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=analyzer, probe=lambda _: VideoMetadata(10_000, 1, 1), text_embedder=FixedTextEmbedder(), thumbnailer=WritingThumbnailer(), cache_dir=root / "cache").index_folder(root)
            self.assertEqual([Segment(4_000, 10_000)], analyzer.calls)
            first = database.list_shots()[0]
            self.assertEqual([1.0, 0.0], database.get_text_vector(first["id"], "text-embedding-v1"))
            self.assertTrue(Path(database.get_shot_details(first["id"])["frames"][0]["path"]).is_file())

    def test_source_change_is_hidden_even_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); video = root / "a.mp4"; video.write_bytes(b"old")
            database = Database(root / "db.sqlite3")
            Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: VideoMetadata(10_000, 1, 1)).index_folder(root)
            old_fingerprint = database.get_video_by_path(str(video))["fingerprint"]
            video.write_bytes(b"new and different")
            result = Indexer(database=database, segmenter=FixedSegmenter(), analyzer=RecordingAnalyzer(), probe=lambda _: (_ for _ in ()).throw(ValueError("probe failed"))).index_folder(root)
            self.assertEqual(1, result["failed"])
            self.assertEqual([], database.list_shots(ready_only=True))
            self.assertEqual(old_fingerprint, database.get_video_by_path(str(video))["fingerprint"])

    def test_indexes_each_segment_and_skips_an_unchanged_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "wedding.mp4"
            video.write_bytes(b"test video fingerprint")
            database = Database(root / "index.sqlite3")
            analyzer = RecordingAnalyzer()
            indexer = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=analyzer,
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
            )

            first = indexer.index_folder(root)
            second = indexer.index_folder(root)

            self.assertEqual(
                {"discovered": 1, "indexed": 1, "skipped": 0, "failed": 0, "shots": 2},
                first,
            )
            self.assertEqual(
                {"discovered": 1, "indexed": 0, "skipped": 1, "failed": 0, "shots": 0},
                second,
            )
            self.assertEqual(2, len(analyzer.calls))
            shots = database.list_shots()
            self.assertEqual([(0, 4_000), (4_000, 10_000)], [(s["start_ms"], s["end_ms"]) for s in shots])
            self.assertEqual("傍晚", shots[0]["when_period"])
            self.assertEqual("ready", database.get_video_by_path(str(video))["status"])

    def test_indexes_text_and_representative_frame_vectors_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "wedding.mov"
            video.write_bytes(b"video")
            database = Database(root / "index.sqlite3")
            indexer = Indexer(
                database=database,
                segmenter=FixedSegmenter(),
                analyzer=RecordingAnalyzer(),
                probe=lambda _: VideoMetadata(duration_ms=10_000, width=1920, height=1080),
                text_embedder=FixedTextEmbedder(),
                visual_embedder=FixedVisualEmbedder(),
                thumbnailer=WritingThumbnailer(),
                cache_dir=root / "cache",
            )

            result = indexer.index_folder(root)

            self.assertEqual(2, result["shots"])
            shots = database.list_shots()
            first_id = shots[0]["id"]
            self.assertEqual(
                [1.0, 0.0], database.get_text_vector(first_id, "text-embedding-v1")
            )
            frame = database.get_shot_details(first_id)["frames"][0]
            self.assertEqual("thumbnail", frame["kind"])
            self.assertTrue(Path(frame["path"]).exists())
            self.assertEqual(
                [0.0, 1.0],
                database.get_visual_vector(frame["id"], "visual-embedding-v1"),
            )


if __name__ == "__main__":
    unittest.main()
