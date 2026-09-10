import unittest
from pathlib import Path

from video_search.source_metadata import infer_source_metadata


class SourceMetadataTest(unittest.TestCase):
    def test_infers_project_date_type_and_version_from_library_layout(self) -> None:
        root = Path("/media/素材库")
        path = root / "摄影师 James Hirata" / "2025年" / "250208 Min & Sam Trelawn Wedding" / "v2_HL_8 feb 2025Min And Sam.mp4"

        metadata = infer_source_metadata(root, path)

        self.assertEqual("250208 Min & Sam Trelawn Wedding", metadata.project_name)
        self.assertEqual("2025-02-08", metadata.event_date)
        self.assertEqual("highlight", metadata.media_type)
        self.assertEqual("v2", metadata.edit_version)
        self.assertEqual(str(path.relative_to(root)), metadata.relative_path)
        self.assertIn("精剪", metadata.search_text)
        self.assertNotIn("白天", metadata.search_text)

    def test_infers_ceremony_as_searchable_source_metadata(self) -> None:
        root = Path("/media/素材库")
        path = root / "250305 Odylia & Ben_Paradise Forest" / "Ceremony_250305_Odylia&Benjamin.mp4"

        metadata = infer_source_metadata(root, path)

        self.assertEqual("ceremony", metadata.media_type)
        self.assertIn("仪式", metadata.search_text)


if __name__ == "__main__":
    unittest.main()
