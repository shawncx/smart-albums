# Albums: browsing, ingestion association and compatibility

Albums are a data organization concept, not a fourth Skill capability. Public album viewing belongs to [management](management.md); import-time association belongs to [ingestion](ingest.md).

One state directory contains one SQLite database with libraries, photos, albums, memberships and previews. A library is a scanning root; it is not an automatically synchronized album. Several albums can reference one photo without copying its preview or index results. Separately ingested copies/renamed original paths are not automatically merged.

Album names are trimmed, Unicode-normalized and case-insensitively unique in the database. Album IDs survive renaming through a compatibility interface.

## Public read-only viewing

Prefix examples with the same interpreter/script/`--state-dir` used for ingestion:

```text
management albums --limit 100
management album <album-id> --limit 100
management photos --album-id <album-id> --limit 100
management photo <photo-id> --html <output-directory>\photo.html
management search "旅行" --mode metadata --target albums
```

Use returned IDs. Follow `next_cursor` with `--after`, retaining filters. Browse limits are 1–1000, default 100. Album/photo views optionally export read-only JSON/HTML snapshots and work without originals, a model or a configured default profile.

Metadata album-name lookup uses Unicode NFC/casefold literal substrings, not SQL wildcards. New management views have no create/rename/delete/member controls or selection widgets.

## Import-time association

```text
ingestion <absolute-photo-root> --album-name <name>
```

This retained ingestion option creates/reuses the album and associates successful new, updated, restored and unchanged photos. Failed photos get no new membership; existing failed/missing members remain. Omitting `--album-name` changes no memberships. Later source additions require another ingestion with the target album to join it.

Album relationships are not part of image-index identity. Adding a relationship during ingestion does not load a model, duplicate a vector or regenerate another valid profile's index. A migration does not automatically convert source libraries into albums.

## Compatibility-only album writes

Existing top-level `albums`, `album-create`, `album-rename`, `album-add`, `album-remove`, `photos --album-id` and legacy `search-add` interfaces remain available for old clients and explicitly requested compatibility use. They are **not public management operations**, not automatic follow-ups to a new search, and not another Skill capability.

The old create operation reuses a matching name. Explicit member add/remove operations validate IDs and are atomic/idempotent; removing membership deletes only the relationship, not photo records, previews, observations, vectors or original files. Do not route new management snapshots into legacy selection; see [search compatibility](search.md).

Observation commands and their old tables have been retired. Schema v7 migration preserves retired data in its backup before removing the tables. Album browsing, saved snapshot selection and image-index search do not depend on them.

## Previews and schema migration

SQLite holds the current JPEG BLOB plus source version, profile, hash and dimensions. Exported JPEGs are derived copies, not the authoritative preview store. Original file bytes are not inserted into SQLite.

Schema v7 retains current photo/album/preview and image-index data, but drops the nine unused analysis/workflow/text-vector tables after a consistent backup. New databases contain only active tables. Opening an older supported database can perform this migration even for read-only browsing. Older thumbnail migration still requires valid saved preview files; repair reported errors rather than dropping data. Old preview files are not automatically deleted.

State backups do not contain originals. Use consistent SQLite backup operations and preserve separately stored source photos. The migration contract is not a report of a verified live-library upgrade; see [index design](../../docs/index-design.md).
