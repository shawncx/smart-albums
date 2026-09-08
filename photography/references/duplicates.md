# Duplicate discovery

Use this reference for `management duplicates`, a sibling of `management search`. It discovers all matches under explicit duplicate rules and displays connected candidate groups. It does not require a text query or an AI review/selection stage.

Compatibility: album schema 12 retains 40 registered tables, including `virtual_folders` and `virtual_folder_photos`; this feature adds none. v1–v10 files are rejected unchanged, with no automatic migration. Legacy management exports keep `album-snapshot-v2`. Stage 2 OR search is implemented; targeted integration checks have passed, and legacy metadata/semantic behavior remains unchanged. Duplicate discovery has its own snapshot and acceptance tests below.

## Entry and scopes

Use the selected interpreter and installed Skill's `scripts/photography.py`, with global `--database <absolute-album.sqlite>` before `management`. The examples below omit that prefix. File placeholders must become actual absolute paths.

```text
management duplicates scan --all --output <snapshot.json> --html <report.html>
management duplicates scan --folder-id <folder-id> --mode exact --output <snapshot.json>
management duplicates scan --folder-id <first> --folder-id <second> --folder-match union --output <snapshot.json>
management duplicates scan --ids-file <photo-ids.json> --mode similar --profile-id <hash-profile-id> --max-distance 8 --output <snapshot.json>
management duplicates groups <snapshot.json> --limit 50
management duplicates group <snapshot.json> --group-id <group-id> --limit 100
management duplicates pairs <snapshot.json> --group-id <group-id> --limit 100
management duplicates report <snapshot.json> --output <report.html>
```

`scan` requires exactly one of `--all`, repeated `--folder-id`, or `--ids-file`. The latter is a JSON array of real photo IDs; duplicate IDs are normalized, `[]` is empty, and `null` or nonexistent IDs are errors. Multiple distinct folders require `--folder-match union|intersection`. Only photos within the selected scope are compared; a duplicate outside it is not considered. Empty scopes succeed without a configured hash profile.

`--output` is required and saves a complete JSON snapshot. Standard output is a summary with the snapshot path, not the full input/group arrays. Optional `--html` produces a complete local report from the same snapshot. Protect input files, albums, originals and cache paths with the existing export rules; do not use a snapshot or ID file as an output destination.

## Modes and evidence

| Mode | Match rule | Required evidence |
| --- | --- | --- |
| `exact` | Identical saved SHA-256 (`content_version`) | Valid ingested photo records |
| `similar` | dHash64 Hamming distance at or below threshold | Complete, current results from one perceptual_hash profile |
| `all` (default) | Combine both rules; exact has precedence for the same pair | Independently track both kinds of input coverage |

`--profile-id` refers to **perceptual_hash**, never image_embedding. If omitted, use that component's saved default; do not silently choose another profile. An explicitly wrong or nonexistent profile is an error. `--max-distance` is an integer 0–64, initially defaulting to 8. Exact mode rejects profile/distance arguments. Even distance 0 is visual fingerprint equality, not proof of byte equality; distance is not a duplicate probability. Crops, strong edits, low-texture scenes and burst photos need separate quality evaluation.

Inputs are read in one SQLite snapshot, then comparisons run outside the transaction. Exact discovery groups saved SHA values in O(N); similar discovery traverses the upper triangle of current hashes, O(M²) comparisons with O(N) stored input/group evidence. There is no dense matrix, top-K, candidate count cap or sampling. At 10,000 current hashes, the maximum is 49,995,000 comparisons. Large or dense collections may be slow. Report actual measured performance rather than claiming linear similarity search.

No original files are read or checked, no previews are decoded during scanning, no model is loaded, and no feature/comparison/index plan is created. Indexing, including hash generation, remains a separate [index](index.md) operation. Exact matches are based on the last ingested content, not current disk verification.

## Groups and direct pairs

