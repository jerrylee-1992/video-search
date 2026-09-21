import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import subprocess
import shutil
from pathlib import Path

import video_search.server as server_module
from video_search.database import Database
from video_search.server import create_server


class ServerTest(unittest.TestCase):
    def test_search_api_can_limit_results_to_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            database = Database(root / "index.sqlite3")
            database.initialize()
            shot_ids = []
            for index, folder in enumerate(("selected", "outside")):
                video_id = database.upsert_video(
                    path=str(root / folder / "clip.mp4"),
                    fingerprint=f"{index}:1",
                    duration_ms=1_000,
                    segmentation_version="v1",
                )
                shot_ids.append(
                    database.insert_shot(
                        video_id=video_id,
                        shot_index=0,
                        start_ms=0,
                        end_ms=1_000,
                        summary="人物挥手",
                        search_text="人物 挥手",
                        when_period=None,
                        lighting=None,
                        environment=None,
                        venue=None,
                        analysis_json={},
                        analysis_version="a",
                    )
                )
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            parameters = urllib.parse.urlencode(
                {"q": "人物挥手", "path": str(root / "selected")}
            )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/search?{parameters}"
                ) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(str(root / "selected"), payload["path"])
            self.assertEqual(
                [shot_ids[0]], [result["shot_id"] for result in payload["results"]]
            )

    def test_failure_page_lists_raw_results_and_serves_failed_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "failed.mp4"
            media.write_bytes(b"0123456789")
            thumbnail = root / "failed.jpg"
            thumbnail.write_bytes(b"jpeg")
            database = Database(root / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path=str(media), fingerprint="10:20", duration_ms=10_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id, shot_index=3, start_ms=2_000, end_ms=5_000,
                summary="模型分析失败", search_text="", when_period=None,
                lighting=None, environment=None, venue=None,
                analysis_json={"failure": {"error": "invalid", "attempts": [{
                    "mode": "compact", "error": "invalid JSON",
                    "raw_response": "raw model response",
                }]}},
                analysis_version="test-v1", status="failed", error="invalid",
            )
            database.add_shot_frame(
                shot_id=shot_id, timestamp_ms=3_500, path=str(thumbnail),
                kind="thumbnail", quality_score=None,
            )
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(f"{base}/api/failures") as response:
                    payload = json.load(response)
                with urllib.request.urlopen(f"{base}/failures") as response:
                    page = response.read().decode("utf-8")
                request = urllib.request.Request(
                    f"{base}/media/{shot_id}", headers={"Range": "bytes=2-5"}
                )
                with urllib.request.urlopen(request) as response:
                    media_bytes = response.read()
                with urllib.request.urlopen(f"{base}/thumbnail/{shot_id}") as response:
                    thumbnail_bytes = response.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(1, payload["count"])
            self.assertEqual("raw model response", payload["failures"][0]["analysis"]["failure"]["attempts"][0]["raw_response"])
            self.assertEqual(2.0, payload["failures"][0]["start_seconds"])
            self.assertTrue(payload["failures"][0]["thumbnail_available"])
            self.assertIn("失败镜头", page)
            self.assertEqual(b"2345", media_bytes)
            self.assertEqual(b"jpeg", thumbnail_bytes)

    def test_search_failure_returns_json_500_and_next_request_can_recover(self) -> None:
        class FailsOnceSearcher:
            def __init__(self) -> None:
                self.failed = False

            def search(self, query: str, *, limit: int) -> list[dict[str, object]]:
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("model unavailable")
                return []

        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                ("127.0.0.1", 0),
                Database(Path(directory) / "db.sqlite3"),
                FailsOnceSearcher(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(f"{base}/api/search?q=test")
                self.assertEqual(500, failure.exception.code)
                self.assertEqual(
                    {"error": "search failed", "type": "RuntimeError"},
                    json.load(failure.exception),
                )
                with urllib.request.urlopen(f"{base}/api/search?q=test") as response:
                    self.assertEqual(
                        {"query": "test", "count": 0, "results": []},
                        json.load(response),
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
    def test_invalid_search_limit_returns_json_400(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(("127.0.0.1", 0), Database(Path(directory) / "db.sqlite3"))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/search?q=x&limit=nope")
                self.assertEqual(400, failure.exception.code)
                self.assertEqual({"error": "limit must be an integer"}, json.load(failure.exception))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_latest_request_gate_rejects_stale_success_and_error(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for browser behavior test")
        subprocess.run([node, str(Path(__file__).with_name("check_web.cjs"))], check=True)

    def test_token_prevents_detail_and_media_from_following_reused_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); media = root / "x.mp4"; media.write_bytes(b"x")
            database = Database(root / "db.sqlite3"); database.initialize()
            video_id = database.upsert_video(path=str(media), fingerprint="1:1", duration_ms=1_000, segmentation_version="v1")
            database.set_video_status(video_id, "ready")
            shot_id = database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="x", search_text="x", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a")
            token = database.get_shot(shot_id)["identity_token"]
            server = create_server(("127.0.0.1", 0), database); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base}/api/shots/{shot_id}?token={token}") as response: self.assertEqual(200, response.status)
                for endpoint in ("api/shots", "media"):
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        urllib.request.urlopen(f"{base}/{endpoint}/{shot_id}?token=wrong")
                    self.assertEqual(404, missing.exception.code)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
    def test_shot_detail_api_returns_analysis_source_and_time_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/footage/bride.mov",
                fingerprint="20:30",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            analysis = {
                "summary": "新娘手持花束站在户外草地",
                "who": [
                    {
                        "role": "新娘",
                        "count": 1,
                        "appearance": "白色婚纱和长款头纱",
                        "confidence": 0.98,
                    }
                ],
                "where": {
                    "environment": "户外草地",
                    "venue": "庄园",
                    "background": "远处雪山",
                },
                "when": {"period": "白天", "lighting": "自然光"},
                "events": [
                    {
                        "sequence": 0,
                        "subject_role": "新娘",
                        "action": "站立",
                        "object": "花束",
                        "target": None,
                        "description": "新娘手持花束站立",
                        "confidence": 0.97,
                    }
                ],
                "objects": [{"name": "花束", "confidence": 0.95}],
                "camera": {
                    "shot_size": "中景",
                    "movement": "固定",
                    "viewpoint": "平视",
                },
            }
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=1_500,
                end_ms=7_500,
                summary=analysis["summary"],
                search_text="新娘 花束 户外草地",
                when_period="白天",
                lighting="自然光",
                environment="户外草地",
                venue="庄园",
                analysis_json=analysis,
                analysis_version="test-v1",
            )
            database.replace_shot_details(
                shot_id,
                roles=[{"role": "新娘", "count": 1, "confidence": 0.98}],
                events=analysis["events"],
                objects=analysis["objects"],
            )
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/shots/{shot_id}"
                ) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual("/footage/bride.mov", payload["video_path"])
            self.assertEqual(1_500, payload["start_ms"])
            self.assertEqual(7_500, payload["end_ms"])
            self.assertEqual("白色婚纱和长款头纱", payload["analysis"]["who"][0]["appearance"])
            self.assertEqual("远处雪山", payload["analysis"]["where"]["background"])
            self.assertEqual("中景", payload["analysis"]["camera"]["shot_size"])
            self.assertEqual("站立", payload["events"][0]["action"])
            self.assertEqual("花束", payload["objects"][0]["name"])

    def test_shot_detail_api_returns_not_found_for_unknown_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_port}/api/shots/999"
                    )
                error_content_type = missing.exception.headers["Content-Type"]
                error_body = missing.exception.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(404, missing.exception.code)
            self.assertEqual("application/json; charset=utf-8", error_content_type)
            self.assertEqual(
                {"error": "shot not found", "shot_id": 999},
                json.loads(error_body),
            )

    def test_keyboard_interrupt_stops_the_server_without_propagating(self) -> None:
        class InterruptedServer:
            def __init__(self) -> None:
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        interrupted = InterruptedServer()
        original_create_server = server_module.create_server
        server_module.create_server = lambda address, database, searcher=None: interrupted
        try:
            server_module.serve(Database(Path("unused.sqlite3")))
        finally:
            server_module.create_server = original_create_server

        self.assertTrue(interrupted.closed)

    def test_client_disconnect_does_not_raise_while_streaming_media(self) -> None:
        class DisconnectingWriter:
            def write(self, data: bytes) -> None:
                raise BrokenPipeError("client closed the video stream")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "large.mp4"
            media.write_bytes(b"video bytes")
            database = Database(root / "index.sqlite3")
            server = create_server(("127.0.0.1", 0), database)
            handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
            handler.headers = {}
            handler.wfile = DisconnectingWriter()
            handler.send_response = lambda status: None
            handler.send_header = lambda name, value: None
            handler.end_headers = lambda: None
            try:
                handler._send_file(media)
            finally:
                server.server_close()

    def test_serves_a_saved_search_session_and_result_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "index.sqlite3")
            database.initialize()
            session = database.create_search_session(
                query="夜晚户外跳舞",
                results=[
                    {
                        "shot_id": 7,
                        "video_path": "/footage/night.mp4",
                        "start_ms": 2_000,
                        "end_ms": 5_000,
                        "start_seconds": 2.0,
                        "end_seconds": 5.0,
                        "summary": "夜晚户外有人跳舞",
                        "when_period": "夜晚",
                        "environment": "户外",
                        "venue": None,
                        "score": 0.9,
                    }
                ],
            )
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(
                    f"{base_url}/api/search-sessions/{session['session_id']}"
                ) as response:
                    payload = json.load(response)
                with urllib.request.urlopen(
                    f"{base_url}/search/session/{session['session_id']}"
                ) as response:
                    page = response.read().decode("utf-8")
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(
                        f"{base_url}/api/search-sessions/missing-session"
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual("夜晚户外跳舞", payload["query"])
            self.assertEqual(7, payload["results"][0]["shot_id"])
            self.assertIn("镜头搜索", page)
            self.assertEqual(404, missing.exception.code)

    def test_serves_search_and_only_indexed_media_with_range_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "wedding.mp4"
            media.write_bytes(b"0123456789")
            database = Database(root / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path=str(media),
                fingerprint="10:20",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=1_000,
                end_ms=4_000,
                summary="新娘整理头纱",
                search_text="新娘 整理头纱 室内 清晨",
                when_period="清晨",
                lighting="自然光",
                environment="室内",
                venue="酒店",
                analysis_json={},
                analysis_version="test-v1",
            )
            server = create_server(("127.0.0.1", 0), database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(
                    f"{base_url}/api/search?q=%E6%95%B4%E7%90%86%E5%A4%B4%E7%BA%B1"
                ) as response:
                    payload = json.load(response)
                request = urllib.request.Request(
                    f"{base_url}/media/{shot_id}", headers={"Range": "bytes=2-5"}
                )
                with urllib.request.urlopen(request) as response:
                    media_bytes = response.read()
                    status = response.status
                    content_range = response.headers["Content-Range"]
                with urllib.request.urlopen(base_url) as response:
                    page = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(shot_id, payload["results"][0]["shot_id"])
            self.assertEqual(206, status)
            self.assertEqual("bytes 2-5/10", content_range)
            self.assertEqual(b"2345", media_bytes)
            self.assertIn("自然语言搜索", page)


if __name__ == "__main__":
    unittest.main()
