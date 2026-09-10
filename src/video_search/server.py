from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from video_search.database import Database
from video_search.search import HybridSearcher


def create_server(
    address: tuple[str, int],
    database: Database,
    searcher: HybridSearcher | None = None,
) -> ThreadingHTTPServer:
    database.initialize()
    active_searcher = searcher or HybridSearcher(database)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/" or re.fullmatch(
                r"/search/session/[a-f0-9]{32}", parsed.path
            ):
                self._serve_page()
                return
            if parsed.path == "/request-gate.js":
                self._serve_static("request-gate.js", "text/javascript; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._send_json(database.status())
                return
            if parsed.path == "/api/search":
                parameters = parse_qs(parsed.query)
                query = parameters.get("q", [""])[0].strip()
                try:
                    requested_limit = int(parameters.get("limit", ["20"])[0])
                except ValueError:
                    self._send_json({"error": "limit must be an integer"}, status=HTTPStatus.BAD_REQUEST)
                    return
                if not 1 <= requested_limit <= 100:
                    self._send_json({"error": "limit must be between 1 and 100"}, status=HTTPStatus.BAD_REQUEST)
                    return
                limit = requested_limit
                try:
                    results = active_searcher.search(query, limit=limit) if query else []
                except Exception as error:
                    self._send_json(
                        {"error": "search failed", "type": type(error).__name__},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json({"query": query, "count": len(results), "results": results})
                return
            session_match = re.fullmatch(
                r"/api/search-sessions/([a-f0-9]{32})", parsed.path
            )
            if session_match:
                try:
                    session = database.get_search_session(session_match.group(1))
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(session)
                return
            detail_match = re.fullmatch(r"/api/shots/(\d+)", parsed.path)
            if detail_match:
                shot_id = int(detail_match.group(1))
                token = parse_qs(parsed.query).get("token", [None])[0]
                try:
                    detail = database.get_shot_bundle(shot_id, identity_token=token)
                except KeyError:
                    self._send_json(
                        {"error": "shot not found", "shot_id": shot_id},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json(detail)
                return
            match = re.fullmatch(r"/(media|thumbnail)/(\d+)", parsed.path)
            if match:
                token = parse_qs(parsed.query).get("token", [None])[0]
                self._serve_shot_file(match.group(1), int(match.group(2)), token)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _serve_page(self) -> None:
            page = (Path(__file__).parent / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def _serve_static(self, name: str, content_type: str) -> None:
            body = (Path(__file__).parent / "web" / name).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(
            self,
            payload: object,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_shot_file(self, kind: str, shot_id: int, token: str | None) -> None:
            try:
                path = database.get_shot_file_path(
                    shot_id, kind, identity_token=token
                )
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(path)

        def _send_file(self, path: Path) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            partial = False
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
                if not match:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
                if start >= size or end < start:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                end = min(end, size - 1)
                partial = True
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self.send_header(
                "Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(address, Handler)


def serve(
    database: Database,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    searcher: HybridSearcher | None = None,
) -> None:
    server = create_server((host, port), database, searcher)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
