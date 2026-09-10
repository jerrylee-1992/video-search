import tempfile
import unittest
from pathlib import Path

from video_search.database import Database
from video_search.search import HybridSearcher


class MeaningEmbedder:
    version = "test-text-v1"

    def embed_query(self, text: str) -> list[float]:
        if "浪漫互动" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


class VisualMeaningEmbedder:
    version = "test-visual-v1"

    def embed_text(self, text: str) -> list[float]:
        return [1.0, 0.0] if "白色婚纱" in text else [0.0, 1.0]


class FlatMeaningEmbedder:
    version = "test-flat-text-v1"

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


class SearchTest(unittest.TestCase):
    def test_project_and_media_type_metadata_are_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/media/250208 Min & Sam/Ceremony.mp4",
                fingerprint="1:2",
                duration_ms=5_000,
                segmentation_version="v2",
                source_metadata={
                    "source_root": "/media",
                    "relative_path": "250208 Min & Sam/Ceremony.mp4",
                    "project_name": "250208 Min & Sam",
                    "event_date": "2025-02-08",
                    "media_type": "ceremony",
                    "edit_version": None,
                    "metadata_search_text": "250208 Min Sam ceremony 仪式 2025-02-08",
                },
            )
            shot_id = database.insert_shot(
                video_id=video_id, shot_index=0, start_ms=0, end_ms=5_000,
                summary="两个人站在室内", search_text="两个人 室内 站立",
                when_period="白天", lighting=None, environment="室内", venue=None,
                analysis_json={}, analysis_version="a",
            )

            results = HybridSearcher(database).search("Min Sam ceremony")

            self.assertEqual([shot_id], [result["shot_id"] for result in results])
            self.assertEqual("250208 Min & Sam", results[0]["project_name"])
            self.assertEqual("2025-02-08", results[0]["event_date"])
            self.assertEqual("ceremony", results[0]["media_type"])
    def test_excludes_non_ready_shots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(path="/x.mp4", fingerprint="1:1", duration_ms=2_000, segmentation_version="v1")
            database.set_video_status(video_id, "ready")
            ready = database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="拥抱", search_text="拥抱", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a", status="ready")
            database.insert_shot(video_id=video_id, shot_index=1, start_ms=1_000, end_ms=2_000, summary="拥抱", search_text="拥抱", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a", status="processing")
            self.assertEqual([ready], [item["shot_id"] for item in HybridSearcher(database).search("拥抱")])

    def test_missing_semantic_vectors_do_not_return_zero_score_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(path="/x.mp4", fingerprint="1:1", duration_ms=1_000, segmentation_version="v1")
            database.set_video_status(video_id, "ready")
            database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="婚礼", search_text="婚礼", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a")
            self.assertEqual([], HybridSearcher(database, text_embedder=MeaningEmbedder()).search("火星机器人维修"))

    def test_semantic_minimum_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(path="/x.mp4", fingerprint="1:1", duration_ms=1_000, segmentation_version="v1")
            database.set_video_status(video_id, "ready")
            shot_id = database.insert_shot(video_id=video_id, shot_index=0, start_ms=0, end_ms=1_000, summary="其它", search_text="其它", when_period=None, lighting=None, environment=None, venue=None, analysis_json={}, analysis_version="a")
            database.put_text_vector(shot_id=shot_id, embedding_version="test-text-v1", values=[0.6, 0.8])
            self.assertEqual([], HybridSearcher(database, text_embedder=MeaningEmbedder(), semantic_min_score=0.7).search("浪漫互动"))
            self.assertEqual([shot_id], [x["shot_id"] for x in HybridSearcher(database, text_embedder=MeaningEmbedder(), semantic_min_score=0.5).search("浪漫互动")])
    def test_structured_events_break_semantic_ties_for_specific_actions(self) -> None:
        cases = [
            (
                "宾客列队走进婚礼现场",
                [
                    {"sequence": 0, "action": "站立观看", "description": "宾客观看婚礼"},
                ],
                [
                    {"sequence": 0, "action": "行走", "description": "新人走向宾客"},
                    {"sequence": 1, "action": "列队", "description": "宾客在草坪列队"},
                ],
            ),
            (
                "新人拥抱后走向直升机并起飞",
                [
                    {"sequence": 0, "action": "微笑", "description": "新人坐在直升机内微笑"},
                ],
                [
                    {"sequence": 0, "action": "拥抱", "description": "新人相互拥抱"},
                    {"sequence": 1, "action": "走向直升机", "description": "新人走向直升机"},
                    {"sequence": 2, "action": "起飞", "description": "直升机起飞"},
                ],
            ),
            (
                "新人闭眼依偎并轻抚对方",
                [
                    {"sequence": 0, "action": "拥抱", "description": "新人亲密互动"},
                ],
                [
                    {"sequence": 0, "action": "闭眼依偎", "description": "新人闭眼依偎"},
                    {"sequence": 1, "action": "轻抚", "description": "新郎轻抚新娘"},
                ],
            ),
            (
                "雪山湖边新人牵手后拥抱",
                [
                    {"sequence": 0, "action": "亲吻", "description": "新人在雪山湖边亲吻"},
                    {"sequence": 1, "action": "拥抱", "description": "新人随后拥抱"},
                ],
                [
                    {"sequence": 0, "action": "牵手", "description": "新人在雪山湖边牵手"},
                    {"sequence": 1, "action": "拥抱", "description": "新人随后拥抱"},
                ],
            ),
        ]

        for query, distractor_events, target_events in cases:
            with self.subTest(query=query), tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "index.sqlite3")
                database.initialize()
                video_id = database.upsert_video(
                    path="/weddings/actions.mp4",
                    fingerprint="11:12",
                    duration_ms=10_000,
                    segmentation_version="test-v1",
                )
                shot_ids = []
                for index, events in enumerate((distractor_events, target_events)):
                    shot_id = database.insert_shot(
                        video_id=video_id,
                        shot_index=index,
                        start_ms=index * 5_000,
                        end_ms=(index + 1) * 5_000,
                        summary="婚礼动作镜头",
                        search_text="新人 婚礼 动作",
                        when_period="白天",
                        lighting="自然光",
                        environment="户外",
                        venue=None,
                        analysis_json={},
                        analysis_version="test-v1",
                    )
                    database.replace_shot_details(
                        shot_id,
                        roles=[{"role": "新人", "count": 2}],
                        events=events,
                        objects=[],
                    )
                    database.put_text_vector(
                        shot_id=shot_id,
                        embedding_version="test-flat-text-v1",
                        values=[1.0, 0.0],
                    )
                    shot_ids.append(shot_id)

                results = HybridSearcher(
                    database, text_embedder=FlatMeaningEmbedder()
                ).search(query, limit=2)

                self.assertEqual(shot_ids[1], results[0]["shot_id"])

    def test_event_sequence_matching_prefers_query_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/sequence.mp4",
                fingerprint="13:14",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            shot_ids = []
            for index, actions in enumerate(
                (("拥抱", "牵手"), ("牵手", "拥抱"))
            ):
                shot_id = database.insert_shot(
                    video_id=video_id,
                    shot_index=index,
                    start_ms=index * 5_000,
                    end_ms=(index + 1) * 5_000,
                    summary="新人互动",
                    search_text="雪山湖边 新人 互动",
                    when_period="白天",
                    lighting="自然光",
                    environment="雪山湖边",
                    venue=None,
                    analysis_json={},
                    analysis_version="test-v1",
                )
                database.replace_shot_details(
                    shot_id,
                    roles=[{"role": "新人", "count": 2}],
                    events=[
                        {
                            "sequence": sequence,
                            "action": action,
                            "description": f"新人{action}",
                        }
                        for sequence, action in enumerate(actions)
                    ],
                    objects=[],
                )
                database.put_text_vector(
                    shot_id=shot_id,
                    embedding_version="test-flat-text-v1",
                    values=[1.0, 0.0],
                )
                shot_ids.append(shot_id)

            results = HybridSearcher(
                database, text_embedder=FlatMeaningEmbedder()
            ).search("新人牵手后拥抱", limit=2)

            self.assertEqual(shot_ids[1], results[0]["shot_id"])

    def test_frequent_single_action_does_not_override_better_semantic_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/portraits.mp4",
                fingerprint="15:16",
                duration_ms=35_000,
                segmentation_version="test-v1",
            )
            target_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=5_000,
                summary="新娘坐在干草覆盖的山坡上",
                search_text="新娘 坐在 干草覆盖 山坡",
                when_period="白天",
                lighting="自然光",
                environment="山坡",
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            database.replace_shot_details(
                target_id,
                roles=[{"role": "新娘", "count": 1}],
                events=[{"sequence": 0, "action": "坐", "description": "新娘坐在山坡"}],
                objects=[],
            )
            database.put_text_vector(
                shot_id=target_id,
                embedding_version="test-flat-text-v1",
                values=[1.0, 0.0],
            )
            for index in range(1, 7):
                shot_id = database.insert_shot(
                    video_id=video_id,
                    shot_index=index,
                    start_ms=index * 5_000,
                    end_ms=(index + 1) * 5_000,
                    summary="新人在婚礼上微笑",
                    search_text="新人 婚礼 微笑",
                    when_period="白天",
                    lighting="自然光",
                    environment="婚礼现场",
                    venue=None,
                    analysis_json={},
                    analysis_version="test-v1",
                )
                database.replace_shot_details(
                    shot_id,
                    roles=[{"role": "新人", "count": 2}],
                    events=[
                        {"sequence": 0, "action": "微笑", "description": "新人微笑"}
                    ],
                    objects=[],
                )
                database.put_text_vector(
                    shot_id=shot_id,
                    embedding_version="test-flat-text-v1",
                    values=[0.99, 0.141067],
                )

            results = HybridSearcher(
                database, text_embedder=FlatMeaningEmbedder()
            ).search("新娘坐在干草覆盖的山坡上微笑", limit=7)

            self.assertEqual(target_id, results[0]["shot_id"])

    def test_structured_events_can_retrieve_actions_without_an_embedder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/no-embedder.mp4",
                fingerprint="17:18",
                duration_ms=5_000,
                segmentation_version="test-v1",
            )
            shot_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=0,
                end_ms=5_000,
                summary="婚礼互动镜头",
                search_text="新人 婚礼 互动",
                when_period="白天",
                lighting="自然光",
                environment="户外",
                venue=None,
                analysis_json={},
                analysis_version="test-v1",
            )
            database.replace_shot_details(
                shot_id,
                roles=[{"role": "新人", "count": 2}],
                events=[
                    {"sequence": 0, "action": "牵手", "description": "新人牵手"},
                    {"sequence": 1, "action": "拥抱", "description": "新人拥抱"},
                ],
                objects=[],
            )

            results = HybridSearcher(database).search("新人牵手后拥抱", limit=5)

            self.assertEqual([shot_id], [result["shot_id"] for result in results])

    def test_semantic_search_returns_video_and_exact_shot_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/ceremony.mp4",
                fingerprint="10:20",
                duration_ms=20_000,
                segmentation_version="test-v1",
            )
            embrace_id = database.insert_shot(
                video_id=video_id,
                shot_index=0,
                start_ms=1_250,
                end_ms=8_750,
                summary="新人在草坪相互拥抱",
                search_text="新人 户外草坪 相互拥抱 傍晚 自然逆光",
                when_period="傍晚",
                lighting="自然逆光",
                environment="户外草坪",
                venue="湖边庄园",
                analysis_json={},
                analysis_version="test-analysis-v1",
            )
            dance_id = database.insert_shot(
                video_id=video_id,
                shot_index=1,
                start_ms=8_750,
                end_ms=15_000,
                summary="宾客在宴会厅跳舞",
                search_text="宾客 室内宴会厅 跳舞 夜晚 彩色灯光",
                when_period="夜晚",
                lighting="彩色灯光",
                environment="室内宴会厅",
                venue="湖边庄园",
                analysis_json={},
                analysis_version="test-analysis-v1",
            )
            database.replace_shot_details(
                embrace_id,
                roles=[{"role": "新人", "count": 2}],
                events=[
                    {
                        "sequence": 0,
                        "action": "拥抱",
                        "description": "新人相互拥抱",
                    }
                ],
                objects=[],
            )
            database.replace_shot_details(
                dance_id,
                roles=[{"role": "宾客", "count": 6}],
                events=[
                    {
                        "sequence": 0,
                        "action": "跳舞",
                        "description": "宾客跳舞",
                    }
                ],
                objects=[],
            )
            database.put_text_vector(
                shot_id=embrace_id,
                embedding_version="test-text-v1",
                values=[1.0, 0.0],
            )
            database.put_text_vector(
                shot_id=dance_id,
                embedding_version="test-text-v1",
                values=[0.0, 1.0],
            )

            results = HybridSearcher(database, text_embedder=MeaningEmbedder()).search(
                "浪漫互动", limit=10
            )

            self.assertEqual(embrace_id, results[0]["shot_id"])
            self.assertEqual("/weddings/ceremony.mp4", results[0]["video_path"])
            self.assertEqual(1.25, results[0]["start_seconds"])
            self.assertEqual(8.75, results[0]["end_seconds"])

    def test_free_text_filter_is_strict_without_using_enums(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/day.mp4",
                fingerprint="1:2",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            for index, period in enumerate(["蓝调时刻", "正午"]):
                database.insert_shot(
                    video_id=video_id,
                    shot_index=index,
                    start_ms=index * 5_000,
                    end_ms=(index + 1) * 5_000,
                    summary=f"{period}的婚礼镜头",
                    search_text=f"{period} 婚礼",
                    when_period=period,
                    lighting=None,
                    environment="任意环境",
                    venue=None,
                    analysis_json={},
                    analysis_version="test-v1",
                )

            results = HybridSearcher(database).search("when:蓝调时刻 婚礼", limit=10)

            self.assertEqual(1, len(results))
            self.assertEqual("蓝调时刻", results[0]["when_period"])

    def test_visual_text_search_compares_query_with_representative_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.sqlite3")
            database.initialize()
            video_id = database.upsert_video(
                path="/weddings/portraits.mov",
                fingerprint="7:8",
                duration_ms=10_000,
                segmentation_version="test-v1",
            )
            shot_ids = []
            for index, summary in enumerate(["人物站在窗前", "人物站在舞台"]):
                shot_ids.append(
                    database.insert_shot(
                        video_id=video_id,
                        shot_index=index,
                        start_ms=index * 5_000,
                        end_ms=(index + 1) * 5_000,
                        summary=summary,
                        search_text=summary,
                        when_period=None,
                        lighting=None,
                        environment=None,
                        venue=None,
                        analysis_json={},
                        analysis_version="test-v1",
                    )
                )
            for shot_id, values in zip(shot_ids, ([1.0, 0.0], [0.0, 1.0]), strict=True):
                frame_id = database.add_shot_frame(
                    shot_id=shot_id,
                    timestamp_ms=2_500,
                    path=f"/cache/{shot_id}.jpg",
                    kind="thumbnail",
                    quality_score=None,
                )
                database.put_visual_vector(
                    frame_id=frame_id,
                    embedding_version="test-visual-v1",
                    values=list(values),
                )

            results = HybridSearcher(
                database, visual_embedder=VisualMeaningEmbedder()
            ).search("白色婚纱", limit=10)

            self.assertEqual(shot_ids[0], results[0]["shot_id"])
            self.assertEqual("/cache/1.jpg", results[0]["thumbnail_path"])


if __name__ == "__main__":
    unittest.main()
