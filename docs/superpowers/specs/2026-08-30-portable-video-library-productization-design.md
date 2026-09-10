# Portable Video Library Productization Design

**Status:** Approved in conversation on 2026-08-30

## 1. Goal

Turn the existing local wedding-video search prototype into a general-purpose, local video-footage library for macOS. A user can create or open a library on any writable disk, add multiple source folders, manually scan them, search all completed shots with natural language, and play the exact source-video ranges.

The library must be portable when its database, thumbnails, and source videos are on the same removable disk. The same video content may exist at multiple filesystem locations, but it is analyzed once and appears once in search results.

## 2. Confirmed Product Decisions

- The product is for general video footage; wedding footage remains a regression dataset, not a product constraint.
- The web service binds only to `127.0.0.1`. Public access, Cloudflare Tunnel, login, HTTPS, and multi-user behavior are out of scope.
- The first version uses a local web control panel and a macOS double-click launcher. A visible terminal window is acceptable.
- The launcher starts and checks both OptiQ/Mage-VL and video-search.
- A library can contain multiple source folders.
- Folders are selected with the native macOS Finder folder picker.
- Scans are manual. There is no filesystem watcher and no automatic background scan.
- A full scan and an individual-folder scan are both available.
- Source videos remain in place. The product never copies, moves, renames, edits, or deletes them.
- Removing a source folder disables it and its locations in the active library. It does not delete the folder record, analysis data, or source files; re-adding the same folder reactivates them.
- Deleting generated analysis is a separate, confirmed action and deletes only reproducible index data and thumbnails.
- Offline assets are hidden from search by default. An `Include offline footage` switch includes them as non-playable results with relink actions.
- Identical video content appears once in search even when it has multiple locations or filenames.
- Mage-VL remains single-concurrency on the current Apple Silicon machine.

## 3. Non-Goals

- A signed or sandboxed native macOS application.
- Automatic folder monitoring.
- Copying source footage into a managed media folder.
- Public or LAN hosting.
- Multi-user permissions or accounts.
- Network-filesystem guarantees.
- Shipping Mage-VL or embedding model weights inside a portable library.
- Replacing the current scene detector or adding visual embedding as part of this productization phase.
- Claiming general-domain search accuracy from the wedding-only evaluation set.

## 4. User Experience

### 4.1 Startup and library selection

The user double-clicks the launcher. It starts the local controller, opens the browser, and displays either the last library or a library chooser.

The chooser provides:

- `Create library`: choose a writable folder and create the portable library structure.
- `Open library`: choose a library directory or its `library.sqlite3` file.
- `Recent libraries`: open a previously used location if it is currently available.

Opening an existing library never requires Mage-VL merely to search or play existing results. If the BGE query model is missing on the new computer, lexical and structured-event search remain available while the UI offers an explicit one-time model download for semantic search.

### 4.2 Search page

The current search and exact-range playback behavior remains, generalized to video footage:

- Natural-language query input.
- Optional filters for source folder and existing structured attributes.
- `Include offline footage` switch, off by default.
- Result cards with thumbnail, summary, attributes, source information, and shot start/end time.
- Online cards seek the source video to `start_ms` and pause at `end_ms`.
- Offline cards preserve description, thumbnail, and time range; they show an offline badge, disable playback, and provide a relink entry point.
- One asset shot appears once even if multiple source locations exist.

### 4.3 Library page

The library page provides:

- `Add source folder`, using the macOS Finder folder picker.
- `Scan all folders`.
- Per-folder `Scan updates`.
- Per-folder online/offline state, video count, pending count, failure count, and last scan time.
- Current file, current shot, completed shots, and overall job progress.
- Stop-after-current-shot behavior.
- Retry for failed or interrupted work.
- Relink an offline source folder.
- Disable a source folder and its locations without deleting generated analysis; re-adding the same folder restores them.
- View a content-unique asset and all of its known locations.
- A separate `Delete generated analysis` action with confirmation and an explicit statement that source files are untouched.

### 4.4 System page

The system page shows:

- Current library directory and SQLite path.
- Mage-VL state: stopped, starting, ready, or failed.
- Text embedding model state.
- Active indexing job and latest error.
- Available disk space for the library and model cache.
- `Close library`, which stops new writes and safely checkpoints SQLite before the user ejects a removable disk.

## 5. Portable Library Layout

The managed directory layout is:

```text
My Video Library/
  library.json
  library.sqlite3
  thumbnails/
    <asset-id>/
      shot-<shot-index>.jpg
  logs/
```

`library.json` is a small manifest containing the library UUID, display name, format version, and SQLite filename. It contains no source-video copies, credentials, model weights, or machine-specific absolute paths.

