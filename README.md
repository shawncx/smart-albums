# Smart Albums

A photography Skill for importing local photos, organizing albums, saving visual observations, and searching those observations in Chinese or English with a local multilingual embedding model.

## Capabilities

- Incremental photo ingestion with metadata and JPEG previews.
- Multiple albums in one SQLite database; a photo can belong to several albums.
- Explicitly requested visual analysis, with saved results and version-aware caching.
- Local description embeddings with incremental generation and coverage reporting.
- Semantic search, ranked HTML previews, and adding selected results to an album.

Original photos remain in their existing locations. The database stores their paths, metadata, previews, analysis and vectors. Browsing saved content and local search work without reading the originals. New visual analysis requires original-version checks and a configured provider.

Importing and searching do not automatically run visual analysis. Search only covers photos with valid descriptions and vectors, and reports missing coverage. Clustering, duplicate removal, automatic curation and standalone speech recognition are not implemented.

## Get started

Python 3.12 or a compatible newer version is required. From the repository root, create a virtual environment and install the base dependencies:

```text
python -m venv .venv
python -m pip install -r photography/requirements.txt
```

Run the installation command with the virtual environment's Python after activating it, or use its full executable path. On Windows this is `.venv\Scripts\python.exe`; on Linux/macOS it is `.venv/bin/python`.

The Skill source is in [photography/SKILL.md](photography/SKILL.md), with the public name **Smart Albums** and identifier `smart-albums`. Copy the entire `photography` folder as `smart-albums` into your host's supported skill directory. The host must support local Python execution and access to your photo and state directories. Host-specific installation and authentication behavior can differ.

## Import and organize

The examples use placeholders: substitute an absolute photo directory and a separate, non-overlapping state directory. Keep that state directory consistent across commands.

```text
python photography/scripts/photography.py --state-dir <state-directory> ingest <absolute-photo-directory> --album-name Travel
python photography/scripts/photography.py --state-dir <state-directory> albums
python photography/scripts/photography.py --state-dir <state-directory> analysis-status --album-id <album-id>
```

Use returned IDs rather than inventing them. Repeating ingestion skips unchanged files. Album membership does not copy photos or regenerate analysis.

## Visual analysis

Visual analysis is a separate, explicitly invoked operation. The default provider uses an OpenAI API key configured in the process environment. An optional Codex CLI adapter uses a compatible, already authenticated local Codex installation; this is not a general mechanism for reusing every host's login.

```text
python photography/scripts/photography.py --state-dir <state-directory> analyze --album-id <album-id> --limit 3
python photography/scripts/photography.py --state-dir <state-directory> analysis-execute <plan-id> --confirm <reviewed-digest>
```

The first command proposes work and does not call a model. Review the channel/model, cache counts, immediate or asynchronous mode, photos per request, concurrency and estimates before confirming the exact proposal. OpenAI Batch and optional multi-image immediate requests are supported for the documented model catalog; existing Codex login uses single-photo immediate processing. Defaults remain one photo per request. Never commit API keys or runtime credentials. See the [planning and execution workflow](photography/references/analysis-workflow.md) and [observation/cache reference](photography/references/analyze.md). Live service behavior and multi-image quality require a separately authorized trial; regression tests are offline.

## Local multilingual search

Install the optional dependencies and explicitly download the pinned local model:

```text
python -m pip install -r photography/requirements-embedding.txt
python photography/scripts/photography.py --state-dir <state-directory> embedding-setup
python photography/scripts/photography.py --state-dir <state-directory> embed --album-id <album-id>
python photography/scripts/photography.py --state-dir <state-directory> search "black and white trees" --album-id <album-id> --html <output-directory>/search.html
```

The multilingual model reuses one set of saved descriptions for Chinese and English queries. Model installation needs network access; normal vector generation and search load only local files and do not invoke a cloud vision model. No model weights are distributed in this repository.

Search scores indicate relative semantic similarity, not probabilities or guaranteed matches. Descriptions may omit details. Inspect the returned images before saving a selection. The HTML report is a snapshot with selection export, not a live search server.

```text
python photography/scripts/photography.py --state-dir <state-directory> search-add <output-directory>/search.json --album-name Portfolio --ids-file <output-directory>/selected-photo-ids.json
```

## References and tests

- [Ingestion](photography/references/ingest.md)
- [Albums](photography/references/albums.md)
- [Visual analysis](photography/references/analyze.md)
- [Local model setup, indexing and search](photography/references/search.md)

```text
python -m unittest discover -s tests -v
python tests/demo_ingest.py
```

Regression tests use temporary synthetic images and explicit test doubles; they do not call paid visual models or require model downloads. The demo creates its own synthetic gallery under `artifacts`.

Local databases, photo previews, downloaded models, credentials, generated reports and development-session notes are excluded from version control. Keep runtime data outside the source tree or in an ignored state directory. Back up your actual state directory separately; pushing this repository is not a backup of your photo library.
