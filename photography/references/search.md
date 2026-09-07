# Search within the selected album

Search is part of **management**, not a separate public Skill capability. Existing `search` retains two explicit modes:

```text
management search "<filename-or-recorded-path-fragment>" --mode metadata
management search "<Chinese or English visual query>" --mode semantic --profile-id <profile-id>
```

Prefix commands with `python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite>`. Optional global `--model-cache-dir <cache-root>` also goes before `management`, consistently with index setup/execution. See [management](management.md) for pagination, result fields and read-only HTML/JSON exports.

**Stage 2 OR search is implemented** as the separate condition workflow below; targeted integration checks have passed. The six opt-in [feature components](index.md#stage-1-six-opt-in-components) supply persisted evidence. Metadata/semantic modes and defaults remain unchanged. Do not use OCR/objects/scene/color/composition/hash details to rerank or verify existing semantic candidates.

## Explicit virtual folder scope

`management photos` and both search modes accept repeatable `--folder-id <id>` and `--folder-match union|intersection`. Multiple distinct IDs require the operator; duplicates of one ID do not. No IDs means the entire album. Invalid folders are errors, never fallback; empty folders/intersections return empty results without loading an encoder. Semantic search still requires a valid explicit/configured profile.

Filter scope before vector inspection, ranking and top-K; union members are deduplicated. Counts, pagination, coverage and score gaps are scoped. Metadata `album_total` is the actual whole count alongside `scope_total`. Retain folder IDs/operator with query/profile while paging.

`scope` is `{"kind":"album"}` or `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}` (also `intersection`); `coverage_scope` is `entire_album` or `selected_folders`. Folder labels identify scope, never semantic evidence.

Folders are static, flat many-to-many collections in one SQLite album. Manual custom CRUD/add/remove is primary, without an index; use a one-element ID array for a single photo. Removing/deleting a folder relation never deletes photo data or other memberships. See [manual folders and optional date organization](management.md#manual-custom-virtual-folders-primary-workflow), not a new search capability or live grouping rule.

## Metadata: no model needed

Metadata mode uses Unicode NFC/casefold literal substrings in photo filenames and recorded absolute/relative paths. It needs neither a model nor a default profile. `%` and `_` are literal, not SQL wildcards. It does not search album names, descriptions, other SQLite files or an internal album/library target.

Results are in stable photo-ID order. Default limit is 100, accepted range 1–1000; follow `next_cursor` with `--after`, retaining the query/profile. A blank query is an error; no matches remain an empty result, not an automatic switch to semantic mode.

## Semantic: matched image/text space

Semantic mode searches **image embeddings**, not saved descriptions. Use [index](index.md) for the optional `requirements-index.txt` runtime, authorized `index setup`, explicit profile selection, confirmed indexing and recovery. First setup does not choose a default; configure explicitly or pass a profile ID. Image/text encoders must match the same profile.

The stored image vectors represent whole SQLite previews in a paired semantic space (`image_text_semantic`, `stored_modality: image`, `input_scope: stored_thumbnail`, `granularity: whole_image`). The initial SigLIP 2 Base 224 profile produces 768 dimensions. Query vectors are transient, not saved image results or a separate persistent query index.

Search is offline and read-only. It encodes only the query, reports coverage across the entire selected scope, and never generates missing photo vectors, changes a default, downloads weights, accesses originals or repairs paths. An empty eligible candidate set returns without model loading. Saved `original_status` is not a new filesystem check.

Use the user's Chinese or English query directly, without automatic translation or old description-retrieval prefixes. The current text limit is 64 tokens including EOS; overlong text is rejected, not silently truncated.

Semantic search returns ranked top-K candidates, default 10 and range 1–1000, with photo-ID tie-breaking. It rejects `--after`. Numeric candidates include score/rank/gap information and stable input/result identities, but no filenames, photo metadata, image bytes or thumbnail payloads. Scores are cosine similarities, not probabilities, guaranteed matches, exact count/negation filters or focus/blur measurements. Report missing/stale/invalid coverage rather than implying all photos were searched.

## Default display selection

The agent selects useful candidates using only the query and embedding-derived scores, ranks and gaps. **Do not send originals or thumbnails to the agent**, use image tools, or inspect image-containing reports to make that decision. Do not automatically show all candidates or force a fixed number; an empty selection is permitted. Label the result as similarity-based selection, not visual verification.

Save the raw candidates with `--output`, write the chosen IDs as a JSON array, then use:

```text
management show-results <candidates.json> --ids-file <selected.json> --html <results.html>
```

The helper validates the album, candidate membership and current selected input/result identities, preserves saved scores/ranks/gaps verbatim (including the gap to the next candidate outside returned top-K), and runs no new query or image encoding. It does not recompute gaps from the selection. It accepts `[]` for no suitable candidates. The local report contains only selected images for the user to view; give the user its link without feeding its image contents back to the agent. Keep raw candidates for optional diagnostics, not default all-photo display. A top-K subset cannot establish exhaustive matches across the selected scope.

For a requested one-time addition to an explicit destination, use `management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>`. It reuses `management.select_search_results` validation, performs no new query or image encoding and does not remove source memberships. Never default to all top-K. Saved EXIF date organization is separately authorized deterministic grouping, not semantic evidence; it does not permit image/HTML-pixel inspection.

## Stage 2 OR condition workflow

```text
management query --query-file <query.json> --output <private-snapshot.json>
management query-evidence <private-snapshot.json> --condition-id A [--page N]
management finalize-query <private-snapshot.json> --decisions-file <decisions.json> --output <ranked.json>
management show-query-results <ranked.json> [--limit N] [--after <opaque-cursor>] [--output <page.json>] [--html <report.html>]
management query-pairs <ranked.json> --condition-id D [--limit N] [--after <opaque-cursor>] [--output <pairs.json>]
```

### Query JSON and profile discovery

Retrieve actual profile IDs with `index profiles` for embeddings and `index profiles --component ocr|objects|color|composition|scene|perceptual_hash` for the relevant component (choose one component per command). Returned immutable profiles expose supported object labels, scene catalog IDs, saved thresholds and dependency profiles. Never invent taxonomy IDs, silently register/configure a profile or compute a missing index.

Example `multi-condition-query-v1` input; placeholders must be replaced with discovered IDs:

```json
{
  "schema": "multi-condition-query-v1",
  "operator": "or",
  "scope": {"folder_ids": [], "match": null},
  "semantic_candidates": 10,
  "review_page_size": 100,
  "random_seed": "my-repeatable-query",
  "conditions": [
    {"id": "A", "kind": "semantic", "query": "beach", "profile_id": "<embedding-id>", "scoring": "graded"},
    {"id": "B", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 2, "score_threshold": 0.3, "profile_id": "<objects-id>", "scoring": "exact"},
    {"id": "C", "kind": "ocr_contains", "text": "海", "profile_id": "<ocr-id>", "scoring": "exact"},
    {"id": "D", "kind": "color_fraction", "color": "blue", "minimum": 0.2, "profile_id": "<color-id>", "scoring": "graded"},
    {"id": "E", "kind": "subject_position", "horizontal": "right", "vertical": null, "profile_id": "<composition-id>", "scoring": "exact"},
    {"id": "F", "kind": "scene", "scene_id": "<saved-scene-id>", "minimum": 0.2, "profile_id": "<scene-id>", "scoring": "graded"},
    {"id": "G", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 8, "profile_id": "<hash-id>", "scoring": "exact"}
  ]
}
```

Only top-level `or` is supported; no top-level AND or RRF. IDs are unique, stable nonblank text. Identical normalized predicates deduplicate to the first ID and return an `aliases` mapping, so repeats never inflate match counts. Missing profiles resolve the relevant saved default once and freeze an explicit ID; missing configuration is an error, not permission to change defaults. Query values and unknown fields are strictly validated.

Scope defaults to the whole album (`folder_ids: []`, `match: null`); multiple distinct folders require `union` or `intersection`. Names/membership are captured before candidate retrieval. `semantic_candidates` defaults to 10 per semantic condition, or explicit `"all"`; `review_page_size` defaults to 100, range 1–1000. Omit/null `random_seed` to generate a saved seed; supply a bounded string for explicit reproducibility.

| Kind | Predicate and scoring |
| --- | --- |
| `semantic` | One matching text encoder call per unique condition with eligible vectors; `graded` only. Scores/ranks/gaps propose candidates, never automatic matches. |
| `object_count` | Complete saved instances; `operator: eq|ge|le`, nonnegative integer `value`, supported `class_id`, threshold at least the profile's saved floor. Exact scoring. Missing/incomplete evidence is unknown, not zero. |
| `ocr_contains` | NFKC/casefold/whitespace-normalized literal substring. 1–2 characters use scoped `INSTR`; 3+ use trigram candidates plus literal `INSTR` confirmation. `%`, `_`, quotes and FTS operators stay literal. One photo hits once, snippet at most 160 characters; exact scoring. |
| `color_fraction` | Versioned `hsv-palette-v1` fraction at least `minimum` in [0,1]. Named colors black/white/gray/red/orange/yellow/green/cyan/blue/purple/magenta. Exact or graded (higher fraction). |
| `subject_position` | Saved primary-box center thirds: horizontal left/center/right and/or vertical top/middle/bottom. Both supplied axes apply inside one predicate; exact scoring. Missing/incomplete subject is unknown, not aesthetics or segmentation. |
| `scene` | Saved supported catalog cosine at least `minimum` in [-1,1], exact or graded (higher cosine). Programmatic scene predicate is separate from free-semantic decisions; cosine is not probability. |
| `has_near_duplicate` | `metric: exact` uses saved SHA-256 content versions, `profile_id: null`, `max_distance: 0`; `hamming` uses current same-profile dHash64 with explicit 0–64 distance. Both endpoints are in the captured scope; exact Boolean scoring. No pHash, N×N dense matrix, deletion/merge or automatic membership changes. |

### Private review and complete decisions

`query` returns only `condition-query-summary-v1` and the output path. The saved `condition-search-snapshot-v1` is private: **the agent must not read the private snapshot file or feature matrix**. Only programmatic structured predicates inspect saved feature cells. For semantic relevance, do not consult OCR, scene labels, other condition hits, metadata, filenames, screenshots or pixels.

Use `query-evidence` for each semantic condition and page (0-based, default 0). Its `condition-semantic-evidence-v1` contains the query, numeric scores/ranks/gaps, IDs/input hashes and a frozen page ID; it excludes structural cells, OCR text, labels, filenames and pixels. IDs/hashes are identifiers, not relevance evidence. Review every returned page even for candidates retrieved by a different condition. A top-K candidate is not a matched result.

Write only explicit page decisions:

```json
{
  "schema": "condition-decisions-v1",
  "snapshot_id": "<returned-snapshot-id>",
  "pages": [
    {"condition_id": "A", "page_id": "<returned-page-id>", "matched_photo_ids": []}
  ]
}
```

Include every required page exactly once. Empty matched-ID arrays mean reviewed but no suitable match; missing/duplicate/unknown/wrong-condition pages and IDs outside their page are errors. Full semantic review precedes counts/ranking. `finalize-query` validates album/source identities and persists a separate finalized snapshot, returning summary only with `model_calls: 0`; saved historical `query_model_calls` remains unchanged. A query without reviewable semantic cells finalizes automatically; use its output directly.

### Ranking, coverage and display

The pool is the union of structured matches and semantic candidates within the scope; every pool photo is evaluated for every condition with eligible evidence. OR results have at least one matched unique condition. Statuses are `matched|not_matched|unknown_missing_index|unknown_stale|unknown_invalid|unsupported|not_reviewed`; unknown is not negative and adds no match. No unresolved `not_reviewed` cell may enter final ranking.

1. Higher `matched_count` first.
2. `exact` matches have `normalized_score: 1`. `graded` matches normalize direction-aware average ranks within that condition's **matched** population to [0,1]; ties use average ranks, N=1/all equal maps to 1. Nonmatches/unknowns have null normalized scores.
3. Only the identical matched-condition ID set compares its equal-weight mean (`pattern_score`), with stable ties. Different ID sets with the same match count interleave using the frozen seed while preserving each set's order. Cross-pattern score comparisons and raw-score mixing are invalid.
4. Limits/cursors apply only after this full ranking; `result_rank` is global, never reset per page.

`show-query-results` accepts finalized snapshots only, revalidates current selected sources without re-encoding, defaults to 100 rows (1–1000), and returns `condition-search-page-v1`. Follow opaque `next_cursor` with the same saved file. Page fields include conditions, aliases, scope, `scope_total`, `candidate_count`, final `total`, per-row matched IDs/count, pattern score and per-condition raw/normalized scores.

`coverage` is **input eligibility over the captured scope**, not final outcome: semantic `not_reviewed` means eligible at query time. `evaluated_coverage` is **final matched/not_matched/unknown over the candidate pool**. `retrieval` reports semantic limits, eligible counts and unretrieved count. Partial semantic coverage and missing indexes mean no-results cannot prove that no matching photos exist.

Both `.json` and `.html` destinations are validated before source/current validation, preview reads or any write; query destinations are checked before model loading. Preserve query/candidate/decision inputs, including aliases/hardlinks. These commands are read-only against schema 10: no DDL, default/configuration changes, image inference, downloads, original checks or automatic folder writes.

HTML is **user-only**, standalone, escaped and protected by CSP without scripts, forms, selection widgets or backend. It embeds only matching saved previews for this page; never open/screenshot the report or send its image payloads to the agent. Conditions/OCR snippets are escaped untrusted text. Folder scope/names, counts and scores remain historical after folder changes; changed selected source identities still fail, while unavailable preview bytes display an error rather than replacement. Give the user the link.

Only an explicit subsequent `management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>` writes memberships. It validates the finalized selection in the write transaction, returns separate `source_query` provenance and performs no query/classification/model call. The flag is mutually exclusive with `--search-snapshot`; JSON `null` errors and `[]` adds nothing.

### Standalone duplicate-pair view

Use `management query-pairs <ranked.json> --condition-id D` with the actual ID of a `has_near_duplicate` condition in the finalized snapshot. It rejects unfinished snapshots, other condition kinds, changed current sources and another snapshot/condition's cursor. Optional `--limit` defaults to 100 (1–1000); follow its own opaque `next_cursor`. Optional `--output <pairs.json>` uses the same target/input-alias protection before validation or writes. There is no HTML option or preview access.

`condition-duplicate-pairs-v1` includes snapshot identity, condition, historical scope, `input_coverage`, total pair count and `pairs`: each has `photo_id_a`, `photo_id_b`, `distance` and `metric`. It enumerates actual qualifying pairs once from saved exact SHA-256 or current same-profile dHash64 sources. No original verification or model calls occur, and no `index compare` run is created. `grouping: pairwise_only_not_transitive` means A–B/B–C does not establish A–C or a permanent group. Never automatically delete/merge photos or change folders. Empty or incomplete index coverage cannot prove all photos are unique.

## Read-only snapshots and validation boundary

JSON/HTML uses `album-snapshot-v2`, identifies the album UUID/path and preserves saved input identities and historical scope. Candidates, `show-results` and reports keep query-time folder IDs/names even after rename/deletion or membership changes; they do not re-query membership. Changed selected photo/input/result identity still produces a stale error. HTML validates embedded previews without loading originals; it shows errors for changed/unavailable previews rather than substituting another version.

These JSON/HTML exports have no selection controls or membership writes; there is no live search server or retained `search-add` protocol. Old databases/reports are not migrated or converted into this format; do not restore the removed description-analysis runtime.

Only schema 10 with 37 registered tables is supported: 32 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows (excluding internal `sqlite_sequence`), including unchanged `virtual_folders` and `virtual_folder_photos`; v1–v9 databases are rejected unchanged with no migration.

Historical folder regressions do not validate stage-one features; see [validation status](../../docs/TODO.md). No real-model performance, retrieval quality, latency, memory or disconnected-runtime claim follows from synthetic tests or authorization alone.
