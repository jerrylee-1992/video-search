from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Sequence

from video_search.analysis import CommandAnalyzer
from video_search.database import Database
from video_search.embeddings import (
    DEFAULT_TEXT_EMBEDDING_MODEL,
    CommandTextEmbedder,
    CommandVisualEmbedder,
    FastEmbedTextEmbedder,
    backfill_text_embeddings,
)
from video_search.evaluation import evaluate_suite
from video_search.indexer import Indexer
from video_search.media import (
    FFmpegSceneSegmenter,
    FFmpegThumbnailer,
    preflight_folder,
    probe_video,
)
from video_search.mage_client import DEFAULT_MAGE_MAX_LONG_EDGE, MageServiceAnalyzer
from video_search.mage_local import DEFAULT_MODEL, environment_report, serve_local_mage
from video_search.search import HybridSearcher
from video_search.server import serve


DEFAULT_DATABASE = Path.home() / ".local" / "share" / "video-search" / "index.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-search")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create the local search database")

    scan = commands.add_parser("scan", help="read-only video folder preflight")
    scan.add_argument("folder", type=Path)
    scan.add_argument("--ffprobe-timeout", type=float, default=120)

    index = commands.add_parser("index", help="index videos in a folder")
    index.add_argument("folder", type=Path)
    analyzer = index.add_mutually_exclusive_group(required=True)
    analyzer.add_argument(
        "--analyzer-command",
        help="command that reads a shot request from stdin and writes analysis JSON",
    )
    analyzer.add_argument(
        "--mage-base-url",
        help="OpenAI-compatible Mage-VL service URL, usually ending in /v1",
    )
    index.add_argument("--mage-model", default="microsoft/Mage-VL")
    index.add_argument("--mage-prompt", type=Path)
    index.add_argument("--mage-timeout", type=float, default=300)
    index.add_argument("--ffmpeg-timeout", type=float, default=3_600)
    index.add_argument("--max-shot-seconds", type=float, default=30)
    index.add_argument("--resume-job", type=int)
    index.add_argument("--mage-max-tokens", type=int, default=2_400)
    index.add_argument(
        "--mage-max-long-edge",
        type=int,
        default=DEFAULT_MAGE_MAX_LONG_EDGE,
        help="maximum sampled-frame long edge in pixels (default: %(default)s)",
    )
    index.add_argument(
        "--analysis-version",
        required=True,
        help="combined model, prompt and schema version",
    )
    _add_embedding_arguments(index)

    search = commands.add_parser("search", help="search indexed shots")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument(
        "--save-session",
        action="store_true",
        help="save this ranked result set for a stable local web page",
    )
    search.add_argument(
        "--web-base-url",
        default="http://127.0.0.1:8765",
        help="base URL used in the saved session result link",
    )
    _add_embedding_arguments(search)
    search.add_argument("--semantic-min-score", type=float)

    evaluate = commands.add_parser("eval", help="evaluate ranked search queries")
    evaluate.add_argument("suite", type=Path)
    _add_embedding_arguments(evaluate)
    evaluate.add_argument("--semantic-min-score", type=float)

    inspect = commands.add_parser("inspect", help="show one indexed shot")
    inspect.add_argument("shot_id", type=int)

    commands.add_parser("status", help="show indexing status")
    jobs = commands.add_parser("jobs", help="list persistent indexing jobs")
    jobs.add_argument("--limit", type=int, default=50)
    stop = commands.add_parser("stop", help="request a safe stop after the current shot")
    stop.add_argument("job_id", type=int)

    embed_text = commands.add_parser(
        "embed-text",
        help="generate missing text embeddings for already indexed shots",
    )
    _add_text_embedding_arguments(embed_text)

    preflight = commands.add_parser(
        "mage-preflight",
        help="check local Mage-VL readiness without downloading anything",
    )
    preflight.add_argument("--cache-root", type=Path)

    mage_server = commands.add_parser(
        "mage-serve",
        help="run the experimental local Mage-VL OpenAI-compatible service",
    )
    mage_server.add_argument("--host", default="127.0.0.1")
    mage_server.add_argument("--port", type=int, default=30_000)
    mage_server.add_argument("--model", default=DEFAULT_MODEL)
    mage_server.add_argument("--device", default="mps", choices=("mps", "cpu"))

    server = commands.add_parser("serve", help="run the local search website")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    _add_embedding_arguments(server)
    server.add_argument("--semantic-min-score", type=float)
    return parser


