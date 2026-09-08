# ingestion: metadata and proportional previews

Ingestion is independent of models and indexing. It reads source photos, records their paths/metadata and saves JPEG previews in the **explicitly selected, existing SQLite album file**. It never creates an album implicitly, deletes originals, installs a model or performs inference.

## Supported input

Recursive, regular JPEG, PNG, WebP, TIFF and BMP files; extensions are case-insensitive. Animated/multi-page images return a per-file error. RAW and HEIC/HEIF are not supported. Symbolic links and directory junctions inside a photo root are not followed. Empty source folders are valid.

The photo root and database path must be absolute. SQLite can be beside, above, below or on a different drive from photos: there is no directory-overlap ban. The selected database, its sidecars and machine-local model cache are excluded from photo enumeration. Generated JPEG exports must stay outside saved scan source directories so previews do not become new originals.

One SQLite file is one album. It can receive scans from multiple roots, without a separate library/internal album selector. Source roots are not virtual folders; ingestion never automatically adds or regroups folder members. See [album-file entry](library.md).

## Commands

Use actual paths and returned IDs. Commands emit UTF-8 JSON; required global `--database` precedes the capability. Accept `.sqlite`, `.sqlite3` and `.db`; do not infer a default file.

```text
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> ingestion <absolute-photo-root>
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> management photos --limit 100
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> management photo <photo-id>
```

`--thumbnail-size` (64–4096) and `--thumbnail-quality` (1–95) override preview settings. Keep settings consistent across scans; a changed preview profile can require rebuilding previews. There is no `ingest` alias, `--album-name`, `--album-id`, `--library-id` or old API compatibility layer.

Preview export and saved scan diagnostics belong to management:

```text
management scan <scan-id>
management scan-events <scan-id> --changes-only --limit 100
management thumbnail <photo-id> --output <output-directory>\preview.jpg
```

Prefix these with the same interpreter/script/database options. `scan-events` follows integer `next_cursor` with `--after`, retaining filters (limit 1–1000). `changed_photo_ids` is not a whole-album listing; browse with management to find existing photos.

## Preview and version contract

Previews default to maximum edge 1024 px, JPEG quality 85, proportional dimensions with pixel rounding, and no upscaling. EXIF orientation is applied. Valid embedded color profiles are converted to sRGB; conversion failures are metadata warnings. Transparency is composited on white, and source EXIF is not copied into previews.

The database records original/display dimensions, format, basic camera/exposure/date EXIF, size, nanosecond mtime and SHA-256. The content version is a SHA-256 digest. Each current SQLite thumbnail has its source version, generation profile, hash and image metadata; it is not a permanent exported JPEG path.

Index's official SigLIP **224×224 square resize happens only at inference**. It does not replace these stored proportional previews with square ones.

Only ingestion updates content versions. Changed source content or preview profile/hash invalidates affected image-embedding and feature results according to their input/dependency manifests. Historical rows and other profiles remain intact; no model is run during scanning. A repaired preview with exactly the same input identity can reuse an existing valid result. Old preview bytes may be replaced; preserving vectors is not a promise to replay historical images.

## Portable paths and stable identity

Each record has explicit `original_absolute_path` and nullable `original_relative_path` columns. The relative path is slash-separated data relative to the **current database parent**, can include `..`, and is never scan-root- or CWD-relative. Windows cross-drive/UNC-share cases store `NULL` with a visible `RELATIVE_PATH_UNAVAILABLE` warning; the valid absolute path still works.

Within the selected file, ingestion matches normalized saved paths, not `library_id` plus a source root:

1. Reuse the photo matching its absolute location.
2. If its absolute location is missing/unusable on this platform and the database-relative location resolves to the scanned file, retain the photo ID and repair its paths.
3. Identical content/preview identity reuses previews and embeddings; changed content retains the matched ID but updates the saved version/preview and makes old vectors stale.
4. With no unambiguous path match, create a new photo record. Identical content hashes alone never merge separate copies.

