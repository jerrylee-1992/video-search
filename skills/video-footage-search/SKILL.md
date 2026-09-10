---
name: video-footage-search
description: Use when a user wants to find, filter, inspect, play, or open previously indexed shots from the local video footage library with natural language.
---

# Video Footage Search

Search the local SQLite index through the stable CLI. Keep the ranked result set addressable so follow-up phrases such as “第二个镜头” resolve to the same search snapshot.

## Local configuration

- Project: `/Users/jerry/Documents/Projects/video-search`
- Prefer a database path explicitly provided by the user.
- Otherwise use `VIDEO_SEARCH_DB` when set, then the existing project database `.video-search-cache/optiq-test/index.sqlite3`, then the CLI default database.
- Never index, reanalyze, move, upload, or delete footage unless the user separately asks.

## Search

From the project directory, run:

```bash
.venv/bin/video-search --db "$VIDEO_SEARCH_DB_PATH" search "$QUERY" \
  --limit 20 \
  --text-embedding-model BAAI/bge-small-zh-v1.5 \
  --save-session \
  --web-base-url http://127.0.0.1:8765
```

Use a task-specific shell variable such as `VIDEO_SEARCH_DB_PATH`; do not repurpose system variables. Treat the JSON as the source of truth. A zero-count response means no indexed match; do not substitute visually plausible footage unless the user asks for approximate results.

Return a concise numbered list. For each result retain `shot_id`, `video_path`, `start_ms`, `end_ms`, `summary`, `who`, `when_period`, `environment`, `venue`, `actions`, and `score`. Also retain `session_id` and `result_url` in the current task so later references remain exact. Video bytes are not part of the conversation context.

## Open results

Only when the user asks to open, show, view, or play results:

1. Check `http://127.0.0.1:8765/api/status`.
2. If unavailable, start the local server with the same database and text embedding model.
3. Open the returned `result_url` with the available local browser capability.

For “打开第二个镜头”, select rank 2 from the saved ordered results, not `shot_index=1` and not a new search. Open:

```text
<result_url>?shot=<shot_id>
```

Echo the selected rank, `shot_id`, source file, and time range so the user can verify the reference. If the original result snapshot is no longer available, rerun the query and say that a new session was created.

## Inspect

Use `inspect <shot_id>` when the user requests the complete stored analysis. Do not load the entire database into conversation context; fetch only the selected shots.

## Common mistakes

| Mistake | Correct behavior |
|---|---|
| Using the first SQLite file found | Follow the database selection order above |
| Treating “第二个” as shot index 1 | Resolve rank 2 within the saved session |
| Re-searching before a follow-up | Reuse `session_id` and its ordered results |
| Claiming a path lets cloud ChatGPT read a file | A path is metadata; local access or upload is still required |