Thumbnails are referenced relative to the library directory. Source locations on the same volume are stored relative to a stable source-folder record. Locations on other volumes retain an absolute fallback and can be relinked after moving to another computer.

The command-line `--db` option remains supported for diagnostics and automation. The product UI treats the parent directory of the selected SQLite file as the managed library directory.

## 6. Data Model

### 6.1 Library metadata

A singleton metadata record stores:

- library UUID;
- schema version;
- created and updated timestamps;
- default analysis, segmentation, and embedding versions.

### 6.2 Source folders

`source_folders` represents folders selected by the user:

- `id`;
- display name;
- stored path and path kind (`library_relative` or `absolute`);
- enabled flag;
- online/offline state;
- last successful scan time;
- last error;
- created and updated timestamps.

Duplicate folders and parent/child overlaps are rejected after canonical path resolution. This prevents the same directory tree from being scanned twice.

### 6.3 Content-unique assets

`video_assets` replaces the content identity currently conflated with `videos.path`:

- `id`;
- full-file SHA-256 content hash with a uniqueness constraint;
- size, duration, width, and height;
- segmentation version;
- processing state and error;
- created and updated timestamps.

Full hashing happens once for a new or changed file, with streaming progress. It is substantially cheaper than duplicate Mage-VL analysis and gives deterministic content identity across filenames, copies, symlinks, and computers.

Shots, roles, events, objects, frames, and versioned vectors belong to `video_assets`. Existing shot tables keep their current semantics, with foreign keys migrated from the old `videos` rows to assets.

### 6.4 Asset locations

`video_locations` records every known filesystem location:

- `id`;
- `video_asset_id`;
- `source_folder_id`;
- relative path within the source folder;
- last observed size and modification time for fast unchanged checks;
- filesystem device/inode identity when available;
- online/offline state and last-seen time;
- created and updated timestamps.

The unique key is source folder plus normalized relative path. Symlink resolution and device/inode identity prevent the same physical file from being inserted twice during one scan. The asset content hash prevents copies with different names or locations from being analyzed twice.

Playback resolves locations in this order:

1. online locations belonging to enabled source folders;
2. relative locations on the current library volume;
3. other currently reachable absolute locations.

If no location is reachable, the asset is offline.

### 6.5 Index jobs

`index_jobs` stores each manual scan:

- target source folder or all-folders scope;
- state and current phase;
- discovered, skipped, completed, failed, and total counts;
- current asset and shot;
- cancellation request;
- error summary;
- started, updated, and finished timestamps.

`index_job_items` stores per-file progress and error details so an interrupted scan can resume deterministically without parsing logs.

Only one analysis worker runs at a time. Database reads remain available while a scan writes completed work in short transactions.

## 7. Import, Deduplication, and Change Detection

For each manually triggered folder scan:

1. Resolve the source folder and confirm it is online.
2. Recursively discover supported video extensions.
3. Normalize each relative path and resolve symlinks.
4. Match an existing location and compare size and modification time.
5. Skip unchanged, fully indexed content with matching segmentation and analysis versions.
6. For new or changed files, probe metadata and stream a full SHA-256 hash.
7. If the hash already belongs to an asset, add or update only the location.
8. If the hash is new, create an asset, segment it, analyze shots with Mage-VL, create thumbnails, and generate text embeddings.
9. Make each completed shot searchable immediately.
10. Mark previously known but unseen locations offline after a complete successful folder scan. A stopped or failed scan does not mark unseen locations offline.

If content at an existing path changes, its location is reassigned to the new content asset. The previous asset and analysis remain available through any other location; if none remain, it becomes offline and is visible only when offline results are included.

## 8. General-Domain Analysis

The JSON schema remains suitable for general video footage because `who`, `where`, `when`, `events`, `objects`, and camera attributes are already free text.

A new prompt version removes wedding-specific wording and examples. It instructs Mage-VL to:

- describe visible people by neutral visual role rather than inferred identity;
- preserve ordered actions and interactions;
- describe environment, lighting, objects, and camera behavior;
- avoid inventing audio evidence for silent material;
- return the existing validated JSON structure.

Existing wedding analyses keep their original analysis version and are not reprocessed automatically. New scans use the general prompt version. Mixed analysis versions remain searchable. The wedding evaluation suite remains a regression guard, not evidence of general-domain quality.

## 9. Local Process Architecture

The recommended implementation is a single local controller around the existing components:

```text
macOS launcher
  -> video-search controller
       -> OptiQ subprocess and health check
       -> single indexing worker
       -> SQLite library
       -> BGE query embedder
       -> loopback-only HTTP server
            -> library management API
            -> job status API
            -> search and media API
            -> local web UI
```

