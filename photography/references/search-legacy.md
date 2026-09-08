# Legacy search compatibility

Read only for explicit `--mode semantic`, `query`/`query-evidence`/`finalize-query` commands or existing legacy search snapshots. New natural-content requests use [unified search](search.md). The two protocols below retain their existing behavior; do not apply their candidate limits, photo-ID selection or numeric-only policies to unified search.

Prefix commands with `python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite>`. Use [semantic text preparation](semantic-text.md) for every semantic query and [folder scope](search.md#explicit-virtual-folder-scope) when restricting folders.

## Legacy semantic candidates

```text
management search "搜索带有天空的图片" --mode semantic --visual-query "sky" --output <candidates.json>
management search "黑白的枯树" --mode semantic --visual-query "black and white leafless trees" --output <candidates.json>
management search "有人物的照片" --mode semantic --visual-query "people" --output <candidates.json>
```

Legacy `--mode semantic` returns ranked top-K candidates, default 10 and range 1–1000, with photo-ID tie-breaking. It rejects `--after`. Numeric candidates include score/rank/gap information and stable input/result identities, but no filenames, photo metadata, image bytes or thumbnail payloads. Scores are cosine similarities, not probabilities, guaranteed matches, exact count/negation filters or focus/blur measurements. Report missing/stale/invalid coverage rather than implying all photos were searched.

## Legacy display selection

**Only explicit `--mode semantic` and existing `album-snapshot-v2` snapshots use this protocol.** New natural-content requests use unified numbered review and its requested structural facts instead.

The agent selects useful candidates using only the query and embedding-derived scores, ranks and gaps. **Do not send originals or thumbnails to the agent**, use image tools, or inspect image-containing reports to make that decision. Do not automatically show all candidates or force a fixed number; an empty selection is permitted. Label the result as similarity-based selection, not visual verification.

Save the raw candidates with `--output`, write the chosen IDs as a JSON array, then use:

```text
management show-results <candidates.json> --ids-file <selected.json> --html <results.html>
```

The helper validates the album, candidate membership and current selected input/result identities, preserves saved scores/ranks/gaps verbatim (including the gap to the next candidate outside returned top-K), and runs no new query or image encoding. It does not recompute gaps from the selection. It accepts `[]` for no suitable candidates. The local report contains only selected images for the user to view; give the user its link without feeding its image contents back to the agent. Keep raw candidates for optional diagnostics, not default all-photo display. A top-K subset cannot establish exhaustive matches across the selected scope.

Semantic candidates and selected outputs require `vector_hash` in each result, while retaining `album-snapshot-v2`. Snapshots lacking this hash are rejected, not silently accepted or upgraded. Selection validates the saved hash against the current vector as well as result/input identity. Repairing the same result ID to a different valid vector therefore invalidates old selection and `folders add --search-snapshot`; rerun the search instead of copying a new hash onto old scores. This validation does not add append-only result revisions or repair historical scene dependencies.

For a requested one-time addition to an explicit destination, use `management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>`. It reuses `management.select_search_results` validation, performs no new query or image encoding and does not remove source memberships. Never default to all top-K. Saved EXIF date organization is separately authorized deterministic grouping, not semantic evidence; it does not permit image/HTML-pixel inspection.

## Stage 2 OR condition workflow

**Legacy only:** retained for explicitly requested `query`/`query-evidence`/`finalize-query` compatibility and reading existing snapshots. The per-condition numeric-only policy, matrix protocol, semantic default 10 and OR-only ranking below do not govern default unified search.

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
    {"id": "A", "kind": "semantic", "query": "搜索带有天空的图片", "visual_query": "sky", "profile_id": "<embedding-id>", "scoring": "graded"},
    {"id": "B", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 2, "score_threshold": 0.3, "profile_id": "<objects-id>", "scoring": "exact"},
    {"id": "C", "kind": "ocr_contains", "text": "海", "profile_id": "<ocr-id>", "scoring": "exact"},
    {"id": "D", "kind": "color_fraction", "color": "blue", "minimum": 0.2, "profile_id": "<color-id>", "scoring": "graded"},
    {"id": "E", "kind": "subject_position", "horizontal": "right", "vertical": null, "profile_id": "<composition-id>", "scoring": "exact"},
    {"id": "F", "kind": "scene", "scene_id": "<saved-scene-id>", "minimum": 0.2, "profile_id": "<scene-id>", "scoring": "graded"},
    {"id": "G", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 8, "profile_id": "<hash-id>", "scoring": "exact"}
  ]
}
```

Only top-level `or` is supported; no top-level AND or RRF. IDs are unique, stable nonblank text. Predicate identity excludes `id` and `scoring`, but retains the normalized kind, profile and filter parameters. Identical normalized predicates with the same resolved scoring deduplicate to the first ID and return an `aliases` mapping, including default versus explicitly identical scoring. Conflicting `exact`/`graded` scoring for the same predicate is rejected explicitly, not counted twice or silently merged. Different thresholds remain distinct predicates. Missing profiles resolve the relevant saved default once and freeze an explicit ID; missing configuration is an error, not permission to change defaults. Query values and unknown fields are strictly validated.

Semantic conditions keep original `query` plus optional `visual_query` with the same text-only preparation role as `--visual-query`. Direct English visual input may omit it; non-Latin requests need it for query execution. Semantic duplicate identity uses the prepared visual phrase/profile, not the original request wording. NFC normalization applies both with explicit `visual_query` and to direct English input. Different originals with the same prepared phrase deduplicate once; equivalent English input with the same phrase does too. Keep one logical semantic condition per intent: paraphrases must not increase `matched_count`.

Scope defaults to the whole album (`folder_ids: []`, `match: null`); multiple distinct folders require `union` or `intersection`. Names/membership are captured before candidate retrieval. `semantic_candidates` defaults to 10 per semantic condition, or explicit `"all"`; `review_page_size` defaults to 100, range 1–1000. Omit/null `random_seed` to generate a saved seed; supply a bounded string for explicit reproducibility.

See [typed search predicates](search-predicates.md). Legacy OCR result snippets are limited to 160 characters and remain excluded from semantic decisions.

### Private review and complete decisions

`query` returns only `condition-query-summary-v1` and the output path. The saved `condition-search-snapshot-v1` is private: **the agent must not read the private snapshot file or feature matrix**. Only programmatic structured predicates inspect saved feature cells. For semantic relevance, do not consult OCR, scene labels, other condition hits, metadata, filenames, screenshots or pixels.

Use `query-evidence` for each semantic condition and page (0-based, default 0). Its `condition-semantic-evidence-v1` contains the query, frozen `query_encoding` text recipe, numeric scores/ranks/gaps, IDs/input hashes and a frozen page ID; it excludes structural cells, OCR text, labels, filenames and pixels. IDs/hashes are identifiers, not relevance evidence. Review every returned page even for candidates retrieved by a different condition. A top-K candidate is not a matched result.

New OR snapshots freeze a `query_encodings` map keyed by canonical semantic condition ID. Historical raw-query snapshots may omit this map; do not manufacture preparation metadata for them. Recipe identity is part of `page_id` and final validation, so edited recipes cannot reuse old decisions. Safe summaries/evidence expose text preparation without permitting access to the private matrix. Reports show actual encoded text. Finalization and display perform no additional encoding.

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

`show-query-results` accepts finalized snapshots only, revalidates current selected sources without re-encoding, defaults to 100 rows (1–1000), and returns `condition-search-page-v1`. Follow opaque `next_cursor` with the same saved file. Page fields include conditions, aliases, `query_encodings`, scope, `scope_total`, `candidate_count`, final `total`, per-row matched IDs/count, pattern score and per-condition raw/normalized scores.

`coverage` is **input eligibility over the captured scope**, not final outcome: semantic `not_reviewed` means eligible at query time. `evaluated_coverage` is **final matched/not_matched/unknown over the candidate pool**. `retrieval` reports semantic limits, eligible counts and unretrieved count. Partial semantic coverage and missing indexes mean no-results cannot prove that no matching photos exist.

Both `.json` and `.html` destinations are validated before source/current validation, preview reads or any write; query destinations are checked before model loading. Preserve query/candidate/decision inputs, including aliases/hardlinks. These commands are read-only against schema 11: no DDL, default/configuration changes, image inference, downloads, original checks or automatic folder writes.

HTML is **user-only**, standalone, escaped and protected by CSP without scripts, forms, selection widgets or backend. It embeds only matching saved previews for this page; never open/screenshot the report or send its image payloads to the agent. Conditions/OCR snippets are escaped untrusted text. Folder scope/names, counts and scores remain historical after folder changes; changed selected source identities still fail, while unavailable preview bytes display an error rather than replacement. Give the user the link.

Only an explicit subsequent `management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>` writes memberships. It validates the finalized selection in the write transaction, returns separate `source_query` provenance and performs no query/classification/model call. The flag is mutually exclusive with `--search-snapshot`; JSON `null` errors and `[]` adds nothing.

### Standalone duplicate-pair view

Use `management query-pairs <ranked.json> --condition-id D` with the actual ID of a `has_near_duplicate` condition in the finalized snapshot. It rejects unfinished snapshots, other condition kinds, changed current sources and another snapshot/condition's cursor. Optional `--limit` defaults to 100 (1–1000); follow its own opaque `next_cursor`. Optional `--output <pairs.json>` uses the same target/input-alias protection before validation or writes. There is no HTML option or preview access.

`condition-duplicate-pairs-v1` includes snapshot identity, condition, historical scope, `input_coverage`, total pair count and `pairs`: each has `photo_id_a`, `photo_id_b`, `distance` and `metric`. It enumerates actual qualifying pairs once from saved exact SHA-256 or current same-profile dHash64 sources. No original verification or model calls occur, and no `index compare` run is created. `grouping: pairwise_only_not_transitive` means A–B/B–C does not establish A–C or a permanent group. Never automatically delete/merge photos or change folders. Empty or incomplete index coverage cannot prove all photos are unique.


## Add selected results to a folder

For legacy semantic candidates use:

```text
management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>
```

`--search-snapshot` reuses `management.select_search_results` to validate album, candidate identity, selected subset and current selected photo/input/result identities in the membership transaction. It does not repeat the query, encode images or inspect their content. Selection still uses embedding-derived numbers only; do not automatically add all top-K, remove source memberships or treat folder labels as semantic evidence. Source scope/names remain historical even if source folders later change or disappear; selected photo identity changes are still stale errors.

For explicitly selected finalized condition results, use:

```text
management folders add <folder-id> --ids-file <selected.json> --query-snapshot <ranked.json>
```

All three snapshot flags (`--review-snapshot`, `--search-snapshot`, `--query-snapshot`) are mutually exclusive. A supplied JSON `null` is invalid, never plain-manual fallback. For legacy OR, `condition_search.select_results` validates finalized stage, album, selected subset and current selected sources inside the explicit membership transaction before applying old membership logic. `[]` makes no changes. Response `source_query` is separate from existing `source_search`; neither source invokes a query, classification or model. Querying alone never adds folders or members.
