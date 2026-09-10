from __future__ import annotations

import json
import fcntl
import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    segmentation_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    source_root TEXT,
                    relative_path TEXT,
                    project_name TEXT,
                    event_date TEXT,
                    media_type TEXT,
                    edit_version TEXT,
                    metadata_search_text TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS shots (
                    id INTEGER PRIMARY KEY,
                    identity_token TEXT,
                    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                    shot_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    when_period TEXT,
                    lighting TEXT,
                    environment TEXT,
                    venue TEXT,
                    analysis_json TEXT NOT NULL,
                    analysis_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(video_id, shot_index),
                    CHECK(start_ms >= 0),
                    CHECK(end_ms > start_ms)
                );

                CREATE TABLE IF NOT EXISTS shot_roles (
                    id INTEGER PRIMARY KEY,
                    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    count INTEGER,
                    confidence REAL
                );

                CREATE TABLE IF NOT EXISTS shot_events (
                    id INTEGER PRIMARY KEY,
                    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    subject_role TEXT,
                    action TEXT NOT NULL,
                    object_name TEXT,
                    target TEXT,
                    description TEXT NOT NULL,
                    confidence REAL,
                    UNIQUE(shot_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS shot_objects (
                    id INTEGER PRIMARY KEY,
                    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    confidence REAL
                );

                CREATE TABLE IF NOT EXISTS shot_frames (
                    id INTEGER PRIMARY KEY,
                    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                    timestamp_ms INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    quality_score REAL,
                    UNIQUE(shot_id, timestamp_ms, kind)
                );

                CREATE TABLE IF NOT EXISTS shot_text_vectors (
                    shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
                    embedding_version TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY(shot_id, embedding_version)
                );

                CREATE TABLE IF NOT EXISTS frame_visual_vectors (
                    frame_id INTEGER NOT NULL REFERENCES shot_frames(id) ON DELETE CASCADE,
                    embedding_version TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY(frame_id, embedding_version)
                );

                CREATE TABLE IF NOT EXISTS search_sessions (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS index_jobs (
                    id INTEGER PRIMARY KEY,
                    folder TEXT NOT NULL,
                    analysis_version TEXT NOT NULL,
                    segmentation_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    completed_files INTEGER NOT NULL DEFAULT 0,
                    total_shots INTEGER NOT NULL DEFAULT 0,
                    completed_shots INTEGER NOT NULL DEFAULT 0,
                    current_path TEXT,
                    current_shot_index INTEGER,
                    current_stage TEXT,
                    error TEXT,
                    run_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(shots)")
            }
            if "identity_token" not in columns:
                connection.execute("ALTER TABLE shots ADD COLUMN identity_token TEXT")
            missing = connection.execute(
                "SELECT id FROM shots WHERE identity_token IS NULL OR identity_token = ''"
            ).fetchall()
            connection.executemany(
                "UPDATE shots SET identity_token = ? WHERE id = ?",
                [(uuid.uuid4().hex, int(row["id"])) for row in missing],
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS shots_identity_token_uq ON shots(identity_token)"
            )
            video_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(videos)")
            }
            for name in (
                "source_root", "relative_path", "project_name", "event_date",
                "media_type", "edit_version", "metadata_search_text",
            ):
                if name not in video_columns:
                    connection.execute(f"ALTER TABLE videos ADD COLUMN {name} TEXT")
            job_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(index_jobs)")
            }
            if "run_started_at" not in job_columns:
                connection.execute("ALTER TABLE index_jobs ADD COLUMN run_started_at TEXT")
                connection.execute(
                    "UPDATE index_jobs SET run_started_at = created_at WHERE run_started_at IS NULL"
                )

    @contextmanager
    def index_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".index.lock")
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("an index process is already running for this database") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create_index_job(
        self,
        *,
        folder: str,
        analysis_version: str,
        segmentation_version: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO index_jobs(folder, analysis_version, segmentation_version)
                VALUES (?, ?, ?)
                """,
                (folder, analysis_version, segmentation_version),
            )
        return int(cursor.lastrowid)

    def get_index_job(self, job_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"index job not found: {job_id}")
        return self._with_job_metrics(dict(row))

    def list_index_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM index_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._with_job_metrics(dict(row)) for row in rows]

    def update_index_job(self, job_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status", "stop_requested", "total_files", "completed_files",
            "total_shots", "completed_shots", "current_path",
            "current_shot_index", "current_stage", "error",
            "run_started_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported index job fields: {sorted(unknown)}")
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = [*fields.values(), job_id]
            with self.connect() as connection:
                cursor = connection.execute(
                    f"UPDATE index_jobs SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    values,
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"index job not found: {job_id}")
        return self.get_index_job(job_id)

    def request_index_job_stop(self, job_id: int) -> dict[str, Any]:
        self.get_index_job(job_id)
        return self.update_index_job(job_id, stop_requested=1)

    def resume_index_job(
        self,
        job_id: int,
        *,
        folder: str,
        analysis_version: str,
        segmentation_version: str,
    ) -> dict[str, Any]:
        job = self.get_index_job(job_id)
        expected = (folder, analysis_version, segmentation_version)
        actual = (
            str(job["folder"]), str(job["analysis_version"]),
            str(job["segmentation_version"]),
        )
        if actual != expected:
            raise ValueError("resume job folder or versions do not match")
        if job["status"] == "completed":
            raise ValueError("completed index job cannot be resumed")
        return self.update_index_job(
            job_id,
            status="running",
            stop_requested=0,
            completed_files=0,
            total_shots=0,
            completed_shots=0,
            current_path=None,
            current_shot_index=None,
            current_stage="resuming",
            error=None,
            run_started_at=self._utc_timestamp(),
        )

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def _with_job_metrics(cls, job: dict[str, Any]) -> dict[str, Any]:
        started = cls._parse_timestamp(job.get("run_started_at"))
        if str(job.get("status")) == "running":
            ended = datetime.now(timezone.utc)
        else:
            ended = cls._parse_timestamp(job.get("updated_at"))
        elapsed = max(0, round((ended - started).total_seconds())) if started and ended else 0
        total_files = int(job.get("total_files") or 0)
        completed_files = int(job.get("completed_files") or 0)
        total_shots = int(job.get("total_shots") or 0)
        completed_shots = int(job.get("completed_shots") or 0)
        files_observed = completed_files
        if job.get("current_path") and total_shots > 0:
            files_observed = min(total_files, completed_files + 1)
        estimated_total: int | None = None
        if total_files and files_observed:
            estimated_total = max(
                total_shots,
                round(total_shots * total_files / files_observed),
            )
        elif str(job.get("status")) == "completed":
            estimated_total = total_shots
        rate = completed_shots / elapsed if completed_shots and elapsed else None
        remaining = (
            round(max(0, estimated_total - completed_shots) / rate)
            if estimated_total is not None and rate
            else None
        )
        if str(job.get("status")) == "completed":
            remaining = 0
        job.update(
            run_elapsed_seconds=elapsed,
            shots_per_hour=round(rate * 3_600, 2) if rate else None,
            estimated_total_shots=estimated_total,
            estimated_remaining_seconds=remaining,
            progress_percent=(
                round(completed_shots * 100 / estimated_total, 2)
                if estimated_total
                else (100.0 if str(job.get("status")) == "completed" else 0.0)
            ),
        )
        return job

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    def upsert_video(
        self,
        *,
        path: str,
        fingerprint: str,
        duration_ms: int,
        segmentation_version: str,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> int:
        metadata = self._source_metadata_values(source_metadata)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO videos(
                    path, fingerprint, duration_ms, segmentation_version,
                    source_root, relative_path, project_name, event_date,
                    media_type, edit_version, metadata_search_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    duration_ms = excluded.duration_ms,
                    segmentation_version = excluded.segmentation_version,
                    source_root = COALESCE(excluded.source_root, videos.source_root),
                    relative_path = COALESCE(excluded.relative_path, videos.relative_path),
                    project_name = COALESCE(excluded.project_name, videos.project_name),
                    event_date = COALESCE(excluded.event_date, videos.event_date),
                    media_type = COALESCE(excluded.media_type, videos.media_type),
                    edit_version = COALESCE(excluded.edit_version, videos.edit_version),
                    metadata_search_text = COALESCE(excluded.metadata_search_text, videos.metadata_search_text),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (path, fingerprint, duration_ms, segmentation_version, *metadata),
            )
            row = connection.execute(
                "SELECT id FROM videos WHERE path = ?", (path,)
            ).fetchone()
        assert row is not None
        return int(row["id"])

    @staticmethod
    def _source_metadata_values(
        source_metadata: Mapping[str, Any] | None,
    ) -> tuple[Any, ...]:
        source_metadata = source_metadata or {}
        return tuple(
            source_metadata.get(name)
            for name in (
                "source_root", "relative_path", "project_name", "event_date",
                "media_type", "edit_version", "metadata_search_text",
            )
        )

    def update_video_source_metadata(
        self, video_id: int, source_metadata: Mapping[str, Any]
    ) -> None:
        metadata = self._source_metadata_values(source_metadata)
        with self.connect() as connection:
            connection.execute(
                """UPDATE videos SET source_root = ?, relative_path = ?, project_name = ?,
                   event_date = ?, media_type = ?, edit_version = ?, metadata_search_text = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (*metadata, video_id),
            )

    def insert_shot(
        self,
        *,
        video_id: int,
        shot_index: int,
        start_ms: int,
        end_ms: int,
        summary: str,
        search_text: str,
        when_period: str | None,
        lighting: str | None,
        environment: str | None,
        venue: str | None,
        analysis_json: dict[str, Any],
        analysis_version: str,
        status: str = "ready",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shots(
                    video_id, shot_index, start_ms, end_ms, summary, search_text,
                    when_period, lighting, environment, venue,
                    analysis_json, analysis_version, status
                    , identity_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    shot_index,
                    start_ms,
                    end_ms,
                    summary,
                    search_text,
                    when_period,
                    lighting,
                    environment,
                    venue,
                    json.dumps(analysis_json, ensure_ascii=False),
                    analysis_version,
                    status,
                    uuid.uuid4().hex,
                ),
            )
            return int(cursor.lastrowid)

    def get_shot(self, shot_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"shot not found: {shot_id}")
        shot = dict(row)
        shot["analysis"] = json.loads(shot.pop("analysis_json"))
        return shot

    def replace_shot_details(
        self,
        shot_id: int,
        *,
        roles: list[dict[str, Any]],
        events: list[dict[str, Any]],
        objects: list[dict[str, Any]],
    ) -> None:
        with self.connect() as connection:
            for table in ("shot_roles", "shot_events", "shot_objects"):
                connection.execute(f"DELETE FROM {table} WHERE shot_id = ?", (shot_id,))
            connection.executemany(
                """
                INSERT INTO shot_roles(shot_id, role, count, confidence)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (shot_id, item["role"], item.get("count"), item.get("confidence"))
                    for item in roles
                ],
            )
            connection.executemany(
                """
                INSERT INTO shot_events(
                    shot_id, sequence, subject_role, action, object_name,
                    target, description, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        shot_id,
                        item["sequence"],
                        item.get("subject_role"),
                        item["action"],
                        item.get("object"),
                        item.get("target"),
                        item["description"],
                        item.get("confidence"),
                    )
                    for item in events
                ],
            )
            connection.executemany(
                """
                INSERT INTO shot_objects(shot_id, name, confidence)
                VALUES (?, ?, ?)
                """,
                [
                    (shot_id, item["name"], item.get("confidence"))
                    for item in objects
                ],
            )

    def add_shot_frame(
        self,
        *,
        shot_id: int,
        timestamp_ms: int,
        path: str,
        kind: str,
        quality_score: float | None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shot_frames(shot_id, timestamp_ms, path, kind, quality_score)
                VALUES (?, ?, ?, ?, ?)
                """,
                (shot_id, timestamp_ms, path, kind, quality_score),
            )
            return int(cursor.lastrowid)

    def delete_shot_frame(self, frame_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM shot_frames WHERE id = ?", (frame_id,))

    def get_shot_details(self, shot_id: int) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            roles = [
                dict(row)
                for row in connection.execute(
                    "SELECT role, count, confidence FROM shot_roles WHERE shot_id = ? ORDER BY id",
                    (shot_id,),
                )
            ]
            events = []
            for row in connection.execute(
                """
                SELECT sequence, subject_role, action, object_name, target,
                       description, confidence
                FROM shot_events WHERE shot_id = ? ORDER BY sequence
                """,
                (shot_id,),
            ):
                item = dict(row)
                item["object"] = item.pop("object_name")
                events.append(item)
            objects = [
                dict(row)
                for row in connection.execute(
                    "SELECT name, confidence FROM shot_objects WHERE shot_id = ? ORDER BY id",
                    (shot_id,),
                )
            ]
            frames = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, timestamp_ms, path, kind, quality_score
                    FROM shot_frames WHERE shot_id = ? ORDER BY timestamp_ms
                    """,
                    (shot_id,),
                )
            ]
        return {"roles": roles, "events": events, "objects": objects, "frames": frames}

    @staticmethod
    def _pack_vector(values: list[float]) -> bytes:
        return struct.pack(f"<{len(values)}f", *values)

    @staticmethod
    def _unpack_vector(value: bytes, dimensions: int) -> list[float]:
        return list(struct.unpack(f"<{dimensions}f", value))

    def put_text_vector(
        self, *, shot_id: int, embedding_version: str, values: list[float]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO shot_text_vectors(shot_id, embedding_version, dimensions, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(shot_id, embedding_version) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    vector = excluded.vector
                """,
                (shot_id, embedding_version, len(values), self._pack_vector(values)),
            )

    def get_text_vector(self, shot_id: int, embedding_version: str) -> list[float]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT dimensions, vector FROM shot_text_vectors
                WHERE shot_id = ? AND embedding_version = ?
                """,
                (shot_id, embedding_version),
            ).fetchone()
        if row is None:
            raise KeyError(f"text vector not found for shot {shot_id}")
        return self._unpack_vector(row["vector"], row["dimensions"])

    def put_visual_vector(
        self, *, frame_id: int, embedding_version: str, values: list[float]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO frame_visual_vectors(frame_id, embedding_version, dimensions, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(frame_id, embedding_version) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    vector = excluded.vector
                """,
                (frame_id, embedding_version, len(values), self._pack_vector(values)),
            )

    def get_visual_vector(self, frame_id: int, embedding_version: str) -> list[float]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT dimensions, vector FROM frame_visual_vectors
                WHERE frame_id = ? AND embedding_version = ?
                """,
                (frame_id, embedding_version),
            ).fetchone()
        if row is None:
            raise KeyError(f"visual vector not found for frame {frame_id}")
        return self._unpack_vector(row["vector"], row["dimensions"])

    def get_video_by_path(self, path: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE path = ?", (path,)
            ).fetchone()
        return dict(row) if row is not None else None

    def video_has_analysis_version(self, video_id: int, analysis_version: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN analysis_version = ? THEN 1 ELSE 0 END) AS matching
                FROM shots WHERE video_id = ?
                """,
                (analysis_version, video_id),
            ).fetchone()
        assert row is not None
        return int(row["total"]) > 0 and int(row["matching"] or 0) == int(row["total"])

    def prepare_video_index(self, video_id: int, *, preserve_shots: bool = False) -> None:
        with self.connect() as connection:
            if not preserve_shots:
                connection.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
            connection.execute(
                "UPDATE videos SET status = 'processing', error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (video_id,),
            )

    def prepare_shot_index(
        self,
        *,
        video_id: int,
        shot_index: int,
        start_ms: int,
        end_ms: int,
        analysis_version: str,
    ) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, start_ms, end_ms, analysis_version, status
                FROM shots WHERE video_id = ? AND shot_index = ?
                """,
                (video_id, shot_index),
            ).fetchone()
            if (
                row is not None
                and row["start_ms"] == start_ms
                and row["end_ms"] == end_ms
                and row["analysis_version"] == analysis_version
                and row["status"] == "ready"
            ):
                return int(row["id"])
            if row is not None:
                connection.execute("DELETE FROM shots WHERE id = ?", (row["id"],))
        return None

    def set_video_status(self, video_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE videos SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, video_id),
            )

    def set_video_shots_status(self, video_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE shots SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE video_id = ?",
                (status, video_id),
            )

    def set_shot_status(self, shot_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE shots SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, shot_id),
            )

    def replace_video_analysis(
        self,
        *,
        path: str,
        fingerprint: str,
        duration_ms: int,
        segmentation_version: str,
        shots: list[dict[str, Any]],
        source_metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Publish a fully staged video analysis in one short transaction."""
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM videos WHERE path = ?", (path,)).fetchone()
            metadata = self._source_metadata_values(source_metadata)
            if row is None:
                cursor = connection.execute(
                    """INSERT INTO videos(
                           path, fingerprint, duration_ms, segmentation_version, status,
                           source_root, relative_path, project_name, event_date,
                           media_type, edit_version, metadata_search_text
                       ) VALUES (?, ?, ?, ?, 'processing', ?, ?, ?, ?, ?, ?, ?)""",
                    (path, fingerprint, duration_ms, segmentation_version, *metadata),
                )
                video_id = int(cursor.lastrowid)
            else:
                video_id = int(row["id"])
                connection.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
            for item in shots:
                analysis: Any = item["analysis"]
                cursor = connection.execute(
                    """INSERT INTO shots(
                           video_id, shot_index, start_ms, end_ms, summary, search_text,
                           when_period, lighting, environment, venue, analysis_json,
                           analysis_version, status, identity_token
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)""",
                    (
                        video_id, item["shot_index"], item["segment"].start_ms,
                        item["segment"].end_ms, analysis.summary,
                        item.get("search_text", analysis.search_text),
                        analysis.when_period, analysis.lighting, analysis.environment,
                        analysis.venue, json.dumps(analysis.raw, ensure_ascii=False),
                        item["analysis_version"], uuid.uuid4().hex,
                    ),
                )
                shot_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO shot_roles(shot_id, role, count, confidence) VALUES (?, ?, ?, ?)",
                    [(shot_id, value["role"], value.get("count"), value.get("confidence")) for value in analysis.roles],
                )
                connection.executemany(
                    """INSERT INTO shot_events(shot_id, sequence, subject_role, action,
                           object_name, target, description, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(shot_id, value["sequence"], value.get("subject_role"), value["action"], value.get("object"), value.get("target"), value["description"], value.get("confidence")) for value in analysis.events],
                )
                connection.executemany(
                    "INSERT INTO shot_objects(shot_id, name, confidence) VALUES (?, ?, ?)",
                    [(shot_id, value["name"], value.get("confidence")) for value in analysis.objects],
                )
                text_vector = item.get("text_vector")
                if text_vector is not None:
                    version, values = text_vector
                    connection.execute(
                        "INSERT INTO shot_text_vectors(shot_id, embedding_version, dimensions, vector) VALUES (?, ?, ?, ?)",
                        (shot_id, version, len(values), self._pack_vector(values)),
                    )
                frame = item.get("frame")
                if frame is not None:
                    cursor = connection.execute(
                        """INSERT INTO shot_frames(shot_id, timestamp_ms, path, kind, quality_score)
                           VALUES (?, ?, ?, 'thumbnail', NULL)""",
                        (shot_id, frame["timestamp_ms"], frame["path"]),
                    )
                    visual_vector = frame.get("visual_vector")
                    if visual_vector is not None:
                        version, values = visual_vector
                        connection.execute(
                            "INSERT INTO frame_visual_vectors(frame_id, embedding_version, dimensions, vector) VALUES (?, ?, ?, ?)",
                            (int(cursor.lastrowid), version, len(values), self._pack_vector(values)),
                        )
            connection.execute(
                """UPDATE videos SET fingerprint = ?, duration_ms = ?, segmentation_version = ?,
                   source_root = COALESCE(?, source_root),
                   relative_path = COALESCE(?, relative_path),
                   project_name = COALESCE(?, project_name),
                   event_date = COALESCE(?, event_date),
                   media_type = COALESCE(?, media_type),
                   edit_version = COALESCE(?, edit_version),
                   metadata_search_text = COALESCE(?, metadata_search_text),
                   status = 'ready', error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (fingerprint, duration_ms, segmentation_version, *metadata, video_id),
            )
        return video_id

    def list_shots(self, *, ready_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as connection:
            where = "WHERE shots.status = 'ready'" if ready_only else ""
            rows = connection.execute(
                f"""
                SELECT shots.*, videos.path AS video_path, videos.source_root,
                       videos.relative_path, videos.project_name, videos.event_date,
                       videos.media_type, videos.edit_version, videos.metadata_search_text
                FROM shots JOIN videos ON videos.id = shots.video_id
                {where}
                ORDER BY videos.path, shots.shot_index
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_video_shots(self, video_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shots WHERE video_id = ? ORDER BY shot_index", (video_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_search_shots(
        self,
        *,
        text_embedding_version: str | None = None,
        visual_embedding_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load every ready candidate and its scoring data from one snapshot."""
        records: list[dict[str, Any]] = []
        with self.connect() as connection:
            connection.execute("BEGIN")
            shots = connection.execute(
                """SELECT shots.*, videos.path AS video_path, videos.source_root,
                          videos.relative_path, videos.project_name, videos.event_date,
                          videos.media_type, videos.edit_version, videos.metadata_search_text
                   FROM shots JOIN videos ON videos.id = shots.video_id
                   WHERE shots.status = 'ready'
                   ORDER BY videos.path, shots.shot_index"""
            ).fetchall()
            for row in shots:
                shot = dict(row)
                shot_id = int(shot["id"])
                roles = [dict(value) for value in connection.execute(
                    "SELECT role, count, confidence FROM shot_roles WHERE shot_id = ? ORDER BY id", (shot_id,)
                )]
                events = []
                for value in connection.execute(
                    """SELECT sequence, subject_role, action, object_name, target, description, confidence
                       FROM shot_events WHERE shot_id = ? ORDER BY sequence""", (shot_id,)
                ):
                    event = dict(value); event["object"] = event.pop("object_name"); events.append(event)
                objects = [dict(value) for value in connection.execute(
                    "SELECT name, confidence FROM shot_objects WHERE shot_id = ? ORDER BY id", (shot_id,)
                )]
                frames = [dict(value) for value in connection.execute(
                    "SELECT id, timestamp_ms, path, kind, quality_score FROM shot_frames WHERE shot_id = ? ORDER BY timestamp_ms", (shot_id,)
                )]
                text_vector = None
                if text_embedding_version is not None:
                    vector_row = connection.execute(
                        "SELECT dimensions, vector FROM shot_text_vectors WHERE shot_id = ? AND embedding_version = ?",
                        (shot_id, text_embedding_version),
                    ).fetchone()
                    if vector_row is not None:
                        text_vector = self._unpack_vector(vector_row["vector"], vector_row["dimensions"])
                visual_vectors: dict[int, list[float]] = {}
                if visual_embedding_version is not None:
                    for vector_row in connection.execute(
                        """SELECT frame_visual_vectors.frame_id, dimensions, vector
                           FROM frame_visual_vectors JOIN shot_frames ON shot_frames.id = frame_visual_vectors.frame_id
                           WHERE shot_frames.shot_id = ? AND embedding_version = ?""",
                        (shot_id, visual_embedding_version),
                    ):
                        visual_vectors[int(vector_row["frame_id"])] = self._unpack_vector(
                            vector_row["vector"], vector_row["dimensions"]
                        )
                records.append({
                    "shot": shot,
                    "details": {"roles": roles, "events": events, "objects": objects, "frames": frames},
                    "text_vector": text_vector,
                    "visual_vectors": visual_vectors,
                })
        return records

    def get_shot_with_video(
        self, shot_id: int, *, identity_token: str | None = None, ready_only: bool = False
    ) -> dict[str, Any]:
        with self.connect() as connection:
            conditions = ["shots.id = ?"]
            values: list[object] = [shot_id]
            if identity_token is not None:
                conditions.append("shots.identity_token = ?")
                values.append(identity_token)
            if ready_only:
                conditions.append("shots.status = 'ready'")
            row = connection.execute(
                f"""
                SELECT shots.*, videos.path AS video_path
                FROM shots JOIN videos ON videos.id = shots.video_id
                WHERE {' AND '.join(conditions)}
                """,
                values,
            ).fetchone()
        if row is None:
            raise KeyError(f"shot not found: {shot_id}")
        return dict(row)

    def get_shot_bundle(
        self, shot_id: int, *, identity_token: str | None = None
    ) -> dict[str, Any]:
        """Read identity, analysis, and children from one SQLite snapshot."""
        with self.connect() as connection:
            connection.execute("BEGIN")
            conditions = ["shots.id = ?", "shots.status = 'ready'"]
            values: list[object] = [shot_id]
            if identity_token is not None:
                conditions.append("shots.identity_token = ?")
                values.append(identity_token)
            row = connection.execute(
                f"""SELECT shots.*, videos.path AS video_path
                    FROM shots JOIN videos ON videos.id = shots.video_id
                    WHERE {' AND '.join(conditions)}""",
                values,
            ).fetchone()
            if row is None:
                raise KeyError(f"shot not found: {shot_id}")
            shot = dict(row)
            shot["analysis"] = json.loads(shot.pop("analysis_json"))
            shot["roles"] = [dict(value) for value in connection.execute(
                "SELECT role, count, confidence FROM shot_roles WHERE shot_id = ? ORDER BY id", (shot_id,)
            )]
            events = []
            for value in connection.execute(
                """SELECT sequence, subject_role, action, object_name, target, description, confidence
                   FROM shot_events WHERE shot_id = ? ORDER BY sequence""", (shot_id,)
            ):
                event = dict(value); event["object"] = event.pop("object_name"); events.append(event)
            shot["events"] = events
            shot["objects"] = [dict(value) for value in connection.execute(
                "SELECT name, confidence FROM shot_objects WHERE shot_id = ? ORDER BY id", (shot_id,)
            )]
            shot["frames"] = [dict(value) for value in connection.execute(
                "SELECT id, timestamp_ms, path, kind, quality_score FROM shot_frames WHERE shot_id = ? ORDER BY timestamp_ms", (shot_id,)
            )]
            return shot

    def get_shot_file_path(
        self, shot_id: int, kind: str, *, identity_token: str | None = None
    ) -> Path:
        token_clause = "AND shots.identity_token = ?" if identity_token is not None else ""
        values: list[object] = [shot_id]
        if identity_token is not None:
            values.append(identity_token)
        with self.connect() as connection:
            if kind == "media":
                row = connection.execute(
                    f"""SELECT videos.path FROM shots JOIN videos ON videos.id = shots.video_id
                        WHERE shots.id = ? AND shots.status = 'ready' {token_clause}""", values
                ).fetchone()
            else:
                row = connection.execute(
                    f"""SELECT shot_frames.path FROM shots
                        JOIN shot_frames ON shot_frames.shot_id = shots.id
                        WHERE shots.id = ? AND shots.status = 'ready' {token_clause}
                          AND shot_frames.kind = 'thumbnail'
                        ORDER BY shot_frames.timestamp_ms LIMIT 1""", values
                ).fetchone()
        if row is None:
            raise KeyError(f"shot file not found: {shot_id}")
        return Path(str(row["path"]))

    def create_search_session(
        self,
        *,
        query: str,
        results: list[dict[str, object]],
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        protected_results = [dict(result) for result in results]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO search_sessions(id, query, results_json)
                VALUES (?, ?, ?)
                """,
                (
                    session_id,
                    query,
                    json.dumps(protected_results, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return self.get_search_session(session_id)

    def get_search_session(self, session_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, query, results_json, created_at
                FROM search_sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"search session not found: {session_id}")
        stored_results = json.loads(str(row["results_json"]))
        results = []
        expired_count = 0
        for result in stored_results:
            token = result.get("shot_token")
            try:
                shot = self.get_shot_with_video(
                    int(result["shot_id"]),
                    identity_token=str(token) if token is not None else None,
                    ready_only=True,
                )
            except (KeyError, TypeError, ValueError):
                expired_count += 1
                current = dict(result)
                current["expired"] = True
                results.append(current)
                continue
            if token is None:
                expired_count += 1
                current = dict(result)
                current["expired"] = True
                results.append(current)
                continue
            current = dict(result)
            current["expired"] = False
            results.append(current)
        return {
            "session_id": str(row["id"]),
            "query": str(row["query"]),
            "count": len(results),
            "available_count": len(results) - expired_count,
            "results": results,
            "expired_count": expired_count,
            "created_at": str(row["created_at"]),
        }

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            video_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM videos GROUP BY status"
            ).fetchall()
            shot_count = connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
            ready_shot_count = connection.execute(
                "SELECT COUNT(*) FROM shots WHERE status = 'ready'"
            ).fetchone()[0]
        videos = {"total": sum(int(row["count"]) for row in video_rows)}
        videos.update({str(row["status"]): int(row["count"]) for row in video_rows})
        return {"videos": videos, "shots": int(shot_count), "ready_shots": int(ready_shot_count)}
