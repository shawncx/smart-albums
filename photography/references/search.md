# Local description embeddings and semantic search

This version uses one multilingual model for Chinese and English search over the same saved descriptions. No bilingual description duplication, image upload, API key or visual model call is required. Model installation needs a network connection; normal operation is local.

## Prepare the runtime and model

Use Python 3.12+ in a project virtual environment. The base Skill remains usable without these optional dependencies. Substitute actual installation/state paths below; keep the same state directory used for ingestion.

```text
python -m pip install -r <skill-directory>/requirements-embedding.txt
python <skill-directory>/scripts/photography.py --state-dir <state-directory> embedding-setup
```

The pinned `intfloat/multilingual-e5-small` model is distributed as `Xenova/multilingual-e5-small`, revision `761b726dd34fb83930e26aab4e9ac3899aa1fa78`. Its int8 ONNX weights and tokenizer total 135,390,915 bytes, stored under `<state-directory>/models/multilingual-e5-small-onnx-int8`. Setup verifies SHA-256 hashes, publishes complete files atomically, preserves prior files on errors and resumes by reusing valid files. The model has an MIT license; see the [publisher model card](https://huggingface.co/intfloat/multilingual-e5-small) and [pinned ONNX distribution](https://huggingface.co/Xenova/multilingual-e5-small/tree/761b726dd34fb83930e26aab4e9ac3899aa1fa78).

Runtime pins: ONNX Runtime 1.22.1, tokenizers 0.21.2, NumPy 2.2.6. CPU execution uses up to four intra-operation threads; no GPU is required. Ordinary commands never download models or instantiate cloud providers. `--model-dir` may select another local copy of the same pinned model, not arbitrary model weights. A missing/corrupt model returns a setup error; status, dry-run and an empty search index do not need a loaded model.

## Generate vectors and inspect coverage

```text
python <skill-directory>/scripts/photography.py --state-dir <state-directory> embed --album-id <album-id> --dry-run
python <skill-directory>/scripts/photography.py --state-dir <state-directory> embed --album-id <album-id>
python <skill-directory>/scripts/photography.py --state-dir <state-directory> embed <photo-id> <photo-id>
python <skill-directory>/scripts/photography.py --state-dir <state-directory> embedding-status --album-id <album-id>
```

`embed` requires IDs, a UTF-8 JSON array via `--ids-file`, or an explicit `--album-id`/`--library-id`. A scope selects all its photos by default; `--limit N` limits that selection before processing. `--force` rebuilds valid vectors too. Repeated IDs are deduplicated. `--dry-run` reads candidate versions and cache status without loading the model. Repeating a successful run reuses cached vectors across all albums. Failed or interrupted runs retain completed rows, so running again resumes remaining work.

Counts: `generated`, `cached`, `skipped`, `failed`, `pending` (dry-run), `unprocessed` (stopped early). Results identify failures by photo ID. `local_model_calls` counts local inference attempts; `visual_model_calls` remains zero. `token_count` is the local tokenizer's input length, including the retrieval prefix and special tokens; it is not billed API usage. `truncated` indicates document-tail truncation.

Status values:

| Status | Meaning |
| --- | --- |
| ready | Current description and compatible, valid vector |
| missing | Current description exists but no vector for this encoder |
| stale | Saved analysis or text version changed; rebuild the vector |
| needs_analysis | No observation matching the indexed photo/preview; no automatic analysis |
| invalid | Corrupt vector or unusable saved text; inspect the reason and retry/repair |

`embedding-status` defaults to the whole database, with optional album/library scope. `--status`, `--limit` (1–1000) and `--after` page the items; counts cover the complete selected scope. It verifies vector bytes but does not load image BLOBs or access original files. Results refer to indexed versions, even when originals are offline. Rescan to discover changed originals.

## Search and view exact results

```text
python <skill-directory>/scripts/photography.py --state-dir <state-directory> search "黑白的枯树" --album-id <album-id> --limit 10 --html <output-directory>/trees.html
python <skill-directory>/scripts/photography.py --state-dir <state-directory> search "black and white leafless trees" --output <output-directory>/trees-en.json
```

Omitting scope searches all indexed descriptions in the current encoder space. Search does not prepare missing document vectors. SQL selects the candidate space and scope; current analysis/text checks exclude stale rows, and Python computes exact cosine similarity. Results sort by decreasing score, then stable photo ID. No vector extension, separate service or approximate index is needed in this version.

Each result includes photo ID, score, description, analysis ID, indexed content version and thumbnail ID. Coverage always reports the full selected photo count and each embedding state. Cosine scores are relative ranking signals, not probabilities. E5 can return a fairly high score for unrelated text; there is no calibrated automatic match cutoff. Ask for fewer results or inspect the returned photos rather than treating every neighbor as relevant.

`--html` exports ranked cards and, unless `--output` names a JSON path, a companion JSON with the same stem. Exports must be outside source and state directories. The standalone page displays database thumbnails and works without original files. Checkboxes export a JSON array of selected photo IDs. It is a snapshot and does not run new searches or write the database from JavaScript.

## Save a selection to an album

```text
python <skill-directory>/scripts/photography.py --state-dir <state-directory> search-add <output-directory>/trees.json --album-name "冬季作品集" --ids-file <output-directory>/selected-photo-ids.json
python <skill-directory>/scripts/photography.py --state-dir <state-directory> search-add <output-directory>/trees.json --album-name "冬季作品集" -- <photo-id>
```

Only explicit IDs contained in that saved result are accepted. The album is created or reused and membership is added atomically. Repeat selection adds no duplicates. The query is not rerun and no analysis/vector is copied. Existing `album-add` remains available for other explicit photo selections.

## Encoding and storage contract

Recipe `photo-text-v1` normalizes Unicode to NFC and collapses field whitespace. It joins nonempty fields in this order: description, subjects, scene, composition, color, lighting, mood, tags, with Chinese labels. Technical boilerplate, file paths, usage and model instructions are excluded. The model sees `passage: ` before document text and `query: ` before either language's query, following the publisher's retrieval convention. This is a fixed encoder input format, not a generative prompt.

Documents longer than 512 tokens retain the first 511 tokens and final separator; the full pre-truncation text hash and token count are saved. Queries exceeding 512 tokens including prefix/special tokens are rejected rather than silently shortened. Average pooling over attended tokens is normalized to unit length, stored as 384 little-endian float32 values (1,536 bytes per vector). Int8 describes model weights; the stored output vectors remain float32.

Schema v4 adds `embedding_encoders` and `photo_embeddings`. Encoder identity fingerprints the pinned weights, tokenizer, runtime versions, pooling, prefixes, dimensions and recipe. Each `(photo_id, encoder_id)` stores one current vector plus its analysis ID, content version, text hash, vector checksum, truncation metadata and creation time. New vectors are saved atomically after the analysis/text is rechecked. Existing v1–v3 databases get a consistent backup before migration. Original photo records, analysis, albums and thumbnails are preserved.

Different model spaces are isolated even if dimensions match. The current CLI encodes only the pinned multilingual model; `--encoder-id` guards against selecting an incompatible space, while status can inspect a known saved encoder. A future model adapter can use the same tables. It does not imply multiple selectable inference models are already implemented.

Cold CLI invocations load the model again. The Python API can reuse one `LocalEncoder` instance for many queries; loading time and query encoding/ranking time are reported separately. Larger libraries, detailed negations, precise counts, visual details missing from descriptions, and retrieval quality beyond the tested samples need separate evaluation. Clustering and standalone speech recognition remain outside this release.
