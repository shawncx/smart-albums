# Smart Albums

**One album is one SQLite file.** One photography Skill exposes exactly three independent capabilities:

| Capability | Purpose |
| --- | --- |
| **ingestion** | Import local photo metadata, original paths and proportional JPEG previews into the selected album. |
| **index** | Explicitly set up/configure, plan, generate/reuse, inspect and resume image embeddings or six opt-in local feature components. |
| **management** | Create/open/backup the album file, manually manage static virtual folders, browse/search within explicit scopes, organize by saved date and locate/relink originals. |

These are not an automatic pipeline. Ingestion never calls a model. Browse/search never writes the database or checks original files; creation, folder membership and original-path maintenance are explicit management operations. The program never deletes originals or automatically regroups folders.

## Install the base Skill

Use a compatible Python 3.12+ interpreter in a project virtual environment. Base ingestion, browsing, metadata search, manual folders and date organization need only [requirements.txt](photography/requirements.txt), not PyTorch or model weights. From the repository root on Windows:

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r photography\requirements.txt
```

The optional model runtime is narrower: **standard, GIL-enabled CPython 3.14, 64-bit x86-64**, with pinned Windows CPU dependencies. Portable album files do not imply that this runtime supports ARM, other Python versions or every host. See [index setup](photography/references/index.md); do not install into a shared interpreter.

Copy the entire `photography` directory into your host's supported Skill directory as `smart-albums`. Its entry is [photography/SKILL.md](photography/SKILL.md). This directory is the complete distributable Skill: its runtime references, scripts and requirements files are self-contained. Repository `docs` and `tests` are development material, not installation dependencies. Do not copy virtual environments, model caches or album files into the Skill. The host needs local Python execution and access to the selected album and requested files.

The examples below run from a repository checkout. After installation, replace `photography` in script/requirements paths with the absolute installed Skill directory; do not resolve them against the host's working directory. Using the chosen environment's interpreter, verify the installed entry without an album or models:

```text
python <installed-skill-directory>\scripts\photography.py --help
```

For OCR/objects, explicitly supply the separate worker's absolute interpreter using `--worker-python` or `SMART_ALBUMS_FEATURE_PYTHON`. Repository-local `.venv-features` discovery is only a checkout convenience, not an installed-directory contract; see the bundled [runtime instructions](photography/references/index.md#feature-runtime-and-assets).

## Select or create the album file first

Before a data operation, select an existing **SQLite album file** or explicitly choose to create a new one. Reuse an already selected file; there is no second internal album selection. Informational questions and `--help` need no album.

All operation commands require global `--database <absolute-album-file>` before the capability. Acceptable extensions are `.sqlite`, `.sqlite3` and `.db`; arbitrary filenames are supported. There is no default database, `--state-dir`, old alias or compatibility mode.

Here and below, `python` means the chosen virtual environment's interpreter. Replace placeholders with actual absolute paths and returned IDs:

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management create
python photography\scripts\photography.py --database <absolute-album.sqlite> management open
```

Use `create` only for an explicitly requested, unused destination in an existing directory. It never overwrites. `open` validates an existing file read-only: no creation, DDL or migration. A missing/invalid/old file is an error, not permission to create a replacement. Responses identify the album by UUID, filename-derived display name and full database path. Switching files resets prior photo/profile/run/folder selections and pending confirmations. See [album-file entry](photography/references/library.md).

