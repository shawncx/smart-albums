# Smart Albums

**One album is one SQLite file.** One photography Skill exposes exactly three independent capabilities:

| Capability | Purpose |
| --- | --- |
| **ingestion** | Import local photo metadata, original paths and proportional JPEG previews into the selected album. |
| **index** | Explicitly set up/configure, plan, generate/reuse, inspect and resume local image embeddings. |
| **management** | Create/open/backup the album file, browse/search photos, export previews and explicitly locate/relink originals. |

These are not an automatic pipeline. Ingestion never calls a model. Browse/search never writes the database or checks original files; creation and original-path maintenance are explicit management operations. The program never deletes originals.

## Install the base Skill

Use a compatible Python 3.12+ interpreter in a project virtual environment. Base ingestion, browsing and metadata search need only [requirements.txt](photography/requirements.txt), not PyTorch or model weights. From the repository root on Windows:

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r photography\requirements.txt
```

The optional model runtime is narrower: **standard, GIL-enabled CPython 3.14, 64-bit x86-64**, with pinned Windows CPU dependencies. Portable album files do not imply that this runtime supports ARM, other Python versions or every host. See [index setup](photography/references/index.md); do not install into a shared interpreter.

Copy the entire `photography` directory into your host's supported Skill directory as `smart-albums`. Its entry is [photography/SKILL.md](photography/SKILL.md). The host needs local Python execution and access to the selected album and requested files.

## Select or create the album file first

Before a data operation, select an existing **SQLite album file** or explicitly choose to create a new one. Reuse an already selected file; there is no second internal album selection. Informational questions and `--help` need no album.

All operation commands require global `--database <absolute-album-file>` before the capability. Acceptable extensions are `.sqlite`, `.sqlite3` and `.db`; arbitrary filenames are supported. There is no default database, `--state-dir`, old alias or compatibility mode.

Here and below, `python` means the chosen virtual environment's interpreter. Replace placeholders with actual absolute paths and returned IDs:

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management create
python photography\scripts\photography.py --database <absolute-album.sqlite> management open
```

Use `create` only for an explicitly requested, unused destination in an existing directory. It never overwrites. `open` validates an existing file read-only: no creation, DDL or migration. A missing/invalid/old file is an error, not permission to create a replacement. Responses identify the album by UUID, filename-derived display name and full database path. Switching files resets prior photo/profile/run selections and pending confirmations. See [album-file entry](photography/references/library.md).

## ingestion: import without inference

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> ingestion <absolute-photo-directory>
```

Saved previews preserve aspect ratio: default longest edge 1024, JPEG quality 85, no upscaling. Repeated scans reuse unchanged inputs. Only ingestion updates content versions; changed inputs make historical embeddings ineligible for current search without deleting their history.

After import, the Skill explains the returned `index_prompt` and explicitly asks, **in the user's language**, whether to prepare an index for its exact successful, not-ready photo IDs:

- **Without an index:** browse photos, saved previews and basic metadata; find filenames/recorded paths.
- **With a valid index and matching local model:** also search visible content in Chinese or English. Results are similar candidates, not guaranteed detections or exact filters.

Fully indexed repeat scans do not prompt again. Importing, accepting the invitation or choosing an album never automatically downloads a model or authorizes an unseen execution plan. See [ingestion](photography/references/ingest.md).

## index: explicit setup, configuration and execution

Install optional dependencies in a dedicated compatible CPython 3.14 x64 virtual environment. Model download and real-model trials require separate authorization; code approval is not that authorization.

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

## management: browse, search and explicit maintenance

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management photos --limit 100
python photography\scripts\photography.py --database <absolute-album.sqlite> management photo <photo-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "IMG_01" --mode metadata
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "黑白的枯树" --mode semantic --output <output-directory>\candidates.json
python photography\scripts\photography.py --database <absolute-album.sqlite> management search "black and white leafless trees" --mode semantic --profile-id <profile-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management original <photo-id>
python photography\scripts\photography.py --database <absolute-album.sqlite> management relink <photo-id> --path <absolute-original-file>
python photography\scripts\photography.py --database <absolute-album.sqlite> management backup --output <new-backup.sqlite>
```

