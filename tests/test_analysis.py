import json
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from video_search.analysis import CommandAnalyzer, ShotAnalysis, adaptive_frame_count
from video_search.media import Segment


class ShotAnalysisTest(unittest.TestCase):
    def test_command_analyzer_applies_timeout_to_clip_and_command(self) -> None:
        payload = {
            "summary": "测试镜头",
            "who": [],
            "where": {},
            "when": {},
            "events": [],
            "objects": [],
            "camera": {},
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("video_search.analysis.temporary_clip") as clip, patch(
            "video_search.analysis.subprocess.run", return_value=completed
        ) as run:
            clip.return_value.__enter__.return_value = Path("shot.mp4")
            analyzer = CommandAnalyzer(["analyze"], version="v1", timeout_seconds=29)
            analyzer.analyze(Path("source.mp4"), Segment(0, 1_000))

        clip.assert_called_once_with(
            Path("source.mp4"), Segment(0, 1_000), timeout_seconds=29
        )
        self.assertEqual(29, run.call_args.kwargs["timeout"])

    def test_rejects_invalid_nested_fields_before_database_write(self) -> None:
        base = {
            "summary": "人物挥手",
            "who": [{"role": "人物", "count": 1, "confidence": 0.8}],
            "where": {},
            "when": {},
            "events": [{"sequence": 0, "action": "挥手", "description": "人物挥手"}],
            "objects": [{"name": "帽子", "confidence": 0.5}],
            "camera": {},
        }
        invalid = [
            ({**base, "who": [{"count": 1}]}, "who.role"),
            ({**base, "who": [{"role": "人物", "count": True}]}, "who.count"),
            ({**base, "who": [{"role": "人物", "count": -1}]}, "who.count"),
            ({**base, "objects": [{"confidence": 0.5}]}, "objects.name"),
            ({**base, "objects": [{"name": "帽子", "confidence": math.nan}]}, "objects.confidence"),
            ({**base, "events": [{"sequence": True, "action": "挥手", "description": "人物挥手"}]}, "event.sequence"),
            ({**base, "events": [{"sequence": 0, "action": "挥手", "description": "人物挥手", "confidence": 1.1}]}, "event.confidence"),
            ({**base, "events": [{"sequence": 0, "action": "挥手", "description": "人物挥手", "target": 3}]}, "event.target"),
        ]
        for payload, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                ShotAnalysis.from_dict(payload)
    def test_accepts_free_text_attributes_and_builds_dynamic_search_text(self) -> None:
        analysis = ShotAnalysis.from_dict(
            {
                "summary": "新娘在屋顶露台回头并向镜头挥手",
                "who": [{"role": "新娘", "count": 1, "confidence": 0.98}],
                "where": {
                    "environment": "半开放式屋顶露台",
                    "venue": "酒店顶层",
                },
                "when": {"period": "蓝调时刻", "lighting": "柔和环境光"},
                "events": [
                    {
                        "sequence": 0,
                        "subject_role": "新娘",
                        "action": "回头",
                        "object": None,
                        "target": "镜头",
                        "description": "新娘先回头看向镜头",
                        "confidence": 0.95,
                    },
                    {
                        "sequence": 1,
                        "subject_role": "新娘",
                        "action": "挥手",
                        "object": None,
                        "target": "镜头",
                        "description": "随后微笑挥手",
                        "confidence": 0.94,
                    },
                ],
                "objects": [{"name": "头纱", "confidence": 0.9}],
                "camera": {"movement": "缓慢推进", "shot_size": "中景"},
            }
        )

        self.assertEqual("蓝调时刻", analysis.when_period)
        self.assertEqual("半开放式屋顶露台", analysis.environment)
        self.assertIn("回头", analysis.search_text)
        self.assertIn("挥手", analysis.search_text)
        self.assertIn("缓慢推进", analysis.search_text)
        self.assertEqual([0, 1], [event["sequence"] for event in analysis.events])

    def test_rejects_event_sequence_that_is_not_contiguous(self) -> None:
        with self.assertRaisesRegex(ValueError, "event sequence"):
            ShotAnalysis.from_dict(
                {
                    "summary": "宾客鼓掌",
                    "who": [],
                    "where": {},
                    "when": {},
                    "events": [
                        {
                            "sequence": 2,
                            "action": "鼓掌",
                            "description": "宾客鼓掌",
                        }
                    ],
                    "objects": [],
                    "camera": {},
                }
            )

    def test_command_analyzer_uses_a_temporary_clip_and_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "source.mp4"
            script = root / "analyzer.py"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x180:d=1:r=25",
                    "-y",
                    str(video),
                ],
                check=True,
            )
            script.write_text(
                """
import json
import pathlib
import sys
request = json.load(sys.stdin)
clip = pathlib.Path(request["clip_path"])
assert clip.exists() and clip.stat().st_size > 0
json.dump({
    "summary": str(clip),
    "who": [],
    "where": {"environment": "室内"},
    "when": {"period": "任意自由值"},
    "events": [],
    "objects": [],
    "camera": {},
}, sys.stdout, ensure_ascii=False)
""".strip(),
                encoding="utf-8",
            )
            analyzer = CommandAnalyzer(
                [sys.executable, str(script)],
                version="mage-command-v1+prompt-v1+schema-v1",
            )

            analysis = analyzer.analyze(video, Segment(100, 900))
            temporary_clip = Path(analysis.summary)

            self.assertEqual("任意自由值", analysis.when_period)
            self.assertFalse(temporary_clip.exists())

    def test_adaptive_frame_count_grows_with_shot_duration(self) -> None:
        self.assertEqual(8, adaptive_frame_count(8_000))
        self.assertEqual(16, adaptive_frame_count(20_000))
        self.assertEqual(24, adaptive_frame_count(30_000))


if __name__ == "__main__":
    unittest.main()