Groups use `grouping: connected_candidates_not_equivalence`. If A–B and B–C pass the threshold but A–C does not, they can be in the same candidate group; A–C is absent from the direct-pair list. This display rule does not change legacy `query-pairs` or create static virtual folders.

Each matched photo appears once in the main grouped result. Groups are `exact`, `similar`, or `mixed`; exact subgroups and each member's best direct match are preserved. Exact-only groups sort first, followed by increasing minimum Hamming distance, decreasing group size and photo ID. Group IDs are deterministic for the same members/input identities and parameters; numeric group/member labels are meaningful only within the indicated snapshot.

All matches are available. `groups` pages group summaries, `group` pages the members of one group including saved metadata and direct-match evidence, and `pairs` pages actual matching pairs. Use returned `group_id` values, not guessed IDs. Each accepts a positive `--limit` or `all`, plus the returned `--after` cursor. Cursors bind the snapshot digest, view and group and cannot be reused elsewhere. Group summary pages omit full member arrays, so one huge group does not defeat pagination.

Pairs return `photo_id_a`, `photo_id_b`, `metric: exact|hamming`, and `distance`. Exact cliques are represented implicitly in the snapshot. Pair pages generate relations from frozen values on demand and continue from a position within that group rather than rescanning the whole album.

## Coverage and outcomes

The snapshot records `coverage.exact` and `coverage.similar` with `requested`, `total`, `eligible`, `unchecked`, per-state `counts` and `complete`. Hash states distinguish missing, stale, invalid input/result, incomplete result, missing dependency and unconfigured profile. No hash is borrowed from another record even when the original file content is identical.

`scan_finished: true` means all eligible inputs were compared. Overall `complete` additionally requires coverage of every requested branch across the scope; it is not a guarantee of detecting every real-world duplicate transformation.

- Fully covered scopes return `status: completed`, exit 0, including empty results.
- Missing coverage returns known matches plus `status: partial`, `complete: false`, exit 1. In default mode, an unconfigured hash profile still permits exact results.
- No match with incomplete coverage means only no match among checked evidence. It does not establish that unchecked photos have no duplicates.
- Invalid arguments, profiles, snapshots or mismatched album UUIDs are errors (exit 2).
- Interruptions return 130. Scan interruption does not publish a truncated final snapshot; v1 restarts the scan rather than resuming.
- If JSON publication succeeds and HTML fails, the error includes the valid JSON `output` path. Retry `duplicates report` with that snapshot; no scan is necessary. On HTML interruption the same recovery path is included with exit 130.

To prepare missing hashes, read the saved `inputs` statuses and report the affected IDs/reasons. Only prepare the requested component/scope, using existing index setup/plan/execute contracts and authorization. Do not silently index a full album to make the report complete.

## Historical snapshot and preview report

`duplicate-snapshot-v1` stores the album UUID, frozen scope and members, algorithm/profile/threshold, limited photo metadata, content/preview identities, hash source identities and values, coverage, group structures and pair counts. It does not store embeddings, OCR text, image bytes or every matching edge. The digest checks integrity; it is not a signature or authorization token.

Group/member/pair reads validate snapshot structure and album identity without querying current photos or features. Reingestion, reindexing, folder changes or moving the album do not rewrite those historical results. Run a new scan to inspect the new state.

The HTML contains all result groups and every member once, with proportional previews, file names, recorded paths, original dimensions when known, byte sizes, capture dates, exact subgroup labels and direct-match references. Missing metadata is shown as unknown. Stored previews are validated against the frozen content/preview hash; changed, missing or damaged previews produce placeholders, never substituted new content. Reindexing alone does not prevent showing an unchanged preview.

HTML is static, escaped and self-contained, with no remote resources. It is intended for the user to view locally; do not load photo-containing reports or images into agent vision/browser tools. Large exports include every result and may be large. No delete, merge, keeper selection or folder mutation is part of this workflow.