Metadata mode uses Unicode NFC/casefold literal substrings in filenames and recorded absolute/relative paths; no model/default is needed. Semantic mode uses the selected album's current vectors and a matching query encoder, without generating missing embeddings or mixing profiles. Empty candidates load no model. Report incomplete coverage; cosine scores are not probabilities or guaranteed matches.

By default, the Skill treats semantic results as internal candidates and selects which IDs to display using **embedding similarity scores, ranks and score gaps only**. Images and thumbnails are not passed to the agent, and selection is not visual verification. It may select fewer results or none instead of always returning the whole candidate list.

```text
python photography\scripts\photography.py --database <absolute-album.sqlite> management show-results <output-directory>\candidates.json --ids-file <output-directory>\selected.json --html <output-directory>\results.html
```

The selected ID file may contain `[]`. This helper does not repeat a query or modify the album. It validates the captured scope, preserves scores/ranks and creates a report with only the selected photos for the user; the agent returns its link without inspecting its images. The original candidates remain available as diagnostics.

Both modes and plain browsing are read-only and do not stat originals or repair paths. HTML/JSON are snapshots, with no selection widgets, album membership writes or live service. `management thumbnail`, `scan` and `scan-events` retain preview export and ingestion diagnostics. See [management](photography/references/management.md) and [search](photography/references/search.md).

## Portable paths, cache and backups

SQLite can be beside, above, below or on a different drive from photos. Each photo records an absolute original path and a nullable path relative to the **current database parent**, including `..`. Explicit original lookup prefers the absolute path. Only when it is missing does a usable relative path repair the absolute location; an absolute hit does not silently rewrite the relative path. Both missing means source missing; permissions/I/O failures are separate errors. Read-only repair failure is reported, never claimed as success.

Relink requires matching SHA-256 and updates both paths. Path-only repair preserves photo IDs, previews, content versions and vectors. Cross-drive/share relative paths may be `NULL`, with a warning that moving devices may require explicit relink. Moving and reingesting preserves IDs only when recorded path identity resolves unambiguously; there is no hash-based deduplication.

Models are shared machine-local files, independent of albums: `Config.model_cache_root` defaults to `%LOCALAPPDATA%\SmartAlbums\models` on Windows or the platform's application cache elsewhere. Optional global `--model-cache-dir <cache-root>` precedes `index` or `management` and works consistently for setup, execution and semantic search. A known older model cache can be reused by explicitly selecting its root; it is not automatically moved or deleted.

Use **one writer on one device at a time**. For cloud storage: download a complete local file, operate locally, stop/close all operations, then copy or sync it back. Do not copy an actively written bare SQLite file or delete its sidecars. `management backup --output <new-file>` makes a consistent no-overwrite SQLite snapshot containing previews and embeddings, **not originals, model weights or Python environments**. Each host needs its own compatible runtime/cache.

## Format and validation status

New albums use **schema 8**, `application_id = 0x53414C42`, and exactly **11 tables**: `album_metadata`, `photos`, `thumbnails`, `scans`, `scan_events`, plus `image_embedding_profiles/results/runs/items/claims/settings`. There are no internal albums/libraries, legacy image-index/text-analysis tables or empty `technical_*` placeholders.

Existing v1–v7 databases are **rejected unchanged**: no automatic migration, cleanup, overwrite or old CLI/API compatibility. The user's old database and backups must remain untouched. Existing static reports are not converted into new album snapshots.

The portable-album code is implemented; consolidated regression acceptance is still pending. **No real-model trial against the new format was performed in this implementation.** Earlier v7 measurements are not new-format acceptance. Synthetic tests cannot establish real retrieval quality, latency, memory or actual disconnected operation.

```text
python -m unittest discover -s tests -v
```

- [Current storage/index design](docs/index-design.md)
- [Portable-album implementation plan](docs/portable-album-plan.md) (plan text, not live completion status)
- [Earlier implementation plan](docs/ingestion-index-management-plan.md) (historical; superseded for storage/CLI)
- [Pending validation and future work](docs/TODO.md)

Private databases, photos, weights and generated reports are not distributed with the Skill. Pushing this repository does not back up an album or its originals.
