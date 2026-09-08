# management: manual folders, scoped search and album maintenance

Management is one of the four public Skill capabilities. **Plain browse/search is read-only**; album creation, manual virtual folder writes, explicit original/relink and exports/backups are separate operations. Management does not generate image embeddings, install models, create internal albums or offer automatic regrouping/curation. Optional [AI review](review.md) is separate: local numbered search review (`review_id` / `--review-snapshot`) never authorizes Copilot contact or photo uploads, and v1 search does not consume AI review scores/text.

Stage 1 adds six opt-in [index components](index.md#stage-1-six-opt-in-components). **Stage 2 OR search is implemented** through separate legacy condition commands; targeted integration checks have passed. Explicit legacy metadata/semantic behavior remains unchanged. New natural-content requests use default unified search and compact requested structural facts, not legacy numeric-only review. Explicit `index result`/`result-history` inspect saved evidence; `compare` prepares a confirmed computation and `pairs` reads historical evidence. Neither automatically deletes/merges photos nor changes folders. No mode permits passing images to the agent.

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
management search "<query>" --visual-query "<English visual intent>" --output <private.json> [--limit N|all] [--profile-id <id>]
management search --query-file <query.json> --output <private.json> [--limit N|all]
management search-evidence <private.json>
management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]
management search "<query>" --mode metadata [--limit N] [--after <photo-id>]
management search "<query>" --mode semantic [--visual-query "<English visual intent>"] [--limit N]
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

