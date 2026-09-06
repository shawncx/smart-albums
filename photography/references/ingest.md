# Ingestion and database previews (schema v3)

## Supported input

Recursive, regular JPEG, PNG, WebP, TIFF and BMP files. Extensions are case-insensitive. Animated or multi-page images return a per-file error. RAW and HEIC/HEIF are not supported yet. Symbolic links and directory junctions inside a photo root are not followed. Empty folders are valid libraries.

The root must be absolute. It is canonicalized so alternate spellings of the same root reuse its library. Identity within a library follows the normalized relative path: replacing content retains the photo ID; moving/renaming currently produces a missing old record and a new record.

## Storage and previews

State includes `photography.db` with thumbnail BLOBs and optional migration backups. One database manages multiple roots and albums. Legacy `thumbnails/` files are retained after migration but no longer used for reads. Previews are JPEG, default maximum edge 1024 px, quality 85, aspect ratio preserved, no upscaling. EXIF orientation is applied. Valid embedded color profiles are converted to sRGB; profile conversion failures are recorded as metadata warnings. Transparency is composited on white. Source EXIF is not copied into previews.

The database records original and display dimensions, image format, basic camera/exposure/date EXIF, file size, nanosecond modification time and SHA-256. Content version is the SHA-256 digest. Future analysis must match the current content/configuration versions; `needs_analysis` is an initial scheduling flag, not a substitute for that check.

## Commands

All commands emit UTF-8 JSON. Global `--state-dir` appears before the subcommand. These examples show argument structure; use actual IDs returned by previous calls.

```text
python <skill-directory>/scripts/photography.py --state-dir <state-directory> ingest <photo-root>
python <skill-directory>/scripts/photography.py --state-dir <state-directory> libraries
python <skill-directory>/scripts/photography.py --state-dir <state-directory> photos --library-id <library-id> --limit 100
python <skill-directory>/scripts/photography.py --state-dir <state-directory> photo <photo-id>
python <skill-directory>/scripts/photography.py --state-dir <state-directory> scan <scan-id>
python <skill-directory>/scripts/photography.py --state-dir <state-directory> scan-events <scan-id> --changes-only --limit 100
```

`photos` and `scan-events` return `items` plus `next_cursor`. Pass a non-null cursor as `--after` on the next call, keeping other filters unchanged. Limit: 1–1000. Pagination is for quiescent data; re-query after changes. Photos include `thumbnail_id`, not a permanent preview path; `photo` adds thumbnail metadata. Use `thumbnail <photo-id> --output <preview.jpg>` to export an image. `photos` also accepts `--album-id` instead of `--library-id`.

Add `--album-name <name>` to ingestion to create/reuse an album and associate every successful photo, including unchanged ones. Omit it to update the source index without changing memberships. Read [albums](albums.md) for exact behavior, status queries and migration.

For ingestion only, optional `--thumbnail-size` (64–4096) and `--thumbnail-quality` (1–95) override preview settings. Keep those settings consistent across later scans; a change rebuilds previews.

## Ingestion result

The response contains `scan_id`, `library_id`, `status`, `scanned`, `added`, `updated`, `restored`, `unchanged`, `missing`, `failed`, `changed_photo_ids` and `errors`. It also returns `successful_photo_ids`, nullable `album`, `album_added`, `album_unchanged`, `analysis_summary`, `analysis_scope`, `analysis_suggested` and `model_calls: 0`. Analysis summary covers successful photos in this scan, with no model configuration assumed. Retrieve an album-wide or configuration-specific summary through `analysis-status`.

```text
scanned = added + updated + restored + unchanged + failed
```

`missing` counts newly missing records separately. `changed_photo_ids` contains successfully ingested changed photos needing downstream processing. It is not a full library listing and does not re-emit every pending photo on each unchanged rescan. Query photos to resume downstream work after an interruption. Errors contain path, existing photo ID when available, code and message.

Small MVP scans return the complete change-ID and error lists, not full photo records. Persisted event pages support later inspection without fetching a whole photo library.

## Incremental and failure behavior

Unchanged source size/mtime plus a valid database preview skips hashing/decoding the original. The stored preview itself is validated. A changed size/mtime triggers source hashing; identical content with a valid preview only updates file attributes. Missing, corrupt or obsolete previews are repaired. Preserving source size and mtime while altering bytes can evade the fast check; a full-source-hash verification mode is not implemented yet.

Single-file failures continue the scan and mark an existing record as errored; old metadata is retained but is not current. New unreadable files have scan error entries and receive a photo ID only after successful ingestion. Restored paths retain existing photo IDs. Valid downstream state is retained when only a preview is repaired or identical content is restored.

A full directory traversal is required before missing markers are applied. Incomplete traversal or interruption records a failed scan without new missing markers. Database writes are serialized for a scan; successful partial work may be committed on a handled scan interruption. Each photo, its preview and new membership are protected by a savepoint. An unexpected crash rolls back the scan transaction. Legacy files are not automatically cleaned up.

Python callers may import `photography_lib.ingest` with the skill's `scripts` directory on the import path. Optional keyword-only `config` and `storage` support setup/testing. `ingest(path, album_name="Travel")` adds album membership; ordinary `ingest(path)` leaves it unchanged. No ingestion path invokes the model adapters.