## ingestion: import without inference

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> ingestion <absolute-photo-directory>
```

Saved previews preserve aspect ratio: default longest edge 1024, JPEG quality 85, no upscaling. Repeated scans reuse unchanged inputs. Only ingestion updates content versions; changed inputs make historical embeddings ineligible for current search without deleting their history.

After import, the Skill explains the returned `index_prompt` and explicitly asks, **in the user's language**, whether to prepare an index for its exact successful, not-ready photo IDs:

- **Without an index:** browse photos, saved previews and basic metadata; find filenames/recorded paths and folder names; create/manage custom folders, manually add/remove photos and prepare/apply confirmed EXIF date organization.
- **With a valid index and matching local model:** also search visible content in Chinese or English. Results are similar candidates, not guaranteed detections or exact filters.

Fully indexed repeat scans do not prompt again. Importing, accepting the invitation or choosing an album never automatically downloads a model or authorizes an unseen execution plan. See [ingestion](photography/references/ingest.md).

## index: explicit setup, configuration and execution

`ocr`, `objects`, `scene`, `color`, `composition` and `perceptual_hash` are components inside index, not six new public capabilities. Existing commands without `--component` still select `image_embedding`; ingestion invitations and metadata/semantic search defaults are unchanged. **Stage 2 OR search is implemented** as separate management commands consuming saved evidence; targeted integration checks have passed, not a real-photo quality claim.

Install image-embedding dependencies in a dedicated compatible CPython 3.14 x64 virtual environment. Model download and real-model trials require separate authorization; code approval is not that authorization.

```text
python -m pip install -r photography\requirements-index.txt
python photography\scripts\photography.py --database <absolute-album.sqlite> index setup
python photography\scripts\photography.py --database <absolute-album.sqlite> index profiles
python photography\scripts\photography.py --database <absolute-album.sqlite> index configure --default-profile <profile-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> index plan --ids-file <photo-ids.json>
python photography\scripts\photography.py --database <absolute-album.sqlite> index execute <run-id> --confirm <reviewed-digest>
python photography\scripts\photography.py --database <absolute-album.sqlite> index status
python photography\scripts\photography.py --database <absolute-album.sqlite> index job <run-id>
```

`setup` is the only model-download operation and **never sets the default**, even on first use. Configure explicitly or pass `--profile-id`. Planning requires exactly `--all` or `--ids-file`; optional `--limit` bounds the scope, and `--dry-run` does not save a plan. Review the component, album, profile, scope, pending/cached/invalid counts and digest before execution.

The fixed model is `google/siglip2-base-patch16-224`, revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, Transformers + PyTorch CPU FP32. It encodes **stored SQLite thumbnails**, using the official **224×224 square resize** without added crop/padding. Stored previews remain proportional.

The persisted product is a **768-dimensional, whole-image semantic image vector in a paired image/text space**: `embedding_kind: image_text_semantic`, `stored_modality: image`, `input_scope: stored_thumbnail`, `granularity: whole_image`. Matching text-query vectors are transient. This is not caption generation, object detection, focus/blur measurement or another technical analysis.

`index resume <run-id>` reuses existing inference approval. If the saved status is `running`, it additionally requires `--confirm-stopped` after confirming workers on **all devices** have stopped. This flag cannot steal a live OS lock. Cache-only work loads no model and cannot expand into encoding. See [index reference](photography/references/index.md).

### Six opt-in feature components

| Component | Input and saved evidence |
| --- | --- |
| `ocr` | Verified, EXIF-oriented original bytes; RapidOCR/PP-OCRv6-small text, normalized text, blocks and available scores |
| `objects` | Stored proportional preview (default longest edge 1024); YOLOX Nano 416 CPU ONNX instances, scores and normalized boxes |
| `scene` | Existing matching image vectors plus persisted, versioned text prototypes; full catalog cosine scores |
| `color` | Stored sRGB preview; versioned palette, hue and saturation statistics |
| `composition` | Existing objects result; deterministic box geometry, not aesthetics or segmentation |
| `perceptual_hash` | Stored preview dHash64; explicit comparisons use current fingerprints or saved SHA-256 content versions |

OCR/objects use an isolated `.venv-features` with [requirements-features.txt](photography/requirements-features.txt), not an upgrade of the embedding environment. After explicit installation/download authorization:

```text
python -m venv .venv-features
.\.venv-features\Scripts\python.exe -m pip install -r photography\requirements-features.txt
```

Select its absolute interpreter with `--worker-python` on feature setup/plan/execute/resume, or `SMART_ALBUMS_FEATURE_PYTHON`. Weights stay in the machine-local cache, never SQLite; setup verifies fixed asset checksums, and execution is offline with no implicit downloads. YOLOX weight licensing requires explicit review; synthetic-evaluation permission is not general-use approval. See [runtime and asset gates](photography/references/index.md#feature-runtime-and-assets).

Append these commands to the same script/database prefix:

```text
index setup --component color
index setup --component ocr --worker-python <absolute-worker-python>
index setup --component composition --dependency-profile-id <objects-profile-id>
index setup --component scene --dependency-profile-id <embedding-profile-id>
index profiles --component color
index configure --component color --default-profile <profile-id>
index register-profile <profile.json>
index plan --component color --ids-file <photo-ids.json> --profile-id <profile-id>
index execute <feature-run-id> --confirm <digest>
index result <photo-id> --component ocr --profile-id <profile-id>
index result <photo-id> --component ocr --profile-id <profile-id> --details --limit 20 --after 0
index result-history <photo-id> --component ocr --profile-id <profile-id> --limit 20
index result <photo-id> --component ocr --result-id <historical-result-id> --details --after 0 --limit 20
index prototypes --profile-id <scene-profile-id> --dry-run
index compare --ids-file <photo-ids.json> --metric hamming --max-distance 8 --profile-id <hash-profile-id>
index pairs <feature-run-id> --limit 100 --after 0
index rebuild-fts --confirm
```

Setup/registration never selects a default; each component has its own configuration. `prototypes` and `compare` **prepare plans**, not computations; execute the returned `feature_...` run only after exact-digest approval, including non-ML computations. Omit `--dry-run` to persist a prototype plan. Text encoding happens only during approved prototype execution; scene and composition report `dependency_missing` rather than automatically indexing other components or the whole album.

Feature status is `ready|missing|stale|invalid_input|invalid_result|dependency_missing`, separate from execution-item state. Profiles version output-affecting parameters/assets/recipes; `input_fingerprint` binds content and exact dependencies, not paths. History remains inspectable. Status/result reads do not stat originals or run inference; a ready OCR result describes the last ingested content, not live disk verification.

Results default to summaries (OCR `text_length` and block `detail_count`), with explicitly requested, paged `--details`; never dump whole-album OCR text. `result-history` pages result IDs; `result --result-id` reads that exact historical result and pages its details independently by offset. No configured default is needed for an explicit result ID; the photo/component and optional `--profile-id` must match, otherwise `FEATURE_RESULT_MISMATCH`. Its `historical: true` response is not current coverage. A successful empty result differs from not computed; `complete: false` cannot prove exhaustive counts or absence. Pair comparison streams distances without an N×N dense matrix; absent pairs outside a completed scope prove nothing. It never deletes/merges photos or changes folders. `rebuild-fts --confirm` is an explicit write rebuilding only derived text from saved documents, without originals/models.

## management: manual folders, scoped search and maintenance

### Manual custom folders are the primary workflow

A virtual folder is a static, flat many-to-many collection inside one SQLite album, not a disk directory or another album. Create empty custom folders and add one or more photos without an index:

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders list --query "精选"
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders create --name "精选" --description "My picks"
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders show <folder-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders add <folder-id> --ids-file <photo-ids.json>
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders remove <folder-id> --ids-file <photo-ids.json>
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders rename <folder-id> --name "旅行精选"
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders delete <folder-id>
```