If several records match a path, or a scanned relative copy conflicts with a still-valid preferred absolute file elsewhere, report `PHOTO_PATH_CONFLICT`. Never choose the copy silently. Permissions/I/O errors are not missing-file evidence and do not permit fallback to another original.

Moving SQLite and photos together with their relative layout intact can preserve IDs on reingestion. Arbitrary renames/moves without surviving path identity cannot be inferred. Use explicit content-verified [relink](management.md) for a known photo's new location. A path-only repair does not change IDs, content versions, previews or vectors; an ordinary absolute lookup does not rewrite the relative path. Ingestion and explicit relink can recalculate paths for their requested scope.

## Ingestion response

Scan fields include `scan_id`, `album`, `source_root`, `status`, `scanned`, `added`, `updated`, `restored`, `unchanged`, `missing`, `failed`, `successful_photo_ids`, `changed_photo_ids`, `errors`, `warnings` and `source_errors`. `album` identifies the UUID, filename-derived name and actual database path, not an optional membership association.

```text
scanned = added + updated + restored + unchanged + failed
```

`index_summary` describes saved image-embedding state with `component: image_embedding`; `index_scope` is `successful_photos_in_this_scan`. `model_calls` is zero. Without a configured default, the summary has `status: not_configured`, a null profile and the successful-photo total, not inferred search coverage. The scan summary is not the whole album's coverage or proof of completed technical analysis.

