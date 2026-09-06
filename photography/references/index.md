# index: local whole-image semantic embeddings

Index is one of the three public Skill capabilities, operating on one explicitly selected album file. Its current component is `image_embedding`: it uses **SQLite JPEG previews directly**, without cloud analysis, descriptions, original-file reads or automatic translation. See [management](management.md) for querying the resulting image vectors.

## Optional runtime and authorization

The base Skill does not import optional inference dependencies for ingestion, browsing, metadata search or index status. The pinned optional environment is **standard GIL-enabled CPython 3.14, 64-bit x86-64**, targeting Windows CPU wheels. It is not the broader Python 3.12+ base contract and does not support substituting a free-threaded interpreter.

Install [requirements-index.txt](../requirements-index.txt) only into a dedicated compatible project virtual environment:

```text
python -m pip install -r <skill-directory>\requirements-index.txt
```

Here `python` must refer to that virtual environment's interpreter. The dependency file is authoritative for exact package versions; runtime identity checks reject incompatible interpreters or versions rather than silently substituting packages. Do not modify a shared Python installation or assume base support guarantees PyTorch support. Wheel metadata/compatibility checks are not proof that the environment was installed or real inference succeeded.

Dependency installation is not model installation. **Model download and real-model trials require separate authorization.** Code approval or album creation does not authorize them. Old database migration is not implemented or implicitly permitted. No weights are bundled in the repository.

Portable SQLite files do not guarantee a portable model runtime. The current profile is fixed to CPU CPython 3.14 GIL x86-64; ARM, other Python versions and alternate host environments are not promised by this refactor.

## Fixed model and profile

| Property | Initial implementation |
| --- | --- |
| Model | `google/siglip2-base-patch16-224` |
| Fixed revision | `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2` |
| Published license | Apache-2.0; fixed official checkpoint, no bundled weights |
| Runtime | Transformers + PyTorch, CPU FP32, single-image serial execution |
| Image processing | Fixed revision's official SiglipImageProcessor, RGB, 224×224 square resize/rescale/normalize |
| Features | Matching official image and text feature interfaces; explicit L2 normalization |
| Saved vector | 768 little-endian float32 values, 3072 bytes |
| Query | Same checkpoint's tokenizer/text encoder, direct text; no retrieval prefixes or automatic translation |
| Profile schema | `image-embedding-profile-v1` |
| Purpose | `embedding_kind: image_text_semantic`, a paired image/text semantic space |
| Persisted modality | `stored_modality: image`; text-query vectors are transient |
| Input / granularity | `input_scope: stored_thumbnail`, `granularity: whole_image` |

The square resize is an accepted initial trade-off, not an aspect-preserving inference transform. There is no added center crop/padding, and it does **not** alter ingestion's proportional previews. NaFlex, technical parameters and ONNX/quantization are deferred; see [roadmap](../../docs/TODO.md).

Profile identity includes the semantic fields above, fixed weights, image/text processing, feature extraction, normalization, dimensions, backend/dtype and runtime identity. A cache path is not model identity: global `--model-cache-dir` locates the **same pinned files**, not arbitrary weights. Matching dimensions alone never make profiles interchangeable. The expanded profile contract can produce a different ID from v7 without changing or redownloading weight bytes.

Text length is limited to **64 tokens including EOS**, with no added BOS and right-padding to the fixed context length. `QUERY_TOO_LONG` reports the actual count and limit; queries are never silently truncated. Ordinary operation loads only local files and does not execute remote custom model code.

## CLI reference

Prefix commands with:

```text
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> [--model-cache-dir <cache-root>]
```

Use the selected existing album; `.sqlite`, `.sqlite3` and `.db` are accepted. Both global options precede `index`. Arguments below use placeholders:

```text
index setup
index profiles
index configure --default-profile <profile-id>
index plan --all [--profile-id <id>] [--limit N] [--dry-run]
index plan --ids-file <ids.json> [--profile-id <id>] [--limit N] [--dry-run]
index execute <run-id> --confirm <digest>
index status [--profile-id <id>] [--limit N] [--after <cursor>] [--status <status>]
index job <run-id>
index resume <run-id> [--confirm-stopped]
```