For one photo, the Skill writes a one-element JSON ID array using its actual ID. `[]` means no changes, never all photos. Names are trimmed, nonempty and control-character-free, unique by NFC + casefold; rename preserves the stable folder ID. A photo may belong to several folders or none. Removing membership/deleting a folder never deletes photos, originals, previews or embeddings, or affects other memberships. Batches validate all IDs and commit atomically; duplicates/unchanged members are reported. `photo` includes memberships; folder lists show counts. No hierarchy, live rules, jobs or fourth capability is added.

### Browse and search

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management photos --limit 100
python photography\scripts\photography.py --database <absolute-album.sqlite> management photos --folder-id <folder-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management photo <photo-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "IMG_01" --mode metadata
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "黑白的枯树" --mode semantic --output <output-directory>\candidates.json
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "black and white leafless trees" --mode semantic --profile-id <profile-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management original <photo-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management relink <photo-id> --path <absolute-original-file>
python photography\scripts\photography.py --database <absolute-album.sqlite> management backup --output <new-backup.sqlite>
```

Metadata mode uses Unicode NFC/casefold literal substrings in filenames and recorded absolute/relative paths; no model/default is needed. Semantic mode uses the selected album's current vectors and a matching query encoder, without generating missing embeddings or mixing profiles. Empty candidates load no model. Report incomplete coverage; cosine scores are not probabilities or guaranteed matches.

`photos` and both search modes accept repeatable `--folder-id` plus `--folder-match union|intersection`, required for multiple distinct IDs. No IDs means the entire album; invalid folders are errors, never whole-album fallback. Empty folders/intersections return empty results without loading an encoder (semantic search still requires a valid selected/configured profile). Scope filtering happens before vector inspection, ranking and top-K; unions deduplicate. Counts, pagination, coverage and score gaps describe that scope; metadata `album_total` remains the actual whole count alongside `scope_total`.

By default, the Skill treats semantic results as internal candidates and selects which IDs to display using **embedding similarity scores, ranks and score gaps only**. Images and thumbnails are not passed to the agent, and selection is not visual verification. It may select fewer results or none instead of always returning the whole candidate list.

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management show-results <output-directory>\candidates.json --ids-file <output-directory>\selected.json --html <output-directory>\results.html
```

