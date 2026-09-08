# Semantic text preparation

Read for semantic input in unified or explicit legacy search. Literal metadata/OCR is never translated.

Semantic mode searches **image embeddings**, not saved descriptions. Use [index](index.md) for the optional `requirements-index.txt` runtime, authorized `index setup`, explicit profile selection, confirmed indexing and recovery. First setup does not choose a default; configure explicitly or pass a profile ID. Image/text encoders must match the same profile.

The stored image vectors represent whole SQLite previews in a paired semantic space (`image_text_semantic`, `stored_modality: image`, `input_scope: stored_thumbnail`, `granularity: whole_image`). The initial SigLIP 2 Base 224 profile produces 768 dimensions. Query vectors are transient, not saved image results or a separate persistent query index.

Search is offline and read-only. It encodes only the query, reports coverage across the entire selected scope, and never generates missing photo vectors, changes a default, downloads weights, accesses originals or repairs paths. An empty eligible candidate set returns without model loading. Saved `original_status` is not a new filesystem check.

The host agent prepares English visual intent using **text only**, retaining the user's original natural-language request in `query` and supplying `--visual-query`. Preserve all scene, actions, colors, negation and count constraints while removing search verbs. “搜索带有天空的图片” means `sky`, without adding blue, clear, dominant or outdoor restrictions. If faithful translation is uncertain, clarify instead of substituting guessed keywords; never derive intent from images, OCR or album metadata.

Python does not translate. Already-English visual input may omit `--visual-query`; an unprepared non-Latin request fails with `VISUAL_QUERY_REQUIRED`, never a silent fallback. All semantic entry points use one fixed recipe, `english-visual-intent-v1`: encode the NFC-normalized English visual phrase directly, with no prefix, caption template or ensemble. The frozen recipe has `prompts: [visual_query]` and `weights: [1.0]`, not selectable versions or arbitrary caller prompts/weights. There is exactly one text encoder call per unique semantic condition with eligible vectors (`model_calls: 1` for plain search); no candidates means zero calls. The prompt has a 64-token limit including EOS; `QUERY_TOO_LONG` rejects overflow without truncation or dropping constraints.

Plain snapshots freeze `query_encoding`: `strategy`, original `query`, English `visual_query`, actual `prompts` and `weights`. Preserve it through `show-results` and search-selected folder-add provenance, and show the user the actual encoded text. Query-side preparation leaves the image profile, checkpoint, 768-dimensional vectors and supported schema 11/12 unchanged; no image reindexing is required. Recipe traceability is not a measured-quality claim.