There are no album/library scope options, `--model-dir`, `--state-dir` or old command/API aliases. The selected file is the album. Plans/status/execution identify `component: image_embedding`; this does not represent completion of future technical processing.

### Shared machine-local cache

`Config.model_cache_root` is independent of the album. It defaults to `%LOCALAPPDATA%\SmartAlbums\models` on Windows; `SmartAlbums` → `models` under the user's Library cache on macOS; or `smart-albums` → `models` under `$XDG_CACHE_HOME` (fallback `.cache`) elsewhere. A global `--model-cache-dir` overrides it; `SMART_ALBUMS_MODEL_CACHE_DIR` is an environment fallback for the cache only.

The fixed model directory is `<cache-root>\siglip2-base-patch16-224\<revision>`. Choose the **root**, not the revision directory, and use it consistently for setup, planning, execution/resume and `management search --mode semantic`. A known older cache with this model/revision layout can be reused via an explicit root. No old installation is automatically searched for, moved, deleted or redownloaded.

Multiple album files can share the same verified model files on one machine, but each album registers its own profiles and explicitly chosen default. Cache files and runtime dependencies do not travel inside an album backup. Ordinary commands never install/download missing files.

### Setup does not configure a default

The fixed manifest contains six files totaling **1,535,212,390 bytes (about 1.43 GiB)**. This is the declared file payload, not measured network usage, runtime memory or required free disk space; staging and preserved snapshots can require additional disk space. Review the model, size and target directory before authorizing setup. The fast tokenizer loads `tokenizer.json` directly; this profile does not require `tokenizer.model`, SentencePiece or torchvision.

`index setup` is the only model-download command. It uses the pinned file manifest, validates file sizes/hashes and publishes verified files without replacing a usable installation with an incomplete download. It reports the profile/profile ID, destination, `downloaded_files` and `reused_files`. A retry reuses validated files; a failure must be resolved, not silently replaced by another model or cloud API.

Setup registers the profile **but does not select it as the default**, even on the first installation. Run `index profiles` to retrieve the actual ID. Either deliberately save it:

```text
index configure --default-profile <profile-id>
```

or supply `--profile-id` to planning/search/status. Profile resolution uses an explicit ID first, then the saved default; without either, profile-dependent operations explain the missing configuration. Browsing and metadata search do not require a default.

New index generation never changes a query default. Do not use setup merely to browse status. When a non-default cache root is needed, supply the same global option again to planning/execution/resume and semantic search; it does not change the profile ID.

### Plan an explicit scope

`index plan` requires exactly one of `--all` or `--ids-file`. The latter contains a UTF-8 JSON array of nonempty photo IDs; use IDs returned by ingestion/management, including `index_prompt.photo_ids` for an accepted post-import invitation. Duplicates are removed. Omitting scope is an error, not whole-album selection. Positive `--limit` bounds selection. Selecting/opening a file alone does not authorize processing every photo.

Planning validates the profile and saved inputs, determines reuse versus generation, and freezes component, album UUID, profile, photo IDs, input versions/hashes and actions in a digest. The movable database/cache paths are not input identity. Planning does not encode photos or download weights; pending inference may require local runtime/file readiness. A dry-run opens read-only and does not persist a run or perform DDL.

Present component, album, selected/cached/pending/invalid counts, profile and exact scope for approval. `index execute <run-id> --confirm <digest>` executes only the actual approved proposal. Copy the returned digest; do not manufacture approval from the digest's existence or a request to import/plan. New scope/profile/input requires a new reviewed plan; execution must not silently expand to new photos.

### Execute, reuse and resume

Each item is claimed, its JPEG/input identity validated, encoded if needed, rechecked and committed in a short transaction. No model call holds a write transaction. One execution reuses its model instance; pure cache hits do not load the model. There is no force-overwrite flag.

Use `index job <run-id>` to inspect saved progress and item errors. `index resume <run-id>` requires prior confirmation when the frozen plan contains encoding work; it cannot grant that authorization. A cache-only run can resume without inference confirmation and still cannot expand into encoding. Still-valid persisted successes are reused, not encoded again. A computation lost before persistence may need redoing after safe recovery.

