# Albums are files, not internal collections

Start with [selecting or creating a SQLite album file](library.md). This reference
describes file lifecycle, not the removed multiple-albums-per-database interface.
There are exactly three Skill capabilities; file lifecycle belongs to management.

Prefix examples with:

```text
python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite>
```

Then use:

```text
management create
management open
management photos --limit 100
management photo <photo-id> --html <output-directory>\photo.html
management backup --output <new-backup.sqlite>
```

- **Create:** explicit new-file permission, unused path, no overwrite.
- **Open:** read-only format/identity/coverage inspection; no DDL, repair or migration.
- **Browse:** saved photos/previews without model installation or original-file access.
- **Backup:** consistent SQLite snapshot at a new destination; originals and models
  are not included.

The response's album UUID survives moving/renaming; the display name is the current
filename without extension. The database path selects the album, not an internal
ID. Switching files requires clearing the previous photo/profile/run context and
pending confirmations. A copy/backup retains the UUID and is not a new independent
album to edit concurrently.

Ingestion can add photos from several folders to the same file. Source roots are
saved scan information, not libraries to select or membership collections. There
is no `--album-name`, `--album-id`, `--library-id`, internal membership operation or
retained legacy command/selection protocol. Search covers only the selected file.

New albums use schema 8 with 11 tables. Old v1–v7 databases are rejected unchanged,
not migrated, emptied or overwritten. Cross-file search, merging albums, shared
photo/vector records and automated cloud synchronization are outside this version.

See [management](management.md) for original-path maintenance and exports,
[ingestion](ingest.md) for stable path identity, and [index](index.md) for confirmed
embedding work. Stop all writers before moving/syncing files; a portable database
does not imply that originals or a compatible runtime are available on another host.
