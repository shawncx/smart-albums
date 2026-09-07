# Portable album, image embeddings and stage-one feature design

This is the current schema 10 contract, including stage-one feature indexing and stage-two read-only queries, not a benchmark. The [portable-album plan](portable-album-plan.md) records the earlier schema 8 milestone; this document supersedes its format/scope contract without rewriting that history. See [validation status](TODO.md) for the tested scope. The [earlier plan](ingestion-index-management-plan.md) is also historical, not a live progress record. **Stage 2 OR search is implemented** through separate management commands; targeted integration checks have passed and metadata/semantic modes and defaults remain unchanged.

## 1. Exactly three public capabilities

| Capability | Responsibility | Boundary |
| --- | --- | --- |
| ingestion | Import sources, metadata, dual original paths and proportional SQLite JPEG previews | No inference, automatic indexing or original deletion |
| index | Explicit setup/configure/plan/execute/status/job/resume for image embeddings and opt-in OCR/objects/scene/color/composition/perceptual_hash, plus result/history, prototype plans and comparisons | No description intermediate, cloud fallback, automatic upstream indexing or search behavior change |
| management | Album-file create/open/backup, manual virtual folders, scoped photo browse/search, one-time date organization, preview/scan diagnostics and original/relink | Browse/search is read-only; no internal albums, selection widgets, automatic regrouping or photo deletion |

These are independent operations within one `smart-albums` Skill. **One album is one SQLite file**, not a container of user-selected internal albums. Select/open or explicitly create the file before data operations; informational questions and `--help` need no file. Switching files clears previous photo/profile/run/folder choices and pending confirmations.

Global `--database <absolute-file>` is required, accepts `.sqlite`, `.sqlite3` and `.db`, and has no hidden default. Optional global `--model-cache-dir <root>` consistently configures machine-local weights for index and semantic search. Both precede the capability. No `--state-dir`, internal album/library selectors, old aliases or API compatibility layer remain. Removed analysis/description and `search-add` workflows are not fallbacks.

## 2. File lifecycle and format boundary

- `SQLiteStorage.create(path)` exclusively claims a nonexistent destination, then initializes in a transaction. The destination parent must already exist. It cannot overwrite an existing file; failure cleans up only the file it owns.
- `SQLiteStorage.open(path, writable=False)` opens an existing file with SQLite URI `mode=ro`; explicit writers use `mode=rw`, never automatic-create mode.
- Open verifies the application marker/version, exact table set, required columns, one valid album metadata row, integrity and foreign keys. It performs no DDL, automatic repair, cleanup or migration.
- New albums use `PRAGMA application_id = 0x53414C42`, `PRAGMA user_version = 10` and rollback journaling rather than default WAL. Commands close connections on completion.
- Missing/invalid/unrelated/unsupported files and storage access failures are errors, not creation permission. **Existing v1–v9 databases are rejected unchanged.** The user's old database/backups remain untouched; there is no migration and a new file is not an implicit conversion.

Management create/open returns `album: {id, name, database_path, ...}`. The ID is a stable UUID; the name is the current filename without extension. Moving/renaming changes location/display name, not identity. Backups/copies retain the UUID and are not independently mergeable branches.

Plain open/browse/search uses read-only storage and never stats originals or writes path repair. `management original`/`relink` are explicit path-maintenance exceptions: they first resolve/check, then open a writer only when persistence is needed. A usable path whose repair cannot be saved is reported as an error with `persisted: false`, not success or missing data.

## 3. Schema 10: 37 registered tables

The actual schema contains **32 ordinary tables + one external-content FTS5 virtual table + four shadow tables = 37 registered tables**. SQLite additionally owns `sqlite_sequence` for pair IDs; it is not a registered application/FTS table (38 table entries including it). The previous 13 ordinary tables below remain, including the unchanged six `image_embedding_*` tables:

| Table | Purpose |
| --- | --- |
| `album_metadata` | Exactly one row containing the stable album UUID and creation time |
| `photos` | Photo identity, dual paths, ingested version/metadata, ingestion state and original-location state |
| `thumbnails` | Current per-photo JPEG BLOB, input version, preview profile, dimensions and hash |
| `scans` | Source absolute/database-relative paths, scan status and saved result |
| `scan_events` | Per-file outcomes/errors, with stable event IDs |
| `image_embedding_profiles` | Immutable paired image/text semantic profile JSON and identity |
| `image_embedding_results` | Versioned whole-image vectors, input/profile identity and validation data |
| `image_embedding_runs` | Frozen plans/digests, approval, execution state and summaries |
| `image_embedding_items` | Per-photo snapshots/actions, result links, attempts and errors |
| `image_embedding_claims` | Exclusive current-input execution ownership |
| `image_embedding_settings` | This album's explicitly chosen default image/text profile |
| `virtual_folders` | Stable folder ID, display name/unique normalized key, optional description and timestamps |
| `virtual_folder_photos` | Static folder/photo memberships and addition time |

Nineteen additional ordinary tables separate new feature evidence from embedding storage:

| Table | Purpose |
| --- | --- |
| `image_feature_profiles` | Immutable component/provider/recipe/runtime/assets/dependency profile |
| `image_feature_results` | Successful typed result identity, input manifest/fingerprint, completeness and canonical payload/hash |
| `image_feature_dependencies` | Same-photo composition → exact objects result/profile/payload dependency |
| `image_feature_embedding_dependencies` | Same-photo scene → exact existing image-embedding result/profile/vector dependency |
| `image_feature_settings` | Independently selected default per component |
| `image_feature_runs` | Frozen extract/prototypes/compare plans, approval and progress |
| `image_feature_items` | Immutable input/action snapshot, mutable attempts/state/error/result/progress |
| `image_feature_claims` | Exclusive planned-input execution ownership |
| `image_ocr_documents` | One authoritative original/normalized text document per OCR result, integer document ID and block count |
| `image_ocr_blocks` | Ordered text/quadrilaterals and nullable SDK scores |
| `image_object_instances` | Ordered class IDs, scores and normalized xyxy boxes |
| `image_scene_scores` | Complete catalog of scene IDs and cosine scores |
| `image_scene_prototype_sets` | Immutable scene-profile catalog set linked to its embedding profile |
| `image_scene_prototypes` | Ordered labels/prompts and normalized text vectors/hashes |
| `image_color_features` | Whole-image saturation/hue distribution statistics |
| `image_color_palette` | Ordered RGB palette entries and pixel fractions |
| `image_composition_features` | Nullable selected-subject geometry, union/bounding areas and uncovered fraction |
| `image_perceptual_hashes` | Versioned dHash64 algorithm/bits/BLOB fingerprint |
| `image_similarity_pairs` | Ordered endpoints, source versions/results, metric/distance and comparison-run provenance |

FTS is registered explicitly, not by arbitrary suffix acceptance:

| Table | Kind |
| --- | --- |
| `image_ocr_fts` | External-content FTS5, `content='image_ocr_documents'`, `content_rowid='document_id'`, `tokenize='trigram'` |
| `image_ocr_fts_data` | FTS shadow |
| `image_ocr_fts_idx` | FTS shadow |
| `image_ocr_fts_docsize` | FTS shadow |
| `image_ocr_fts_config` | FTS shadow |

Format checks validate the exact registered table/column/schema relationships, including the virtual-table configuration and its legitimate shadows. OCR documents are authoritative; transactional maintenance and explicit `index rebuild-fts --confirm` rebuild derived normalized text without models or original reads. Read-only open/result/search never repairs it. Stage 2 `management query` already supports `ocr_contains`: normalized queries of 1–2 characters use literal `INSTR` over saved documents; queries of 3+ characters use trigram FTS5 MATCH plus a literal `INSTR` check, restricted to validated saved results. Neither path performs OCR inference.

There are no `libraries`, `albums`, `album_photos`, `image_index_*`, old analysis/text-vector tables, compatibility views or empty `technical_*` placeholders. An empty claims table remains necessary. Source directories are scan scope, not another resource to select.

Photo paths are explicit queryable columns, not duplicated inside metadata JSON:

```text
photo_id
original_absolute_path
original_relative_path         nullable; relative to the current SQLite parent
content_version                successful ingested bytes' SHA-256
thumbnail_profile
size_bytes / mtime_ns / metadata_json
ingest_state / last_ingest_error
original_status / last_original_check / last_path_error
created_at / updated_at / path_updated_at
```

`ingest_state` is `available|error`; `original_status` is `not_checked|available|missing|unavailable`. Availability of an original is not validity of an ingested preview or an embedding. A path repair never clears an ingestion error.

### Static virtual-folder contract

```text
virtual_folders(folder_id, name, name_key, description, created_at, updated_at)
virtual_folder_photos(folder_id, photo_id, added_at)
```

Names are trimmed, nonempty and control-character-free, with unique NFC + casefold `name_key` per album. The stable `folder_id` survives rename; timestamps track creation/actual changes. Names/descriptions are untrusted text, not paths or instructions. Memberships have foreign keys to folders/photos, primary key `(folder_id, photo_id)` and reverse index `(photo_id, folder_id)`; no duplicated photo/path/vector records.

Manual custom CRUD/add/remove is the primary workflow and requires no index. Empty folders and static, flat many-to-many memberships are supported inside this one SQLite album. The Skill resolves actual IDs and writes a one-element JSON array for a single-photo request. Validate all folder/photo IDs and atomically write the whole batch; invalid IDs roll back, duplicates/nonmember removals report unchanged counts, and `[]` means zero changes, never all photos. Folder lists include counts and `photo` includes memberships. Explicit folder writes use writable storage; list/show/browse remain read-only.

Removing membership/deleting a folder never deletes photo records, originals, thumbnails or embeddings and never removes other memberships. Relations bind stable photo IDs; content/path/profile changes do not regroup them. Backups/moves preserve them. No hierarchy, cross-album folders, folder embeddings, jobs or live rules/new capability are added. Full CLI: [management](../photography/references/management.md).

### Optional one-time organization

