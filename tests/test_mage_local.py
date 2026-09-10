import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from video_search.mage_local import (
    TransformersMageEngine,
    create_local_mage_server,
    environment_report,
)


class RecordingEngine:
    model_id = "microsoft/Mage-VL"

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.prompt = ""

    def generate(self, frames: list[bytes], prompt: str, max_tokens: int) -> str:
        self.frames = frames
        self.prompt = prompt
        return '{"summary":"测试返回","who":[],"where":{},"when":{},"events":[],"objects":[],"camera":{}}'


class MageLocalTest(unittest.TestCase):
    def test_transformers_engine_builds_the_official_video_prompt(self) -> None:
        engine = TransformersMageEngine.__new__(TransformersMageEngine)
        engine.device = "mps"
        engine._lock = threading.Lock()
        engine._torch = SimpleNamespace(inference_mode=nullcontext)
        image = MagicMock()
        image.convert.return_value = image
        engine._image_class = SimpleNamespace(open=MagicMock(return_value=image))
        input_ids = MagicMock()
        input_ids.shape = (1, 4)
        input_ids.to.return_value = input_ids
        pixel_values = MagicMock()
        pixel_values.to.return_value = pixel_values
        engine.processor = MagicMock()
        engine.processor.apply_chat_template.return_value = "rendered prompt"
        engine.processor.return_value = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
        }
        engine.processor.batch_decode.return_value = ["模型回答"]
        engine.model = MagicMock()
        engine.model.dtype = "float16"

        answer = engine.generate([b"jpeg"], "只输出 JSON", 300)

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "video"},
                    {"type": "text", "text": "只输出 JSON"},
                ],
            }
        ]
        engine.processor.apply_chat_template.assert_called_once_with(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        self.assertEqual("模型回答", answer)

    def test_local_server_exposes_health_and_openai_chat(self) -> None:
        engine = RecordingEngine()
        server = create_local_mage_server(("127.0.0.1", 0), engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/health") as response:
                health = json.load(response)
            body = json.dumps(
                {
                    "model": "microsoft/Mage-VL",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/jpeg;base64,/9h0ZXN0/9k="
                                    },
                                },
                                {"type": "text", "text": "只输出 JSON"},
                            ],
                        }
                    ],
                    "max_tokens": 300,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                base + "/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual("ready", health["status"])
        self.assertEqual("microsoft/Mage-VL", health["model"])
        self.assertEqual(b"\xff\xd8test\xff\xd9", engine.frames[0])
        self.assertEqual("只输出 JSON", engine.prompt)
        self.assertIn("测试返回", result["choices"][0]["message"]["content"])

    def test_environment_report_does_not_download_model_weights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = environment_report(cache_root=Path(directory))

        self.assertEqual("arm64", report["machine"])
        self.assertIn("free_disk_gb", report)
        self.assertFalse(report["model_cached"])
        self.assertEqual("microsoft/Mage-VL", report["model"])


if __name__ == "__main__":
    unittest.main()
