# Image index design

This document describes the ingestion/index/management implementation contract. It supersedes the retired local-description-analysis extension; it is **not** a real-model benchmark or proof of a live-library migration. The [implementation plan](ingestion-index-management-plan.md) records the approved scope and subsequent analysis retirement.

## 1. Three public capabilities

| Capability | Responsibility | Boundary |
| --- | --- | --- |
| ingestion | Scan sources, store basic metadata, original paths and proportional JPEG previews; optional import-time album association | No inference, automatic index or original deletion |
| index | Explicit model setup/profile configuration; plan, confirm, generate/reuse, inspect and resume image embeddings | No cloud fallback, description intermediate or technical-parameter generation |
| management | Browse albums/photos; Unicode name/filename lookup; Chinese/English semantic photo search | No album mutation, selection widgets, curation, clustering or deletion |

These are operations within one `smart-albums` Skill, not three separately installed Skills. The old OpenAI/Codex analysis workflow has been removed; it is neither a prerequisite nor a fallback. Top-level album writes and `search-add` remain compatibility-only. Obsolete schema is no longer created by production code; v7 removes it after backup. Shared JSON fingerprints live in `fingerprints.py` without changing existing profile IDs.

## 2. Images and model identity

### Stored input versus inference input

Ingestion saves one current proportional JPEG preview per photo in SQLite. Defaults remain longest edge 1024, JPEG quality 85, no upscaling, EXIF orientation applied and sRGB conversion where possible. Source files stay at their recorded locations; the program never deletes them.

Index reads only that stored JPEG and its identity metadata. It does not reopen the original, ask a vision service for a caption or require any saved observation.

The fixed first checkpoint is:

- Model: `google/siglip2-base-patch16-224`.
- Revision: `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`.
- Runtime: Transformers + PyTorch CPU FP32; single-image serial execution with a conservative CPU thread limit.
- Optional dependency environment: pinned standard GIL-enabled CPython 3.14, 64-bit x86-64; Windows CPU package versions live in `requirements-index.txt`, not a separate documentation pin list.
- Image preprocessing: the official fixed-revision SiglipImageProcessor, including **224×224 square resize**, rescale, normalize and resampling settings.
- No custom center crop/padding, NaFlex, ONNX, quantization, GPU, Ollama or remote fallback in this first profile.

The inference resize intentionally does not preserve aspect ratio; **the saved preview does**. Resizing in memory does not rewrite the stored thumbnail. This distinction must remain visible in user documentation.

The official paired image/text feature interfaces produce 768-dimensional L2-normalized vectors. Stored values are little-endian float32, 3072 bytes per result. Validation checks dimensions, dtype, finite values, nonzero norm, normalization, byte length and checksum rather than trusting a BLOB's presence.

Queries use the same checkpoint's tokenizer and text encoder with the fixed direct-text template and official padding. They do not use description-model retrieval prefixes or automatic translation. The limit is 64 tokens including EOS, with no added BOS and right-padding to the fixed context. Overlong queries are rejected, not silently truncated.

### Immutable profile, relocatable installation

A profile identifies the weights, fixed revision/files, processor/tokenizer behavior, feature extraction, normalization, dimensions, quantization setting and runtime/backend/dtype. The installation directory is deliberately excluded from that identity. Relocating identical pinned files does not create a new profile; arbitrary new weights cannot impersonate the old profile.

The manifest binds required safetensors/configuration/tokenizer files to sizes/hashes and their published provenance. Runtime loads local files only, without remote custom code.

`index setup [--model-dir <directory>]` is the only model-download operation. It validates the pinned files and registers a profile without changing the query default, including first setup. `index profiles` discovers IDs. `index configure --default-profile <id>` is a separate explicit write.

An operation selects its explicit `--profile-id`, otherwise the saved default. Required profile-dependent operations fail clearly when neither exists. Browsing/metadata search can show `not_configured` without requiring setup. Generating another profile's index never chooses it as the default.

## 3. SQLite schema v7

The current schema has 13 user tables. Seven manage ingestion and albums: `libraries`, `photos`, `scans`, `scan_events`, `albums`, `album_photos`, `thumbnails`. The other six, originally introduced in v6, manage model-independent image indexing:

