import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from video_search.media import (
    FFmpegSceneSegmenter,
    FFmpegThumbnailer,
    Segment,
    discover_videos,
    fingerprint,
    preflight_folder,
    probe_video,
)


class MediaTest(unittest.TestCase):
    def test_probe_video_applies_a_subprocess_timeout(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            '{"streams":[{"width":1920,"height":1080}],"format":{"duration":"2.5"}}',
            "",
        )
        with patch("video_search.media.subprocess.run", return_value=completed) as run:
            metadata = probe_video(Path("clip.mp4"), timeout_seconds=17)

        self.assertEqual(2_500, metadata.duration_ms)
        self.assertEqual(17, run.call_args.kwargs["timeout"])

    def test_thumbnailer_applies_a_subprocess_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "thumb.jpg"
            with patch("video_search.media.subprocess.run") as run:
                timestamp = FFmpegThumbnailer(timeout_seconds=19).extract(
                    Path("clip.mp4"), Segment(1_000, 3_000), destination
                )

        self.assertEqual(2_000, timestamp)
        self.assertEqual(19, run.call_args.kwargs["timeout"])

    def test_segmenter_version_tracks_non_default_configuration(self) -> None:
        self.assertIn("max_scene_ms=30000", FFmpegSceneSegmenter().version)
        self.assertNotEqual(
            FFmpegSceneSegmenter().version,
            FFmpegSceneSegmenter(threshold=0.4, min_scene_ms=500).version,
        )
        self.assertNotEqual(
            FFmpegSceneSegmenter(threshold=0.3200001).version,
            FFmpegSceneSegmenter(threshold=0.3200002).version,
        )

    def test_segmenter_splits_long_no_cut_video_into_contiguous_bounded_segments(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("video_search.media.subprocess.run", return_value=completed):
            segments = FFmpegSceneSegmenter(max_scene_ms=30_000).segment(
                Path("long.mp4"), 95_000
            )

        self.assertEqual(Segment(0, 23_750), segments[0])
        self.assertEqual(Segment(71_250, 95_000), segments[-1])
        self.assertEqual(4, len(segments))
        self.assertTrue(all(segment.end_ms - segment.start_ms <= 30_000 for segment in segments))
        self.assertTrue(
            all(left.end_ms == right.start_ms for left, right in zip(segments, segments[1:]))
        )

    def test_real_no_cut_video_honors_maximum_scene_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "long-no-cut.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=64x36:d=65:r=1",
                    "-y",
                    str(video),
                ],
                check=True,
            )
            metadata = probe_video(video)
            segments = FFmpegSceneSegmenter(
                threshold=0.99, max_scene_ms=30_000
            ).segment(video, metadata.duration_ms)

        self.assertEqual(3, len(segments))
        self.assertEqual(0, segments[0].start_ms)
        self.assertEqual(metadata.duration_ms, segments[-1].end_ms)
        self.assertTrue(
            all(segment.end_ms - segment.start_ms <= 30_000 for segment in segments)
        )
    def test_discovers_supported_video_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "A.MOV").touch()
            (root / "nested" / "b.mp4").touch()
            (root / "notes.txt").touch()

            paths = discover_videos(root)

            self.assertEqual([root / "A.MOV", root / "nested" / "b.mp4"], paths)
            self.assertRegex(fingerprint(paths[0]), r"^0:\d+$")

    def test_discovery_ignores_appledouble_and_hidden_tree_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / ".cache").mkdir()
            (root / "nested" / "real.mp4").write_bytes(b"video")
            (root / "nested" / "._real.mp4").write_bytes(b"metadata")
            (root / ".cache" / "hidden.mov").write_bytes(b"hidden")

            self.assertEqual([root / "nested" / "real.mp4"], discover_videos(root))

    def test_preflight_reports_readable_videos_and_isolates_probe_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.mp4"
            bad = root / "bad.mov"
            sidecar = root / "._good.mp4"
            good.write_bytes(b"1234")
            bad.write_bytes(b"bad")
            sidecar.write_bytes(b"metadata")

            def probe(path: Path):
                if path == bad:
                    raise ValueError("unreadable")
                from video_search.media import VideoMetadata
                return VideoMetadata(duration_ms=12_000, width=3840, height=2160)

            report = preflight_folder(root, probe=probe)

            self.assertEqual(2, report["discovered"])
            self.assertEqual(1, report["readable"])
            self.assertEqual(1, report["failed"])
            self.assertEqual(4, report["total_bytes"])
            self.assertEqual(12_000, report["total_duration_ms"])
            self.assertEqual(1, report["ignored_sidecars"])
            self.assertEqual(str(bad), report["errors"][0]["path"])
            self.assertEqual(1, report["estimated_shots_low"])
            self.assertEqual(4, report["estimated_shots_high"])
            self.assertEqual(512 * 1_024, report["estimated_cache_bytes_low"])
            self.assertEqual(4 * 512 * 1_024, report["estimated_cache_bytes_high"])
            self.assertGreater(report["cache_free_bytes"], 0)

    def test_probes_duration_and_segments_a_hard_cut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "cut.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:d=1:r=25",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=white:s=320x180:d=1:r=25",
                    "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                    "-map",
                    "[v]",
                    "-y",
                    str(video),
                ],
                check=True,
            )

            metadata = probe_video(video)
            segments = FFmpegSceneSegmenter(threshold=0.1, min_scene_ms=200).segment(
                video, metadata.duration_ms
            )

            self.assertEqual(320, metadata.width)
            self.assertEqual(180, metadata.height)
            self.assertGreaterEqual(metadata.duration_ms, 1_900)
            self.assertLessEqual(metadata.duration_ms, 2_100)
            self.assertEqual(2, len(segments))
            self.assertLessEqual(abs(segments[0].end_ms - 1_000), 80)
            self.assertEqual(segments[0].end_ms, segments[1].start_ms)
            self.assertEqual(metadata.duration_ms, segments[-1].end_ms)


if __name__ == "__main__":
    unittest.main()
