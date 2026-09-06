# index: local image embeddings

Index is one of the three public Skill capabilities. It uses **SQLite JPEG previews directly**, without cloud analysis, descriptions, original-file reads or automatic translation. See [management](management.md) for querying the resulting image vectors.

## Optional runtime and authorization

The base Skill does not import optional inference dependencies for ingestion, browsing, metadata search or index status. The pinned optional environment is **standard GIL-enabled CPython 3.14, 64-bit x86-64**, targeting Windows CPU wheels. It is not the broader Python 3.12+ base contract and does not support substituting a free-threaded interpreter.

Install [requirements-index.txt](../requirements-index.txt) only into a dedicated compatible project virtual environment:

```text
python -m pip install -r <skill-directory>\requirements-index.txt
```

Here `python` must refer to that virtual environment's interpreter. The dependency file is authoritative for exact package versions; runtime identity checks reject incompatible interpreters or versions rather than silently substituting packages. Do not modify a shared Python installation or assume base support guarantees PyTorch support. Wheel metadata/compatibility checks are not proof that the environment was installed or real inference succeeded.

Dependency installation is not model installation. **Model download, real-model trials and live-library migration require separate authorization.** Code approval does not authorize any of these. No weights are bundled in the repository.

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

The square resize is an accepted initial trade-off, not an aspect-preserving inference transform. There is no added center crop/padding, and it does **not** alter ingestion's proportional previews. NaFlex, technical parameters and ONNX/quantization are deferred; see [roadmap](../../docs/TODO.md).

Profile identity includes fixed weights, image/text processing, feature extraction, normalization, dimensions, backend/dtype and runtime identity. A cache path is not model identity: `--model-dir` can relocate the **same pinned files**, not select arbitrary weights. Matching dimensions alone never make two profiles interchangeable.

Text length is limited to **64 tokens including EOS**, with no added BOS and right-padding to the fixed context length. `QUERY_TOO_LONG` reports the actual count and limit; queries are never silently truncated. Ordinary operation loads only local files and does not execute remote custom model code.

## CLI reference

Prefix commands with:

```text
python <skill-directory>\scripts\photography.py --state-dir <state-directory>
```

Use the same state directory used for ingestion. Arguments below use placeholders:

```text
index setup [--model-dir <directory>]
index profiles
index configure --default-profile <profile-id>
index plan --album-id <album-id> [--profile-id <id>] [--model-dir <directory>] [--limit N] [--dry-run]
index plan --library-id <library-id> [--profile-id <id>] [--model-dir <directory>] [--limit N] [--dry-run]
index plan --ids-file <ids.json> [--profile-id <id>] [--model-dir <directory>] [--limit N] [--dry-run]
index execute <run-id> --confirm <digest> [--model-dir <directory>]
index status [--album-id <id> | --library-id <id>] [--profile-id <id>] [--limit N] [--after <cursor>] [--status <status>]
index job <run-id>
index resume <run-id> [--model-dir <directory>]
```

### Setup does not configure a default

The fixed manifest contains six files totaling **1,535,212,390 bytes (about 1.43 GiB)**. This is the declared file payload, not measured network usage, runtime memory or required free disk space; staging and preserved snapshots can require additional disk space. Review the model, size and target directory before authorizing setup. The fast tokenizer loads `tokenizer.json` directly; this profile does not require `tokenizer.model`, SentencePiece or torchvision.

`index setup` is the only model-download command. It uses the pinned file manifest, validates file sizes/hashes and publishes verified files without replacing a usable installation with an incomplete download. It reports the profile/profile ID, destination, `downloaded_files` and `reused_files`. A retry reuses validated files; a failure must be resolved, not silently replaced by another model or cloud API.

Setup registers the profile **but does not select it as the default**, even on the first installation. Run `index profiles` to retrieve the actual ID. Either deliberately save it:

```text
index configure --default-profile <profile-id>
```

or supply `--profile-id` to planning/search/status. Profile resolution uses an explicit ID first, then the saved default; without either, profile-dependent operations explain the missing configuration. Browsing and metadata search do not require a default.

New index generation never changes a query default. Do not use setup merely to browse status. An alternate local model directory must be supplied again to index planning/execution/resume and `management search --mode semantic` when needed; it does not change the profile ID.

### Plan an explicit scope