If the saved prior status is **`running`**, resume additionally requires `--confirm-stopped` after verifying every previous worker, including on another device, has stopped. Without it, expect `INDEX_STOPPED_CONFIRMATION_REQUIRED`. This is distinct from inference approval and applies to cache-only recovery too. Missing inference approval still requires the original exact-plan execute/confirmation; the stopped flag cannot grant it.

If an item was planned for **reuse** but its cached result later becomes invalid, that item fails and needs a **new repair plan**. Execution/resume must not turn approved cache reuse into unapproved inference. An item already approved for encoding can repair a damaged same-key result; a valid result remains reusable. For progress reporting, `model_calls` is cumulative for the run; `model_calls_this_execution` counts the current invocation.

The execution lock includes the full filename and component, for example `<album.sqlite>.image-embedding.lock`, so same-stem files with different extensions do not share a lock accidentally. **`--confirm-stopped` never steals a live OS lock.** Cross-process locks and input claims prevent taking over active work solely because time passed. They are not distributed locks: a missing local PID does not prove a remote worker stopped.

Report a busy lock; do not delete claims/sidecars to bypass it. Stop all work before file copying or cloud sync, and use only one device writer. Changed inputs fail/become stale, missing/corrupt weights require authorized setup/repair, and ingestion-error photos require rescan. Do not blindly retry configuration/resource failures.

## Input validity and history

Current result identity:

```text
(photo_id, profile_id, content_version, thumbnail_profile, input_image_hash)
```

A result must also pass dimension, dtype, byte-length, finite-value, normalization and vector-hash checks when used. A valid same-key result is reused. A corrupt same-key result can be repaired in an approved plan; this must not overwrite another model/input's history.

`original_status: missing|unavailable` does not block a complete saved JPEG. Index verifies the saved version only and does not claim to have rechecked original contents. A photo with `ingest_state: error`, a mismatched preview version or a corrupt JPEG is not a valid execution input. Original availability, ingestion state and embedding coverage are different facts.

Only ingestion updates content versions. Its changes invalidate results that no longer match current input; past-version/profile rows remain. Moving an album or repairing original paths does not change photo IDs, input identity or vectors. Changing the default does not reindex/delete anything. Old thumbnail bytes can be replaced, so historical vectors do not guarantee historical image replay.

## Coverage versus task status

`index status` inspects the chosen profile across the selected album file. It does not load a model or access originals. Default limit is 100, range 1–1000. `--after` and `--status` filter the returned photo-ID-ordered items; counts cover the entire album, not just the page. Retain filters/profile while following `next_cursor`.

| Coverage status | Meaning |
| --- | --- |
| `ready` | A matching current result passes the reported result checks |
| `missing` | No result for this photo/profile |
| `stale` | Historical results exist but none matches the current input |
| `invalid_input` | Saved photo/preview metadata cannot supply a valid current input |
| `invalid_vector` | The result fails vector validation |

Status uses **preview metadata**, not JPEG BLOB decoding. A `ready` vector is not proof that saved image bytes decode correctly; report preview integrity as unchecked until the relevant operation validates it. Vector validation and preview validation are separate checks. Source availability, task state and last failure are separate from coverage; a failed attempt must not erase another valid profile's result.

Report coverage for the scope actually inspected and distinguish unknown/not-checked from verified. `component: image_embedding` ready never means technical parameters are ready. No description/analysis record is a prerequisite.

## Storage and validation boundary

Schema 8/application ID `0x53414C42` has 11 tables: five for the album/photos/previews/scans and six `image_embedding_profiles/results/runs/items/claims/settings`. Claims remain required even when empty. No `image_index_*`, description-vector, multi-album or placeholder `technical_*` tables are created. Query vectors are not persisted.

Old v1–v7 files are rejected unchanged, with no migration or compatibility layer. The user's existing old database and backups remain untouched. See [index design](../../docs/index-design.md).

The portable code is implemented; consolidated regression acceptance is pending. **No new-format real-model trial was performed in this implementation.** Prior v7 timing/quality results are not new-format acceptance. Synthetic tests cannot establish actual download recovery, disconnected inference, memory, latency or retrieval quality; these need separately authorized evaluation.
