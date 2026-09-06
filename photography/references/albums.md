# Albums and saved results

One state directory contains one SQLite database with scanning libraries, photos, albums, memberships, thumbnails and analyses. Album names are trimmed, Unicode-normalized and case-insensitively unique within that database. IDs remain stable when an album is renamed. Multiple albums reference the same photo record. This does not merge separately indexed copies or renamed original files.

## Commands

Use the same Python/script/state-directory prefix as ingestion. All names and IDs below are placeholders.

```text
ingest <absolute-folder> --album-name <name>
albums
album-create <name>
album-rename <album-id> <new-name>
album-add <album-id> <photo-id> <photo-id>
album-remove <album-id> <photo-id>
photos --album-id <album-id> --limit 100
thumbnail <photo-id> --output <absolute-preview.jpg>
analysis-status --album-id <album-id>
analysis-status --album-id <album-id> --status never_analyzed --limit 100
analysis-status --album-id <album-id> --provider codex --model <model> --reasoning low
analysis-report --album-id <album-id> --output <absolute-report.html>
```

`album-create` reuses an existing matching name and reports `created: false`. Membership changes are atomic for the requested unique photo IDs. An unknown photo or album fails the operation. Repeating an add/remove is harmless. Removal only removes the relationship. Photo lists and status lists paginate via `next_cursor`/`--after` (limit 1–1000); retain other filters between pages.

Ingestion with an album adds successful new, updated, restored and unchanged photos. Failed files are excluded from new membership; existing membership is retained for failed/missing photos. Without an album argument it creates no album and changes no memberships. Later source-directory additions require ingestion with the target album to join that album. A migration alone does not turn old libraries into albums.

## Analysis status without model calls

`analysis-status` also accepts `--library-id` instead of `--album-id`. It needs no credentials and never initializes a provider. The summary counts cover the entire selected album/library even when the returned list is filtered/paginated.

| Status | Meaning |
| --- | --- |
| `never_analyzed` | No successful analysis is saved for this photo |
| `saved` | A saved result matches the indexed photo/preview; no next-call model configuration was selected |
| `cached` | A saved result also matches the explicitly selected analysis configuration |
| `needs_update` | History exists, but no result matches the indexed image or requested configuration |

These states are mutually exclusive. Source availability, missing preview metadata and the last failed attempt are separate fields/counts, which can overlap these states. Status reads hashes/versions from database metadata, not image BLOBs. `preview_integrity: not_checked` and the scope note make this limit explicit. Full image validation and original checks occur before analysis, and reports validate the BLOBs they display.

After ingest, `analysis_summary` covers `successful_photo_ids` from that scan; it is not an album-wide summary. `analysis_suggested` flags never-analyzed/outdated saved versions. Offer the pending list and an optional next analysis step; do not run it without authorization. If a specific analysis configuration is relevant, query its status before promising cache reuse. A failed previous attempt does not erase a valid saved analysis.

## Explicit analysis

```text
analyze --album-id <album-id> --provider <provider> --limit 3
analyze --album-id <album-id> --provider <provider> --pending-only --limit 3
analyze --album-id <album-id> --provider <provider> --pending-only --all
```

Use only the scope authorized by the user. Album/library selection defaults to five photos. `--pending-only` filters matching saved results before applying the sample limit; it cannot be combined with `--force`. Unavailable candidates are kept so preflight returns a visible error. Empty album/filter selections return zero work without checking credentials or calling a model. Explicit empty photo ID lists remain invalid.

## Preview storage, browsing and migration

Schema v3 stores one current JPEG BLOB per photo, plus its source version, generation profile, image hash, dimensions and byte length. Photo lists do not load BLOBs. `thumbnail` exports a derived JPEG outside state/source directories; temporary exports are not the authoritative thumbnail store. Original file bytes are never inserted into the database.

Reports with no provider/model filter show saved results across models, labeling history that no longer matches indexed input. With a filter they distinguish matching results from history. Offline originals do not prevent reading thumbnails, saved analyses or editing memberships. This does not establish that an offline original is unchanged, and new analysis still requires the source checks.

Opening a v1/v2 database makes a SQLite backup in `backups/`, then migrates legacy preview bytes transactionally. Photo IDs, analysis rows and image bytes are preserved; old preview files remain. Missing/invalid/externally located legacy previews produce `THUMBNAIL_MIGRATION_FAILED` with per-photo repair details and roll back schema/data changes. Restore those previews using the prior version or backup, then retry. Backups contain indexed data and, for v3, previews; they do not include originals. Use a consistent SQLite backup operation, not an arbitrary copy during writes.
