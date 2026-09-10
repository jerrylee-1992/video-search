from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Protocol

from video_search.database import Database


DEFAULT_TEXT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


class TextEmbedder(Protocol):
    version: str

    def embed_document(self, text: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _run_embedding_command(command: list[str], request: dict[str, Any]) -> list[float]:
    completed = subprocess.run(
        command,
        input=json.dumps(request, ensure_ascii=False),
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("embedding command did not return valid JSON") from error
    vector = payload.get("vector") if isinstance(payload, dict) else None
    if not isinstance(vector, list) or not vector:
        raise ValueError("embedding response must contain a non-empty vector")
    if any(not isinstance(value, (int, float)) for value in vector):
        raise ValueError("embedding vector must contain numbers")
    result = [float(value) for value in vector]
    if any(not math.isfinite(value) for value in result):
        raise ValueError("embedding vector must contain finite numbers")
    return result


class CommandTextEmbedder:
    def __init__(self, command: list[str], *, version: str) -> None:
        if not command:
            raise ValueError("embedding command is required")
        self.command = list(command)
        self.version = version

    def embed(self, text: str) -> list[float]:
        return _run_embedding_command(self.command, {"kind": "text", "text": text})

    def embed_document(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)


class FastEmbedTextEmbedder:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_TEXT_EMBEDDING_MODEL,
        cache_dir: Path | None = None,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise RuntimeError(
                "local text embedding requires: uv sync --extra text-embedding"
            ) from error
        self.model_name = model_name
        self.version = f"fastembed-v1:{model_name}"
        resolved_cache = cache_dir or Path.home() / ".cache" / "video-search" / "fastembed"
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(resolved_cache),
        )

    @staticmethod
    def _vector(values: object) -> list[float]:
        try:
            result = [float(value) for value in values]  # type: ignore[union-attr]
        except (TypeError, ValueError) as error:
            raise ValueError("embedding model returned an invalid vector") from error
        if not result or any(not math.isfinite(value) for value in result):
            raise ValueError("embedding model returned an invalid vector")
        return result

    def embed_document(self, text: str) -> list[float]:
        return self._vector(next(iter(self._model.passage_embed([text]))))

    def embed_query(self, text: str) -> list[float]:
        return self._vector(next(iter(self._model.query_embed([text]))))


def backfill_text_embeddings(
    database: Database,
    embedder: TextEmbedder,
) -> dict[str, int | str]:
    database.initialize()
    shots = database.list_shots()
    embedded = 0
    skipped = 0
    for shot in shots:
        shot_id = int(shot["id"])
        try:
            database.get_text_vector(shot_id, embedder.version)
        except KeyError:
            database.put_text_vector(
                shot_id=shot_id,
                embedding_version=embedder.version,
                values=embedder.embed_document(str(shot["search_text"])),
            )
            embedded += 1
        else:
            skipped += 1
    return {
        "embedded": embedded,
        "skipped": skipped,
        "total": len(shots),
        "version": embedder.version,
    }


class CommandVisualEmbedder:
    def __init__(self, command: list[str], *, version: str) -> None:
        if not command:
            raise ValueError("embedding command is required")
        self.command = list(command)
        self.version = version

    def embed_text(self, text: str) -> list[float]:
        return _run_embedding_command(self.command, {"kind": "text", "text": text})

    def embed_image(self, path: Path) -> list[float]:
        return _run_embedding_command(
            self.command, {"kind": "image", "image_path": str(path)}
        )
