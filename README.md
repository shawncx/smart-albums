# Smart Albums

One photography Skill with exactly three public capabilities:

| Capability | Purpose |
| --- | --- |
| **ingestion** | Import local photos, basic metadata and proportional JPEG previews; optionally associate successfully scanned photos with an album. |
| **index** | Set up a local image encoder, explicitly select a profile, and plan, generate, inspect or resume image embeddings. |
| **management** | View albums/photos, find names and filenames, and search photos semantically in Chinese or English. |

These are independent operations, not an automatic pipeline. Ingestion never runs a model. Management is read-only: no album mutations, selection widgets, automatic curation, clustering or deletion. Original photos stay in place and are never deleted by the program.

## Install the base Skill

Use a compatible Python 3.12+ interpreter and a project virtual environment. Base ingestion, browsing and metadata search need only [the base requirements](photography/requirements.txt), not PyTorch or model weights. Windows examples, from the repository root:

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r photography\requirements.txt
```

The optional index runtime is pinned for **standard, GIL-enabled CPython 3.14, 64-bit x86-64**, with Windows CPU dependencies in its separate requirements file; see [index setup](photography/references/index.md). Base Python support is broader. Do not assume every newer Python release has compatible PyTorch wheels or install into a shared interpreter.

The Skill source is [photography/SKILL.md](photography/SKILL.md), with identifier `smart-albums`. Copy the entire `photography` directory into your host's supported Skill directory as `smart-albums`. The host must support local Python execution and access to the photo and state directories.

## ingestion: import without inference

Use the virtual environment's Python for every command. Replace placeholders with actual paths and returned IDs; photo and state directories must be absolute, separate, non-overlapping trees. Keep the same state directory across commands.

```text
.\.venv\Scripts\python.exe photography\scripts\photography.py --state-dir <state-directory> ingestion <absolute-photo-directory> --album-name Travel
```

`ingest` remains an alias. Repeated scans skip unchanged files; `--album-name` creates/reuses that album and adds successful photos, including unchanged ones. Without it, memberships remain unchanged. The response includes an index summary, not an instruction to run cloud analysis.

Saved previews retain aspect ratio (default longest edge 1024, JPEG quality 85, no upscaling). Ingestion detects changed source versions; corresponding old index results become ineligible for current search without deleting their history. See [ingestion](photography/references/ingest.md).

## index: explicit setup, configuration and execution

Install the optional dependencies into a dedicated CPython 3.14 64-bit project virtual environment. Use the dependency file as the source of package pins; installed-package compatibility and real inference are not established by these examples. Model download and any real-model trial require separate authorization; approval of code changes or ingestion is not that authorization.

```text
python -m pip install -r photography\requirements-index.txt
python photography\scripts\photography.py --state-dir <state-directory> index setup
python photography\scripts\photography.py --state-dir <state-directory> index profiles
python photography\scripts\photography.py --state-dir <state-directory> index configure --default-profile <profile-id>
python photography\scripts\photography.py --state-dir <state-directory> index plan --album-id <album-id>
python photography\scripts\photography.py --state-dir <state-directory> index execute <run-id> --confirm <reviewed-digest>
python photography\scripts\photography.py --state-dir <state-directory> index status --album-id <album-id>
```

Here `python` means the selected virtual environment's interpreter. `setup` is the only model-download operation. It registers a profile but **does not set a default**, including on first use. Configure explicitly, or supply `--profile-id` when planning/searching. Creating another profile or generating its index never silently switches the default.

The initial model is `google/siglip2-base-patch16-224`, fixed revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2`, using Transformers and PyTorch CPU FP32. Inference uses the checkpoint's official **224×224 square resize**, not an added crop or padding step; this does not alter proportional previews in SQLite. NaFlex is deferred.

Indexing reads validated SQLite previews, not originals or saved descriptions. Offline/missing originals do not block a valid saved preview; an ingestion-error record is not a valid current input. A matching result is reused. Model profiles and old input results coexist; index operations do not rewrite old analysis or text-vector records.

Plans require an explicit album, library or IDs file. Review the exact scope, profile, pending/cached/invalid counts and digest before execution. `--dry-run` does not persist a plan. Ordinary planning, indexing and search do not download models or fall back to a cloud service. See [index reference](photography/references/index.md) for local model paths, recovery and coverage.

## management: browse and search, read-only

```text
python photography\scripts\photography.py --state-dir <state-directory> management albums
python photography\scripts\photography.py --state-dir <state-directory> management album <album-id>
python photography\scripts\photography.py --state-dir <state-directory> management photos --album-id <album-id>
python photography\scripts\photography.py --state-dir <state-directory> management photo <photo-id>
python photography\scripts\photography.py --state-dir <state-directory> management search "旅行" --mode metadata --target albums
python photography\scripts\photography.py --state-dir <state-directory> management search "IMG_01" --mode metadata --target photos
python photography\scripts\photography.py --state-dir <state-directory> management search "黑白的枯树" --mode semantic --album-id <album-id> --html <output-directory>\trees.html
python photography\scripts\photography.py --state-dir <state-directory> management search "black and white leafless trees" --mode semantic --profile-id <profile-id>
```

Browsing and metadata search need no model or default profile. Metadata mode requires `--target albums|photos` and uses Unicode NFC/casefold literal substrings: album names or photo filenames/relative paths, not descriptions or SQL wildcards.

Semantic mode returns photos only. It uses one explicit or configured profile and its **matching image/text encoders**, encodes only the query, and reports incomplete index coverage. It never generates missing image embeddings or combines vectors from different profiles. Empty candidates return an empty result without loading a model. Scores are relative cosine similarity, not probabilities or guaranteed matches; Chinese/English quality on a real gallery remains to be evaluated.

HTML/JSON exports are read-only snapshots, not a live service or an album-selection workflow. Saved previews remain browsable while originals are offline. See [management](photography/references/management.md) for scopes, pagination and exports.

## Storage, compatibility and validation

Schema v7 contains 13 active tables: photo/library/album/scan/thumbnail storage and the six `image_index_*` tables. It removes nine obsolete analysis/workflow/text-vector tables after taking a consistent backup. All current photo records, album membership, previews and image-index history remain unchanged; new databases never create retired tables. Opening a v1-v6 database can trigger this transactional upgrade even from a read-only command, so review the [migration contract](docs/index-design.md) before using an older library.

The old OpenAI/Codex analysis commands, adapters, credentials workflow and observation reports have been removed. Image indexing has no description-generation dependency. Retired table data is recoverable from the pre-migration backup, not retained in the active v7 database. Previously exported snapshots remain usable. Top-level album-write commands and `search-add` remain compatibility-only interfaces; see [album compatibility](photography/references/albums.md) and [search compatibility](photography/references/search.md).

- [Index design and data-retention contract](docs/index-design.md)
- [Approved implementation plan, preserved as a historical plan](docs/ingestion-index-management-plan.md)
- [Roadmap and unverified follow-up work](docs/TODO.md)

Existing offline regression entry point:

```text
python -m unittest discover -s tests -v
```

Synthetic images and test adapters do not validate real-model speed, memory use, retrieval quality, network isolation or a production migration. Downloads, real-photo trials and live-library migration require separate authorization.

Local databases, previews, model weights, credentials and generated reports are not distributed with the Skill. Keep runtime data in a separate state directory and back it up consistently; pushing this repository does not back up a photo library.
