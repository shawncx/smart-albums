# management: manual folders, scoped search and album maintenance

For standalone duplicate discovery, use [duplicates](duplicates.md): `management duplicates` is a sibling of `search`, with explicit-scope scans, complete frozen results, candidate groups, direct-pair pagination and local HTML reports. It reads saved SHA-256/dHash evidence without invoking models, writing the album or checking originals. Result groups are only a presentation of connected candidates, not virtual folders or transitive duplicate assertions.

Management is one of the four public Skill capabilities. Requested local writes follow [authorization and recovery](authorization.md), without repeating existing consent. **Plain browse/search is read-only**; album creation, manual virtual folder writes, explicit original/relink and exports/backups are separate operations. Management does not generate image embeddings, install models, create internal albums or offer automatic regrouping/curation. Optional [AI review](review.md) is separate: local numbered search review (`review_id` / `--review-snapshot`) never authorizes Copilot contact or photo uploads, and v1 search does not consume AI review scores/text.

For feature result/history inspection, read [index](index.md#feature-identity-result-status-and-privacy).

## Commands

Use the selected interpreter and album:

```text
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> [--model-cache-dir <cache-root>]
```

The required global database path accepts `.sqlite`, `.sqlite3` and `.db`. The optional global cache root goes before `management`, consistently with index commands. Append one command:

```text
management create
management open
management photos [--limit N] [--after <photo-id>]
management photo <photo-id>
management search "<query>" --visual-query "<English visual intent>" --output <private.json> [--limit N|all] [--profile-id <id>]
management search --query-file <query.json> --output <private.json> [--limit N|all]
management search-evidence <private.json>
management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]
management search "<query>" --mode metadata [--limit N] [--after <photo-id>]
management original <photo-id>
management relink <photo-id> --path <absolute-original-file>
management backup --output <new-backup.sqlite>
management thumbnail <photo-id> --output <output-directory>\preview.jpg
management scan <scan-id>
management scan-events <scan-id> [--limit N] [--after <event-id>] [--changes-only]
```

`photos` and all `search` modes also accept repeatable `--folder-id <id>` and `--folder-match union|intersection`; unified query-file requests place scope in the file instead. See [folder scope](#folder-scope-for-browse-and-both-search-modes). Read [search](search.md) for unified retrieval and selection; [legacy compatibility](search-legacy.md) is only for explicit legacy commands or snapshots.

The album/photo view commands **create/open/photos/photo** and explicit legacy **search --mode metadata|semantic** share these options; unified search and folder commands have their own contracts:

- `--profile-id <id>` to inspect/search an explicit registered profile rather than the saved default.
- `--output <output-directory>\snapshot.json` for a UTF-8 JSON snapshot.
- `--html <output-directory>\snapshot.html` for a standalone read-only HTML view.

For those legacy/view commands, CLI JSON is returned without `--output`. Export paths use `html_output` and `output` in the result. HTML does not automatically create a companion JSON file; pass both options if needed. Backup/thumbnail use their own required `--output`, not the view options. Unified search requires private JSON output and does not render raw candidate HTML.

`show-results` uses the profile captured by its input snapshot; it does not accept a profile override or perform a new search. Legacy snapshots use photo-ID arrays; unified snapshots require integer candidate numbers, `--review-id` and a separate selected `--output`.

## File lifecycle

Start with [album-file selection](library.md). `create` requires explicit permission and a new path in an existing directory; it never overwrites. `open` validates an existing file read-only and returns the album's stable UUID, filename-derived name, absolute database path, photo count and embedding coverage. It does not create a file, perform DDL, migrate or retain a background connection.

New albums use schema 12/application ID `0x53414C42`, with 40 registered tables: 35 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows (excluding internal `sqlite_sequence`), with no internal albums. Old v1–v10 files are rejected unchanged, with no migration. `virtual_folders(folder_id, name, name_key, description, created_at, updated_at)` and `virtual_folder_photos(folder_id, photo_id, added_at)` are unchanged. Feature storage and the three `ai_review_*` tables remain separate from the existing six embedding tables; see the bundled [storage summary](library.md#storage-summary). Do not use a missing/old database as permission to create a replacement or modify the user's old database/backups.

`management backup --output <new-file>` creates a consistent SQLite snapshot using SQLite's backup API. Existing destinations are refused. It preserves album UUID/data, including previews/embeddings, feature results/dependencies, scene prototypes, OCR FTS, review results/runs/batches and folder memberships, but not external originals, credentials, weights or runtimes. Use one device writer; stop all operations before moving/copying/cloud-syncing the local file. Backup copies are not concurrent branches with automatic merge.

## Manual custom virtual folders: primary workflow

Virtual folders are static, flat many-to-many collections in one SQLite album, not disk directories or another album. Manual CRUD/add/remove is primary and needs no index, model or default profile. Empty custom folders are supported; each photo can belong to many folders or none.

```text
management folders list [--query <name>] [--limit N] [--after <folder-id>]
management folders create --name <name> [--description <text>]
management folders show <folder-id> [--profile-id <profile-id>]
management folders rename <folder-id> --name <new-name>
management folders delete <folder-id>
management folders add <folder-id> --ids-file <photo-ids.json>
management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>
management folders remove <folder-id> --ids-file <photo-ids.json>
```

Names are trimmed, nonempty and control-character-free, unique by NFC + casefold `name_key` within the album. They are text, not paths. Rename preserves the stable `folder_id`. Lists use literal normalized name substrings, stable folder-ID pagination (default 100, range 1–1000) and member counts. `show` reports the folder and saved coverage; no model is loaded, and a supplied profile must be valid. Browse members with `management photos --folder-id <id>`; `management photo <id>` includes its folders.

The Skill resolves actual IDs from the selected album and writes a JSON array for add/remove, including a one-element array for one photo. `[]` means zero changes, never all photos. Validate the folder and every photo before any write; invalid IDs roll back the whole batch. Duplicate IDs are deduplicated; repeated add or removing a valid nonmember reports unchanged counts. Create/rename/delete/add/remove explicitly open a writer and commit atomically; list/show remain read-only.

Removing membership or deleting a folder never deletes photo records, originals, thumbnails or embeddings and never removes memberships in other folders. Members bind stable photo IDs: content/path/profile changes do not regroup them. No hierarchy, cross-album folders, folder-specific embeddings, jobs or live rules are added.

## Optional one-time organization

### Add explicitly selected search results

Use the requested destination folder and selected numbers with [unified selected-result validation](search.md#unified-evidence-selection-and-display). Only explicit legacy search snapshots need [legacy folder-add provenance](search-legacy.md#add-selected-results-to-a-folder). A query alone never authorizes folder writes; an empty selection changes nothing.

### Plan and confirm EXIF date organization

```text
management folders organize-date --all --granularity year|month|day --output <date-plan.json>
management folders organize-date --ids-file <photo-ids.json> --granularity year|month|day --output <date-plan.json>
management folders apply-date-plan <date-plan.json> --confirm <digest>
```

Planning requires exactly one of `--all` or `--ids-file`, independent of browse pagination/top-K; an empty ID array is an empty scope. It reads saved EXIF `datetime_original` only, requiring a valid calendar date in camera-local time with no UTC conversion. It produces flat `YYYY`, `YYYY-MM` or `YYYY-MM-DD` names. Missing/invalid dates and unavailable ingestion metadata are skipped with counts; no fallback to modification time, other date fields or original-file reads. This authorized deterministic operation is not semantic evidence and needs no index/model.

Planning is read-only. CLI JSON wraps `plan`, `digest`, `output`, `album`, `model_calls: 0` (and `image_model_calls: 0`); the output file contains the raw plan, not this envelope. Inspect its exact scope, member IDs, create/reuse preview, skips and digest. If the user already requested this organization for this scope/granularity, summarize and apply without another confirmation. A plan-only request does not authorize applying it. Use the [shared authorization policy](authorization.md); the digest itself is not consent. It revalidates album, saved inputs and target identities/names, reports conflicts/staleness explicitly, and atomically creates/reuses folders and adds members. No partial writes, jobs/new tables, live rules or replacement of other manual members. Later imports or manually removed photos are never automatically regrouped. Plan exports use existing output protection and cannot overwrite their ID input file.

## Browse saved photos

`photos` lists the selected album's photos; `photo <id>` inspects one record. Neither rescans sources, stats originals, repairs paths nor writes the database. No model or default profile is needed. Without one, `embedding_configuration` is `not_configured`; a supplied profile ID must still be valid. A configured profile supplies saved coverage, not inference.

Records expose both `original_absolute_path` and nullable `original_relative_path`, plus separate `ingest_state` and `original_status`. Original verification is `not_checked`, regardless of stored availability. Saved previews remain browsable while originals are offline. JSON metadata inspection does not decode every JPEG BLOB; HTML/thumbnail export validates the previews it uses and reports corrupt/changed inputs.

`photos` and metadata search sort by stable photo ID, default limit 100, range 1–1000. Follow non-null `next_cursor` with `--after` and retain the album/query/profile and folder scope. Pages are not a durable multi-page transaction; re-query after changes.

## Folder scope for browse and both search modes

The scope rules also apply to unified search, with query-file scope supplied inside the file rather than CLI flags.

```text
management photos --folder-id <a>
management search "IMG" --mode metadata --folder-id <a> --folder-id <b> --folder-match union
```

Multiple distinct IDs require explicit `--folder-match union|intersection`; repeated copies of one ID are one folder. No folder IDs means the entire album. Invalid folders are errors, never whole-album fallback. Empty folders/intersections yield empty results without loading an encoder; semantic search still requires a valid explicit/configured profile.

Resolve and filter scope before vector inspection, ranking and top-K. Union members are deduplicated. Counts, coverage, cursor pages and score gaps belong to the selected scope; filtering a whole-album top-K afterward is incorrect. Metadata `album_total` remains the actual whole-album count, alongside `scope_total` and query-match counts.

`scope` is `{"kind":"album"}` or `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}` (`intersection` is also valid). `coverage_scope` is `entire_album` or `selected_folders`. Folder names are scope labels, never semantic evidence.

## Metadata search

```text
management search "IMG_01" --mode metadata
management search "旅行" --mode metadata
management search "été" --mode metadata
```

Matching uses Unicode NFC normalization followed by casefold on query and filename/recorded absolute or relative path. It is a **literal substring**, not regex, SQL wildcards, album-name lookup, description search or a semantic hybrid. `%` and `_` are literal; canonically equivalent text follows the same rule.

Never translate literal metadata or OCR searches. `--visual-query` is rejected in metadata mode; semantic preparation must not rewrite these literals.

No model, weights, credentials or embeddings are required. A blank query is an error. No hits return an empty page without silently switching modes.

## Explicit original lookup and relink

`management original <photo-id>` explicitly checks original locations and may write path/status repair. It returns a resolution for the host to use when the user requests the original; it is not the ordinary saved-photo view.

| Location outcome | Action |
| --- | --- |
| Absolute path is usable | Prefer it; do not replace it with a relative copy or automatically rewrite the relative path |
| Absolute missing, relative path usable | Resolve from the current SQLite parent, update absolute path and path timestamp |
| Both missing, or absolute missing with no relative path | Record source missing as `original_status: missing`; retain photo/preview/vectors |
| Permission denied, I/O error or non-file location | Report `unavailable`/access error, not missing or permission to select another copy |
| Location usable but repair cannot be saved | Return an error including resolution and `persisted: false`; never claim repair success |

The relative path permits `..`; SQLite and originals have no fixed directory relationship. Its stored slash-separated components are not relative to CWD, the scan root or an old database location. Windows cross-drive/different-share relative paths can be `NULL`, with `RELATIVE_PATH_UNAVAILABLE`; explain that future moves may require explicit relink.

Locating returns `content_verified: false`: existence does not prove bytes match the ingested version. An absolute hit that needs no status/path change can remain read-only. If a change is required, the operation opens a writer, verifies album/input identity and persists in a short transaction. Read-only/locked failure is an error, even when the file was found.

`management relink <photo-id> --path <absolute-original-file>` requires an explicit new path, reads its SHA-256 and compares it to the ingested content version. Matching content updates both saved paths; mismatch is `ORIGINAL_CONTENT_MISMATCH`, with no force override. Conflicting photo paths are rejected. A changed file with no surviving identity must be deliberately ingested as a new photo rather than guessed into an existing one.

Path-only operations preserve photo ID, content version, metadata, previews and vectors. Only ingestion updates content versions. Relink/path lookup does not clear `ingest_state: error`; repair/rescan that input before indexing. `original_status: missing|unavailable` alone does not invalidate a complete saved preview.

## Preview and scan diagnostics

`management thumbnail <photo-id> --output <file.jpg>` (or `.jpeg`) exports a validated saved preview without reading originals. `management scan <scan-id>` retrieves its persisted ingestion result, including successful subsets after handled interruption/failure.

`management scan-events <scan-id>` returns saved event pages, default limit 100 and range 1–1000. Its `--after` cursor is a nonnegative **event ID**, unlike photo pagination. Retain `--changes-only` while following `next_cursor`; do not confuse a partial scan's changed IDs with the whole album.

## Snapshot and output safety

Legacy views use **`album-snapshot-v2`**, with schema/version, album UUID/path, mode, explicit scope/coverage scope and selected profile identity. Browse/metadata records use `items`; legacy semantic rankings use `results` with result/profile IDs, input identity, `vector_hash` and scores. Semantic candidate and selected outputs require this hash without changing the snapshot schema; snapshots lacking it are rejected. `show-results` and `folders add --search-snapshot` validate the current vector hash, so a same-ID repair to a different valid vector makes the old selection stale. Rerun search rather than editing the old snapshot. Unified private snapshots likewise freeze review/source identities, but their public evidence is compact and numbered. A snapshot is not live state, and image-embedding readiness does not mean future technical components are complete.

Exports protect the database, journal/WAL/shared-memory/execution-lock sidecars, model cache and recorded original paths. There is no blanket prohibition on the database parent directory. JPEG preview exports must additionally stay outside saved scan source directories. Choose dedicated output files; a normal report/preview export is not a no-overwrite backup command.

All JSON, HTML and JPEG exports use atomic staged publication, not direct truncating writes to the final path. Preflight freezes directory identities, and parent directories are pinned through publication to prevent redirection. A destination absent at preflight never overwrites a file that appears before publication; an existing permitted export is replaced as a directory entry, not opened and truncated. Failed staging preserves the old destination rather than publishing partial content. Initial target/input protection remains required before model work or source validation.

Native POSIX publication paths have only mocked checks on Windows, not actual Linux/macOS runtime validation. Do not infer cross-host or filesystem compatibility from those checks.

HTML escapes data, embeds validated previews and binds them to the snapshot's album/input identities. Changed/missing/corrupt previews display errors instead of substitute images. No original access is required. There are no selection checkboxes, membership controls, live server or retained `search-add` protocol.

Claim no real-model performance, quality, memory, timing, disconnected operation or cross-host support without recorded verification; synthetic tests and authorization alone are not acceptance.
