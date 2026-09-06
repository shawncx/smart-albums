# Visual analysis

For current user-facing entry points, read [Plan and execute visual analysis](analysis-workflow.md). Since schema v5, `analyze` creates a proposal and `analysis-execute` requires its confirmed digest. The single-photo Python function `analyze()` remains available internally. Its legacy retry/dry-run behavior is distinguished below; it is not the Skill's confirmation workflow.

Album selection, credential-free status and SQLite previews are implemented in schema v3; see [albums and saved results](albums.md). Importing or creating an album never invokes analysis automatically.

## Optional local Codex login

Use `--provider codex` when the user wants to reuse a saved local Codex login instead of calling the standalone API. This requires the Codex CLI on PATH and a successful `codex login status` in the process environment. The adapter does not open credential files, copy login tokens, or change login configuration. The normal user environment may be required when a restricted child process cannot locate its home directory.

```text
python <skill>/scripts/photography.py --state-dir <state> analyze --provider codex --model gpt-6-astra --reasoning low --library-id <library-id> --limit 3
python <skill>/scripts/photography.py --state-dir <state> analysis-report --provider codex --model gpt-6-astra --reasoning low --library-id <library-id> --output <report.html>
```

Codex defaults: model `gpt-6-astra`, reasoning `low`, language `zh-CN`, timeout 240 seconds. It starts an ephemeral CLI turn for each JPEG preview in an isolated temporary directory, requests schema-constrained output, uses a read-only sandbox and disables shell, app, browser and computer tools. The CLI's user config is not loaded for that invocation; the model, reasoning and safety options are passed explicitly. Auth still uses Codex's saved login. Managed policies remain in effect. Temporary preview/schema/output files are removed when the call ends. No automatic retry is made by this adapter; Codex's own service connection behavior can still affect timing.

This is a local Codex integration, **not** the deferred general host-model workflow for all ChatGPT environments. Availability of an installed CLI/login does not guarantee a particular model is accessible until a call succeeds.

Measurements: `model_elapsed_seconds` times the provider call, including CLI startup and its service request. Result `elapsed_seconds` includes per-photo input preparation and model/validation work up to persistence. For a full user-observed wall-clock measurement, time the enclosing `analyze` call (including preflight and commit). Codex `usage_scope` is `codex_turn`; it includes turn context and image input, not merely the photo's own token cost. `cached_input_tokens` is included within `input_tokens`, and `total_tokens = input_tokens + output_tokens`. Missing counters remain unknown, never zero by assumption. `model_source: requested_cli_model` distinguishes the requested CLI model from a provider-reported resolved model version. The API adapter uses `provider_response` for the latter.

