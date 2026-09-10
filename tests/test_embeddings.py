import json
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from video_search import embeddings
from video_search.embeddings import CommandTextEmbedder, CommandVisualEmbedder
from video_search.media import FFmpegThumbnailer, Segment


class EmbeddingsTest(unittest.TestCase):
    def test_command_embedding_adapters_validate_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "embed.py"
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"not inspected by command")
            script.write_text(
                """
import json
import sys
request = json.load(sys.stdin)
vectors = {"text": [1, 0, 0], "image": [0, 1, 0]}
json.dump({"vector": vectors[request["kind"]]}, sys.stdout)
""".strip(),
                encoding="utf-8",
            )

            text = CommandTextEmbedder(
                [sys.executable, str(script)], version="bge-m3-command-v1"
            )
            visual = CommandVisualEmbedder(
                [sys.executable, str(script)], version="siglip2-command-v1"
            )

            self.assertEqual([1.0, 0.0, 0.0], text.embed_document("新娘挥手"))
            self.assertEqual([1.0, 0.0, 0.0], text.embed_query("浪漫互动"))
            self.assertEqual([1.0, 0.0, 0.0], visual.embed_text("白色婚纱"))
            self.assertEqual([0.0, 1.0, 0.0], visual.embed_image(image))

    def test_fastembed_adapter_uses_passage_and_query_paths(self) -> None:
        adapter_class = getattr(embeddings, "FastEmbedTextEmbedder", None)
        self.assertIsNotNone(adapter_class, "FastEmbedTextEmbedder is not implemented")

        calls: list[tuple[str, list[str]]] = []

        class FakeTextEmbedding:
            def __init__(self, *, model_name: str, cache_dir: str) -> None:
                self.model_name = model_name
                self.cache_dir = cache_dir

            def passage_embed(self, texts: list[str]):
                calls.append(("passage", texts))
                yield [1.0, 0.0]

            def query_embed(self, texts: list[str]):
                calls.append(("query", texts))
                yield [0.0, 1.0]

        fake_module = types.ModuleType("fastembed")
        fake_module.TextEmbedding = FakeTextEmbedding
        with patch.dict(sys.modules, {"fastembed": fake_module}):
            adapter = adapter_class(
                model_name="BAAI/bge-small-zh-v1.5",
                cache_dir=Path("/tmp/video-search-fastembed-test"),
            )

        self.assertEqual([1.0, 0.0], adapter.embed_document("直升机里的新人"))
        self.assertEqual([0.0, 1.0], adapter.embed_query("空中庆祝婚礼"))
        self.assertEqual(
            [
                ("passage", ["直升机里的新人"]),
                ("query", ["空中庆祝婚礼"]),
            ],
            calls,
        )
        self.assertEqual(
            "fastembed-v1:BAAI/bge-small-zh-v1.5",
            adapter.version,
        )

    def test_thumbnail_is_extracted_at_the_shot_midpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "source.mp4"
            destination = root / "frames" / "shot.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=green:s=320x180:d=2:r=25",
                    "-y",
                    str(video),
                ],
                check=True,
            )

            timestamp_ms = FFmpegThumbnailer().extract(
                video, Segment(500, 1_500), destination
            )

            self.assertEqual(1_000, timestamp_ms)
            self.assertTrue(destination.exists())
            self.assertGreater(destination.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