| Table | Purpose |
| --- | --- |
| `image_index_profiles` | Immutable profile identity/configuration and creation time |
| `image_index_results` | Versioned image vectors, input/profile identity and validation metadata |
| `image_index_runs` | Frozen plans, digests, approval/execution state and summaries |
| `image_index_items` | Per-photo input snapshots, actions, result links, attempts and errors |
| `image_index_claims` | Exclusive current-input execution ownership |
| `image_index_settings` | Independent explicit default profile |

Result identity is:

```text
(photo_id, profile_id, content_version, thumbnail_profile, input_image_hash)
```

Result records also carry result ID, vector/hash, dimensions, dtype, normalization metadata and timestamp. They do not reference `analysis_id`. Task/result relationships must refer to the same photo/input/profile; database constraints and transactional checks protect this, not only the UI.

The same valid input/profile reuses its row. A damaged same-key row may be repaired, but another input/profile's row is retained. There is no force-generate-every-time history mode. Attempt records preserve task diagnostics separately from vector history.

Current image-index history, albums, memberships, scans and previews remain. Empty `image_index_claims` is still necessary for concurrency protection and must not be mistaken for obsolete storage.

The v7 retirement list is exact, not a wildcard or a deletion of all empty tables:

- `analysis_claims`, `analysis_plan_items`, `analysis_requests`, `analysis_plans`, `analysis_settings`
- `analysis_runs`, `analyses`
- `photo_embeddings`, `embedding_encoders`

Dependencies are dropped before parents with foreign keys enabled. Retired records, including nonempty old vectors, are preserved in the pre-migration backup rather than converted into image vectors. A retained/custom table referencing a retired table blocks the migration instead of silently losing dependent data. Fresh databases do not create any retired tables; old DDL exists only in test fixtures.

### Migration and backups

A v1-v6 database receives a consistent SQLite backup before migration. New empty databases initialize at v7. Earlier upgrades retain the existing thumbnail migration chain. Neither retirement nor migration requires an original-photo rescan or model load.

Migration is transactional, with version advancement only after integrity/foreign-key checks and row/BLOB comparisons for all retained tables, including every `image_index_*` table. Intentional retirement is the only excluded set. Failure after a DROP restores the old schema, records and version. Legacy preview errors need repair through the previous version/backup; old preview files are not deleted to make migration pass.

Even read-only management may open storage through this migration path. “Read-only” describes business operations, not a promise that an old database's schema is never initialized/upgraded. Live-library migration is separately authorized. Rollback uses the older code and pre-migration backup, not a direct schema downgrade. State backups preserve stored data/previews, not external originals.

The migration implementation is authoritative; this document intentionally contains no second executable SQL draft or unvalidated example dataset.

## 4. Current input and historical results

Ingestion updates current photo/preview versions. Results whose saved identity no longer matches become ineligible for current retrieval, without deleting their rows. Other profiles keep their independent results. Switching A → B → A can reuse A when A still matches the current input. Album membership is outside result identity.

Stored preview history need not be retained: ingestion can replace old thumbnail bytes. Historical embeddings therefore do not guarantee recovery or replay of old photos.

Input rules:

- Missing/offline originals are allowed when the saved preview/version is complete. This is processing a saved input, not verification of current source bytes.
- An ingestion `error` record is rejected as a current input even if an older preview still exists; repair and rescan it.
- Invalid JPEG/hash/version metadata or an input change during execution fails/stales the item; it must not become a current successful result.
- Ingestion's size/mtime fast path is unchanged. Source changes preserving both values can evade discovery; preview-only indexing is not an original-file monitor or full-source-hash audit.

## 5. Planned and resumable execution

`index plan` requires one explicit album, library or IDs-file scope. It selects the scope's photos, optionally bounded by a positive limit; absent scope never means index the entire database. `--dry-run` returns preflight without saving a run.

The plan freezes photo IDs, profile, versions/hashes and reuse/generation actions in a digest. Planning performs no inference/download. Execution requires the exact approved digest; a digest alone is not evidence of user approval. A changed plan requires review again rather than scope expansion during execution.

For each confirmed item:

1. Acquire the process/run lock and exclusive input claim.
2. Re-read and validate the stored input.
3. Reuse an existing valid result or encode the preview.
4. In a short transaction, recheck current identity, save the result/item state and release the claim.

Model calls do not hold a write transaction. Pure-cache plans do not load the model. A run reuses its loaded model instance rather than loading per photo.

Reuse/encode/skip actions are frozen with the input identity. If a planned reuse loses its valid cached result, execution fails that item and requires a new repair plan; it cannot silently expand reuse into inference.

`index job` reports progress; `index resume` requires prior confirmation for planned encoding work. Cache-only runs can resume without inference confirmation, but cannot expand into encoding. Persisted success is never re-encoded merely because a later item failed or the process stopped. Work computed but not committed before a crash can be retried after safe recovery. Time elapsed alone must not steal a live worker's claim; cross-process locks distinguish active execution.

Configuration/model errors are not blindly retried. Per-item failures retain reasons. There is no automatic model substitution, forced overwrite of valid vectors or deletion of source data.

## 6. Coverage, search and snapshots

Coverage states are `ready`, `missing`, `stale`, `invalid_input` and `invalid_vector`. Task execution states and last errors are separate; one failed run cannot erase another current valid result.

`index status` reads no original files, loads no model and uses preview metadata rather than decoding JPEG BLOBs. Vector checks do not certify preview integrity. Full validation at execution/search/rendering applies to the data each operation uses; unchecked preview bytes remain explicitly unchecked.

Metadata search applies Unicode NFC then casefold and literal substring matching to album names or photo filenames/relative paths. Its target is required, photo scopes are explicit or all-database, and no model/default is needed. Browse/metadata pagination uses stable IDs, default 100, range 1–1000.

Semantic search uses a consistent saved-data snapshot and one selected profile. It validates current candidate inputs/vectors, reports full-scope coverage and excludes invalid/stale/absent results. With no candidates, it returns empty results without model loading. Otherwise it encodes the text once, performs exact cosine ranking and breaks ties by photo ID. Default top-K is 10, range 1–1000; semantic ranking does not accept ID-cursor pagination.

Search does not create embeddings, mix profiles, alter defaults/albums, translate queries or silently change modes. Similarity is not probability, a calibrated match threshold or a guaranteed logical filter.

`management-snapshot-v1` is a separate, versioned read-only JSON/HTML format with mode, target, scope and profile identity. It does not depend on description/analysis fields. HTML escapes data, uses validated saved previews and reports changed/unavailable inputs instead of showing replacement versions. Protected export paths cannot overwrite source/state files.

No new snapshot includes selection checkboxes, album-write controls or a live server. Legacy `search-add` accepts its legacy snapshots only; retaining that protocol does not add management write capabilities.

## 7. Authorization and unverified work

Ordinary index/search operations are local/offline and never download missing files. Dependency installation and explicit model setup remain separate from inference. Code approval is not permission to download weights, process real photos, benchmark a gallery or migrate a live database.

Offline synthetic regression covers contracts and failure paths, not real-model quality, speed, memory, actual disconnected inference or a live migration. Later authorized trials should:

- Verify pinned-file setup/recovery and actual offline operation.
- Start with a non-sensitive image, then a deliberately selected representative gallery.
- Evaluate paired Chinese/English queries, no-match cases and difficult combinations with human labels and retrieval metrics.
- Measure cold load, image encoding, query encoding, ranking and memory separately; leave unknown timings unknown.
- Decide broader rollout only after reporting results and obtaining the intended scope authorization.

Required follow-ups in [TODO](TODO.md): NaFlex as a new profile/input budget with comparison against 224; independently defined technical parameters; and ONNX/quantization as separately validated runtime/vector identities. None is enabled or quality-verified by this design.

## References

- [Index CLI and recovery](../photography/references/index.md)
- [Management CLI, scopes and snapshots](../photography/references/management.md)
- [Pinned official checkpoint](https://huggingface.co/google/siglip2-base-patch16-224/tree/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2)
- [Pinned official image processor](https://huggingface.co/google/siglip2-base-patch16-224/blob/75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2/preprocessor_config.json)
- [Official SigLIP feature interfaces](https://huggingface.co/docs/transformers/model_doc/siglip)