The official [non-interactive CLI documentation](https://learn.chatgpt.com/docs/non-interactive-mode) describes reuse of saved authentication, structured output and JSONL token usage.

## Runtime and configuration

Analysis uses the same Python 3.12+, Pillow and state directory as ingestion. The HTTP adapter uses Python's standard library; no additional SDK is required.

Set `OPENAI_API_KEY` in the local environment of the process that invokes the script. Never put the key in a command argument, chat message, source file or committed configuration. A desktop process may need restarting to see newly configured user environment variables.

Defaults:

| Setting | Default | Override |
| --- | --- | --- |
| Provider | OpenAI Responses | Python `VisionProvider` adapter boundary |
| API base | `https://api.openai.com/v1` | `OPENAI_BASE_URL` for an explicitly chosen compatible endpoint |
| Model | `gpt-5.4-mini-2026-03-17` | `PHOTOGRAPHY_MODEL` or `--model` |
| Output language | `zh-CN` | `--language en` |
| Input | Ingestion JPEG preview, image detail high | Python `AnalysisConfig` |
| Output limit | 2,400 tokens | Python `AnalysisConfig` |
| Timeout | 60 seconds per HTTP attempt | `--timeout` (1–300) |
| Retries | At most 2, after the initial attempt | `--retries` (0–3) |

The endpoint must support Responses image input and strict JSON Schema. HTTPS is required except for loopback HTTP test services. The selected model must support these capabilities. Arbitrary compatible providers and alternate models have not been live validated. Redirects are refused to avoid forwarding a credential/photo to a different endpoint. A timed-out request might still have been processed by the provider; retries can therefore incur additional usage.

Only the generated JPEG preview and a generic description request are sent. Original paths, original photo bytes and original EXIF are not included. Requests set `store: false`; this is not a claim of zero provider retention. Observations and usage counters are saved locally; credentials, image data URLs and raw provider errors are not saved.

## Photo selection and legacy low-level behavior

The commands below now select photos and create proposals only. Follow the workflow reference to confirm and execute. The exact invocation-time configuration is returned in the proposal; saved defaults can override the initial defaults listed above.

All examples use placeholders. Resolve IDs with `libraries` and `photos`; retain the same `--state-dir`.

```text
python <skill>/scripts/photography.py --state-dir <state> analyze --library-id <library-id> --dry-run
python <skill>/scripts/photography.py --state-dir <state> analyze --library-id <library-id>
python <skill>/scripts/photography.py --state-dir <state> analyze --library-id <library-id> --all
python <skill>/scripts/photography.py --state-dir <state> analyze <photo-id> <photo-id>
python <skill>/scripts/photography.py --state-dir <state> analyze --ids-file <ids.json>
python <skill>/scripts/photography.py --state-dir <state> analyze <photo-id> --force
```

Library or `--album-id` selection defaults to five photos sorted by relative filename and photo ID. Use `--limit N` for another sample or `--all` for the whole selection. `--pending-only` excludes matching saved results before the limit; unavailable candidates remain visible as preflight errors. Empty album/filter selections return zero work; explicit empty ID lists are invalid. IDs files contain a UTF-8 JSON array of strings (a BOM is accepted). Explicit IDs, a file, a library and an album are alternative selections. IDs are deduplicated in first-occurrence order.

The CLI `--dry-run` validates originals, thumbnails and caches, does not check credentials, and does not save a proposal. Opening the database may initialize/upgrade it with a backup before migration to v5. The internal Python `analyze(dry_run=True)` retains its old readiness check and may report `MODEL_CREDENTIAL_MISSING`; do not use that internal path for user-facing planning. For browsing or import follow-up, `analysis-status` reads saved metadata with originals offline.

The legacy Python executor returns `run_id`, timestamps, ordered unique `photo_ids`, counts, public configuration and `results`. Each result contains `photo_id`, status and either `analysis_id` or an error. Its completion equation is:

```text
requested = analyzed + cached + failed
```

CLI exit codes: `0` successfully returned a proposal/status or completed execution, `1` partial/failed execution, `2` invalid input/configuration/storage or blocked operation, `130` interrupted. Always inspect the JSON status: success returning a proposed, paused or submitted plan is not proof that photos were analyzed. The workflow records additional pending/stale/uncertain states; use its summary rather than the legacy equation above.

## Saved observations

The model returns exactly these fields; extra keys and invalid types are rejected:

| Field | Shape |
| --- | --- |
| subjects, composition, color, lighting, mood, tags | Lists of short strings; empty lists allowed |
| scene | Short string, empty when unknown |
| visual_description | Nonempty overall description |
| technical_observations.visible_issues | List of observations about the preview |
| technical_observations.assessment_scope | Always `preview` |
| technical_observations.limitations | Nonempty list explaining preview limitations |

No score, keep/reject or curation decision is requested. Mood is interpretive. The program validates structure, not the factual accuracy or aesthetic usefulness of descriptions; inspect a real sample before expanding the batch.

Each saved record also includes source content version, actual preview SHA-256, preview profile, configuration fingerprint, prompt/schema fingerprints, requested/returned model, creation time, response ID and available token counters.

## Cache and recovery

Cache requires the same photo ID, source content version, preview bytes/profile, endpoint, model, language, prompt, schema and output settings. Timeouts/retry limits and credentials are excluded because they do not change the requested observations. Pinned model snapshots reduce unexpected alias changes. Force saves a new history record; failure does not replace a prior successful record.

The program checks indexed state, original file size/mtime and the actual preview. Rescan originals that changed since ingestion. These checks inherit ingestion's size/mtime limitation: an externally changed source with identical size and restored mtime is not detected by a full-source hash check here.

No database transaction is held during a model call. Before committing, the code rechecks the current indexed content version, preview profile/bytes and source size/mtime. It atomically saves the observation, photo analysis flag and batch checkpoint. Results for changed photos are discarded. Per-photo checkpoints survive later failures. Keyboard interruption records failures for unfinished photos. A hard process termination can leave a run marked `running`; already committed results remain valid. Reissue the same selection to reuse them and finish the rest.

The legacy Python executor is serial and has no cross-process claims. The confirmed workflow adds persistent input claims and bounded concurrency. It checks versions before submission and commit, keeps uncertain requests reserved, and uses explicit recovery; see the workflow reference. Avoid bypassing it with direct low-level Python calls.

## Query and display

```text
python <skill>/scripts/photography.py --state-dir <state> analysis <analysis-id>
python <skill>/scripts/photography.py --state-dir <state> analyses <photo-id> --limit 100
python <skill>/scripts/photography.py --state-dir <state> analysis-run <run-id>
python <skill>/scripts/photography.py --state-dir <state> analysis-report --library-id <library-id> --output <report.html>
```

History is oldest first; follow numeric `next_cursor` using `--after`. `matches_indexed_photo` compares the stored source version/profile with the index; it is not a full check against current image bytes or model configuration. Reports support either library or album selection and validate embedded database previews. Without a provider/model filter they show saved results across models; with a filter they identify matching results and label other records as history. Original availability is shown separately and does not block browsing. Export outside state/source directories. Reports are standalone snapshots with search, filtering and full records; regenerate after changes.

## Implementation references

The adapter follows official [image input documentation](https://developers.openai.com/api/docs/guides/images-vision) and [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). The default snapshot is listed on the [GPT-5.4 mini model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini).
