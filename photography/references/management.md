# management: manual folders, scoped search and album maintenance

Management is one of the three public Skill capabilities. **Plain browse/search is read-only**; album creation, manual virtual folder writes, explicit original/relink and exports/backups are separate operations. Management does not generate image embeddings, install models, create internal albums or offer automatic regrouping/curation.

Stage 1 adds six opt-in [index components](index.md#stage-1-six-opt-in-components). **Stage 2 OR search is implemented** through separate condition commands; targeted integration checks have passed. Existing metadata/semantic modes and defaults remain unchanged. Explicit `index result`/`result-history` inspect saved feature evidence; `compare` prepares a confirmed computation and `pairs` reads historical evidence. Neither automatically deletes/merges photos nor changes folders. Feature details are not permission to rerank existing semantic candidates or pass images to the agent.

For paged historical feature details, use `index result <photo-id> --component <component> --result-id <historical-result-id> --details --after <offset> --limit N`. No configured default is needed with an explicit result ID; photo/component and any optional `--profile-id` must match (`FEATURE_RESULT_MISMATCH` otherwise). Preserve `historical: true`; this read-only inspection is not current coverage or new search evidence.

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
management show-results <candidates.json> --ids-file <selected.json> [--html <results.html>] [--output <displayed.json>]
management query --query-file <query.json> --output <private-snapshot.json>
management query-evidence <private-snapshot.json> --condition-id A [--page N]
management finalize-query <private-snapshot.json> --decisions-file <decisions.json> --output <ranked.json>
management show-query-results <ranked.json> [--limit N] [--after <opaque-cursor>] [--output <page.json>] [--html <report.html>]
management query-pairs <ranked.json> --condition-id D [--limit N] [--after <opaque-cursor>] [--output <pairs.json>]
```

`photos` and both `search` modes also accept repeatable `--folder-id <id>` and `--folder-match union|intersection`; see [folder scope](#folder-scope-for-browse-and-both-search-modes).

The album/photo view commands **create/open/photos/photo/search** share these options; folder commands have their own options below:

- `--profile-id <id>` to inspect/search an explicit registered profile rather than the saved default.
- `--output <output-directory>\snapshot.json` for a UTF-8 JSON snapshot.
- `--html <output-directory>\snapshot.html` for a standalone read-only HTML view.

CLI JSON is returned without `--output`. Export paths use `html_output` and `output` in the result. HTML does not automatically create a companion JSON file; pass both options if needed. Backup/thumbnail use their own required `--output`, not the view options.

`show-results` uses the profile captured by its input snapshot; it does not accept a profile override or perform a new search. It accepts only its listed export options and an explicit ID-array file, including an empty array.

Condition commands also open the album read-only and never perform DDL/default writes, image inference, downloads or original reads. `query` requires a separate `.json` private snapshot and prints only a safe summary. The agent must not read the private snapshot file/feature matrix to judge semantic relevance. Use numeric-only `query-evidence` (page default 0), then all explicit `condition-decisions-v1` matched-ID pages; empty per-page selections are valid, missing pages are not. Finalization reports `model_calls: 0`, retaining historical snapshot `query_model_calls`. A query without reviewable semantic cells already finalizes automatically.

`show-query-results` validates finalized source identities, pages after ranking (default 100, 1–1000), and retains global ranks with opaque snapshot-bound cursors. Its `condition-search-page-v1` JSON can be exported together with a user-only HTML report containing exactly that page's previews. Input `coverage` describes eligibility over the scope (`not_reviewed` semantic cells mean query-time eligible); `evaluated_coverage` describes final candidate-pool outcomes. Show conditions/aliases, matched counts/ID sets, per-condition cells, normalized scores and partial semantic coverage. Reports are escaped, script/form/widget/backend-free historical views, not new original checks or current-membership queries. Never open/screenshot their images for agent review.

`query-pairs` takes a finalized snapshot and a `has_near_duplicate` condition ID. Its `condition-duplicate-pairs-v1` returns actual photo-ID pairs, distance/metric, total, condition, historical scope and `input_coverage`; default limit 100, range 1–1000, with a separate opaque `next_cursor`. Only optional JSON `--output` is supported, not HTML. It validates current sources and uses saved SHA-256/dHash64 values without models, originals, comparison jobs or membership changes. Pairs are not transitive groups; never automatically delete/merge photos. The separate `index pairs` command still reads confirmed historical comparison runs.

Validate **all** `.json`/`.html` destinations before query encoding, current-source validation, preview reads or any write. Do not overwrite/alias/hardlink query, candidate or decision inputs, the album, sidecars, cache or recorded originals. Exports are the only query side effects. See [full OR query/ranking protocol](search.md#stage-2-or-condition-workflow).

There is no `--target`, internal album/library scope, `--model-dir`, `--state-dir`, default database or old command alias.

## File lifecycle

Start with [album-file selection](library.md). `create` requires explicit permission and a new path in an existing directory; it never overwrites. `open` validates an existing file read-only and returns the album's stable UUID, filename-derived name, absolute database path, photo count and embedding coverage. It does not create a file, perform DDL, migrate or retain a background connection.

New albums use schema 10/application ID `0x53414C42`, with 37 registered tables: 32 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows (excluding internal `sqlite_sequence`), with no internal albums. Old v1–v9 files are rejected unchanged, with no migration. `virtual_folders(folder_id, name, name_key, description, created_at, updated_at)` and `virtual_folder_photos(folder_id, photo_id, added_at)` are unchanged. Feature storage remains separate from the existing six embedding tables; see the [schema inventory](../../docs/index-design.md#3-schema-10-37-registered-tables). Do not use a missing/old database as permission to create a replacement or modify the user's old database/backups.

`management backup --output <new-file>` creates a consistent SQLite snapshot using SQLite's backup API. Existing destinations are refused. It preserves album UUID/data, including previews/embeddings, feature results/dependencies, scene prototypes, OCR FTS, runs and folder memberships, but not external originals, weights or runtimes. Use one device writer; stop all operations before moving/copying/cloud-syncing the local file. Backup copies are not concurrent branches with automatic merge.

## Manual custom virtual folders: primary workflow

Virtual folders are static, flat many-to-many collections in one SQLite album, not disk directories or another album. Manual CRUD/add/remove is primary and needs no index, model or default profile. Empty custom folders are supported; each photo can belong to many folders or none.

```text
management folders list [--query <name>] [--limit N] [--after <folder-id>]
management folders create --name <name> [--description <text>]
management folders show <folder-id> [--profile-id <profile-id>]
management folders rename <folder-id> --name <new-name>
management folders delete <folder-id>
management folders add <folder-id> --ids-file <photo-ids.json> [--search-snapshot <candidates.json>]
management folders add <folder-id> --ids-file <photo-ids.json> --query-snapshot <ranked.json>
management folders remove <folder-id> --ids-file <photo-ids.json>
```

Names are trimmed, nonempty and control-character-free, unique by NFC + casefold `name_key` within the album. They are text, not paths. Rename preserves the stable `folder_id`. Lists use literal normalized name substrings, stable folder-ID pagination (default 100, range 1–1000) and member counts. `show` reports the folder and saved coverage; no model is loaded, and a supplied profile must be valid. Browse members with `management photos --folder-id <id>`; `management photo <id>` includes its folders.

The Skill resolves actual IDs from the selected album and writes a JSON array for add/remove, including a one-element array for one photo. `[]` means zero changes, never all photos. Validate the folder and every photo before any write; invalid IDs roll back the whole batch. Duplicate IDs are deduplicated; repeated add or removing a valid nonmember reports unchanged counts. Create/rename/delete/add/remove explicitly open a writer and commit atomically; list/show remain read-only.

Removing membership or deleting a folder never deletes photo records, originals, thumbnails or embeddings and never removes memberships in other folders. Members bind stable photo IDs: content/path/profile changes do not regroup them. No hierarchy, cross-album folders, folder-specific embeddings, jobs or live rules are added.

## Optional one-time organization

### Add explicitly selected search results

Choose the user's destination folder, creating their custom name if needed. For semantic candidates use:

```text
management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>
```

`--search-snapshot` reuses `management.select_search_results` to validate album, candidate identity, selected subset and current selected photo/input/result identities in the membership transaction. It does not repeat the query, encode images or inspect their content. Selection still uses embedding-derived numbers only; do not automatically add all top-K, remove source memberships or treat folder labels as semantic evidence. Source scope/names remain historical even if source folders later change or disappear; selected photo identity changes are still stale errors.

For explicitly selected finalized condition results, use:

```text
management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>
```

The two snapshot flags are mutually exclusive. A supplied JSON `null` is invalid, never plain-manual fallback. `condition_search.select_results` validates finalized stage, album, selected subset and current selected sources inside the explicit membership transaction before applying old membership logic. `[]` makes no changes. Response `source_query` is separate from existing `source_search`; neither source invokes a query, classification or model. Querying alone never adds folders or members.

### Plan and confirm EXIF date organization

```text
management folders organize-date --all --granularity year|month|day --output <date-plan.json>
management folders organize-date --ids-file <photo-ids.json> --granularity year|month|day --output <date-plan.json>
management folders apply-date-plan <date-plan.json> --confirm <digest>
```

Planning requires exactly one of `--all` or `--ids-file`, independent of browse pagination/top-K; an empty ID array is an empty scope. It reads saved EXIF `datetime_original` only, requiring a valid calendar date in camera-local time with no UTC conversion. It produces flat `YYYY`, `YYYY-MM` or `YYYY-MM-DD` names. Missing/invalid dates and unavailable ingestion metadata are skipped with counts; no fallback to modification time, other date fields or original-file reads. This authorized deterministic operation is not semantic evidence and needs no index/model.

Planning is read-only. CLI JSON wraps `plan`, `digest`, `output`, `album`, `model_calls: 0` (and `image_model_calls: 0`); the output file contains the raw plan, not this envelope. Review its exact scope, member IDs, create/reuse preview, skips and digest; the digest itself is not consent. Apply only the approved plan. It revalidates album, saved inputs and target identities/names, reports conflicts/staleness explicitly, and atomically creates/reuses folders and adds members. No partial writes, jobs/new tables, live rules or replacement of other manual members. Later imports or manually removed photos are never automatically regrouped. Plan exports use existing output protection and cannot overwrite their ID input file.

## Browse saved photos

`photos` lists the selected album's photos; `photo <id>` inspects one record. Neither rescans sources, stats originals, repairs paths nor writes the database. No model or default profile is needed. Without one, `embedding_configuration` is `not_configured`; a supplied profile ID must still be valid. A configured profile supplies saved coverage, not inference.

Records expose both `original_absolute_path` and nullable `original_relative_path`, plus separate `ingest_state` and `original_status`. Original verification is `not_checked`, regardless of stored availability. Saved previews remain browsable while originals are offline. JSON metadata inspection does not decode every JPEG BLOB; HTML/thumbnail export validates the previews it uses and reports corrupt/changed inputs.

`photos` and metadata search sort by stable photo ID, default limit 100, range 1–1000. Follow non-null `next_cursor` with `--after` and retain the album/query/profile and folder scope. Pages are not a durable multi-page transaction; re-query after changes.

## Folder scope for browse and both search modes

```text
management photos --folder-id <a>
management search "IMG" --mode metadata --folder-id <a> --folder-id <b> --folder-match union
management search "trees" --mode semantic --folder-id <a> --folder-id <b> --folder-match intersection
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

No model, weights, credentials or embeddings are required. A blank query is an error. No hits return an empty page without silently switching modes.

## Semantic photo search

```text
management search "黑白的枯树" --mode semantic
management search "black and white leafless trees" --mode semantic --profile-id <profile-id> --limit 10
```

Search uses the explicit profile, otherwise the deliberately configured default. If neither exists, explain [index setup/configuration](index.md); never choose silently. If setup used another cache root, supply the same **global** `--model-cache-dir <cache-root>` before `management`. It selects the local pinned files, not an automatic download. Cache configuration is irrelevant to metadata inference because metadata mode performs none.

Image and query encoders must share the **same immutable profile**, not just dimensions/model family. The first profile is `google/siglip2-base-patch16-224`, revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, PyTorch CPU FP32. Persisted vectors represent whole saved previews: paired `image_text_semantic` space, image modality, `stored_thumbnail` input, `whole_image` granularity, 768 dimensions. Query vectors are transient.

Queries use the matching tokenizer/text feature interface directly, without translation or retired retrieval prefixes. The limit is 64 tokens including EOS; overlong queries produce `QUERY_TOO_LONG`, not silent truncation.

A consistent saved-data snapshot supplies current photo/preview identities and vectors within the resolved folder/album scope. Invalid/stale/absent results are excluded and reported; semantic ranking checks saved input metadata and vector integrity, not original files or all preview JPEG bytes. Optional HTML rendering validates its embedded previews separately.

With no eligible candidates, search returns empty results and coverage without loading a model. Otherwise it encodes the query once and ranks locally by exact cosine similarity. It never regenerates image vectors, mixes profiles, changes defaults, installs/downloads weights or falls back to a cloud service.

Semantic search defaults to top 10, accepts 1–1000 and **rejects `--after`**. Results sort by descending similarity, then photo ID. Coverage includes the **entire selected scope**, not only top-K: distinguish `ready`, `missing`, `stale`, `invalid_input` and `invalid_vector` from old task failures and source availability. Incomplete coverage must not be described as searching every photo.

Semantic result rows deliberately omit photo metadata, filenames, paths and thumbnail payloads. They carry record/input identities plus `score`, `candidate_rank`, `score_gap_from_best` and `score_gap_to_next`. The latter is the gap to the next ranked valid candidate, which may be outside the returned top-K; `null` means there is no next ranked candidate. These are embedding-derived numbers, not object labels or probabilities. `display_stage: candidates` marks the raw retrieval output and `selection_evidence: embedding_similarity_only` describes the selection policy.

Scores are relative similarities, not probabilities, guaranteed logical filters, detection counts or technical measurements. Highest-ranked need not be relevant. Do not promise calibrated thresholds, exact negation/count behavior or focus/blur analysis.

### Select what to display without sending images to the agent

The Skill uses only the query and embedding-derived scores/ranks/gaps to select display IDs. It must not inspect originals, thumbnails, screenshots or image-containing HTML. No images are passed to the agent for relevance decisions. This is intentionally heuristic and may omit relevant photos or include false positives.

```text
management search "有人物的照片" --mode semantic --output <candidates.json>
management show-results <candidates.json> --ids-file <selected.json> --html <results.html> --output <displayed.json>
```

`selected.json` is an array of photo IDs from that exact snapshot. Unknown IDs, malformed candidates, another album or stale selected input/result identities are errors. Duplicates are deduplicated; original candidate order, validated saved scores, ranks and score gaps are retained verbatim, including a gap to the next candidate outside returned top-K. Gaps are not recomputed from the selected subset. The output records the source snapshot ID and selected/candidate counts. Candidates, `show-results` and reports preserve historical scope and names after folder rename/deletion or membership changes; they do not re-query current membership. It does not forward arbitrary input fields or image payloads, write the database, or call an encoder. Its selection method is `explicit_candidate_ids`, not an automatic classifier.

An empty selection produces an empty report scoped to the retrieved candidates. It does not prove no match exists among photos outside top-K or without indexes. Selected JSON remains numeric/identity-only. Local HTML rendering can read matching SQLite previews and names **for the user's display**; the agent must return the file link without reading/attaching those images. Raw `search --html` remains an explicitly labeled, unfiltered diagnostic, not the default Skill flow. Input candidate/selection files cannot be overwritten by the selected-result export.

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

Views use **`album-snapshot-v2`**, with schema/version, album UUID/path, mode, explicit scope/coverage scope and selected profile identity. Browse/metadata records use `items`; semantic rankings use `results` with result/profile IDs, input identity and scores. A snapshot is not live state, and image-embedding readiness does not mean future technical components are complete.

Exports protect the database, journal/WAL/shared-memory/execution-lock sidecars, model cache and recorded original paths. There is no blanket prohibition on the database parent directory. JPEG preview exports must additionally stay outside saved scan source directories. Choose dedicated output files; a normal report/preview export is not a no-overwrite backup command.

HTML escapes data, embeds validated previews and binds them to the snapshot's album/input identities. Changed/missing/corrupt previews display errors instead of substitute images. No original access is required. There are no selection checkboxes, membership controls, live server or retained `search-add` protocol.

Historical folder regressions do not validate stage-one features; see [validation status](../../docs/TODO.md). Claim no real-model performance, quality, memory, timing, disconnected operation or cross-host support without recorded verification; synthetic tests and authorization alone are not acceptance.
