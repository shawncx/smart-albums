# ingestion: metadata and proportional previews

Ingestion is independent of models and indexing. It reads source photos, records their paths/metadata and saves JPEG previews in SQLite. It never deletes original files, installs a model or performs inference.

## Supported input

Recursive, regular JPEG, PNG, WebP, TIFF and BMP files; extensions are case-insensitive. Animated/multi-page images return a per-file error. RAW and HEIC/HEIF are not supported. Symbolic links and directory junctions inside a photo root are not followed. Empty folders are valid libraries.

The root must be absolute. Canonicalization reuses alternate spellings of the same root. Within a library, identity follows normalized relative path: replacing contents retains the photo ID; moving/renaming currently produces a missing old record and a new record.

Source and state directories must be separate, non-overlapping trees. Keep the same state directory throughout a workflow. One state directory holds one `photography.db` with multiple libraries and albums.

## Commands

Use actual paths and returned IDs. All commands emit UTF-8 JSON; global `--state-dir` precedes the capability.

```text
python <skill-directory>\scripts\photography.py --state-dir <state-directory> ingestion <absolute-photo-root>
python <skill-directory>\scripts\photography.py --state-dir <state-directory> ingestion <absolute-photo-root> --album-name <name>
python <skill-directory>\scripts\photography.py --state-dir <state-directory> management photos --library-id <library-id> --limit 100
python <skill-directory>\scripts\photography.py --state-dir <state-directory> management photo <photo-id>
```

`ingest` is an alias of `ingestion`; the Python `photography_lib.ingest` import remains compatible. `--thumbnail-size` (64–4096) and `--thumbnail-quality` (1–95) override preview settings. Keep settings consistent across scans; a changed preview profile can require rebuilding previews.

Existing diagnostic CLI commands remain available without constituting another Skill capability:

```text
libraries
scan <scan-id>
scan-events <scan-id> --changes-only --limit 100
thumbnail <photo-id> --output <output-directory>\preview.jpg
```

Prefix these with the same interpreter/script/state options. `scan-events` follows `next_cursor` with `--after`, retaining filters (limit 1–1000). `changed_photo_ids` is not a whole-library listing; browse with management to find existing photos.

## Preview and version contract

Previews default to maximum edge 1024 px, JPEG quality 85, proportional dimensions with pixel rounding, and no upscaling. EXIF orientation is applied. Valid embedded color profiles are converted to sRGB; conversion failures are metadata warnings. Transparency is composited on white, and source EXIF is not copied into previews.

The database records original/display dimensions, format, basic camera/exposure/date EXIF, size, nanosecond mtime and SHA-256. The content version is a SHA-256 digest. Each current SQLite thumbnail has its source version, generation profile, hash and image metadata; it is not a permanent exported JPEG path.

Index's official SigLIP **224×224 square resize happens only at inference**. It does not replace these stored proportional previews with square ones.

Changed source content or preview profile/hash invalidates old image-index results for current queries. The old rows and other profiles remain intact; no model is run during scanning. A repaired preview with exactly the same input identity can reuse an existing valid result. Old preview bytes may be replaced; preserving vectors is not a promise to replay historical images.

## Album association

`--album-name` creates/reuses a name and adds every successfully scanned photo, including unchanged and restored photos. Failed files are excluded from new membership; existing memberships survive failed/missing files.

Without `--album-name`, ingestion does not create an album or change memberships. Libraries are scanning roots, not automatically synchronized albums; later source additions join an album only when ingested with the intended album option. See [albums](albums.md).

## Ingestion response

Scan fields include `scan_id`, `library_id`, `status`, `scanned`, `added`, `updated`, `restored`, `unchanged`, `missing`, `failed`, `changed_photo_ids` and `errors`. Album-related fields include `successful_photo_ids`, nullable `album`, `album_added` and `album_unchanged`.

```text
scanned = added + updated + restored + unchanged + failed
```

`index_summary` describes saved image-index state; `index_scope` is `successful_photos_in_this_scan`, and `index_suggested` indicates a possible next step. `model_calls` is zero. Without a configured default, the summary has `status: not_configured`, a null profile and the successful-photo total, not inferred search coverage. Discover/configure a profile through [index](index.md) only as requested. The scan summary is not the whole album's coverage.

New scan responses no longer contain legacy observation summaries or suggestions. New photo records do not carry a `needs_analysis` scheduling flag; image-index validity is derived from input and profile identity. Old saved scan/photo JSON is not rewritten merely to remove historical fields.

`missing` counts newly missing records separately. Errors include path, an existing photo ID when available, code and message. Complete change/error lists and persisted scan-event pages support later inspection. Exit code 1 indicates partial per-file failure with successful results retained; exit code 2 indicates a call-level failure.

## Incremental behavior and errors

Unchanged size/mtime plus a valid SQLite preview skips source hashing/decoding; the saved preview is still validated. Changed attributes trigger source hashing. Identical bytes with a valid preview update file attributes without inventing a new content version. Missing/corrupt/obsolete previews are repaired.

Changing original bytes while preserving size and mtime can evade this fast path. There is no automatic original-file monitor or full-source-hash verification mode. Preview-only indexing cannot detect source changes ingestion has not discovered.

Single-file failures continue the scan and mark an existing record `error`; retained old metadata is not a current valid input. New unreadable files get error entries and receive a photo ID only after successful ingestion. Index rejects ingestion-error inputs until repaired/rescanned, rather than treating their old previews as current.

Missing originals are different: a record marked missing may still have a complete saved preview that index and management can use. This processes the saved version, not proof that the source remains unchanged.

A complete traversal is required to mark new missing files. Incomplete traversal/interruption records failure without new missing markers. Per-photo savepoints protect photo/preview/membership changes; a handled interruption may retain successful partial work, while an unexpected crash rolls back the scan transaction.

## Storage maintenance

Current schema is v7. Opening a v1-v6 database makes a consistent backup and transactionally removes unused observation/workflow/text-vector tables, retaining photo/album/preview data and current image-index history. Older thumbnail migration steps still run where needed. Retired table data is preserved in the backup; legacy preview files are not automatically deleted. Migration never loads a model or converts old description vectors.

For missing/invalid legacy previews, report the migration error and repair details; restore using the prior version/backup and retry. Do not skip records or delete originals. Use a consistent SQLite backup, not a casual file copy during writes. State backups include stored previews but not original photo files. A production migration remains a separately authorized operation; see [index design](../../docs/index-design.md).