`index plan` requires exactly one of album, library or IDs file. An IDs file contains a UTF-8 JSON array of photo IDs; use IDs returned by ingestion/management. Omitting scope is an error, not whole-database selection. All photos in the selected scope are considered by default; positive `--limit` bounds selection.

Planning validates the profile and saved inputs, determines reuse versus generation, and freezes IDs, input versions/hashes and actions in a digest. It does not encode photos or download weights. A dry-run returns preflight without persisting a run. Opening storage can still perform its protected schema migration.

Present selected/cached/pending/invalid counts, selected profile and exact scope for approval. `index execute <run-id> --confirm <digest>` executes only the actual approved proposal. Copy the returned digest; do not manufacture approval from the existence of a digest or a user's request to import/plan. New scope/profile/input requires a new reviewed plan; execution must not silently expand to new album members.

### Execute, reuse and resume

Each item is claimed, its JPEG/input identity validated, encoded if needed, rechecked and committed in a short transaction. No model call holds a write transaction. One execution reuses its model instance; pure cache hits do not load the model. There is no force-overwrite flag.

Use `index job <run-id>` to inspect saved progress and item errors. `index resume <run-id>` requires prior confirmation when the frozen plan contains encoding work; it cannot grant that authorization. A cache-only run can resume without inference confirmation and still cannot expand into encoding. Still-valid persisted successes are reused, not encoded again. A computation lost before persistence may need redoing after safe recovery.

If an item was planned for **reuse** but its cached result later becomes invalid, that item fails and needs a **new repair plan**. Execution/resume must not turn approved cache reuse into unapproved inference. An item already approved for encoding can repair a damaged same-key result; a valid result remains reusable. For progress reporting, `model_calls` is cumulative for the run; `model_calls_this_execution` counts the current invocation.

Cross-process locks and input claims prevent another task from taking over active work solely because time has passed. Report a busy lock; do not delete claims or recovery files to bypass it. Changed inputs fail/become stale, missing/corrupt weights require setup/repair, and ingestion-error photos require rescan. Do not blindly retry incompatible configuration or resource failures.

## Input validity and history

Current result identity:

```text
(photo_id, profile_id, content_version, thumbnail_profile, input_image_hash)
```

A result must also pass dimension, dtype, byte-length, finite-value, normalization and vector-hash checks when used. A valid same-key result is reused. A corrupt same-key result can be repaired in an approved plan; this must not overwrite another model/input's history.

An offline source or a record marked missing does not block a complete saved JPEG. Index verifies the saved version only and does not claim to have rechecked original contents. A photo marked ingestion `error`, a mismatched preview version or a corrupt JPEG is not a current valid input.

Ingestion changes invalidate only results that no longer match the current input; rows from past versions/profiles remain. Album membership is not part of index identity. Changing the default does not reindex or delete anything. Old thumbnail bytes can be replaced, so historical vectors do not guarantee historical image replay.

## Coverage versus task status

`index status` inspects the chosen profile over the selected scope, or the database if scope is omitted. It does not load a model or access originals. Limit/after/status filter the returned items; retain filters while following the cursor.

| Coverage status | Meaning |
| --- | --- |
| `ready` | A matching current result passes the reported result checks |
| `missing` | No result for this photo/profile |
| `stale` | Historical results exist but none matches the current input |
| `invalid_input` | Saved photo/preview metadata cannot supply a valid current input |
| `invalid_vector` | The result fails vector validation |

Status uses **preview metadata**, not JPEG BLOB decoding. A `ready` vector is not proof that saved image bytes decode correctly; report preview integrity as unchecked until the relevant operation validates it. Vector validation and preview validation are separate checks. Source availability, task state and last failure are separate from coverage; a failed attempt must not erase another valid profile's result.

Report coverage for the scope actually inspected and distinguish unknown/not-checked from verified. No analysis record is required and there is no `needs_analysis` prerequisite.

## Storage and validation boundary

Schema v7 retains the six independent image-index tables and photo/library/album/scan/preview tables. Nine obsolete analysis/workflow/text-vector tables are removed only after a consistent backup of the old database; fresh databases do not create them. Existing image vectors, profiles, defaults, tasks and claims remain unchanged, even when a claims table is temporarily empty. Migration does not run a model. See [index design](../../docs/index-design.md) for the exact retirement list and rollback rules.

Synthetic offline tests cannot establish live migration safety, actual download recovery, real offline inference, memory use, latency or Chinese/English retrieval quality on a user's photos. These require separately authorized evaluation; no measurements or quality promises are implied by the model choice.