def _add_embedding_arguments(parser: argparse.ArgumentParser) -> None:
    _add_text_embedding_arguments(parser)
    parser.add_argument("--visual-embedding-command")
    parser.add_argument("--visual-embedding-version")


def _add_text_embedding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text-embedding-command")
    parser.add_argument("--text-embedding-version")
    parser.add_argument("--text-embedding-model")


def _embedding_adapters(
    args: argparse.Namespace,
    *,
    default_text_model: str | None = None,
) -> tuple[object | None, object | None]:
    text_model = args.text_embedding_model
    if not args.text_embedding_command and text_model is None:
        text_model = default_text_model
    if args.text_embedding_command and text_model:
        raise ValueError(
            "text embedding command and local model are mutually exclusive"
        )
    if bool(args.text_embedding_command) != bool(args.text_embedding_version):
        raise ValueError(
            "text embedding command and version must be provided together"
        )
    if text_model and args.text_embedding_version:
        raise ValueError(
            "text embedding version is automatic when using a local model"
        )
    visual_command = getattr(args, "visual_embedding_command", None)
    visual_version = getattr(args, "visual_embedding_version", None)
    if bool(visual_command) != bool(visual_version):
        raise ValueError(
            "visual embedding command and version must be provided together"
        )
    if args.text_embedding_command:
        text_embedder = CommandTextEmbedder(
            shlex.split(args.text_embedding_command),
            version=args.text_embedding_version,
        )
    elif text_model:
        text_embedder = FastEmbedTextEmbedder(model_name=text_model)
    else:
        text_embedder = None
    visual_embedder = (
        CommandVisualEmbedder(
            shlex.split(visual_command),
            version=visual_version,
        )
        if visual_command
        else None
    )
    return text_embedder, visual_embedder


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    database = Database(args.db)
    try:
        if args.command == "mage-preflight":
            _emit(environment_report(cache_root=args.cache_root))
            return 0
        if args.command == "mage-serve":
            print(
                json.dumps(
                    {
                        "url": f"http://{args.host}:{args.port}/v1",
                        "model": args.model,
                        "device": args.device,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            serve_local_mage(
                model_id=args.model,
                host=args.host,
                port=args.port,
                device=args.device,
            )
            return 0
        if args.command == "scan":
            _emit(
                preflight_folder(
                    args.folder,
                    timeout_seconds=args.ffprobe_timeout,
                    cache_root=args.db.parent,
                )
            )
            return 0
        if args.command == "init":
            database.initialize()
            _emit({"database": str(database.path), "initialized": True})
            return 0
        if args.command == "index":
            database.initialize()
            text_embedder, visual_embedder = _embedding_adapters(args)
            segmenter = FFmpegSceneSegmenter(
                max_scene_ms=round(args.max_shot_seconds * 1_000),
                timeout_seconds=args.ffmpeg_timeout,
            )
            if args.analyzer_command:
                analyzer = CommandAnalyzer(
                    shlex.split(args.analyzer_command),
                    version=args.analysis_version,
                    timeout_seconds=args.ffmpeg_timeout,
                )
            else:
                if args.mage_prompt is None:
                    raise ValueError("--mage-prompt is required with --mage-base-url")
                analyzer = MageServiceAnalyzer(
                    base_url=args.mage_base_url,
                    model=args.mage_model,
                    api_key=os.environ.get("MAGE_API_KEY", "EMPTY"),
                    prompt=args.mage_prompt.read_text(encoding="utf-8"),
                    version=args.analysis_version,
                    timeout_seconds=args.mage_timeout,
                    max_tokens=args.mage_max_tokens,
                    max_long_edge=args.mage_max_long_edge,
                    ffmpeg_timeout_seconds=args.ffmpeg_timeout,
                )
            folder = args.folder.resolve()
            def emit_progress(job: dict[str, object]) -> None:
                print(
                    json.dumps({"event": "index-progress", "job": job}, ensure_ascii=False),
                    file=sys.stderr,
                    flush=True,
                )

            job_id: int | None = None
            with database.index_lock():
                if args.resume_job is None:
                    job_id = database.create_index_job(
                        folder=str(folder),
                        analysis_version=analyzer.version,
                        segmentation_version=segmenter.version,
                    )
                else:
                    job_id = args.resume_job
                    database.resume_index_job(
                        job_id,
                        folder=str(folder),
                        analysis_version=analyzer.version,
                        segmentation_version=segmenter.version,
                    )
                try:
                    result = Indexer(
                        database=database,
                        analyzer=analyzer,
                        segmenter=segmenter,
                        probe=lambda path: probe_video(
                            path, timeout_seconds=args.ffmpeg_timeout
                        ),
                        text_embedder=text_embedder,
                        visual_embedder=visual_embedder,
                        thumbnailer=FFmpegThumbnailer(
                            timeout_seconds=args.ffmpeg_timeout
                        ),
                    ).index_folder(folder, job_id=job_id, progress=emit_progress)
                except KeyboardInterrupt:
                    database.update_index_job(
                        job_id, status="paused", current_stage="paused"
                    )
                    _emit({"folder": str(folder), "job_id": job_id, "paused": True})
                    return 130
                except Exception as error:
                    database.update_index_job(
                        job_id,
                        status="failed",
                        current_stage="failed",
                        error=str(error),
                    )
                    raise
            _emit({"folder": str(folder), "job_id": job_id, **result})
            if result.get("paused"):
                return 3
            return 0 if result["failed"] == 0 else 2

        database.initialize()
        if args.command == "status":
            _emit(database.status())
            return 0
        if args.command == "jobs":
            jobs = database.list_index_jobs(limit=args.limit)
            _emit({"count": len(jobs), "jobs": jobs})
            return 0
        if args.command == "stop":
            job = database.request_index_job_stop(args.job_id)
            _emit({"job_id": args.job_id, "stop_requested": bool(job["stop_requested"])})
            return 0
        if args.command == "embed-text":
            text_embedder, _ = _embedding_adapters(
                args,
                default_text_model=DEFAULT_TEXT_EMBEDDING_MODEL,
            )
            assert text_embedder is not None
            _emit(backfill_text_embeddings(database, text_embedder))
            return 0
        if args.command == "search":
            text_embedder, visual_embedder = _embedding_adapters(args)
            results = HybridSearcher(
                database,
                text_embedder=text_embedder,
                visual_embedder=visual_embedder,
                semantic_min_score=args.semantic_min_score,
            ).search(args.query, limit=args.limit)
            payload: dict[str, object] = {
                "query": args.query,
                "count": len(results),
                "results": results,
            }
            if args.save_session:
                session = database.create_search_session(
                    query=args.query,
                    results=results,
                )
                payload["session_id"] = session["session_id"]
                payload["result_url"] = (
                    f"{args.web_base_url.rstrip('/')}/search/session/"
                    f"{session['session_id']}"
                )
            _emit(payload)
            return 0
        if args.command == "eval":
            text_embedder, visual_embedder = _embedding_adapters(
                args,
                default_text_model=DEFAULT_TEXT_EMBEDDING_MODEL,
            )
            suite = json.loads(args.suite.read_text(encoding="utf-8"))
            if not isinstance(suite, dict):
                raise ValueError("evaluation suite must be a JSON object")
            _emit(
                evaluate_suite(
                    HybridSearcher(
                        database,
                        text_embedder=text_embedder,
                        visual_embedder=visual_embedder,
                        semantic_min_score=args.semantic_min_score,
                    ),
                    suite,
                )
            )
            return 0
        if args.command == "inspect":
            shot = database.get_shot(args.shot_id)
            shot.update(database.get_shot_details(args.shot_id))
            _emit(shot)
            return 0
        if args.command == "serve":
            text_embedder, visual_embedder = _embedding_adapters(args)
            print(
                json.dumps(
                    {
                        "url": f"http://{args.host}:{args.port}",
                        "database": str(database.path),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            serve(
                database,
                host=args.host,
                port=args.port,
                searcher=HybridSearcher(
                    database,
                    text_embedder=text_embedder,
                    visual_embedder=visual_embedder,
                    semantic_min_score=args.semantic_min_score,
                ),
            )
            return 0
    except Exception as error:
        print(
            json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