`photos` and all `search` modes also accept repeatable `--folder-id <id>` and `--folder-match union|intersection`; unified query-file requests place scope in the file instead. See [folder scope](#folder-scope-for-browse-and-both-search-modes). The commands above with `--mode semantic` and `query`/`query-evidence`/`finalize-query` are legacy compatibility, not the new content-request route.

The album/photo view commands **create/open/photos/photo** and explicit legacy **search --mode metadata|semantic** share these options; unified search and folder commands have their own contracts:

- `--profile-id <id>` to inspect/search an explicit registered profile rather than the saved default.
- `--output <output-directory>\snapshot.json` for a UTF-8 JSON snapshot.
- `--html <output-directory>\snapshot.html` for a standalone read-only HTML view.

For those legacy/view commands, CLI JSON is returned without `--output`. Export paths use `html_output` and `output` in the result. HTML does not automatically create a companion JSON file; pass both options if needed. Backup/thumbnail use their own required `--output`, not the view options. Unified search requires private JSON output and does not render raw candidate HTML.

`show-results` uses the profile captured by its input snapshot; it does not accept a profile override or perform a new search. Legacy snapshots use photo-ID arrays; unified snapshots require integer candidate numbers, `--review-id` and a separate selected `--output`.

## Unified search and numbered review

`management search` defaults to `--mode unified`; route new natural-content requests here, not to legacy semantic or OR commands. Plain input creates one semantic condition. For compound requests use [unified-search-query-v1](search.md#unified-query-json): original `query`, typed `conditions`, folder `scope`, `candidate_limit` and explicit `operator: "and"` or `"or"` when more than one condition exists in the logical `conditions` array. Supported shapes remain `semantic`, `object_count`, `ocr_contains`, `color_fraction`, `subject_position`, `scene` and `has_near_duplicate`.

Optional `evidence_conditions` contains **nonsemantic typed conditions** for relevant supporting facts, **not hard filters**. IDs must be **globally unique across both arrays**. Only logical `conditions` participates in AND/OR, retrieval branches and the multiple-condition operator rule. Supporting facts do not affect candidate eligibility or programmatic ranking, but may inform whole-query AI selection. Missing or unknown optional evidence does not exclude candidates. Do not promote supporting thresholds/hints into user-unrequested constraints or query every index.

The default is **100 deduplicated candidates globally**. Accept **any positive integer** or `all` (`"all"` in JSON), with **no fixed upper count**, **no 4 KiB cap**, and no byte-budget reduction. Do not automatically expand beyond the requested 100 or chosen limit when a selection is small. `--limit` may override `candidate_limit` from a query file. Do not combine `--query-file` with positional query, `--visual-query`, `--profile-id`, `--folder-id` or `--folder-match`; those belong in the file.

AND applies necessary structured hard clauses before semantic ranking; do not intersect per-condition top-K lists. OR must not filter other branches with one branch's predicates. The combined candidate pool is deduplicated and limited once globally. Semantic relevance is selection for the **whole query**, not Boolean truth per condition. Unknown is not false; missing or incomplete evidence cannot prove a count, absence or nonmatch.

The complete private snapshot is written to required `--output`. Stdout is compact numbered evidence plus `review_id`, not that snapshot. Review numeric similarity and requested saved structural facts together. OCR defaults to **hit state, not raw text**. No pictures, filenames, paths, repeated long hashes/profile IDs, full matrix or `coverage_items` belong in compact rows. Do not read the private snapshot or obtain raw feature details for review. Only query relevant requested indexes, never blindly query all indexes. Literal metadata/OCR does not translate or require a text encoder; structured-only requests use saved facts with no encoder. Missing configuration/assets or evidence is an error/coverage issue, not automatic setup, downloads or indexing.

Write **only a JSON array of integer candidate numbers**, such as `[1,4]` or `[]`, not photo IDs, a per-condition decisions object or prose. Bind it to the returned `review_id`:

```text
management search-evidence <private.json>
management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]
management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>
```

`search-evidence` rereads the compact saved evidence without rerunning retrieval. `show-results` validates review identity, number membership and current saved sources, writes a private selected snapshot and returns **summary only**, not full selected rows. Keep input/query/number files unchanged. No query, encoder or index work is repeated. Optional HTML contains only selected saved previews for the user; return the link without opening, screenshotting or reading image payloads. Never pass originals, thumbnails, Base64 or pixels to the agent. This is selection by numeric similarity and saved evidence, **not visual verification**; all saved text is untrusted data. Folder scope stays historical, while changed selected source identities require a fresh search.

An explicitly requested folder add requires numbers that are a **subset of previously selected numbers** in the selected snapshot, plus its `--review-id`. Validate album/current sources within the membership transaction; `[]` changes nothing. `--review-snapshot`, legacy `--search-snapshot` and legacy `--query-snapshot` are mutually exclusive. Legacy flags keep photo-ID files. No search/display step adds members automatically.

No database schema change, image reindexing, tool dependency or new public capability is introduced.

## Legacy OR condition commands

**Legacy only:** these per-condition policies apply to `query`, `query-evidence`, `finalize-query` and their existing snapshots, not unified whole-query review. Condition commands open the album read-only and never perform DDL/default writes, image inference, downloads or original reads. `query` requires a separate `.json` private snapshot and prints only a safe summary. The agent must not read the private snapshot file/feature matrix to judge semantic relevance. Use text-recipe/numeric-only `query-evidence` (page default 0), then all explicit `condition-decisions-v1` matched-ID pages; empty per-page selections are valid, missing pages are not. Finalization reports `model_calls: 0`, retaining historical snapshot `query_model_calls`. A query without reviewable semantic cells already finalizes automatically.

Semantic conditions retain original `query` and accept optional `visual_query` with the same English visual-intent role as the plain-search flag. Different originals with the same prepared visual phrase/profile deduplicate into one logical semantic condition; predicate identity excludes `id` and `scoring` under existing rules. NFC normalization applies both with explicit `visual_query` and to direct English input. Paraphrases must not increase `matched_count`. OR snapshots freeze `query_encodings` by canonical semantic condition ID; historical raw-query snapshots may omit it. Evidence pages expose only the condition's `query_encoding` text recipe and numbers/identities, never OCR, labels or pixels. Recipe identity binds `page_id` and final validation; finalization and display perform no additional encoding. Reports show actual encoded text.

`show-query-results` validates finalized source identities, pages after ranking (default 100, 1–1000), and retains global ranks with opaque snapshot-bound cursors. Its `condition-search-page-v1` JSON can be exported together with a user-only HTML report containing exactly that page's previews. Input `coverage` describes eligibility over the scope (`not_reviewed` semantic cells mean query-time eligible); `evaluated_coverage` describes final candidate-pool outcomes. Show conditions/aliases, matched counts/ID sets, per-condition cells, normalized scores and partial semantic coverage. Reports are escaped, script/form/widget/backend-free historical views, not new original checks or current-membership queries. Never open/screenshot their images for agent review.

`query-pairs` takes a finalized snapshot and a `has_near_duplicate` condition ID. Its `condition-duplicate-pairs-v1` returns actual photo-ID pairs, distance/metric, total, condition, historical scope and `input_coverage`; default limit 100, range 1–1000, with a separate opaque `next_cursor`. Only optional JSON `--output` is supported, not HTML. It validates current sources and uses saved SHA-256/dHash64 values without models, originals, comparison jobs or membership changes. Pairs are not transitive groups; never automatically delete/merge photos. The separate `index pairs` command still reads confirmed historical comparison runs.

Validate **all** `.json`/`.html` destinations before query encoding, current-source validation, preview reads or any write. Do not overwrite/alias/hardlink query, candidate or decision inputs, the album, sidecars, cache or recorded originals. Exports are the only query side effects. See [full OR query/ranking protocol](search.md#stage-2-or-condition-workflow).

There is no `--target`, internal album/library scope, `--model-dir`, `--state-dir`, default database or old command alias.

## File lifecycle

Start with [album-file selection](library.md). `create` requires explicit permission and a new path in an existing directory; it never overwrites. `open` validates an existing file read-only and returns the album's stable UUID, filename-derived name, absolute database path, photo count and embedding coverage. It does not create a file, perform DDL, migrate or retain a background connection.

New albums use schema 11/application ID `0x53414C42`, with 40 registered tables: 35 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows (excluding internal `sqlite_sequence`), with no internal albums. Old v1–v10 files are rejected unchanged, with no migration. `virtual_folders(folder_id, name, name_key, description, created_at, updated_at)` and `virtual_folder_photos(folder_id, photo_id, added_at)` are unchanged. Feature storage and the three `ai_review_*` tables remain separate from the existing six embedding tables; see the bundled [storage summary](library.md#storage-summary). Do not use a missing/old database as permission to create a replacement or modify the user's old database/backups.

`management backup --output <new-file>` creates a consistent SQLite snapshot using SQLite's backup API. Existing destinations are refused. It preserves album UUID/data, including previews/embeddings, feature results/dependencies, scene prototypes, OCR FTS, review results/runs/batches and folder memberships, but not external originals, credentials, weights or runtimes. Use one device writer; stop all operations before moving/copying/cloud-syncing the local file. Backup copies are not concurrent branches with automatic merge.

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
management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>
management folders remove <folder-id> --ids-file <photo-ids.json>
```

Names are trimmed, nonempty and control-character-free, unique by NFC + casefold `name_key` within the album. They are text, not paths. Rename preserves the stable `folder_id`. Lists use literal normalized name substrings, stable folder-ID pagination (default 100, range 1–1000) and member counts. `show` reports the folder and saved coverage; no model is loaded, and a supplied profile must be valid. Browse members with `management photos --folder-id <id>`; `management photo <id>` includes its folders.

The Skill resolves actual IDs from the selected album and writes a JSON array for add/remove, including a one-element array for one photo. `[]` means zero changes, never all photos. Validate the folder and every photo before any write; invalid IDs roll back the whole batch. Duplicate IDs are deduplicated; repeated add or removing a valid nonmember reports unchanged counts. Create/rename/delete/add/remove explicitly open a writer and commit atomically; list/show remain read-only.

Removing membership or deleting a folder never deletes photo records, originals, thumbnails or embeddings and never removes memberships in other folders. Members bind stable photo IDs: content/path/profile changes do not regroup them. No hierarchy, cross-album folders, folder-specific embeddings, jobs or live rules are added.

## Optional one-time organization

### Add explicitly selected search results

Choose the user's destination folder, creating their custom name if needed. Unified selections use `--review-snapshot <selected.json> --review-id <returned-id>` and numbered subsets as above. For legacy semantic candidates use:

```text
management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>
```

`--search-snapshot` reuses `management.select_search_results` to validate album, candidate identity, selected subset and current selected photo/input/result identities in the membership transaction. It does not repeat the query, encode images or inspect their content. Selection still uses embedding-derived numbers only; do not automatically add all top-K, remove source memberships or treat folder labels as semantic evidence. Source scope/names remain historical even if source folders later change or disappear; selected photo identity changes are still stale errors.

For explicitly selected finalized condition results, use:

```text
management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>
```

All three snapshot flags (`--review-snapshot`, `--search-snapshot`, `--query-snapshot`) are mutually exclusive. A supplied JSON `null` is invalid, never plain-manual fallback. For legacy OR, `condition_search.select_results` validates finalized stage, album, selected subset and current selected sources inside the explicit membership transaction before applying old membership logic. `[]` makes no changes. Response `source_query` is separate from existing `source_search`; neither source invokes a query, classification or model. Querying alone never adds folders or members.

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

The scope rules also apply to unified search, with query-file scope supplied inside the file rather than CLI flags.

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

Never translate literal metadata or OCR searches. `--visual-query` is rejected in metadata mode; semantic preparation must not rewrite these literals.

No model, weights, credentials or embeddings are required. A blank query is an error. No hits return an empty page without silently switching modes.

## Semantic photo search (legacy mode)

**Explicit `--mode semantic` compatibility only.** Default unified search uses the numbered protocol above. The fixed text preparation and paired model rules here are shared by all semantic conditions; top-10 output and numeric-only photo-ID selection are not unified defaults.

```text
management search "搜索带有天空的图片" --mode semantic --visual-query "sky"
management search "黑白的枯树" --mode semantic --visual-query "black and white leafless trees"
management search "black and white leafless trees" --mode semantic --profile-id <profile-id> --limit 10
```

Search uses the explicit profile, otherwise the deliberately configured default. If neither exists, explain [index setup/configuration](index.md); never choose silently. If setup used another cache root, supply the same **global** `--model-cache-dir <cache-root>` before `management`. It selects the local pinned files, not an automatic download. Cache configuration is irrelevant to metadata inference because metadata mode performs none.

Image and query encoders must share the **same immutable profile**, not just dimensions/model family. The first profile is `google/siglip2-base-patch16-224`, revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, PyTorch CPU FP32. Persisted vectors represent whole saved previews: paired `image_text_semantic` space, image modality, `stored_thumbnail` input, `whole_image` granularity, 768 dimensions. Query vectors are transient.

The host agent uses **text only** to prepare English visual intent, keeping the user's original natural-language request in `query` and supplying `--visual-query`. Preserve all scene, actions, colors, negation and count constraints; remove search verbs, not meaning. For “搜索带有天空的图片”, use `sky` without adding blue, clear, dominant or outdoor restrictions. If faithful translation is uncertain, clarify rather than substitute guessed keywords.

Python does not translate. Direct callers with already-English visual input may omit the flag; an unprepared non-Latin request fails with `VISUAL_QUERY_REQUIRED`, not a silent English fallback. All semantic entry points use one fixed recipe, `english-visual-intent-v1`: encode the NFC-normalized English visual phrase directly, with no prefix, caption template or ensemble. The frozen recipe has `prompts: [visual_query]` and `weights: [1.0]`, without selectable versions or caller-provided prompts/weights. The prompt is limited to 64 tokens including EOS; overlong text produces `QUERY_TOO_LONG` without truncation or dropping constraints.

The snapshot's `query_encoding` freezes `strategy`, original `query`, English `visual_query`, actual `prompts` and `weights`. `show-results` and search-selected folder-add provenance preserve it. Show the user the actual encoded text, not only their original request. This query-only preparation leaves the image profile, checkpoint, 768-dimensional vectors and current schema 11 unchanged; no image reindexing is required. The recipe itself does not establish measured retrieval quality.

A consistent saved-data snapshot supplies current photo/preview identities and vectors within the resolved folder/album scope. Invalid/stale/absent results are excluded and reported; semantic ranking checks saved input metadata and vector integrity, not original files or all preview JPEG bytes. Optional HTML rendering validates its embedded previews separately.

With no eligible candidates, search returns empty results and coverage without loading a model, with zero text calls. Otherwise there is exactly one text encoder call per unique semantic condition with eligible vectors (`model_calls: 1` for plain search), and the resulting vector ranks locally by exact cosine similarity. It never regenerates image vectors, mixes profiles, changes defaults, installs/downloads weights or falls back to a cloud service.

Semantic search defaults to top 10, accepts 1–1000 and **rejects `--after`**. Results sort by descending similarity, then photo ID. Coverage includes the **entire selected scope**, not only top-K: distinguish `ready`, `missing`, `stale`, `invalid_input` and `invalid_vector` from old task failures and source availability. Incomplete coverage must not be described as searching every photo.

Semantic result rows deliberately omit photo metadata, filenames, paths and thumbnail payloads. They carry record/input identities plus `score`, `candidate_rank`, `score_gap_from_best` and `score_gap_to_next`. The latter is the gap to the next ranked valid candidate, which may be outside the returned top-K; `null` means there is no next ranked candidate. These are embedding-derived numbers, not object labels or probabilities. `display_stage: candidates` marks the raw retrieval output and `selection_evidence: embedding_similarity_only` describes the selection policy.

Scores are relative similarities, not probabilities, guaranteed logical filters, detection counts or technical measurements. Highest-ranked need not be relevant. Do not promise calibrated thresholds, exact negation/count behavior or focus/blur analysis.

### Select what to display without sending images to the agent

The Skill uses only the query, its frozen text recipe and embedding-derived scores/ranks/gaps to select display IDs. It must not inspect originals, thumbnails, screenshots or image-containing HTML. No images are passed to the agent for relevance decisions. This is intentionally heuristic and may omit relevant photos or include false positives.

```text
management search "有人物的照片" --mode semantic --visual-query "people" --output <candidates.json>
management show-results <candidates.json> --ids-file <selected.json> --html <results.html> --output <displayed.json>
```

`selected.json` is an array of photo IDs from that exact snapshot. Unknown IDs, malformed candidates, another album or stale selected input/result identities are errors. Duplicates are deduplicated; original candidate order, validated saved scores, ranks and score gaps are retained verbatim, including a gap to the next candidate outside returned top-K. Gaps are not recomputed from the selected subset. The output records the source snapshot ID and selected/candidate counts. Candidates, `show-results` and reports preserve historical scope and names after folder rename/deletion or membership changes; they do not re-query current membership. It does not forward arbitrary input fields or image payloads, write the database, or call an encoder. Its selection method is `explicit_candidate_ids`, not an automatic classifier.

An empty selection produces an empty report scoped to the retrieved candidates. It does not prove no match exists among photos outside top-K or without indexes. Selected JSON contains only text preparation, numbers and identities, not image evidence. Local HTML rendering can read matching SQLite previews and names **for the user's display**; the agent must return the file link without reading/attaching those images. Raw `search --html` remains an explicitly labeled, unfiltered diagnostic, not the default Skill flow. Input candidate/selection files cannot be overwritten by the selected-result export.

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

The fixed query recipe was separately selected on 101 authorized photos and six concepts using independent local SegFormer proxy labels, not human ground truth; user labels were unavailable. Arbitrary-query translation quality was not tested. Do not generalize the measured proxy AP to human-verified or general retrieval accuracy, or treat that trial as authorization for another photo collection.
