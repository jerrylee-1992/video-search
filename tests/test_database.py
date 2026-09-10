import sqlite3
import tempfile
import unittest
from pathlib import Path

from video_search.database import Database


class DatabaseTest(unittest.TestCase):
    def test_index_job_lifecycle_is_persistent_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            job_id = database.create_index_job(
                folder="/media/library",
                analysis_version="analysis-v1",
                segmentation_version="segments-v1",
            )
            initial = database.get_index_job(job_id)
            self.assertIn("run_elapsed_seconds", initial)
            self.assertIn("estimated_total_shots", initial)
            self.assertIn("estimated_remaining_seconds", initial)
            self.assertIn("progress_percent", initial)
            self.assertIsNotNone(initial["run_started_at"])

            database.update_index_job(
                job_id,
                current_path="/media/library/a.mp4",
                current_stage="analyze",
                total_files=2,
                completed_files=0,
                total_shots=3,
                completed_shots=1,
            )
            database.request_index_job_stop(job_id)
            stopped = database.get_index_job(job_id)

            self.assertEqual("running", stopped["status"])
            self.assertEqual(1, stopped["stop_requested"])
            self.assertEqual(1, stopped["completed_shots"])
            self.assertEqual(6, stopped["estimated_total_shots"])
            self.assertEqual(16.67, stopped["progress_percent"])

            database.update_index_job(job_id, status="paused")
            resumed = database.resume_index_job(
                job_id,
                folder="/media/library",
                analysis_version="analysis-v1",
                segmentation_version="segments-v1",
            )

            self.assertEqual("running", resumed["status"])
            self.assertEqual(0, resumed["stop_requested"])
            self.assertEqual(0, resumed["completed_files"])
            self.assertEqual(job_id, database.list_index_jobs()[0]["id"])

    def test_index_lock_rejects_a_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()

            with database.index_lock():
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with database.index_lock():
                        self.fail("second lock unexpectedly acquired")

    def test_old_session_expires_if_a_reused_shot_id_points_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            session = database.create_search_session(query="拥抱", results=[{"shot_id": 1, "video_path": "/old.mp4", "start_ms": 0, "end_ms": 1000}])
            video_id = database.upsert_video(path="/new.mp4", fingerprint="2:2", duration_ms=1_000, segmentation_version="v1")
            database.set_video_status(video_id, "ready")
            database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="其它", search_text="其它", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a")
            loaded = database.get_search_session(session["session_id"])
            self.assertEqual(1, loaded["count"])
            self.assertEqual(0, loaded["available_count"])
            self.assertTrue(loaded["results"][0]["expired"])
            self.assertEqual(1, loaded["expired_count"])

    def test_initialize_migrates_old_database_and_preserves_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            database = Database(path)
            database.initialize()
            video_id = database.upsert_video(path="/old.mp4", fingerprint="1:1", duration_ms=1_000, segmentation_version="v1")
            shot_id = database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="旧镜头", search_text="旧镜头", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a")
            database.replace_shot_details(shot_id, roles=[{"role": "人物"}], events=[], objects=[])
            with database.connect() as connection:
                connection.execute("DROP INDEX IF EXISTS shots_identity_token_uq")
                connection.execute("ALTER TABLE shots DROP COLUMN identity_token")
            database.initialize()
            self.assertEqual("人物", database.get_shot_details(shot_id)["roles"][0]["role"])
            self.assertTrue(database.get_shot(shot_id)["identity_token"])
    def test_search_session_preserves_ranked_results_for_follow_up_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            ranked_results = [
                {
                    "shot_id": 42,
                    "video_path": "/footage/night-dance.mp4",
                    "start_ms": 1_500,
                    "end_ms": 6_000,
                    "summary": "夜晚户外有人跳舞",
                    "score": 0.91,
                }
            ]

            session = database.create_search_session(
                query="夜晚户外有人跳舞",
                results=ranked_results,
            )
            loaded = database.get_search_session(session["session_id"])

            self.assertEqual("夜晚户外有人跳舞", loaded["query"])
            self.assertEqual(1, loaded["count"])
            self.assertEqual([{**ranked_results[0], "expired": True}], loaded["results"])

            with self.assertRaises(KeyError):
                database.get_search_session("missing-session")

    def test_initialize_creates_search_schema_with_free_text_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")

            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/a.mov",
                fingerprint="size:100:mtime:200",
                duration_ms=10_000,
                segmentation_version="ffmpeg-scene-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=1_250,
                end_ms=8_750,
                summary="新娘在露台缓慢转身",
                search_text="新娘 露台 缓慢转身 蓝调时刻",
                when_period="蓝调时刻",
                lighting="柔和自然光",
                environment="半开放式屋顶露台",
                venue="酒店顶层",
                analysis_json={"events": [{"action": "转身"}]},
                analysis_version="mage-vl+prompt-v1+schema-v1",
            )

            shot = database.get_shot(shot_id)

            self.assertEqual("蓝调时刻", shot["when_period"])
            self.assertEqual("半开放式屋顶露台", shot["environment"])
            self.assertEqual({"events": [{"action": "转身"}]}, shot["analysis"])

            with sqlite3.connect(database.path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(shots)").fetchall()
                }
            self.assertTrue(
                {
                    "when_period",
                    "lighting",
                    "environment",
                    "venue",
                    "analysis_json",
                    "analysis_version",
                }.issubset(columns)
            )

    def test_video_source_metadata_is_migrated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/media/library/250208 Min & Sam/Ceremony.mp4",
                fingerprint="1:2",
                duration_ms=10_000,
                segmentation_version="segments-v2",
                source_metadata={
                    "source_root": "/media/library",
                    "relative_path": "250208 Min & Sam/Ceremony.mp4",
                    "project_name": "250208 Min & Sam",
                    "event_date": "2025-02-08",
                    "media_type": "ceremony",
                    "edit_version": None,
                    "metadata_search_text": "Min Sam ceremony 仪式",
                },
            )

            video = database.get_video_by_path("/media/library/250208 Min & Sam/Ceremony.mp4")

            self.assertEqual(video_id, video["id"])
            self.assertEqual("250208 Min & Sam", video["project_name"])
            self.assertEqual("2025-02-08", video["event_date"])
            self.assertEqual("ceremony", video["media_type"])

    def test_store_analysis_details_and_versioned_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/b.mp4",
                fingerprint="size:200:mtime:300",
                duration_ms=20_000,
                segmentation_version="ffmpeg-scene-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=20_000,
                summary="宾客在草坪抛洒花瓣",
                search_text="宾客 草坪 抛洒花瓣 白天",
                when_period="午后",
                lighting="逆光",
                environment="户外草坪",
                venue="庄园",
                analysis_json={},
                analysis_version="mage-vl+prompt-v1+schema-v1",
            )

            database.replace_shot_details(
                shot_id,
                roles=[{"role": "宾客", "count": 8, "confidence": 0.94}],
                events=[
                    {
                        "sequence": 0,
                        "subject_role": "宾客",
                        "action": "抛洒",
                        "object": "花瓣",
                        "target": "新人",
                        "description": "宾客向新人抛洒白色花瓣",
                        "confidence": 0.91,
                    }
                ],
                objects=[{"name": "白色花瓣", "confidence": 0.96}],
            )
            frame_id = database.add_shot_frame(
                shot_id=shot_id,
                timestamp_ms=9_500,
                path="/cache/b-shot-0.jpg",
                kind="thumbnail",
                quality_score=0.88,
            )
            database.put_text_vector(
                shot_id=shot_id,
                embedding_version="bge-m3-v1",
                values=[0.25, -0.5, 0.75],
            )
            database.put_visual_vector(
                frame_id=frame_id,
                embedding_version="siglip2-v1",
                values=[0.1, 0.2, 0.3],
            )

            details = database.get_shot_details(shot_id)

            self.assertEqual("宾客", details["roles"][0]["role"])
            self.assertEqual("抛洒", details["events"][0]["action"])
            self.assertEqual("白色花瓣", details["objects"][0]["name"])
            self.assertEqual(9_500, details["frames"][0]["timestamp_ms"])
            for expected, actual in zip(
                [0.25, -0.5, 0.75],
                database.get_text_vector(shot_id, "bge-m3-v1"),
                strict=True,
            ):
                self.assertAlmostEqual(expected, actual, places=6)
            for expected, actual in zip(
                [0.1, 0.2, 0.3],
                database.get_visual_vector(frame_id, "siglip2-v1"),
                strict=True,
            ):
                self.assertAlmostEqual(expected, actual, places=6)


if __name__ == "__main__":
    unittest.main()