The selected ID file may contain `[]`. This helper does not repeat a query or modify the album. `album-snapshot-v2` candidates, selected results and reports preserve historical scope and folder names even after membership changes or folder rename/deletion; changed selected photo/input identities still error as stale. Scope is `{"kind":"album"}` or `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}` (also `intersection`), with `coverage_scope: entire_album|selected_folders`. The report preserves validated saved scores/ranks/gaps verbatim, including the next-candidate gap beyond returned top-K, and displays only selected photos for the user; the agent returns its link without reading image payloads. Folder labels only identify scope, not semantic evidence.

Both modes and plain browsing are read-only and do not stat originals or repair paths. HTML/JSON are snapshots, with no selection widgets, album membership writes or live service. `management thumbnail`, `scan` and `scan-events` retain preview export and ingestion diagnostics. See [management](photography/references/management.md) and [search](photography/references/search.md).

### Stage 2: explicit OR conditions

The separate query workflow reuses saved indexes without image inference, downloads, DDL, configuration changes or automatic folder writes:

```text
management query --query-file <query.json> --output <private-snapshot.json>
management query-evidence <private-snapshot.json> --condition-id A --page 0
management finalize-query <private-snapshot.json> --decisions-file <decisions.json> --output <ranked.json>
management show-query-results <ranked.json> --limit 100 --output <page.json> --html <report.html>
management show-query-results <ranked.json> --limit 100 --after <opaque-cursor>
management query-pairs <ranked.json> --condition-id G --limit 100 --output <pairs.json>
```

