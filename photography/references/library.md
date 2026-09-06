# Select or create a SQLite album file

**One album corresponds to one SQLite file.** Selecting/creating that file is the
entry to ingestion, index and management, not a fourth capability or a selection
of a library followed by an internal album. The file-based interface is implemented.

## Entry workflow

1. Reuse a file explicitly selected in the conversation or current request.
   Otherwise offer **open an existing album** or **create a new album**, and obtain
   its absolute SQLite path using the host's question/selection UI.
2. **Open existing:** require an existing supported file. Missing, invalid and old
   files are errors, not permission to create an empty replacement. Never infer a
   database from an example, current directory or environment default.
3. **Create new:** require an explicit choice and an unused destination in an
   existing directory. Never overwrite/clear an existing file. Creation does not
   authorize ingestion, downloads, path repair or inference.
4. Announce the full selected path and requested scope. Operate on that file
   directly; there is no internal album ID to choose.
5. Switching files clears previous photo/profile/run selections and pending
   confirmations. Discover the newly selected file's own state before acting.

Ordinary information requests and `--help` need no database. Do not scan unrelated
folders/drives to find one or ask again on every command when it is already selected.

## Commands and format

```text
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> management create
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> management open
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite> management backup --output <new-backup.sqlite>
```

Global `--database` is required before the capability; `.sqlite`, `.sqlite3` and
`.db` are accepted, with no fixed filename or hidden default. `--state-dir`, old
aliases and internal album/library options are removed, not compatibility entry points.

`create` initializes a private temporary file and publishes it without replacing
an existing destination, including a file that appears during creation. Backups
use the same no-overwrite publication boundary. The new album has schema 8,
application ID `0x53414C42`, exactly 11 tables and one album UUID. `open` uses a read-only
connection, validates format/integrity and returns identity/counts/coverage. It
performs no DDL, repairs or migration, creates nothing and retains no background
connection. No installed model/default is needed to open or browse an album.

The `album` response includes `id` (stable UUID), `name` (filename without extension)
and `database_path` (actual absolute location). Moving or renaming the file changes
its location/display name, not its UUID. Copies/backups preserve that same identity;
they are not independently mergeable branches.

Existing v1–v7 databases, unrelated SQLite files and damaged files are rejected,
not reinitialized or migrated. Keep old user databases and backups untouched.
Creating a separately requested new album is not a conversion of the old one.
See [storage design](../../docs/index-design.md).

## Portable originals

The SQLite file can be beside, above, below or on another drive from the photos.
Each photo exposes `original_absolute_path` and nullable `original_relative_path`.
The relative path is interpreted from the **current database parent**, permits `..`,
and is not relative to CWD, a model installation or the old scan root.

Plain browsing/search reads saved data only: it neither stats originals nor
repairs paths. Use the explicit [original/relink operations](management.md):

- A usable absolute path wins, even if a relative copy also exists. This does not
  automatically rewrite the stored relative path.
- Only when the absolute location is missing is the relative fallback tried.
  Success updates the absolute path without changing photo IDs, content versions,
  thumbnails or vectors.
- Both locations missing, or a missing absolute with no relative fallback, means
  source missing (`original_status: missing`). Permission/I/O failures are separate
  `unavailable` errors, not grounds to silently choose another copy.
- If repair cannot be written, report the usable location and unsaved repair,
  never successful persistence or a missing source.
- Across Windows drives or UNC shares, relative-path computation may produce
  `NULL` and `RELATIVE_PATH_UNAVAILABLE`. Explain that relocation may require
  `management relink <photo-id> --path <absolute-original-file>`.

Relink verifies SHA-256 against the ingested content version and updates both
paths. Finding an existing file alone is not content verification. Only ingestion
updates content versions; arbitrary moved/renamed copies are not merged by hash.

## Machine-local model cache

`Config.model_cache_root` is independent of the selected album. The defaults are:

- Windows: `%LOCALAPPDATA%\SmartAlbums\models`.
- macOS: `SmartAlbums` → `models` under the user's Library cache directory.
- Other hosts: `smart-albums` → `models` under `$XDG_CACHE_HOME` (or the user's `.cache`).

Optional global `--model-cache-dir <cache-root>` overrides the root for setup,
planning/execution/resume and semantic search alike; it precedes the capability.
`SMART_ALBUMS_MODEL_CACHE_DIR` can also configure the cache, **not a database**.
Profiles/defaults remain per album while identical weights are shared on the host.

A known older installation may be reused by explicitly selecting the root that
contains its model/revision directories. There is no automatic search, move,
deletion or redownload of old weights. See [index](index.md) for layout and setup.
Cache portability is not runtime portability: the current SigLIP runtime is pinned
to CPython 3.14 GIL x86-64 CPU, not ARM or arbitrary Python versions.

## Move, back up or sync safely

Use one writer on one device at a time. Commands close connections on completion;
while writing, rollback-journal and execution-lock sidecars are not disposable.
Never copy an actively written bare database file or delete a lock to take over.

For cloud storage: **download a complete local album file → operate locally →
stop/close all work → upload/sync**. Do not open a cloud URL, incomplete placeholder
or competing per-device copies as if distributed locking existed. Model files and
compatible Python environments must be provisioned separately on each host.

`management backup` uses SQLite's consistent backup API and refuses an existing
destination. It includes saved metadata, previews, profiles, vectors and runs, but
not originals or model/runtime files. A moved backup can browse saved previews;
originals resolve only if an absolute location or relative layout still works.
Otherwise explicitly relink them. Do not write independently to both copies and
expect automatic conflict merging.

A saved run still marked `running` requires confirmation that all old workers have
stopped before `index resume <run-id> --confirm-stopped`; the flag does not bypass
a live OS lock or grant missing inference approval.

Consolidated regression acceptance is pending. No new-format real-model trial was
performed in this implementation; old v7 results do not verify this workflow.
