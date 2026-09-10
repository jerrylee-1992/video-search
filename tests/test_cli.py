import base64
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from video_search.cli import main
from video_search.database import Database


class CliTest(unittest.TestCase):
    def test_index_lock_conflict_does_not_create_a_phantom_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "index.sqlite3"
            database = Database(database_path)
            database.initialize()

            with database.index_lock():
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    exit_code = main(
                        [
                            "--db",
                            str(database_path),
                            "index",
                            str(root),
                            "--analyzer-command",
                            f"{sys.executable} -c pass",
                            "--analysis-version",
                            "test-v1",
                        ]
                    )

            self.assertEqual(1, exit_code)
            self.assertEqual([], database.list_index_jobs())

    def test_jobs_and_stop_commands_expose_persistent_index_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = Database(database_path)
            database.initialize()
            job_id = database.create_index_job(
                folder="/media/library",
                analysis_version="analysis-v1",
                segmentation_version="segments-v1",
            )

            stopped = self._run(["--db", str(database_path), "stop", str(job_id)])
            jobs = self._run(["--db", str(database_path), "jobs"])

            self.assertEqual(job_id, stopped["job_id"])
            self.assertTrue(stopped["stop_requested"])
            self.assertEqual(1, jobs["count"])
            self.assertEqual(job_id, jobs["jobs"][0]["id"])

    def test_scan_is_read_only_and_reports_video_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            sidecar = root / "._clip.mp4"
            database_path = root / "must-not-exist.sqlite3"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=160x90:d=1:r=6",
                    "-y", str(video),
                ],
                check=True,
            )
            sidecar.write_bytes(b"metadata")

            result = self._run(
                ["--db", str(database_path), "scan", str(root)]
            )

            self.assertEqual(1, result["discovered"])
            self.assertEqual(1, result["readable"])
            self.assertEqual(0, result["failed"])
            self.assertEqual(1, result["ignored_sidecars"])
            self.assertFalse(database_path.exists())

    def test_eval_scores_a_json_query_suite_with_the_search_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "index.sqlite3"
            suite_path = root / "queries.json"
            embed_script = root / "embed.py"
            embed_script.write_text(
                'import json, sys; json.load(sys.stdin); json.dump({"vector": [1, 0]}, sys.stdout)',
                encoding="utf-8",
            )
            database = Database(database_path)
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/eval.mov",
                fingerprint="9:10",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            target_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=5_000,
                summary="新人在湖边拥抱",
                search_text="新人 湖边 拥抱",
                when_period="白天",
                lighting="自然光",
                environment="湖边",
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            other_id = database.insert_shot(
                video_id=video_id,
                shot_index=1,
                start_ms=5_000,
                end_ms=10_000,
                summary="宾客在室内交谈",
                search_text="宾客 室内 交谈",
                when_period="夜晚",
                lighting="室内灯光",
                environment="宴会厅",
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            database.put_text_vector(
                shot_id=target_id,
                embedding_version="test-embedding-v1",
                values=[1.0, 0.0],
            )
            database.put_text_vector(
                shot_id=other_id,
                embedding_version="test-embedding-v1",
                values=[0.0, 1.0],
            )
            suite_path.write_text(
                json.dumps(
                    {
                        "version": "test-suite-v1",
                        "cases": [
                            {
                                "id": "lake-embrace",
                                "category": "action",
                                "query": "浪漫互动",
                                "expected": [
                                    {
                                        "video": "eval.mov",
                                        "start_ms": 0,
                                        "end_ms": 5_000,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = self._run(
                [
                    "--db",
                    str(database_path),
                    "eval",
                    str(suite_path),
                    "--text-embedding-command",
                    f"{sys.executable} {embed_script}",
                    "--text-embedding-version",
                    "test-embedding-v1",
                ]
            )

        self.assertEqual("test-suite-v1", result["version"])
        self.assertEqual(1, result["total"])
        self.assertEqual(1.0, result["top1_accuracy"])
        self.assertEqual(1, result["results"][0]["rank"])

    def test_mage_preflight_is_read_only_and_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                ["mage-preflight", "--cache-root", directory]
            )

        self.assertEqual("microsoft/Mage-VL", result["model"])
        self.assertFalse(result["model_cached"])
        self.assertFalse(result["downloads_performed"])

    def test_status_search_and_inspect_emit_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = Database(database_path)
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/highlight.mov",
                fingerprint="3:4",
                duration_ms=8_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=500,
                end_ms=7_500,
                summary="新娘在窗边整理头纱",
                search_text="新娘 窗边 整理头纱 清晨 柔和侧光",
                when_period="清晨",
                lighting="柔和侧光",
                environment="室内窗边",
                venue="酒店套房",
                analysis_json={"summary": "新娘在窗边整理头纱"},
                analysis_version="test-v1",
            )
            database.set_video_status(video_id, "ready")

            status = self._run(["--db", str(database_path), "status"])
            search = self._run(
                ["--db", str(database_path), "search", "整理头纱", "--limit", "5"]
            )
            inspect = self._run(
                ["--db", str(database_path), "inspect", str(shot_id)]
            )

            self.assertEqual(1, status["videos"]["ready"])
            self.assertEqual(1, status["shots"])
            self.assertEqual(shot_id, search["results"][0]["shot_id"])
            self.assertEqual(0.5, search["results"][0]["start_seconds"])
            self.assertEqual("新娘在窗边整理头纱", inspect["analysis"]["summary"])

    def test_search_can_save_a_stable_web_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "index.sqlite3"
            database = Database(database_path)
            database.initialize()
            video_id = database.upsert_video(
                path="/footage/night-dance.mov",
                fingerprint="30:40",
                duration_ms=8_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=500,
                end_ms=7_500,
                summary="夜晚户外有人跳舞",
                search_text="夜晚 户外 人群 跳舞",
                when_period="夜晚",
                lighting="彩色灯光",
                environment="户外",
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            database.replace_shot_details(
                shot_id,
                roles=[{"role": "人群", "count": 6}],
                events=[
                    {
                        "sequence": 0,
                        "subject_role": "人群",
                        "action": "跳舞",
                        "description": "人群在户外跳舞",
                    }
                ],
                objects=[],
            )

            result = self._run(
                [
                    "--db",
                    str(database_path),
                    "search",
                    "夜晚户外跳舞",
                    "--save-session",
                    "--web-base-url",
                    "http://127.0.0.1:9999/",
                ]
            )

            saved = database.get_search_session(result["session_id"])
            self.assertEqual([shot_id], [item["shot_id"] for item in saved["results"]])
            self.assertEqual(
                f"http://127.0.0.1:9999/search/session/{result['session_id']}",
                result["result_url"],
            )
            self.assertEqual([{"role": "人群", "count": 6}], result["results"][0]["who"])
            self.assertEqual(["跳舞"], result["results"][0]["actions"])

    def test_search_can_use_a_configured_text_embedding_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "index.sqlite3"
            embed_script = root / "embed.py"
            embed_script.write_text(
                'import json, sys; json.load(sys.stdin); json.dump({"vector": [1, 0]}, sys.stdout)',
                encoding="utf-8",
            )
            database = Database(database_path)
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/semantic.mov",
                fingerprint="5:6",
                duration_ms=5_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=5_000,
                summary="两个人靠近",
                search_text="两个人靠近",
                when_period=None,
                lighting=None,
                environment=None,
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            database.put_text_vector(
                shot_id=shot_id,
                embedding_version="test-embedding-v1",
                values=[1.0, 0.0],
            )

            result = self._run(
                [
                    "--db",
                    str(database_path),
                    "search",
                    "浪漫互动",
                    "--text-embedding-command",
                    f"{sys.executable} {embed_script}",
                    "--text-embedding-version",
                    "test-embedding-v1",
                ]
            )

            self.assertEqual(shot_id, result["results"][0]["shot_id"])

    def test_embed_text_backfills_existing_shots_without_rerunning_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "index.sqlite3"
            embed_script = root / "embed.py"
            embed_script.write_text(
                'import json, sys; json.load(sys.stdin); json.dump({"vector": [0.25, 0.75]}, sys.stdout)',
                encoding="utf-8",
            )
            database = Database(database_path)
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/helicopter.mov",
                fingerprint="7:8",
                duration_ms=5_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=5_000,
                summary="新人坐在直升机里",
                search_text="新人 新娘 新郎 直升机内部 微笑互动",
                when_period="白天",
                lighting="自然光",
                environment="直升机内部",
                venue=None,
                analysis_json={},
                analysis_version="mage-test-v1",
            )

            arguments = [
                "--db",
                str(database_path),
                "embed-text",
                "--text-embedding-command",
                f"{sys.executable} {embed_script}",
                "--text-embedding-version",
                "test-embedding-v1",
            ]
            try:
                first = self._run(arguments)
                second = self._run(arguments)
            except SystemExit:
                self.fail("embed-text command is not implemented")

            self.assertEqual(
                {"embedded": 1, "skipped": 0, "total": 1, "version": "test-embedding-v1"},
                first,
            )
            self.assertEqual(
                {"embedded": 0, "skipped": 1, "total": 1, "version": "test-embedding-v1"},
                second,
            )
            self.assertEqual(
                [0.25, 0.75],
                database.get_text_vector(shot_id, "test-embedding-v1"),
            )

    def test_index_can_call_an_openai_compatible_mage_service(self) -> None:
        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                received.update(json.loads(self.rfile.read(length)))
                analysis = {
                    "summary": "新娘在户外草坪挥手",
                    "who": [{"role": "新娘", "count": 1}],
                    "where": {"environment": "户外草坪", "venue": "庄园"},
                    "when": {"period": "白天", "lighting": "自然光"},
                    "events": [
                        {
                            "sequence": 0,
                            "subject_role": "新娘",
                            "action": "挥手",
                            "description": "新娘面向镜头挥手",
                        }
                    ],
                    "objects": [],
                    "camera": {},
                }
                body = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(analysis, ensure_ascii=False)}}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "one-shot.mp4"
            database_path = root / "index.sqlite3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=320x180:d=1:r=12",
                    "-y",
                    str(video),
                ],
                check=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = self._run(
                    [
                        "--db",
                        str(database_path),
                        "index",
                        str(root),
                        "--mage-base-url",
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "--mage-prompt",
                        str(
                            Path(__file__).parents[1]
                            / "prompts"
                            / "shot-analysis-v1.txt"
                        ),
                        "--mage-max-long-edge",
                        "160",
                        "--analysis-version",
                        "mage-vl+prompt-v1+schema-v1",
                    ]
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            shots = Database(database_path).list_shots()
            self.assertEqual(1, result["indexed"])
            self.assertEqual("新娘在户外草坪挥手", shots[0]["summary"])
            self.assertEqual(
                "mage-vl+prompt-v1+schema-v1+max-edge-160",
                shots[0]["analysis_version"],
            )
            content = received["messages"][0]["content"]
            self.assertEqual(8, sum(item["type"] == "image_url" for item in content))
            encoded = content[0]["image_url"]["url"].split(",", 1)[1]
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as frame:
                self.assertEqual((160, 90), frame.size)

    @staticmethod
    def _run(arguments: list[str]) -> dict[str, object]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(arguments)
        if exit_code != 0:
            raise AssertionError(f"CLI exited with {exit_code}")
        return json.loads(output.getvalue())


if __name__ == "__main__":
    unittest.main()