For explicitly selected semantic candidates and a user-chosen destination, `management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>` reuses `management.select_search_results` validation of album/candidate/selected input identities. It performs no new query or image encoding, never defaults to all top-K and does not remove source memberships.

`management folders organize-date (--all|--ids-file <ids.json>) --granularity year|month|day --output <date-plan.json>` prepares a read-only exact-scope plan, independent of pages/top-K. Use only saved EXIF `datetime_original`, calendar-valid camera-local dates with no UTC conversion or fallback to modification/other dates. Missing/invalid dates and unavailable ingestion metadata are skipped with counts, without reading originals or calling a model. This is authorized deterministic organization, not semantic evidence.

The plan previews create/reuse targets (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`), explicit member IDs, skips and digest. CLI output wraps `plan`, `digest`, `output`, `album`, `model_calls: 0`; the file contains the raw plan. `management folders apply-date-plan <date-plan.json> --confirm <digest>` requires actual exact-plan approval, revalidates album/targets/photo inputs, explicitly rejects conflicts/staleness and atomically creates/reuses folders and adds members. It adds no jobs/tables or live rules, does not replace other manual members and never automatically regroups later imports or manual removals.

## 4. Original paths and ingestion identity

SQLite can be beside, above, below or on another drive from photos. Shared `source_paths.py` interprets the absolute location and a nullable slash-separated relative path, based on the **actual database parent**. Relative paths may contain `..`; neither CWD nor scan/cache roots supply their base. Foreign-platform absolute addresses are not treated as current-working-directory paths.

| Condition | Behavior |
| --- | --- |
| Absolute original is usable | Prefer it, even when the relative copy exists; do not rewrite the relative path automatically |
| Absolute missing, relative original usable | Repair the absolute path/location timestamp |
| Both missing, or absolute missing without a relative fallback | Source missing (`original_status: missing`), without deleting saved data |
| Permission/I/O/non-file error | Report `unavailable`, not missing or permission to silently try another copy |
| Found original, but persistence unavailable | Report resolution and unsaved repair as failure |

Windows cross-drive/different-UNC-share relative computation stores `NULL` with `RELATIVE_PATH_UNAVAILABLE`; it does not reject an otherwise valid absolute import. Moving only SQLite cannot guarantee original discovery.

`management original <photo-id>` locates and reports `content_verified: false`; existence is not hashing. `management relink <photo-id> --path <absolute-file>` verifies SHA-256 against the ingested content version, rejects mismatches and conflicts, and updates both paths. No force rebinding is provided. Path writes compare their prior input/path snapshot in a short transaction and preserve IDs, content versions, previews and vectors.

Only **ingestion** updates content versions. It matches normalized absolute/database-relative path candidates inside the selected album. A move/reingest preserves photo ID when the recorded identity still resolves; unchanged content/preview identity can reuse vectors. If a relative candidate conflicts with a still-valid preferred absolute location elsewhere, or multiple records match, ingestion reports `PHOTO_PATH_CONFLICT`. With no unambiguous path identity it creates a new record; identical hashes do not deduplicate separate files or infer arbitrary renames.

Complete scans check new missing records only for their relevant source-folder scope and honor usable fallback originals; other folders are preserved. Incomplete scans do not infer missing files. Photo/preview updates are short per-photo transactions, with identity rechecks after original I/O. Completed work can survive interruption; saved scan summaries/events describe partial success rather than implying an all-or-nothing whole scan.

Enumeration excludes the database, transaction/execution-lock sidecars and model cache. JPEG exports cannot enter saved scan source directories. The old source/database-directory overlap ban is gone, but originals must never be overwritten.

Ingestion's returned `index_prompt` remains mandatory Skill guidance: explain without-index browsing/filename-path lookup, custom folders/manual membership, folder-name search and confirmed date organization versus Chinese/English semantic candidates with a compatible index/model, then ask **in the user's language** about the exact successful not-ready `photo_ids`. No default means configuration is needed, not known cache misses. Empty/all-failed/fully indexed scans do not prompt. An accepted invitation permits preparing a plan, not downloading or executing an unseen digest. A preexisting request to import and index need not repeat the same intent question. Ingestion never automatically adds/regroups virtual folder members.

## 5. Images, semantic purpose and model identity

### Stored preview versus inference input

Ingestion saves one current proportional JPEG per photo: default longest edge 1024, JPEG quality 85, no upscaling, EXIF orientation and sRGB conversion where possible. Originals remain external and are never deleted.

Image-embedding indexing reads the saved JPEG and input identity, not originals or descriptions. The fixed first checkpoint is:

- `google/siglip2-base-patch16-224`, revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`.
- Transformers + PyTorch CPU FP32, single-image serial execution with a conservative CPU thread limit.
- Standard GIL-enabled CPython 3.14, 64-bit x86-64; pinned Windows CPU dependencies in [requirements-index.txt](../photography/requirements-index.txt). No ARM/other-Python runtime guarantee follows from portable storage.
- Official SiglipImageProcessor **224×224 square resize**, rescale and normalization; no added crop/padding, NaFlex, ONNX, quantization, GPU, Ollama or cloud substitute.

The inference resize does not preserve aspect ratio; **the SQLite preview does**. In-memory preprocessing never rewrites the preview.

### Explicit profile semantics

| Profile field | Meaning |
| --- | --- |
| `profile_schema: image-embedding-profile-v1` | Versioned profile contract |
| `embedding_kind: image_text_semantic` | Paired image/text semantic retrieval space |
| `stored_modality: image` | Persisted results are photo-side vectors |
| `input_scope: stored_thumbnail` | Input bytes are saved SQLite JPEGs |
| `granularity: whole_image` | Entire preview, not face/object/crop vectors |
| `dimensions: 768`, float32 little-endian, L2 normalized | 3072 vector bytes per result |

Semantic fields, fixed weights/revision/file hashes, processor/tokenizer behavior, feature extraction, normalization, dimensions and runtime/backend/dtype enter the immutable profile fingerprint. Cache paths do not. The richer contract may change a profile ID relative to v7 even with identical weight bytes; matching model names/dimensions never prove compatibility.

The official paired text encoder maps prepared prompts into the same space. Its tokenizer contract remains 64 tokens including EOS per prompt, no added BOS and fixed right padding; `QUERY_TOO_LONG` rejects overflow without truncation. Query vectors are transient: neither image result rows nor a new query-vector table stores them.

### Fixed query-side visual intent

The host agent uses **text only** to extract English visual intent, retaining the user's original natural-language request in `query` and supplying `--visual-query` for plain search or optional `visual_query` in a semantic condition. Preserve all scene, actions, colors, negation and count constraints, removing search verbs without adding restrictions. For “搜索带有天空的图片”, the visual intent is `sky`, not an added blue, clear, dominant or outdoor requirement. If faithful translation is uncertain, clarify rather than replace the request with guessed keywords. Never translate literal metadata or OCR searches; `--visual-query` is rejected in metadata mode.

```text
management search "搜索带有天空的图片" --mode semantic --visual-query "sky"
```

Python does not translate. Direct already-English visual input may omit the flag; an unprepared non-Latin request fails with `VISUAL_QUERY_REQUIRED`, not a silent English fallback. `semantic_query` owns one fixed recipe, `english-visual-intent-v1`, shared by both semantic entry points: encode the NFC-normalized English visual phrase directly, with no prefix, caption template or ensemble. The frozen recipe has `prompts: [visual_query]` and `weights: [1.0]`. It exposes no selectable strategy versions or arbitrary caller prompts/weights. The recipe's actual encoded text is traceable; this is not itself evidence of improved retrieval quality.

This query-side policy is separate from image profile identity: the profile, pinned checkpoint, 768-dimensional vectors and schema 10 remain unchanged, and no image reindexing is required. `query_encoding` freezes `strategy`, original `query`, English `visual_query`, actual `prompts` and `weights`. `show-results` and search-selected folder-add provenance preserve this recipe; reports show the user the actual encoded text. There is exactly one text encoder call per unique semantic condition with eligible vectors (`model_calls: 1` for plain search), otherwise zero. The prompt must satisfy the unchanged token limit; finalization and showing perform no additional encoding.

### Machine-local setup

`Config.database_path` and `Config.model_cache_root` are separate. Default cache: `%LOCALAPPDATA%\SmartAlbums\models` on Windows; `SmartAlbums` → `models` under the user's Library cache on macOS; `smart-albums` → `models` under `$XDG_CACHE_HOME` or `.cache` elsewhere. Global `--model-cache-dir` overrides the root; `SMART_ALBUMS_MODEL_CACHE_DIR` is a cache-only environment fallback.

The fixed installation is `<cache-root>\siglip2-base-patch16-224\<revision>`. Setup, planning/execution/resume and semantic search use the same resolution. Albums share host model files, not database rows or defaults. Known older weights can be reused from an explicitly chosen root without automatically moving/deleting them. Profile semantic changes do not themselves require downloading new bytes.

`index setup` alone downloads/verifies the fixed manifest and registers the profile. It does not configure the default, including first setup. `index profiles` discovers IDs; `index configure --default-profile <id>` is an explicit album write. Profile-dependent commands use an explicit ID or saved default and otherwise fail clearly. Metadata/browse does not require either.

## 6. Current input and historical results

The following identity describes the unchanged image-embedding subsystem; feature identities are described separately below.

Result identity is:

```text
(photo_id, profile_id, content_version, thumbnail_profile, input_image_hash)
```

Rows also carry result ID, vector/hash, dimensions, dtype, normalization metadata and creation time. Constraints/foreign keys and transactional checks bind results/items to the correct photo/profile/input. Validation checks dimensions, dtype, finite/nonzero values, norm, byte length and checksum.

The same valid key reuses its row. An approved repair can fix a damaged same-key result; it does not overwrite another input/profile's history. There is no force-generate mode. Switching A → B → A can reuse A if its input still matches. Moving a database or repairing original paths does not change vector identity. Album UUID binds plans/snapshots, while movable database paths do not enter vector identity or the fixed plan digest.

Ingestion can replace current preview bytes. Old vector rows remain but no longer participate in current queries when their input identity differs; retained embeddings do not guarantee historical image replay.

- A missing/unavailable original with a complete valid saved input may still be indexed.
- `ingest_state: error` is not a current valid input, even with an old preview.
- Corrupt JPEG/hash/version inputs or mid-run changes fail/stale an execution item.
- Ingestion's size/mtime fast path can miss external content changes preserving both attributes. Indexing is not an original-file monitor or full-source-hash audit.

## 7. Exact plans, execution and recovery

`index plan` requires exactly `--all` or `--ids-file <JSON-array>`, with optional positive `--limit` and `--profile-id`. Selecting an album is not whole-album inference authorization. `--dry-run` preflights read-only without saving a run.

Plans freeze component `image_embedding`, album UUID, profile, photo/input snapshots and reuse/encode/skip actions in the digest. Planning performs no inference/download; pending inference checks local runtime/file readiness. Review component, album, exact scope, profile, cached/pending/invalid counts and digest. `index execute <run-id> --confirm <digest>` requires actual exact-plan approval; the digest itself is not proof of consent.

Execution acquires the full-filename component lock and input claims, validates saved JPEGs, reuses valid results or encodes, then rechecks identity and saves each result/item in a short transaction. Model calls hold no write transaction. A run reuses one loaded model; cache-only plans never load it. A planned reuse that loses its cache cannot silently become inference: it needs a new repair plan.

`index job <run-id>` reports saved progress/errors. `index resume <run-id>` reuses existing inference approval; cache-only work may resume without inference confirmation but cannot expand into encoding. If prior saved status is **`running`**, require `--confirm-stopped` after confirming all old workers on all devices have stopped. This separate confirmation applies even to cache-only recovery and cannot replace missing inference approval.

The lock includes the complete filename, for example `<album.sqlite>.image-embedding.lock`; different extensions do not collide by stem. **`--confirm-stopped` cannot steal a live OS lock.** Time elapsed or absence of a local PID is not evidence that a remote worker stopped. Local locks/SQLite transactions are not distributed coordination.

Persisted valid successes are reused after interruption; computation lost before persistence may be retried. Incompatible/missing weights, invalid inputs and busy locks need explicit resolution. Do not delete claims/sidecars, substitute models or overwrite valid results to bypass errors.

### Stage-one feature identity and execution

`ocr`, `objects`, `scene`, `color`, `composition` and `perceptual_hash` are opt-in index components. Without `--component`, setup/profiles/configure/plan/status still mean `image_embedding`. New `feature_...` runs route job/execute/resume by their saved work kind; no automatic all-component pipeline is introduced.

An `image-feature-profile-v1` versions component, provider, input scope, output schema, parameters, dependencies, runtime and asset hashes. `register-profile <profile.json>` accepts the complete strict profile object; setup/registration never sets a default, and configuration is independent per component. Cache/interpreter paths are locations, not profile identity. Changing output-affecting parameters creates a new profile and preserves historical evidence.

Successful feature identity is `(photo_id, profile_id, input_fingerprint)`, not latest timestamp. Store the canonical manifest and typed payload/hash separately:

| Component | Actual input and manifest identity |
| --- | --- |
| OCR | Verified original bytes, EXIF-oriented dimensions, ingested content version and original hash |
| Objects/color/perceptual_hash | Saved proportional sRGB thumbnail, default longest edge 1024; source version, thumbnail profile/hash and dimensions |
| Scene | Current matching image-embedding result/profile/vector hash plus persisted prototype set/hash |
| Composition | Current objects result/profile/payload hash and dimensions |

Original OCR never silently degrades to thumbnail OCR. It reads verified input without saving path repair; missing originals cause execution `input_unavailable` while saved OCR remains readable. Status/result/history do not stat originals or perform inference; current results describe saved ingestion identity, not undetected external changes. Moves/relinks are not content changes.

Composite constraints bind profile/result/detail component types and same-photo dependencies; public writes validate full payloads and required detail cardinality. Valid parent, typed details, dependencies, OCR derivative and successful item progress publish atomically. Successful empty documents/instance sets have a parent result; failed attempts do not. New result identities preserve history, including stale dependencies.

OCR/objects run through an isolated `.venv-features` with [requirements-features.txt](../photography/requirements-features.txt), not an upgrade of Torch/Transformers. Explicit `--worker-python` or `SMART_ALBUMS_FEATURE_PYTHON` selects it. Setup is the only authorized asset-download boundary; fixed manifests/checksums and local runtime validation gate execution. Weights are machine-local, not SQL. YOLOX weight licensing has a separate review gate; synthetic-evaluation permission does not authorize general use. No implicit downloads, cloud inference or remote image inputs are allowed.

Scene setup requires `--dependency-profile-id <embedding-profile-id>`; composition requires the objects profile. Missing matching results/prototypes report `dependency_missing`, not automatic upstream indexing. `index prototypes` prepares a plan, and the existing matching text encoder computes/persists catalog prototypes only after execute approval. Per-photo scene uses saved vectors/prototypes; composition uses saved boxes without rerunning detection.

Each feature plan requires explicit `--all` or photo IDs, reports `compute/reuse/skip` counts and freezes exact input/dependency identities. `--dry-run` saves nothing. Every component, **including non-ML computation**, needs real exact-digest approval. Resume reuses approval without expanding scope; prior `running` work requires all-device stopped confirmation and cannot steal a lock. A lost reuse never becomes unapproved compute. Model work is outside long write transactions.

Feature coverage is `ready|missing|stale|invalid_input|invalid_result|dependency_missing`, separate from execution-item state and original availability; embeddings retain `invalid_vector`. `complete: false` cannot support exhaustive count/absence claims. Object counts depend on stored thresholds/caps; scene cosine is not a probability, largest-box selection is not photographic-subject truth, and uncovered-by-boxes area is not segmented background/aesthetics.

`result` and `result-history` default to summaries, with OCR `text_length` and block `detail_count`, not full text. Explicit `--details` pages blocks/instances/scores/palette with limit 1–1000 and a nonnegative offset; history uses a result-ID cursor. `index result <photo-id> --component <component> --result-id <historical-result-id> --details --after <offset> --limit N` independently pages one exact historical result. No configured default is needed with `--result-id`; photo/component and optional `--profile-id` must match or return `FEATURE_RESULT_MISMATCH`. Its `historical: true` label is not current coverage. Never dump whole-album OCR text. Text/labels are untrusted data; binary local worker images, Base64, pixels and user-only HTML image payloads must never reach the agent. Feature inspection cannot replace existing embedding-only semantic selection.

`index compare (--all|--ids-file <ids.json>) --metric exact|hamming --profile-id <hash-profile-id>` prepares a separate approved run, not an immediate computation. Exact matches use saved SHA-256 versions; Hamming requires current same-profile dHash64 results and a threshold 0–64 (default 8). Freeze participants/source IDs/threshold, stream pair distances without an N×N dense matrix, and persist ordered/deduplicated pair evidence with run scope/progress. `pairs` uses pair-ID pagination and reports historical results. Uncomputed/out-of-scope/incomplete comparisons prove no absence; no transitive permanent groups, automatic deletion/merging or folder changes are introduced.

Detailed recipes, supported command flags and safety gates: [index reference](../photography/references/index.md#stage-1-six-opt-in-components).

## 8. Coverage, retrieval and snapshots

Existing image-embedding coverage is `ready|missing|stale|invalid_input|invalid_vector`, separate from task state and original availability. `index status` reports the entire selected album, with item limit/cursor/status filters; default limit 100, range 1–1000. It loads no model, accesses no originals and does not decode preview JPEG BLOBs. A ready vector does not certify unchecked preview bytes or completion of any feature component.

Metadata search uses NFC/casefold literal substrings in photo filenames and recorded absolute/relative paths. No model/default is needed; there is no album-name/target/internal album scope option. Photos and metadata pages use stable photo IDs, default 100 and range 1–1000.

`management photos` and both search modes accept repeatable `--folder-id` and `--folder-match union|intersection`, required for multiple distinct IDs. Duplicates of one ID are one folder. No IDs means the entire album; unknown folders are errors, never fallback; empty folders/intersections remain empty. Filter scope before vector inspection, ranking and top-K, deduplicating unions. Counts, coverage, pagination and gaps describe that scope. Metadata `album_total` is the actual whole count, separate from `scope_total` and query-match counts.

Semantic search takes a consistent snapshot of scoped saved input metadata/current vectors in one selected profile. Invalid/stale/absent vectors are excluded. Empty candidates return without model loading, but still require a valid explicit/configured profile; otherwise the direct English visual phrase is encoded once and exactly cosine-ranked, ties by photo ID. The reported text `model_calls` counts actual encoder invocations. Default top-K 10, range 1–1000; `--after` is rejected. Search neither reads originals, repairs paths, writes DB state, regenerates photo vectors, changes defaults nor silently changes modes.

Search checks vector integrity and saved input metadata, not every preview JPEG. Plan/execute and preview rendering validate the bytes they use. Similarity is not probability, a calibrated threshold or guaranteed logical filtering.

`album-snapshot-v2` is a versioned read-only JSON/HTML format with album UUID/path, mode, profile identity and explicit scope: `{"kind":"album"}` or `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}` (also `intersection`). `coverage_scope` is `entire_album` or `selected_folders`. Candidates, `show-results` and reports preserve historical scope and names after folder rename/deletion or membership changes without a new membership query. Selected snapshots retain validated saved scores/ranks/gaps verbatim, including the gap to the next candidate outside returned top-K, rather than recomputing subset gaps. Changed selected photo/input/result identities still error as stale.

Semantic selection uses embedding scores/ranks/gaps only. Folder labels identify scope, never semantic evidence; originals, thumbnails, screenshots and HTML image payloads must not reach the agent. Local HTML is for the user, escapes metadata and verifies embedded preview input identities; changed/corrupt/missing previews are errors, not replacements. No original reads are needed. There are no HTML selection widgets, membership writes, legacy search-selection protocol or live service.

Export protections cover the database, transaction/execution-lock sidecars, local model cache and recorded original candidates, rather than banning the whole database parent. JPEG exports additionally stay outside saved scan roots. Backup destinations must be new; ordinary report/preview outputs are explicitly selected derived files, not no-overwrite database snapshots.

### Stage-two condition snapshots and ranking

The separate `management query --query-file <query.json> --output <private-snapshot.json>`, `query-evidence`, `finalize-query`, `show-query-results` and `query-pairs` workflow adds no tables or migrations: schema 10 and all 37 registered tables remain unchanged. `condition_queries` validates/deduplicates strict `multi-condition-query-v1` OR predicates and freezes profile IDs, scope and aliases; `feature_predicates` reads current saved typed results through storage helpers; `condition_search` captures candidate/source identities and validates explicit review pages; `condition_ranking` ranks detached data without storage/models. CLI/export/report helpers do not implement predicates.

Supported kinds are semantic, object_count, ocr_contains, color_fraction, subject_position, scene and has_near_duplicate. Discover actual profiles/taxonomies with `index profiles --component <component>`. Defaults resolve once; no automatic configure/index/image inference/downloads. OCR uses normalized literal `INSTR` for 1–2 characters and trigram candidates plus literal confirmation for 3+, not query-language interpolation. Counts require complete saved detections and a threshold no lower than the saved floor; incomplete/missing subject evidence is unknown. Scene/color/thirds rules are explicit. Duplicate lookup uses saved exact SHA-256 or same-profile dHash64 Hamming, not pHash, and does not persist comparison jobs or create dense N×N matrices.

`condition-search-snapshot-v1` is a private, digest-bound export containing the matrix, frozen random seed, profiles, source identities, historical scope and ranking metadata. `query` prints only safe `condition-query-summary-v1` and the output path, never the matrix. The agent must not read the private snapshot file or OCR/scene/feature details for semantic judgment; only the text recipe and numeric `condition-semantic-evidence-v1` via `query-evidence` are permitted. Full `condition-decisions-v1` matched-ID pages, including explicit empty selections, are required before final counts/ranking. Top-K is not matched. Queries with no reviewable semantic cells finalize immediately. Finalization makes zero model calls and retains historical `query_model_calls`.

Semantic duplicate identity uses the prepared visual phrase/profile rather than original request wording, excluding `id` and `scoring` under existing rules. NFC normalization applies both with explicit `visual_query` and to direct English input. Different originals with the same prepared phrase deduplicate once. Keep one logical semantic condition per intent: paraphrases must not increase `matched_count`. New OR snapshots freeze `query_encodings` by canonical semantic condition ID; historical raw-query snapshots may omit this map. A semantic evidence page exposes only its `query_encoding` text recipe, numbers and record identities, never OCR/labels/pixels. Recipe identity binds `page_id` and final validation; changing preparation cannot reuse old page decisions. Finalized pages/reports preserve the map and show actual encoded text without re-encoding.

OR results require one unique matched condition. Unknown is not a negative. Match count ranks first; exact matched normalized scores are 1, graded scores are direction-aware matched-population average-rank percentiles (tie averages, N=1/all equal = 1). Only identical matched-ID sets compare equal-weight means; different patterns at the same count use frozen-seed random interleaving preserving their internal order. No cross-pattern score comparison, top-level AND or RRF. Pagination follows complete ranking and preserves global ranks.

Public `condition-search-page-v1` contains conditions/aliases, historical scope, input `coverage` over scope, final `evaluated_coverage` over candidate pool, scope/candidate/result totals, matched IDs/counts and raw/normalized per-condition cells. Semantic input `not_reviewed` means index-eligible at query time; missing/stale/invalid/unsupported remain unknown. Candidate limits/partial coverage must be explained, not converted into nonmatches. Cursors bind finalized snapshot identity/digest. Showing validates selected current source identities, not today's folder membership or original bytes.

`management query-pairs <ranked.json> --condition-id D [--limit N] [--after <opaque-cursor>] [--output <pairs.json>]` exposes the standalone `condition-duplicate-pairs-v1` view for a finalized `has_near_duplicate` condition. Actual qualifying photo-ID pairs include distance/metric and retain condition/scope/input coverage. Current saved sources are validated; opaque pair cursors bind snapshot and condition. Default limit is 100, range 1–1000. This JSON-only read does not render previews, access originals/models, persist jobs or change memberships. No transitive groups or automatic deletion/merging: A–B/B–C does not imply A–C. It is independent of the explicit `index compare`/`index pairs` run lifecycle.

All `.json`/`.html` targets and input aliases/hardlinks are rejected before model work, source validation, preview reads or writes. Exports are the only query side effects. User-only HTML escapes conditions/snippets, uses restrictive CSP with no scripts/forms/widgets/backend, embeds only this page's identity-matching previews and preserves historical scope/global ranks; the agent returns a link without opening/screenshotting/reading its image payloads. Explicit `folders add --query-snapshot` validates finalized selected IDs with `condition_search.select_results` inside the membership transaction and returns separate `source_query` provenance. It is mutually exclusive with `--search-snapshot`; null input is an error, `[]` adds nothing, and no query/classification/model work occurs.

Full JSON/CLI definitions: [condition query reference](../photography/references/search.md#stage-2-or-condition-workflow). Targeted integration checks have passed, including cached synthetic OR retrieval with unchanged SQLite bytes and zero image inference. Synthetic regression is not real-photo retrieval-quality validation. Exact-pair pages use content-hash buckets and algebraic totals, avoiding all-pairs comparison on every page; Hamming lookup still performs bounded-memory pair comparisons and is not a claim of subquadratic work.

## 9. Portability, backups and authorization

Use a single writer on one device. Cloud workflow is **download a complete local file → operate locally → stop/close all work → copy/sync**. Do not open HTTP/S3 URLs or assume network filesystem/concurrent-copy safety. Do not copy a live bare SQLite file or delete its journal/lock sidecars. Each host separately needs compatible dependencies and model files.

`management backup --output <new-file>` uses SQLite's consistent backup API without replacing an existing target. It includes metadata, previews, profiles, vectors, feature evidence/dependencies, prototypes, runs, OCR FTS and folder memberships; excludes external originals, model weights and Python environments. It preserves album UUID and paths. After relocation, originals need a working absolute address or preserved relative layout, otherwise explicit relink. Backups are not independently writable branches with auto-merge.

Dependency installation, model setup, real-photo trials and exact-plan inference are separate authorizations. Creating/opening an album or accepting an index invitation does not grant all of them. Ordinary index/search never downloads missing files or falls back to cloud analysis. The user's existing old database/backups are not trial targets.

## 10. Pending validation and stage two

### Query recipe benchmark

The final shipping recipe is direct English visual intent (`english-visual-intent-v1`), selected from a separately authorized, preregistered comparison of **8 strategies, 6 concepts and 101 provided photos**. Independent local **SegFormer proxy labels, not human ground truth**, supplied relevance targets because user labels were unavailable. Development/holdout assignment was preregistered by content hash; the preregistered selection and sensitivity gates passed. Experimental alternatives remain evaluation-only, not runtime options.

Average precision (AP) against these proxy targets:

| Metric | Development: original query → visual intent | Holdout: original query → visual intent |
| --- | --- | --- |
| Sky AP | 0.8452 → 0.8811 | 0.7431 → 0.8998 |
| Six-concept macro AP | 0.7376 → 0.9034 | 0.5694 → 0.8031 |

These are retrieval results for the tested concepts and proxy definition, not human-verified matches, general retrieval quality or validation of embedding-only display decisions. Arbitrary-query translation quality was not tested. They do not establish cross-host support, performance or new authorization to process photos. Only the single direct phrase is shipped: no prefix, caption template, ensemble or user-selectable strategy.

### Other validation and pending work

Stage-one regression and authorized synthetic integration have passed; see [validation status](TODO.md). The isolated vision suite covers bilingual/EXIF OCR and blank-image YOLOX smoke. A separate three-photo run persisted all six components, prepared 16 scene text prompts using existing SigLIP weights, compared exact/perceptual fingerprints and reopened 18 ready results from a backup with originals offline. Approximately 35.4 MB of new assets were prepared. Ordinary YOLOX use remains license-gated. These checks do not grant real-photo authorization or establish unmeasured quality, performance, cross-host support or OS-level network isolation.

Future separately authorized evaluation should verify download reuse/recovery and actual offline execution, start with non-sensitive images, then use explicitly selected representative photos. Assess paired Chinese/English queries, difficult combinations/no-match cases, and separately measure load/image/query/ranking time and memory.

Mandatory follow-ups in [TODO](TODO.md):

- **NaFlex:** new profile/input budget and quality/resource comparison, never in-place vector replacement.
- **Stage 2 OR search:** implemented as above; targeted integration checks have passed. OCR/field queries and combined ranking reuse persisted stage-one evidence; metadata/semantic modes and defaults remain unchanged. Real-photo quality and larger deployment-scale performance require separate evaluation.
- **Technical parameters:** focus/exposure analysis remains future index work with independent versioned inputs/results and authorization. No empty `technical_*` tables or fake results; preview blur is not original focus quality, and missing data means unknown.
- **Embedding ONNX/quantization:** separate runtime/vector identities and measured numeric, quality and resource validation, not mixing same-dimensional vectors across profiles. The OCR/objects ONNX workers do not change the embedding backend.

Cross-album search, automatic hash-based record merging/deletion, shared vectors between files and automatic cloud synchronization remain out of scope.

## References

- [Album-file entry and safe copying](../photography/references/library.md)
- [Ingestion paths and index invitation](../photography/references/ingest.md)
- [Index CLI and recovery](../photography/references/index.md)
- [Management CLI and snapshots](../photography/references/management.md)
- [Pinned official checkpoint](https://huggingface.co/google/siglip2-base-patch16-224/tree/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2)
- [Pinned official image processor](https://huggingface.co/google/siglip2-base-patch16-224/blob/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2/preprocessor_config.json)
- [Official SigLIP feature interfaces](https://huggingface.co/docs/transformers/model_doc/siglip)
