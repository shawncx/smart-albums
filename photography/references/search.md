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

Semantic search returns ranked top-K, default 10 and range 1–1000, with photo-ID tie-breaking. It rejects `--after`. Scores are cosine similarities, not probabilities, guaranteed matches, exact count/negation filters or focus/blur measurements. Report missing/stale/invalid coverage rather than implying all photos were searched.

## Read-only snapshots and validation boundary

JSON/HTML uses `album-snapshot-v1`, identifies the album UUID/path and preserves saved input identities. HTML validates embedded previews without loading originals; it shows errors for changed/unavailable previews rather than substituting another version.

There are no selection controls, album membership writes, live search server or retained `search-add` protocol. Old databases/reports are not migrated or converted into this format; do not restore the removed description-analysis runtime.

No new-format real-model trial was performed in this implementation. Consolidated regression acceptance is pending, and earlier v7 performance does not establish new-format retrieval quality, latency, memory or offline-runtime acceptance.
