# Search within the selected album

Search is part of **management**, not a separate public Skill capability. It has two explicit modes:

```text
management search "<filename-or-recorded-path-fragment>" --mode metadata
management search "<Chinese or English visual query>" --mode semantic --profile-id <profile-id>
```

Prefix commands with `python <skill-directory>\scripts\photography.py --database <absolute-album.sqlite>`. Optional global `--model-cache-dir <cache-root>` also goes before `management`, consistently with index setup/execution. See [management](management.md) for pagination, result fields and read-only HTML/JSON exports.

## Metadata: no model needed

Metadata mode uses Unicode NFC/casefold literal substrings in photo filenames and recorded absolute/relative paths. It needs neither a model nor a default profile. `%` and `_` are literal, not SQL wildcards. It does not search album names, descriptions, other SQLite files or an internal album/library target.

Results are in stable photo-ID order. Default limit is 100, accepted range 1–1000; follow `next_cursor` with `--after`, retaining the query/profile. A blank query is an error; no matches remain an empty result, not an automatic switch to semantic mode.

## Semantic: matched image/text space

Semantic mode searches **image embeddings**, not saved descriptions. Use [index](index.md) for the optional `requirements-index.txt` runtime, authorized `index setup`, explicit profile selection, confirmed indexing and recovery. First setup does not choose a default; configure explicitly or pass a profile ID. Image/text encoders must match the same profile.

The stored image vectors represent whole SQLite previews in a paired semantic space (`image_text_semantic`, `stored_modality: image`, `input_scope: stored_thumbnail`, `granularity: whole_image`). The initial SigLIP 2 Base 224 profile produces 768 dimensions. Query vectors are transient, not saved image results or a separate persistent query index.

Search is offline and read-only. It encodes only the query, reports coverage across the entire selected album, and never generates missing photo vectors, changes a default, downloads weights, accesses originals or repairs paths. An empty eligible candidate set returns without model loading. Saved `original_status` is not a new filesystem check.

Use the user's Chinese or English query directly, without automatic translation or old description-retrieval prefixes. The current text limit is 64 tokens including EOS; overlong text is rejected, not silently truncated.

Semantic search returns ranked top-K candidates, default 10 and range 1–1000, with photo-ID tie-breaking. It rejects `--after`. Numeric candidates include score/rank/gap information and stable input/result identities, but no filenames, photo metadata, image bytes or thumbnail payloads. Scores are cosine similarities, not probabilities, guaranteed matches, exact count/negation filters or focus/blur measurements. Report missing/stale/invalid coverage rather than implying all photos were searched.

## Default display selection

The agent selects useful candidates using only the query and embedding-derived scores, ranks and gaps. **Do not send originals or thumbnails to the agent**, use image tools, or inspect image-containing reports to make that decision. Do not automatically show all candidates or force a fixed number; an empty selection is permitted. Label the result as similarity-based selection, not visual verification.

Save the raw candidates with `--output`, write the chosen IDs as a JSON array, then use:

```text
management show-results <candidates.json> --ids-file <selected.json> --html <results.html>
```

The helper validates the album, candidate membership and current selected input/result identities, keeps original ranking, and runs no new query or image encoding. It accepts `[]` for no suitable candidates. The local report contains only selected images for the user to view; give the user its link without feeding its image contents back to the agent. Keep raw candidates for optional diagnostics, not default all-photo display. A top-K subset cannot establish exhaustive matches across the album.

## Read-only snapshots and validation boundary

JSON/HTML uses `album-snapshot-v1`, identifies the album UUID/path and preserves saved input identities. HTML validates embedded previews without loading originals; it shows errors for changed/unavailable previews rather than substituting another version.

There are no selection controls, album membership writes, live search server or retained `search-add` protocol. Old databases/reports are not migrated or converted into this format; do not restore the removed description-analysis runtime.

No new-format real-model trial was performed in this implementation. Consolidated regression acceptance is pending, and earlier v7 performance does not establish new-format retrieval quality, latency, memory or offline-runtime acceptance.
