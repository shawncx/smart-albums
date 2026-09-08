# Typed search predicates

Read when composing structured conditions. Discover actual profiles, object labels and scene catalogs with `index profiles` / `index profiles --component <component>`. Use the shapes in [unified query JSON](search.md#unified-query-json); explicit legacy queries reuse these shapes but have their own ranking and decisions.

| Kind | Predicate and scoring |
| --- | --- |
| `semantic` | Original `query` and prepared English `visual_query`. Encode the visual phrase directly: one text call per unique condition with eligible vectors, otherwise zero. `graded` only. Scores/ranks/gaps propose candidates, never automatic matches. |
| `object_count` | Complete saved instances; `operator: eq|ge|le`, nonnegative integer `value`, supported `class_id`, optional `score_threshold` at least the profile's saved floor. Exact scoring. Missing/incomplete evidence is unknown, not zero. |
| `ocr_contains` | Literal `text`, normalized with NFKC/casefold/whitespace. 1–2 characters use scoped `INSTR`; 3+ use trigram candidates plus literal `INSTR` confirmation. `%`, `_`, quotes and FTS operators stay literal. One photo hits once; exact scoring. Unified evidence exposes hit state, not raw text. |
| `color_fraction` | Versioned `hsv-palette-v1` fraction at least `minimum` in [0,1]. `color` is black/white/gray/red/orange/yellow/green/cyan/blue/purple/magenta. Exact or graded (higher fraction). |
| `subject_position` | Saved primary-box center thirds: `horizontal` left/center/right and/or `vertical` top/middle/bottom; an unused axis is null. Both supplied axes apply inside one predicate; exact scoring. Missing/incomplete subject is unknown, not aesthetics or segmentation. |
| `scene` | Supported `scene_id` with saved catalog cosine at least `minimum` in [-1,1], exact or graded (higher cosine). Programmatic scene predicate is separate from free-semantic decisions; cosine is not probability. |
| `has_near_duplicate` | `metric: exact` uses saved SHA-256 content versions, `profile_id: null`, `max_distance: 0`; `metric: hamming` uses current same-profile dHash64 with explicit `max_distance` in 0–64. Both endpoints are in the captured scope; exact Boolean scoring. No pHash, N×N dense matrix, deletion/merge or automatic membership changes. |

Each condition has a unique `id`, its `kind`, a discovered `profile_id` (or null for exact duplicates) and `scoring: exact|graded` as allowed above. For semantic text, use [semantic preparation](semantic-text.md). Missing, stale or incomplete evidence is unknown, not false or an observed zero. Do not add hard filters or supporting thresholds that change the user's requested meaning.
