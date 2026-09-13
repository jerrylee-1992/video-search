import base64
import io
import json
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from video_search.mage_client import (
    MageServiceAnalyzer,
    _validate_analysis_quality,
    analyze_clip,
    extract_uniform_frames,
)
from video_search.media import Segment


class MageClientTest(unittest.TestCase):
    def test_closes_transient_http_error_before_retrying(self) -> None:
        error_body = io.BytesIO(b"temporarily unavailable")
        transient = HTTPError(
            "http://127.0.0.1/v1/chat/completions",
            503,
            "temporarily unavailable",
            {},
            error_body,
        )
        recovered = type("Recovered", (), {"summary": "恢复成功"})()
        analyzer = MageServiceAnalyzer(
            base_url="http://127.0.0.1/v1",
            model="microsoft/Mage-VL",
            api_key="EMPTY",
            prompt="只输出 JSON",
            version="mage-test-v1",
            retry_delays=(0,),
        )

        with patch(
            "video_search.mage_client.extract_uniform_frames",
            return_value=[Path("frame.jpg")],
        ), patch(
            "video_search.mage_client.analyze_clip",
            side_effect=[transient, recovered],
        ):
            analysis = analyzer.analyze(Path("clip.mp4"), Segment(0, 1_000))

        self.assertEqual("恢复成功", analysis.summary)
        self.assertTrue(error_body.closed)

    def test_retries_transient_service_failures_then_returns_analysis(self) -> None:
        attempts = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                nonlocal attempts
                attempts += 1
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                if attempts < 3:
                    self.send_error(503, "temporarily unavailable")
                    return
                answer = json.dumps(
                    {
                        "summary": "人物在室内行走",
                        "who": [{"role": "人物", "count": 1}],
                        "where": {"environment": "室内"},
                        "when": {"period": "白天"},
                        "events": [
                            {
                                "sequence": 0,
                                "action": "行走",
                                "description": "人物向前行走",
                            }
                        ],
                        "objects": [],
                        "camera": {},
                    },
                    ensure_ascii=False,
                )
                body = json.dumps(
                    {"choices": [{"message": {"content": answer}}]},
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
            video = root / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=160x90:d=1:r=6",
                    "-y", str(video),
                ],
                check=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                analyzer = MageServiceAnalyzer(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="microsoft/Mage-VL",
                    api_key="EMPTY",
                    prompt="只输出 JSON",
                    version="mage-test-v1",
                    timeout_seconds=10,
                    retry_delays=(0, 0, 0),
                )
                analysis = analyzer.analyze(video, Segment(0, 1_000))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual("人物在室内行走", analysis.summary)
        self.assertEqual(3, attempts)

    def test_retries_invalid_analysis_with_four_frames_and_a_compact_prompt(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                requests.append(request)
                content = request["messages"][0]["content"]
                image_count = sum(item["type"] == "image_url" for item in content)
                prompt = content[-1]["text"]
                if image_count == 4 and "events 最多 3 项" in prompt:
                    answer = json.dumps(
                        {
                            "summary": "新人在雪山湖边拥抱",
                            "who": [{"role": "新人", "count": 2}],
                            "where": {"environment": "雪山湖边", "venue": "户外"},
                            "when": {"period": "白天", "lighting": "自然光"},
                            "events": [
                                {
                                    "sequence": 0,
                                    "subject_role": "新人",
                                    "action": "拥抱",
                                    "description": "新人面对彼此拥抱",
                                }
                            ],
                            "objects": [],
                            "camera": {"movement": "固定"},
                        },
                        ensure_ascii=False,
                    )
                else:
                    answer = '{"summary": "未闭合的 JSON"'
                body = json.dumps(
                    {"choices": [{"message": {"content": answer}}]},
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
            video = root / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=320x180:d=2:r=12",
                    "-y",
                    str(video),
                ],
                check=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                analyzer = MageServiceAnalyzer(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="microsoft/Mage-VL",
                    api_key="EMPTY",
                    prompt="只输出 JSON",
                    version="mage-test-v1",
                    timeout_seconds=10,
                )
                thumbnail = root / "thumbnail.jpg"
                analysis, timestamp_ms = analyzer.analyze_with_thumbnail(
                    video, Segment(0, 2_000), thumbnail
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            thumbnail_bytes = thumbnail.read_bytes()

        self.assertEqual("新人在雪山湖边拥抱", analysis.summary)
        self.assertTrue(thumbnail_bytes.startswith(b"\xff\xd8"))
        self.assertGreaterEqual(timestamp_ms, 0)
        self.assertLess(timestamp_ms, 2_000)
        self.assertEqual(
            [8, 4],
            [
                sum(
                    item["type"] == "image_url"
                    for item in request["messages"][0]["content"]
                )
                for request in requests
            ],
        )

    def test_retries_prompt_placeholder_analysis_with_four_frames(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                requests.append(request)
                content = request["messages"][0]["content"]
                image_count = sum(item["type"] == "image_url" for item in content)
                prompt = content[-1]["text"]
                if image_count == 4 and "内容质量校验" in prompt:
                    answer_payload = {
                        "summary": "白色兰花在画面中轻微晃动",
                        "who": [],
                        "where": {
                            "environment": "花卉特写",
                            "venue": "无法判断",
                            "background": "虚化的绿色背景",
                        },
                        "when": {"period": "白天", "lighting": "柔和自然光"},
                        "events": [
                            {
                                "sequence": 0,
                                "subject_role": "兰花",
                                "action": "晃动",
                                "description": "白色兰花随风轻微晃动",
                            }
                        ],
                        "objects": [{"name": "白色兰花", "confidence": 0.98}],
                        "camera": {"movement": "固定", "shot_size": "特写"},
                    }
                else:
                    answer_payload = {
                        "summary": "一到两句完整描述，必须包含主要人物、场景和动态内容",
                        "who": [
                            {
                                "role": "无法判断",
                                "count": 1,
                                "appearance": "无法判断",
                                "confidence": 0.0,
                            }
                        ],
                        "where": {
                            "environment": "无法判断",
                            "venue": "无法判断",
                            "background": "无法判断",
                        },
                        "when": {"period": "无法判断", "lighting": "无法判断"},
                        "events": [
                            {
                                "sequence": 0,
                                "subject_role": "无法判断",
                                "action": "无法判断",
                                "object": "无法判断",
                                "target": "无法判断",
                                "description": "无法判断",
                                "confidence": 0.0,
                            }
                        ],
                        "objects": [{"name": "无法判断", "confidence": 0.0}],
                        "camera": {
                            "movement": "无法判断",
                            "shot_size": "无法判断",
                            "viewpoint": "无法判断",
                        },
                    }
                answer = json.dumps(answer_payload, ensure_ascii=False)
                body = json.dumps(
                    {"choices": [{"message": {"content": answer}}]},
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
            video = root / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:d=2:r=12",
                    "-y", str(video),
                ],
                check=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                analyzer = MageServiceAnalyzer(
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="microsoft/Mage-VL",
                    api_key="EMPTY",
                    prompt="只输出 JSON",
                    version="mage-test-v1",
                    timeout_seconds=10,
                )
                analysis = analyzer.analyze(video, Segment(0, 2_000))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual("白色兰花在画面中轻微晃动", analysis.summary)
        self.assertEqual(
            [8, 4],
            [
                sum(
                    item["type"] == "image_url"
                    for item in request["messages"][0]["content"]
                )
                for request in requests
            ],
        )

    def test_quality_check_rejects_an_entirely_unknown_analysis(self) -> None:
        with self.assertRaisesRegex(ValueError, "no meaningful visual description"):
            _validate_analysis_quality(
                {
                    "summary": "无法判断",
                    "who": [{"role": "未知", "confidence": 0.0}],
                    "where": {"environment": "不确定"},
                    "events": [],
                }
            )

    def test_quality_check_accepts_partial_unknown_fields(self) -> None:
        _validate_analysis_quality(
            {
                "summary": "白色兰花在画面中轻微晃动",
                "who": [],
                "where": {"environment": "花卉特写", "venue": "无法判断"},
                "when": {"period": "无法判断"},
                "events": [],
            }
        )

    def test_extracts_the_requested_number_of_uniform_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=320x180:d=2:r=12",
                    "-y",
                    str(video),
                ],
                check=True,
            )

            frames = extract_uniform_frames(video, 4, root / "frames")

            self.assertEqual(4, len(frames))
            self.assertTrue(all(frame.read_bytes().startswith(b"\xff\xd8") for frame in frames))

    def test_extracted_portrait_frames_default_to_896_long_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "portrait.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=720x1440:d=1:r=4",
                    "-y",
                    str(video),
                ],
                check=True,
            )

            frames = extract_uniform_frames(video, 1, root / "frames")

            with Image.open(frames[0]) as frame:
                self.assertEqual((448, 896), frame.size)

    def test_analyzer_applies_custom_long_edge_to_sampled_frames(self) -> None:
        observed_sizes: list[tuple[int, int]] = []
        analysis = type("Analysis", (), {"summary": "测试镜头"})()

        def inspect_frames(*, frame_paths: list[Path], **_: object) -> object:
            for frame_path in frame_paths:
                with Image.open(frame_path) as frame:
                    observed_sizes.append(frame.size)
            return analysis

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "portrait.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=720x1440:d=1:r=8",
                    "-y",
                    str(video),
                ],
                check=True,
            )
            analyzer = MageServiceAnalyzer(
                base_url="http://127.0.0.1/v1",
                model="microsoft/Mage-VL",
                api_key="EMPTY",
                prompt="只输出 JSON",
                version="mage-test-v1",
                max_long_edge=640,
            )

            with patch("video_search.mage_client.analyze_clip", side_effect=inspect_frames):
                result = analyzer.analyze(video, Segment(0, 1_000))

        self.assertEqual("测试镜头", result.summary)
        self.assertEqual([(320, 640)] * 8, observed_sizes)

    def test_analyzer_version_includes_input_resolution(self) -> None:
        analyzer = MageServiceAnalyzer(
            base_url="http://127.0.0.1/v1",
            model="microsoft/Mage-VL",
            api_key="EMPTY",
            prompt="只输出 JSON",
            version="mage-test-v1",
            max_long_edge=640,
        )

        self.assertEqual("mage-test-v1+max-edge-640", analyzer.version)

    def test_analyzer_rejects_non_positive_max_long_edge(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_long_edge must be positive"):
            MageServiceAnalyzer(
                base_url="http://127.0.0.1/v1",
                model="microsoft/Mage-VL",
                api_key="EMPTY",
                prompt="只输出 JSON",
                version="mage-test-v1",
                max_long_edge=0,
            )

    def test_extracts_frames_directly_from_the_requested_source_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:d=4:r=12",
                    "-y", str(video),
                ],
                check=True,
            )

            frames = extract_uniform_frames(
                video,
                4,
                root / "range-frames",
                segment=Segment(1_000, 3_000),
            )

            self.assertEqual(4, len(frames))
            self.assertTrue(all(frame.read_bytes().startswith(b"\xff\xd8") for frame in frames))

    def test_sends_frames_and_accepts_json_inside_a_markdown_fence(self) -> None:
        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                received["_authorization"] = self.headers.get("Authorization")
                received.update(json.loads(self.rfile.read(length)))
                analysis = {
                    "summary": "新人在草坪拥抱",
                    "who": [{"role": "新人", "count": 2}],
                    "where": {"environment": "户外草坪", "venue": "庄园"},
                    "when": {"period": "傍晚", "lighting": "自然逆光"},
                    "events": [
                        {
                            "sequence": 0,
                            "subject_role": "新人",
                            "action": "拥抱",
                            "description": "新人面对彼此拥抱",
                        }
                    ],
                    "objects": [],
                    "camera": {"movement": "缓慢推进"},
                }
                content = "```json\n" + json.dumps(analysis, ensure_ascii=False) + "\n```"
                body = json.dumps(
                    {"choices": [{"message": {"content": content}}]},
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
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8test\xff\xd9")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                analysis = analyze_clip(
                    frame_paths=[frame],
                    prompt="只输出 JSON",
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    model="microsoft/Mage-VL",
                    api_key="EMPTY",
                    timeout_seconds=10,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual("傍晚", analysis.when_period)
        self.assertEqual("拥抱", analysis.events[0]["action"])
        message_content = received["messages"][0]["content"]
        self.assertEqual("image_url", message_content[0]["type"])
        encoded = message_content[0]["image_url"]["url"].split(",", 1)[1]
        self.assertEqual(b"\xff\xd8test\xff\xd9", base64.b64decode(encoded))
        self.assertEqual("只输出 JSON", message_content[-1]["text"])
        self.assertEqual(2_400, received["max_tokens"])
        self.assertIsNone(received["_authorization"])


if __name__ == "__main__":
    unittest.main()
