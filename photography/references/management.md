# management: read-only browsing and search

Management is one of the three public Skill capabilities. It views albums/photos, looks up album names and photo filenames, and searches image embeddings with Chinese or English queries. It has **no album writes, selection widgets, image indexing, model installation or cloud fallback**.

## Commands and common options

Use the same interpreter and state directory as ingestion/index:

```text
python <skill-directory>\scripts\photography.py --state-dir <state-directory>
```

Append one command:

```text
management albums [--limit N] [--after <cursor>]
management album <album-id> [--limit N] [--after <cursor>]
management photos [--album-id <id> | --library-id <id>] [--limit N] [--after <cursor>]
management photo <photo-id>
management search "<query>" --mode metadata --target albums [--limit N] [--after <cursor>]
management search "<query>" --mode metadata --target photos [--album-id <id> | --library-id <id>] [--limit N] [--after <cursor>]
management search "<query>" --mode semantic [--target photos] [--album-id <id> | --library-id <id>] [--profile-id <id>] [--model-dir <directory>] [--limit N]
```

All commands also accept:

- `--profile-id <id>` to inspect an explicit registered index profile rather than the saved default, if any.
- `--output <output-directory>\snapshot.json` to export the UTF-8 JSON result.
- `--html <output-directory>\snapshot.html` to export a standalone read-only HTML view.

JSON is returned on the CLI even without `--output`. Export paths are returned as `html_output` for `--html` and `output` for `--output`. Explicitly pass both flags if you want separate files; HTML alone does not automatically create a companion JSON file.

## Browse albums and photos

`albums` lists albums, counts and a saved cover preview identity; `album <id>` shows album metadata and a page of its members. `photos` lists all photos or an explicit album/library scope; `photo <id>` inspects one stored record. These are not filesystem rescans.

Browsing and metadata search work with **no model installed and no default profile configured**. The result then identifies index configuration as `not_configured`; that does not prevent viewing metadata/previews. An explicit profile ID must still be valid. A configured profile is used for saved index status, not inference.

Saved previews remain available when original files are offline. Ordinary JSON metadata inspection is not full JPEG BLOB validation and never verifies the originals. HTML export validates the previews it embeds; errors are displayed per photo. Changed versions are not silently substituted for the snapshot's images.

## Scopes and pagination

For photo browsing/search, `--album-id` and `--library-id` are mutually exclusive. Omitting both means the entire stored database, not just one source folder. Album-name searches do not accept these photo-scope filters; use `management album <id>` for details.

Browse lists and metadata search default to 100 items, accept limits 1–1000, and sort by stable album/photo ID. Follow non-null `next_cursor` with `--after` and keep query, target, scope and profile unchanged. Cursor pages are not a durable multi-page transaction; re-query after changes.

Semantic search returns top 10 by default, allows 1–1000, and **rejects `--after`**. It is ranked top-K, not ID-cursor pagination. It sorts by descending cosine similarity, then photo ID for ties.

## Metadata search

Metadata mode requires the explicit target:

```text
management search "旅行" --mode metadata --target albums
management search "IMG_01" --mode metadata --target photos --album-id <album-id>
management search "été" --mode metadata --target photos
```

Matching applies Unicode NFC normalization followed by casefold to query and fields. Albums match their names; photos match filename or relative path. Matching is a **literal substring**, not regular expression, SQL wildcard, description search or a semantic hybrid. Characters such as `%` and `_` are literal. Chinese text, case differences and canonically equivalent composed characters follow the same deterministic rule.

This operation needs no model, weights, API credentials or image embedding. A blank query is an error. No hits return an empty page; the program does not silently retry as semantic search.

## Semantic photo search

```text
management search "黑白的枯树" --mode semantic --album-id <album-id>
management search "black and white leafless trees" --mode semantic --profile-id <profile-id> --limit 10
```

Semantic mode accepts only photo targets. It uses the explicit profile, otherwise the deliberately configured default. If neither exists, explain [index setup/configuration](index.md); do not choose one silently. A model/profile change never automatically changes the query default.

If setup used a non-default model location, pass the same `--model-dir <directory>` to semantic search. This selects local files of the same pinned profile, not a new model or a download location for ordinary search. Metadata mode rejects `--model-dir`; browsing does not use it.

The image embeddings and query encoder must have the **same immutable profile**, not merely the same dimensions or model family. The initial fixed profile uses `google/siglip2-base-patch16-224` at revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, PyTorch CPU FP32. Queries go directly through its matching tokenizer/text feature interface without automatic translation or retired retrieval prefixes. The limit is 64 tokens including EOS; overlong queries return `QUERY_TOO_LONG` rather than being silently shortened.

Search takes a consistent snapshot of current saved photo/preview identities and the selected profile's results. Invalid, stale or absent image vectors are excluded and reported in coverage. It does not require an analysis/description, mix profile spaces, regenerate image embeddings or load original files.

If no valid candidates exist, it returns empty results and coverage without loading a model. Otherwise it encodes the query once and performs exact cosine ranking locally. Missing/incompatible weights produce an explicit error, not a download or remote substitute.

Coverage covers the **entire selected photo scope**, not just returned top-K. Interpret `ready`, `missing`, `stale`, `invalid_input` and `invalid_vector` separately from past task failures. Do not say all photos were searched when coverage is incomplete. Original availability is the stored ingestion state, not a new filesystem check; source changes not discovered by ingestion are not discoverable here.

Cosine scores are relative similarities, not probabilities, guaranteed logical filters or an automatic keep/reject decision. No calibrated universal match threshold is provided. Real Chinese/English retrieval quality, precise counts/negations and subject combinations require representative, separately authorized evaluation. Highest-ranked does not necessarily mean relevant.

## Snapshots and read-only guarantees

Both modes and browse views use `management-snapshot-v1`, with explicit schema version, mode, target, scope and selected profile identity. Metadata/browse records use `items`; semantic rankings use `results` and include result/profile IDs, current input identity and scores. A snapshot is not a live query.

Exports must be outside protected source/state trees; paths are validated before encoding/rendering. HTML embeds validated saved previews, escapes content and includes saved metadata. A missing/corrupt or changed preview is shown as an error, not replaced by a different image. Original files are never required just to render saved previews.

There are no checkboxes, selection-export controls, album writes or background server. New snapshots are not the old search-selection protocol and must not be passed to compatibility `search-add`.

“Read-only” means no photo/album mutations, no generated image vectors, no default changes and no model installs. Export writes only the explicitly selected output files. Opening an older supported database may still initialize/upgrade schema with a consistent backup and transactional migration. A live-library migration requires separate authorization; documentation or synthetic tests do not prove it was performed.

See [albums](albums.md) for the storage model, [index](index.md) for preparation and [search compatibility](search.md) for the boundary with retained legacy snapshots.
