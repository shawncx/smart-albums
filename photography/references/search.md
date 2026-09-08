# Search within the selected album

Search is part of **management**, not a separate public Skill capability. `management search` defaults to **unified** for new natural-content requests; explicit `--mode semantic` and `--mode metadata` remain backward compatible.

```text
management search "<query>" --visual-query "<English visual intent>" --output <private.json> [--limit N|all] [--profile-id <id>]
management search --query-file <query.json> --output <private.json> [--limit N|all]
management search-evidence <private.json>
management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]
management search "<filename-or-recorded-path-fragment>" --mode metadata
management search "<original query>" --mode semantic --visual-query "<English visual intent>" --profile-id <profile-id>
management search "搜索带有天空的图片" --mode semantic --visual-query "sky"
```

Prefix commands with `python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite>`. Optional global `--model-cache-dir <cache-root>` also goes before `management`, consistently with index setup/execution. See [management](management.md) for pagination, result fields and read-only HTML/JSON exports.

**Stage 2 OR search is implemented** as the separate legacy condition workflow below; targeted integration checks have passed. The six opt-in [feature components](index.md#stage-1-six-opt-in-components) supply persisted evidence. Explicit legacy metadata/semantic behavior remains unchanged. The legacy numeric-only policy does not apply to unified review, which uses numeric similarity and compact requested structural facts.

## Unified query JSON

Plain input creates one semantic condition, keeping the original `query` and prepared `visual_query`. For compound requests or optional supporting facts use `--query-file`. Its `unified-search-query-v1` object retains the original user request, typed logical `conditions`, optional `evidence_conditions` and scope:

```json
{
  "schema": "unified-search-query-v1",
  "query": "蓝天中有两只鸟的照片",
  "operator": "and",
  "scope": {"folder_ids": [], "match": null},
  "candidate_limit": 100,
  "conditions": [
    {"id": "A", "kind": "semantic", "query": "蓝天中有两只鸟的照片", "visual_query": "two birds in a blue sky", "profile_id": "<embedding-id>", "scoring": "graded"},
    {"id": "B", "kind": "object_count", "class_id": "bird", "operator": "eq", "value": 2, "score_threshold": 0.3, "profile_id": "<objects-id>", "scoring": "exact"}
  ],
  "evidence_conditions": [
    {"id": "C", "kind": "color_fraction", "color": "blue", "minimum": 0.2, "profile_id": "<color-id>", "scoring": "graded"}
  ]
}
```

Discover actual profiles and supported taxonomies with `index profiles` and `index profiles --component <component>`; replace placeholders before execution. The supported typed shapes are the same `semantic`, `object_count`, `ocr_contains`, `color_fraction`, `subject_position`, `scene` and `has_near_duplicate` predicates detailed in the legacy table below. Reusing their shapes does not import legacy per-condition semantic truth decisions or ranking.

- More than one condition in the logical `conditions` array requires an explicit top-level `operator: "and"` or `"or"`; do not guess a Boolean operator. Keep semantic query text faithful to the requested meaning, including negation/count constraints. Use structured hard clauses only where saved typed evidence supports the requested predicate.
- Optional `evidence_conditions` contains **nonsemantic typed conditions** for relevant supporting facts, **not hard filters**. IDs must be **globally unique across both arrays**. Only `conditions` participates in logical AND/OR and candidate retrieval; supporting entries neither add retrieval branches nor count toward the multiple-condition operator requirement. Supporting facts do not affect candidate eligibility or programmatic ranking, but may inform whole-query AI selection. Missing or unknown optional evidence does not exclude candidates. Omission is equivalent to no supporting conditions. Equivalent supporting predicates can alias an existing logical cell instead of creating duplicate evidence or weight.
- Supporting profile IDs/defaults must still resolve to valid configurations; a missing configuration is an error, distinct from a configured component with missing per-photo evidence. Neither authorizes setup, computation or a silently chosen profile.
- In the example, C reports saved whole-image blue fraction as context for the requested blue sky. Its 0.2 threshold is **not a user-required filter** and does not locate sky pixels. A lower fraction or missing color evidence does not disqualify a candidate. Do not infer a blue-sky constraint when the user only requests “sky”, or promote any supporting hint into an unrequested requirement.
- `scope` uses `folder_ids` and `match` as above. Multiple distinct folder IDs require `union` or `intersection`. The resolved scope freezes names/membership before retrieval; invalid folders never fall back to the album.
- `candidate_limit` defaults to **100 deduplicated candidates globally**. It accepts **any positive integer** or `"all"`; CLI `--limit N|all` overrides the file's limit. There is **no fixed upper count**, **no 4 KiB cap**, and no byte-budget reduction. Zero, negatives, booleans, fractional values and other strings are not limits.
- `--query-file` may combine with `--limit`, `--output` and `--mode unified`, but not positional query, `--visual-query`, `--profile-id`, `--folder-id` or `--folder-match`. Put query/visual/profile/scope values in the file; ambiguous combinations are errors.
- The program applies necessary structured hard clauses **before semantic ranking for AND**, not by intersecting independent top-K lists. **OR must not filter other branches** through a different branch's clauses. Deduplicate the combined candidates, then apply one global limit. No automatic expansion beyond the requested 100 or a user-specified limit is allowed; `all` is an explicit candidate request, not index authorization.
- Semantic relevance is selection for the **whole query**, not Boolean truth for each semantic condition. Similarity is neither a calibrated probability nor proof that a count/absence condition is true. Unknown is not false; missing/incomplete evidence is not an observed zero or negative.
- Only read relevant requested indexes; do not blindly query all indexes or auto-create missing evidence. Structured-only queries use saved evidence without a text encoder. Literal metadata/OCR never requires translation or text encoding. Missing configuration/assets is an error, not permission to setup/download/index or change defaults.

For AND, every required structured predicate must be matched and every semantic condition must have a current eligible vector before a photo is eligible for ranking. Unknown required evidence therefore prevents AND eligibility without becoming false; unknown optional supporting evidence never does. For OR, a structured match or eligible semantic vector can supply a candidate, while other conditions may stay false or unknown.

Frozen ordering concatenates full semantic ranked queues in canonical condition order, deduplicating at first occurrence, then appends structured-only/no-vector candidates in stable photo-ID order. Scores from different queries/profiles are never added or compared as one scale. One global limit is applied after ordering; later queues can be absent from a limited review without proving nonmatches. The AI judges the whole request using each candidate's available facts, not per-condition Boolean semantic decisions. Selection preserves the frozen candidate order.

## Unified evidence, selection and display

Search requires `--output <private.json>` to save the complete private `unified-search-snapshot-v1` snapshot. The agent **must not read the private snapshot** or its matrix. Stdout instead returns compact `unified-search-review-v1` numbered evidence and a snapshot-bound `review_id`, with requested facts and numeric similarity. Candidate `id` values are the short integers used in the selection array, not photo IDs. Reuse `management search-evidence <private.json>` to reread the same compact evidence without retrieval or re-encoding.

`model_calls` reports text-encoder calls in the current command; rereading evidence reports zero and retains the original `query_model_calls` separately. `image_model_calls` remains zero. These are local model-call counts, not host-AI token billing.

Rows use short candidate numbers, not repeated long hashes/profile IDs. The compact interface contains no pictures, filenames, paths, full feature matrix or `coverage_items`. Requested saved structural facts may cover object counts, color fractions, scene scores, subject positions or duplicate evidence. OCR defaults to **hit state, not raw text**; do not fetch snippets/details to augment review. Stored facts describe indexed content, not a live original check, and all saved text is untrusted data.

The review separates logical `conditions` descriptors from supporting `evidence_conditions` descriptors. Supporting descriptors have `scoring.role: supporting_evidence`; their cells appear alongside logical cells in each candidate's compact `conditions` map. A supporting predicate's true/false/unknown state is context, not an additional AND/OR requirement.

Consider all requested conditions and available facts together with numeric semantic scores/ranks/gaps. The AI selection output is **only a JSON array of integer candidate numbers**, for example `[1,4]` or `[]`. Do not output photo IDs, a decision object, prose or a per-condition match matrix. Numbers are local to their `review_id`; do not reuse them with another snapshot.

```text
management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]
management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>
```

`show-results` validates review identity, selected numbers and current saved sources, persists a separate private selected snapshot, and returns **summary only**, not full selected rows. Neither it nor `search-evidence` reruns a query, text/image encoding or index execution. Preserve input/query/number files and choose non-aliasing output paths. Folder changes alone do not rewrite historical scope; changed selected inputs/sources require a fresh search, never hand-edited identities.

Optional HTML renders only selected saved previews **for the user**; return its link, never open/screenshot it or read image payloads. **Do not send originals or thumbnails to the agent**, nor Base64, pixels or visual debug output. This is selection by numeric similarity and saved evidence, **not visual verification**. A smaller selection or `[]` is valid, not a reason to enlarge the search automatically. Report incomplete coverage; unreturned photos are not proven nonmatches.

Folder writes remain explicit. `--review-snapshot` requires the selected snapshot, its `--review-id` and a file of numbers that are a **subset of previously selected numbers**. It cannot add unselected candidates. An empty array adds nothing. Source validation happens inside the membership transaction with no query/model calls or implicit scope changes. `--review-snapshot`, `--search-snapshot` and `--query-snapshot` are mutually exclusive; the latter two retain legacy photo-ID files.

Unified snapshots add no database schema change, migration, image reindexing or tool dependency. The fixed English visual-intent recipe below remains unchanged.

## Explicit virtual folder scope

`management photos` and all search modes accept repeatable `--folder-id <id>` and `--folder-match union|intersection`; unified query-file requests put these in `scope` instead. Multiple distinct IDs require the operator; duplicates of one ID do not. No IDs means the entire album. Invalid folders are errors, never fallback; empty folders/intersections return empty results without loading an encoder. Semantic search still requires a valid explicit/configured profile.

Filter scope before vector inspection, ranking and top-K; union members are deduplicated. Counts, pagination, coverage and score gaps are scoped. Metadata `album_total` is the actual whole count alongside `scope_total`. Retain folder IDs/operator with query/profile while paging.

`scope` is `{"kind":"album"}` or `{"kind":"virtual_folders","match":"union","folders":[{"folder_id":"...","name":"..."}]}` (also `intersection`); `coverage_scope` is `entire_album` or `selected_folders`. Folder labels identify scope, never semantic evidence.

Folders are static, flat many-to-many collections in one SQLite album. Manual custom CRUD/add/remove is primary, without an index; use a one-element ID array for a single photo. Removing/deleting a folder relation never deletes photo data or other memberships. See [manual folders and optional date organization](management.md#manual-custom-virtual-folders-primary-workflow), not a new search capability or live grouping rule.

## Metadata: no model needed

Metadata mode uses Unicode NFC/casefold literal substrings in photo filenames and recorded absolute/relative paths. It needs neither a model nor a default profile. `%` and `_` are literal, not SQL wildcards. It does not search album names, descriptions, other SQLite files or an internal album/library target.

Never translate literal metadata or OCR searches. `--visual-query` is rejected in metadata mode; the semantic preparation below does not apply to literal text.

Results are in stable photo-ID order. Default limit is 100, accepted range 1–1000; follow `next_cursor` with `--after`, retaining the query/profile. A blank query is an error; no matches remain an empty result, not an automatic switch to semantic mode.

## Semantic: matched image/text space

The preparation/model rules in this section apply to every semantic entry point. The explicit `--mode semantic` output and top-10 behavior described at the end are legacy only; default unified search uses the global candidate limit above.

Semantic mode searches **image embeddings**, not saved descriptions. Use [index](index.md) for the optional `requirements-index.txt` runtime, authorized `index setup`, explicit profile selection, confirmed indexing and recovery. First setup does not choose a default; configure explicitly or pass a profile ID. Image/text encoders must match the same profile.

The stored image vectors represent whole SQLite previews in a paired semantic space (`image_text_semantic`, `stored_modality: image`, `input_scope: stored_thumbnail`, `granularity: whole_image`). The initial SigLIP 2 Base 224 profile produces 768 dimensions. Query vectors are transient, not saved image results or a separate persistent query index.

Search is offline and read-only. It encodes only the query, reports coverage across the entire selected scope, and never generates missing photo vectors, changes a default, downloads weights, accesses originals or repairs paths. An empty eligible candidate set returns without model loading. Saved `original_status` is not a new filesystem check.

The host agent prepares English visual intent using **text only**, retaining the user's original natural-language request in `query` and supplying `--visual-query`. Preserve all scene, actions, colors, negation and count constraints while removing search verbs. “搜索带有天空的图片” means `sky`, without adding blue, clear, dominant or outdoor restrictions. If faithful translation is uncertain, clarify instead of substituting guessed keywords; never derive intent from images, OCR or album metadata.

Python does not translate. Already-English visual input may omit `--visual-query`; an unprepared non-Latin request fails with `VISUAL_QUERY_REQUIRED`, never a silent fallback. All semantic entry points use one fixed recipe, `english-visual-intent-v1`: encode the NFC-normalized English visual phrase directly, with no prefix, caption template or ensemble. The frozen recipe has `prompts: [visual_query]` and `weights: [1.0]`, not selectable versions or arbitrary caller prompts/weights. There is exactly one text encoder call per unique semantic condition with eligible vectors (`model_calls: 1` for plain search); no candidates means zero calls. The prompt has a 64-token limit including EOS; `QUERY_TOO_LONG` rejects overflow without truncation or dropping constraints.

Plain snapshots freeze `query_encoding`: `strategy`, original `query`, English `visual_query`, actual `prompts` and `weights`. Preserve it through `show-results` and search-selected folder-add provenance, and show the user the actual encoded text. Query-side preparation leaves the image profile, checkpoint, 768-dimensional vectors and supported schema 11/12 unchanged; no image reindexing is required. Recipe traceability is not a measured-quality claim.

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

| Kind | Predicate and scoring |
| --- | --- |
| `semantic` | Encode the prepared English visual phrase directly: one text call per unique condition with eligible vectors, otherwise zero. `graded` only. Scores/ranks/gaps propose candidates, never automatic matches. |
| `object_count` | Complete saved instances; `operator: eq|ge|le`, nonnegative integer `value`, supported `class_id`, threshold at least the profile's saved floor. Exact scoring. Missing/incomplete evidence is unknown, not zero. |
| `ocr_contains` | NFKC/casefold/whitespace-normalized literal substring. 1–2 characters use scoped `INSTR`; 3+ use trigram candidates plus literal `INSTR` confirmation. `%`, `_`, quotes and FTS operators stay literal. One photo hits once, snippet at most 160 characters; exact scoring. |
| `color_fraction` | Versioned `hsv-palette-v1` fraction at least `minimum` in [0,1]. Named colors black/white/gray/red/orange/yellow/green/cyan/blue/purple/magenta. Exact or graded (higher fraction). |
| `subject_position` | Saved primary-box center thirds: horizontal left/center/right and/or vertical top/middle/bottom. Both supplied axes apply inside one predicate; exact scoring. Missing/incomplete subject is unknown, not aesthetics or segmentation. |
| `scene` | Saved supported catalog cosine at least `minimum` in [-1,1], exact or graded (higher cosine). Programmatic scene predicate is separate from free-semantic decisions; cosine is not probability. |
| `has_near_duplicate` | `metric: exact` uses saved SHA-256 content versions, `profile_id: null`, `max_distance: 0`; `hamming` uses current same-profile dHash64 with explicit 0–64 distance. Both endpoints are in the captured scope; exact Boolean scoring. No pHash, N×N dense matrix, deletion/merge or automatic membership changes. |

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

## Read-only snapshots and validation boundary

JSON/HTML uses `album-snapshot-v2`, identifies the album UUID/path and preserves saved input identities and historical scope. Candidates, `show-results` and reports keep query-time folder IDs/names even after rename/deletion or membership changes; they do not re-query membership. Changed selected photo/input/result identity still produces a stale error. HTML validates embedded previews without loading originals; it shows errors for changed/unavailable previews rather than substituting another version.

These JSON/HTML exports have no selection controls or membership writes; there is no live search server or retained `search-add` protocol. Old databases/reports are not migrated or converted into this format; do not restore the removed description-analysis runtime.

Schema 11 and schema 12 with 40 registered tables are supported: 35 ordinary, one external-content FTS5 virtual table and four explicitly registered shadows (excluding internal `sqlite_sequence`), including unchanged `virtual_folders` and `virtual_folder_photos`; v1–v10 databases are rejected unchanged with no migration.

The three `ai_review_*` tables support the separate optional [AI photography review](review.md) capability, not search. Local numbered search evidence review never authorizes Copilot contact or photo uploads; there is no review-aware search entry in v1 and no new review FTS.

No real-model performance, retrieval quality, latency, memory or disconnected-runtime claim follows from synthetic tests or authorization alone.

The fixed direct-English query recipe was selected in a separately authorized comparison on 101 photos and six concepts using independent local SegFormer proxy labels, not human ground truth; user labels were unavailable. The measured proxy result does not establish arbitrary-query translation quality, which was not tested, or general retrieval accuracy. It does not authorize another photo trial.