The controller reuses an already healthy OptiQ service on the configured port. Otherwise it starts OptiQ, captures logs, and waits for readiness before enabling scans. On application exit it stops only the child process it owns.

If model weights are absent, the UI reports the required download and size before starting it. Model weights remain in the per-computer cache and are never written into the portable library.

State-changing HTTP endpoints accept JSON POST requests only from the loopback application origin and require a per-launch request token. CORS is disabled. The server rejects non-loopback Host headers. These controls protect local folder selection and scan endpoints from unrelated browser pages even though no public access is supported.

## 10. Task Recovery and Errors

- `Stop` sets a cancellation request. The worker finishes the current Mage-VL shot call, commits completed data, and pauses before the next shot.
- On restart, jobs left in an active state become `interrupted` and can be resumed.
- A Mage-VL process failure triggers one automatic restart. A second failure pauses the job and exposes the error and retry action.
- Invalid Mage-VL structured output continues to use the existing four-frame compact retry. Network, process, FFmpeg, and disk errors are not hidden as model-output fallback.
- A corrupt or unsupported video fails only its job item; subsequent files continue.
- Removing a drive during a scan pauses the affected job without deleting locations or completed analysis.
- Search and playback continue for completed, online assets during scans.
- Missing playback files return an offline response and update location state rather than reporting success.
- A source-folder removal never cascades into source-file deletion.

## 11. SQLite Portability and Migration

The library uses SQLite WAL mode with full synchronization while open. `Close library` stops new work, waits for the current transaction, runs a truncating WAL checkpoint, and closes all connections. Reopening also lets SQLite recover a valid WAL left by an application crash.

Before the first schema upgrade of an existing database, the application creates a timestamped database backup in the same library directory. Migration converts each old `videos.path` row into one asset and one location while preserving every existing shot ID, analysis row, and vector. It relocates generated thumbnails into the managed library or records them through the portable path resolver. A migration failure leaves the original database and source videos untouched.

An existing database with only absolute paths can be opened on its original computer and converted. If later opened elsewhere, unavailable paths appear offline and can be relinked to a source folder; content verification prevents an incorrect relink.

## 12. HTTP and Service Boundaries

The existing search and range-enabled media endpoints remain. New controller endpoints cover:

- current library metadata and system status;
- create, open, close, and recent libraries;
- native Finder source-folder selection;
- list, add, remove, and relink source folders;
- start all-folder or per-folder scans;
- inspect, stop, resume, and retry jobs;
- inspect assets and locations;
- delete generated analysis after confirmation.

The browser never receives arbitrary filesystem read access. Media and thumbnails are served only after resolving a registered asset location or generated library path.

## 13. Verification and Acceptance

### 13.1 Data safety

- Adding, scanning, removing, relinking, and deleting generated analysis do not modify source-video size, modification time, or content hash.
- Generated-data deletion removes only SQLite analysis rows and library thumbnails.
- The existing 28-shot real database migrates without losing analysis JSON, time ranges, thumbnails, or 512-dimensional text vectors.

### 13.2 Deduplication

The following all result in one asset and one set of analysis:

- repeated scan of one folder;
- overlapping discovery of one physical file;
- symlink and hard-link references;
- renamed copies with identical content;
- identical content on two registered volumes.

Removing or losing one location still permits playback through another online location.

### 13.3 Portable library

- Create and index a library at test path A.
- Close it and verify no uncheckpointed required state remains outside the library directory.
- Move the library and same-volume test footage to path B.
- Open it without Mage-VL and verify search, thumbnails, and exact-range playback.
- Verify external unavailable locations appear offline and recover after a validated relink.

### 13.4 Product flow and recovery

- The double-click launcher starts the controller and either starts or reuses OptiQ.
- Native Finder folder selection adds a source folder.
- Manual all-folder and individual-folder scans report accurate progress.
- Stop, application restart, Mage failure, corrupt video, and removable-drive loss preserve completed work and expose recovery actions.
- Search remains available during indexing.
- Offline-result switch and disabled playback behavior match the specification.
- Exact shot range playback remains correct.
- The existing wedding suite remains Top1 `30/30`, Top3 `30/30`, MRR `1.0`.
- The full automated test suite passes.

## 14. Delivery Stages

1. Portable library manifest, schema versioning, data migration, relative path resolver, content assets, and multiple locations.
2. Source-folder management, manual jobs, progress persistence, stop/resume, and content deduplication.
3. General-domain prompt version and indexing integration.
4. Local controller, OptiQ lifecycle, embedding readiness, and loopback API protections.
5. Search, library, and system UI workflows.
6. macOS double-click launcher and end-to-end portability/recovery verification.

Each stage must preserve the existing search CLI and verified wedding retrieval behavior unless a documented migration requires a compatible interface adjustment.
