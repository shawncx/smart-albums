# management: album lifecycle, browsing, search and path maintenance

Management is one of the three public Skill capabilities. **Plain browse/search is read-only**; creating an album file, explicitly locating/relinking originals and writing chosen exports/backups are separate operations. Management does not generate image embeddings, install models, manipulate internal album memberships or offer automatic selection/curation.

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
management search "<query>" --mode metadata [--limit N] [--after <photo-id>]
management search "<query>" --mode semantic [--limit N]
management original <photo-id>
management relink <photo-id> --path <absolute-original-file>
management backup --output <new-backup.sqlite>
management thumbnail <photo-id> --output <output-directory>\preview.jpg
management scan <scan-id>
management scan-events <scan-id> [--limit N] [--after <event-id>] [--changes-only]
```

Only the view commands **create/open/photos/photo/search** also accept:

- `--profile-id <id>` to inspect/search an explicit registered profile rather than the saved default.
- `--output <output-directory>\snapshot.json` for a UTF-8 JSON snapshot.
- `--html <output-directory>\snapshot.html` for a standalone read-only HTML view.

CLI JSON is returned without `--output`. Export paths use `html_output` and `output` in the result. HTML does not automatically create a companion JSON file; pass both options if needed. Backup/thumbnail use their own required `--output`, not the view options.

There is no `--target`, internal album/library scope, `--model-dir`, `--state-dir`, default database or old command alias.

## File lifecycle

Start with [album-file selection](library.md). `create` requires explicit permission and a new path in an existing directory; it never overwrites. `open` validates an existing file read-only and returns the album's stable UUID, filename-derived name, absolute database path, photo count and embedding coverage. It does not create a file, perform DDL, migrate or retain a background connection.

New albums use schema 8/application ID `0x53414C42`, with 11 tables and no internal albums. Old v1–v7 files are rejected unchanged. Do not use a missing/old database as permission to create a replacement or modify the user's old database/backups.

`management backup --output <new-file>` creates a consistent SQLite snapshot using SQLite's backup API. Existing destinations are refused. It preserves album UUID/data, including previews/embeddings/runs, but not external originals, weights or runtimes. Use one device writer; stop all operations before moving/copying/cloud-syncing the local file. Backup copies are not concurrent branches with automatic merge.

## Browse saved photos

`photos` lists the selected album's photos; `photo <id>` inspects one record. Neither rescans sources, stats originals, repairs paths nor writes the database. No model or default profile is needed. Without one, `embedding_configuration` is `not_configured`; a supplied profile ID must still be valid. A configured profile supplies saved coverage, not inference.

Records expose both `original_absolute_path` and nullable `original_relative_path`, plus separate `ingest_state` and `original_status`. Original verification is `not_checked`, regardless of stored availability. Saved previews remain browsable while originals are offline. JSON metadata inspection does not decode every JPEG BLOB; HTML/thumbnail export validates the previews it uses and reports corrupt/changed inputs.

`photos` and metadata search sort by stable photo ID, default limit 100, range 1–1000. Follow non-null `next_cursor` with `--after` and retain the album/query/profile. Pages are not a durable multi-page transaction; re-query after changes.

## Metadata search

```text
management search "IMG_01" --mode metadata
management search "旅行" --mode metadata
management search "été" --mode metadata
```

Matching uses Unicode NFC normalization followed by casefold on query and filename/recorded absolute or relative path. It is a **literal substring**, not regex, SQL wildcards, album-name lookup, description search or a semantic hybrid. `%` and `_` are literal; canonically equivalent text follows the same rule.

No model, weights, credentials or embeddings are required. A blank query is an error. No hits return an empty page without silently switching modes.

## Semantic photo search

```text
management search "黑白的枯树" --mode semantic
management search "black and white leafless trees" --mode semantic --profile-id <profile-id> --limit 10
```

Search uses the explicit profile, otherwise the deliberately configured default. If neither exists, explain [index setup/configuration](index.md); never choose silently. If setup used another cache root, supply the same **global** `--model-cache-dir <cache-root>` before `management`. It selects the local pinned files, not an automatic download. Cache configuration is irrelevant to metadata inference because metadata mode performs none.

Image and query encoders must share the **same immutable profile**, not just dimensions/model family. The first profile is `google/siglip2-base-patch16-224`, revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, PyTorch CPU FP32. Persisted vectors represent whole saved previews: paired `image_text_semantic` space, image modality, `stored_thumbnail` input, `whole_image` granularity, 768 dimensions. Query vectors are transient.

Queries use the matching tokenizer/text feature interface directly, without translation or retired retrieval prefixes. The limit is 64 tokens including EOS; overlong queries produce `QUERY_TOO_LONG`, not silent truncation.

A consistent saved-data snapshot supplies current photo/preview identities and vectors. Invalid/stale/absent results are excluded and reported; semantic ranking checks saved input metadata and vector integrity, not original files or all preview JPEG bytes. Optional HTML rendering validates its embedded previews separately.

With no eligible candidates, search returns empty results and coverage without loading a model. Otherwise it encodes the query once and ranks locally by exact cosine similarity. It never regenerates image vectors, mixes profiles, changes defaults, installs/downloads weights or falls back to a cloud service.

Semantic search defaults to top 10, accepts 1–1000 and **rejects `--after`**. Results sort by descending similarity, then photo ID. Coverage includes the **entire album**, not only top-K: distinguish `ready`, `missing`, `stale`, `invalid_input` and `invalid_vector` from old task failures and source availability. Incomplete coverage must not be described as searching every photo.

Scores are relative similarities, not probabilities, guaranteed logical filters, detection counts or technical measurements. Highest-ranked need not be relevant. Do not promise calibrated thresholds, exact negation/count behavior or focus/blur analysis.

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

Views use **`album-snapshot-v1`**, with schema/version, album UUID/path, mode and selected profile identity. Browse/metadata records use `items`; semantic rankings use `results` with result/profile IDs, input identity and scores. A snapshot is not live state, and image-embedding readiness does not mean future technical components are complete.

Exports protect the database, journal/WAL/shared-memory/execution-lock sidecars, model cache and recorded original paths. There is no blanket prohibition on the database parent directory. JPEG preview exports must additionally stay outside saved scan source directories. Choose dedicated output files; a normal report/preview export is not a no-overwrite backup command.

HTML escapes data, embeds validated previews and binds them to the snapshot's album/input identities. Changed/missing/corrupt previews display errors instead of substitute images. No original access is required. There are no selection checkboxes, membership controls, live server or retained `search-add` protocol.

The portable code is implemented, but consolidated regression acceptance is pending. No new-format real-model trial was performed; previous v7 results and synthetic tests do not establish new-format quality, memory, timing or cross-host runtime support.