Use `multi-condition-query-v1` with `operator: "or"` and stable condition IDs. Supported kinds are `semantic`, `object_count`, `ocr_contains`, `color_fraction`, `subject_position`, `scene` and `has_near_duplicate` (saved exact SHA-256 or dHash64 Hamming, not pHash). Discover profile IDs and supported object/scene catalogs with `index profiles --component <component>`; defaults resolve once and freeze. Duplicate predicates deduplicate with `aliases`. See the [complete query and decision JSON contracts](photography/references/search.md#stage-2-or-condition-workflow).

`query` prints only a safe summary and output path. Its private snapshot contains an internal feature matrix: **the agent must not read the private snapshot file**, OCR, scene labels or other feature cells to judge semantic relevance. Read only `query-evidence` query/IDs and numeric embedding scores/ranks/gaps, then supply every required page using `condition-decisions-v1` matched-ID lists (an empty list is valid). Full semantic review precedes match counts/ranking; top-K retrieval is not a match decision. Queries with no reviewable semantic cells finalize automatically.

OR keeps photos matching at least one unique condition; unknown is not negative. Ranking prioritizes match count. Exact matches score 1; graded matches use direction-aware average-rank percentiles among that condition's matched photos. Only identical matched-ID sets compare their equal-weight mean; different patterns at the same count interleave using the frozen seed. There is no cross-pattern score comparison, RRF or top-level AND.

`condition-search-page-v1` exposes input `coverage` over the query scope separately from final `evaluated_coverage` over the candidate pool. Semantic input `not_reviewed` means eligible at query time. Report candidate limits and incomplete coverage; unreturned photos are not proven nonmatches. Results retain global ranks and opaque cursors, historical scope and saved source identities. JSON and user-only HTML exports are the only query side effects; give the user the report link without opening, screenshotting or reading its image payloads.

`query-pairs` requires a finalized snapshot and a `has_near_duplicate` condition ID. It returns `condition-duplicate-pairs-v1` with actual ordered photo-ID pairs, distance/metric, historical scope and input coverage; follow its own opaque `next_cursor` (default limit 100, 1–1000). This saved exact SHA-256/dHash64 view is JSON-only, read-only and source-validated: no originals/models, transitive duplicate groups, automatic deletion/merging or folder changes. It does not run the separate `index compare` planning/execution workflow.

For an explicitly requested destination and selected finalized IDs, use `management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>`. It validates sources inside the membership transaction, returns separate `source_query` provenance, and does no query/classification/model work. This flag is mutually exclusive with `--search-snapshot`; `[]` adds nothing.

### Optional one-time organization

For an explicitly chosen destination, add selected semantic candidate IDs with `management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>`. This reuses `management.select_search_results` validation of album/candidate/selected identities without a new query or image encoding; never default to all top-K or remove source memberships.

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders organize-date --all --granularity month --output <date-plan.json>
python photography\scripts\photography.py --database <absolute-album.sqlite> management folders apply-date-plan <date-plan.json> --confirm <digest>
```

Planning requires exactly `--all` or `--ids-file`; `--granularity` accepts `year|month|day`. It uses saved EXIF `datetime_original`, valid camera-local calendar dates with no UTC conversion or fallback to file times. Missing/invalid dates and unavailable ingestion metadata are skipped with counts, without originals/models. This is authorized deterministic organization, not semantic evidence. Review exact scope, create/reuse folder preview, members, skips and digest before applying. CLI output is an envelope (`plan`, `digest`, `output`, `album`, `model_calls: 0`); the file is the raw plan. Apply checks conflicts/staleness and atomically creates/reuses folders and adds members. No live rules, new jobs/tables or automatic regrouping of future imports/removals.

## Portable paths, cache and backups

SQLite can be beside, above, below or on a different drive from photos. Each photo records an absolute original path and a nullable path relative to the **current database parent**, including `..`. Explicit original lookup prefers the absolute path. Only when it is missing does a usable relative path repair the absolute location; an absolute hit does not silently rewrite the relative path. Both missing means source missing; permissions/I/O failures are separate errors. Read-only repair failure is reported, never claimed as success.

Relink requires matching SHA-256 and updates both paths. Path-only repair preserves photo IDs, previews, content versions and vectors. Cross-drive/share relative paths may be `NULL`, with a warning that moving devices may require explicit relink. Moving and reingesting preserves IDs only when recorded path identity resolves unambiguously; there is no hash-based deduplication.

Models are shared machine-local files, independent of albums: `Config.model_cache_root` defaults to `%LOCALAPPDATA%\SmartAlbums\models` on Windows or the platform's application cache elsewhere. Optional global `--model-cache-dir <cache-root>` precedes `index` or `management` and works consistently for setup, execution and semantic search. A known older model cache can be reused by explicitly selecting its root; it is not automatically moved or deleted.

Use **one writer on one device at a time**. For cloud storage: download a complete local file, operate locally, stop/close all operations, then copy or sync it back. Do not copy an actively written bare SQLite file or delete its sidecars. `management backup --output <new-file>` makes a consistent no-overwrite SQLite snapshot containing previews, embeddings, feature results/prototypes and virtual folder memberships, **not originals, model weights or Python environments**. Each host needs its own compatible runtime/cache.

## Format and validation status

New albums use **schema 10**, `application_id = 0x53414C42`, and **37 registered tables**: 32 ordinary tables, one external-content OCR FTS5 virtual table and four explicitly registered shadow tables (SQLite's internal `sqlite_sequence` is excluded). The existing six `image_embedding_*` tables and `virtual_folders` / `virtual_folder_photos` remain separate from the new feature profiles/results, typed details, manifests, dependencies and jobs. See the [complete schema inventory](docs/index-design.md#3-schema-10-37-registered-tables). No internal albums/libraries or empty `technical_*` placeholders are added.

Existing v1–v9 databases are **rejected unchanged**: no migration, cleanup, overwrite or old CLI/API compatibility. The user's old database and backups must remain untouched. Existing management snapshots remain `album-snapshot-v2`; condition queries separately use `condition-search-snapshot-v1` and public `condition-search-page-v1`. Existing static reports are not converted.

Stage-one regression and authorized synthetic integration have passed; see [validation status](docs/TODO.md). The isolated vision suite passed 27 tests, and all six components persisted results for three synthetic photos; 18 results remained readable from a backup with originals offline. Downloaded assets total 35,408,916 bytes (approximately 35.4 MB), with existing SigLIP weights reused. This covers **synthetic evaluation only**; ordinary YOLOX use remains license-gated. Real-photo quality, performance, cross-host support and OS-level network-isolation guarantees do not follow from these checks.

```text
python -m unittest discover -s tests -v
```

- [Current storage/index design](docs/index-design.md)
- [Portable-album implementation plan](docs/portable-album-plan.md) (plan text, not live completion status)
- [Earlier implementation plan](docs/ingestion-index-management-plan.md) (historical; superseded for storage/CLI)
- [Pending validation and future work](docs/TODO.md)
- [Code-review follow-up discussion (中文; deferred proposals, not approved capabilities)](docs/code-review-follow-up.zh-CN.md)

Private databases, photos, weights and generated reports are not distributed with the Skill. Pushing this repository does not back up an album or its originals.