The invitation still concerns only `image_embedding`. Stage-one `ocr`, `objects`, `scene`, `color`, `composition` and `perceptual_hash` require a separate explicit component/scope choice and exact-plan approval, including non-ML computation. OCR execution verifies original bytes; other pixel-consuming components use saved thumbnails, and derived components require existing results. Ingestion does not run any of them or auto-fill dependencies. Stage 2 OR search is implemented through separate legacy [read-only condition commands](search.md#stage-2-or-condition-workflow); targeted integration checks have passed and explicit legacy metadata/semantic behavior is unchanged. New natural-content requests use [default unified search](search.md#unified-query-json), with 100 globally deduplicated candidates unless the user requests any positive count or `all`. Querying never runs ingestion, image inference or automatic membership writes. See [feature indexing](index.md#stage-1-six-opt-in-components).

When `index_suggested` is true, `index_prompt` contains an explicit question asking whether to create/update the semantic-search index, plus `component`, `photo_count`, exact `photo_ids`, `profile_id`, `configuration_required` and `requires_confirmation: true`. It also includes `without_index`, `with_index` and `limitations`: browsing, previews/basic metadata, filename/recorded-path lookup, custom folder CRUD, manual single/batch membership changes, folder-name search and confirmed EXIF date organization work without indexing; a valid index plus its compatible local model adds Chinese/English visual-content semantic search. Semantic scores rank candidates, not guaranteed detections or exact filters.

Manual custom folders are the primary organization workflow, not an index prerequisite or a fourth capability. The Skill resolves actual photo IDs and writes a one-element JSON array for one photo, or a batch array, for `management folders add <folder-id> --ids-file <photo-ids.json>` (or the corresponding `remove` command). `[]` means no changes. Static, flat many-to-many memberships stay inside this SQLite album; removing membership/deleting a folder never deletes photos, originals, thumbnails or embeddings, nor affects other folders. Names are unique by NFC + casefold and stable IDs survive renaming. See [folder commands](management.md#manual-custom-virtual-folders-primary-workflow).

Optional date organization uses only saved, calendar-valid EXIF `datetime_original` in camera-local time, with no UTC conversion or fallback to modification/other dates. Missing/invalid dates and unavailable ingestion metadata are skipped with counts. Prepare exactly `--all` or `--ids-file`, review the create/reuse preview and digest, then explicitly apply the raw plan atomically. It needs no index or original reads and creates no jobs/live rules; later imports or manual removals are not automatically regrouped. This authorized deterministic metadata operation is not semantic evidence: semantic selection still uses embedding scores/ranks/gaps only, never images, HTML pixels or folder labels.

The Skill must explain this distinction and ask the question in the user's language after import so users know indexing is a separate, optional preparation step for semantic search. With a selected profile, only successful photos without a ready result are offered; ready photos and failed imports are excluded. Without a default, the prompt offers the successful scan scope but explains that configuration must come first, not that all those photos have a known missing cache.

Empty/all-failed scans and fully indexed repeat scans return `index_prompt: null`. Unchanged photos still missing an index can prompt again. The invitation is saved with the scan result, including a successful subset of an incomplete scan. Do not automatically create a plan, download weights or run inference from this field. If the user already requested import and indexing together, skip the duplicate intent question, prepare the exact offered scope, and follow [index confirmation](index.md). Otherwise wait for agreement before preparing the plan.

An incomplete scan can expose only an error and `scan_id` at the CLI. Fetch `management scan <scan-id>` to inspect its persisted successful subset and invitation before reporting or offering indexing. Never guess its scope. There is no observation-generation prerequisite or legacy scan JSON conversion.

`missing` counts newly missing records separately. Errors include path, an existing photo ID when available, code and message. Complete change/error lists and persisted scan-event pages support later inspection. Exit code 1 indicates partial per-file failure with successful results retained; exit code 2 indicates a call-level failure.

## Incremental behavior and errors

Unchanged size/mtime plus a valid SQLite preview skips source hashing/decoding; the saved preview is still validated. Changed attributes trigger source hashing. Identical bytes with a valid preview update file attributes without inventing a new content version. Missing/corrupt/obsolete previews are repaired.

Changing original bytes while preserving size and mtime can evade this fast path. There is no automatic original-file monitor or full-source-hash verification mode. Preview-only indexing cannot detect source changes ingestion has not discovered.

Single-file failures continue the scan and ordinarily mark an existing record `ingest_state: error`; conflict/concurrent-change errors must not overwrite another operation's record. Retained old metadata is not a current valid input. New unreadable files get error entries and receive a photo ID only after successful ingestion. Index rejects ingestion-error inputs until repaired/rescanned, rather than treating their old previews as current.

`original_status` is separate from `ingest_state`: `missing`/`unavailable` originals may still have a complete saved preview that index and management can use. This processes the saved version, not proof that the source remains unchanged. `management original` or relink does not repair an ingestion error.

A complete traversal is required to mark new missing files. It checks both saved locations for relevant absent records in **this source folder** only; photos from other folders are preserved. A valid alternate location prevents a missing marker. Permissions/I/O errors are reported separately. Incomplete traversal/interruption does not infer new missing files.

Photo/preview updates use short per-photo transactions with identity rechecks, not a transaction across the entire scan or long original reads. Completed per-photo work can survive interruption/crash; a handled incomplete scan saves its summary and diagnostics. Unexpected termination may leave the scan incomplete, so inspect saved state instead of claiming whole-scan rollback or completion.

## Storage and validation boundary

Only schema 10/application ID `0x53414C42` albums with 37 registered tables are supported: 32 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows, excluding internal `sqlite_sequence`. The unchanged `virtual_folders` / `virtual_folder_photos` and six `image_embedding_*` tables remain separate from feature evidence. Old v1–v9 databases are rejected unchanged; there is no migration, retired-table cleanup or old CLI compatibility. Existing user databases and backups remain untouched. Management exports use `album-snapshot-v2` with historical album/folder scope.

Use `management backup --output <new-file>` for a consistent SQLite snapshot; it includes saved previews/embeddings, feature results/prototypes and folder memberships, not originals or weights. Stop operations before moving/cloud-syncing the local album, and use one device writer at a time.

See the bundled [storage summary](library.md#storage-summary). Claim only recorded verification, not real-model quality/performance or successful execution inferred from synthetic-test or download authorization.
