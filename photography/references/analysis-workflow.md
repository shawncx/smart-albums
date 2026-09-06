# Plan and execute visual analysis

`analyze` creates a proposal. It no longer immediately calls a model. This is an intentional CLI behavior change; Python's existing `analyze()` remains an internal low-level executor for compatible code/tests.

There are two execution modes: **immediate** and **asynchronous Batch**. Immediate mode separately configures photos per request and concurrency. Multi-image is an experimental option inside immediate mode. The default is one photo and one request at a time. Direct APIs support OpenAI only; the existing local Codex login adapter retains single-photo immediate processing.

## Configuration and planning

```text
python <skill>/scripts/photography.py --state-dir <state> analysis-config
python <skill>/scripts/photography.py --state-dir <state> analysis-config --provider openai --model gpt-5.4-mini-2026-03-17 --save
python <skill>/scripts/photography.py --state-dir <state> analyze --album-id <album> --all --html <output>/proposal.html
python <skill>/scripts/photography.py --state-dir <state> analyze --provider codex --model gpt-6-astra --library-id <library> --limit 3 --dry-run
```

`analysis-plan` is an alias of `analyze`. Explicit IDs, `--ids-file`, album or library scopes are supported. Album/library selections default to five photos; `--all` is explicit. `--pending-only` filters before the limit. A snapshot freezes exact photo IDs and input versions; executing it never reruns the album query to add new photos.

Defaults resolve from this invocation, then saved non-secret settings, then documented built-ins. Existing OpenAI model/endpoint environment defaults remain supported on initial setup. Switching channel resets its model and endpoint unless explicitly provided. Use `--remember` to save a successful planning configuration; `analysis-config --reset` resets saved preferences. Credentials never appear in settings. `--key-env` selects an environment variable name; the default is `OPENAI_API_KEY`. Codex manages its own login.

Planning does not check credentials or contact the model service. It does check original availability/version and stored JPEG integrity. A missing key therefore does not prevent reviewing a proposal. Model access is labeled unverified until an authorized call succeeds. Database opening may perform a backed-up schema migration; `--dry-run` means no persisted plan or model invocation, not a promise of no schema maintenance.

Important settings:

| Option | Meaning |
| --- | --- |
| `--mode auto/immediate/batch` | Automatic recommendation or explicit mode |
| `--wait-preference immediate/flexible` | Whether asynchronous turnaround is acceptable |
| `--images-per-request 1..4` | Maximum photos per immediate request; default 1 |
| `--concurrency 1..8` | Maximum concurrent requests; reduced to 1 without known request/token limits |
| `--rpm`, `--tpm` | User-supplied account request/token budgets; not inferred from a sample usage tier |
| `--input-tokens-per-photo` | Input estimate from the selected model's rules or measurements; needed for token admission limits |
| `--queue-tokens` | Optional available Batch input-token budget; requires an input estimate |
| `--input-price`, `--output-price` | Optional standard USD prices per million tokens; never automatic billing claims |
| `--max-output-tokens` | Per-photo output ceiling; multiplied for multi-image |
| `--retries 0..3` | Retry ceiling per immediate request; default 2 |
| `--max-wait-seconds` | Cumulative scheduling wait before pausing; default 60 |
| `--batch-max-requests`, `--batch-max-bytes` | Local batch partition limits within provider limits |

The current conservative capability catalog enables Batch and multi-image for GPT-5.4 mini (alias and March 17, 2026 snapshot) on the official endpoint. Other OpenAI models can use single-image immediate requests; extend the capability catalog after checking the specific model. A compatible endpoint does not automatically support official Batch. No live model compatibility test is implied by this catalog.

Auto mode considers remaining work, waiting preference and available adapter capabilities. Its initial heuristic considers 50 pending photos or five minutes at the supplied request rate substantial; these are product defaults, not provider limits or a guarantee of an optimal choice. It keeps immediate mode for smaller work or an immediate preference, and offers Batch for substantial flexible work. All reasons and chosen settings remain visible and can be changed before confirmation.

Input token estimates and costs may be unknown. Image bytes are not tokens. Supplied input estimates plus output ceilings yield an indicative price interval, excluding retries and estimation error; this is not an enforced spend cap. Codex usage cannot be converted into an actual API bill. No remote language model is called to recommend a plan.

## Confirmation and execution

Inspect `settings`, `snapshots`, counts, `execution`, and the returned `digest`. Explain cache misses such as changed input or configuration. Only after the user approves the complete proposal:

```text
python <skill>/scripts/photography.py --state-dir <state> analysis-execute <plan-id> --confirm <digest>
```

Alternatively, `analysis-confirm <plan-id> --confirm <digest>` records approval without running, and `analysis-execute <plan-id>` executes the confirmed proposal. These are agent/operator interfaces: the digest binds the content, but does not itself prove that a human approved it. The Skill must obtain or use actual session authorization.

Changing model, mode, grouping or selection requires a new proposal and approval. Reducing concurrency or waiting/retrying within the approved bounds does not require repeated prompts. No approved retry ever silently switches model or endpoint. Exact single-photo caches are shared between immediate and Batch; scheduling changes do not invalidate those observations.

For immediate execution, queued tasks obey configured request/token budgets; exhausted response-header budgets and Retry-After delay subsequent sends. Temporary rate limits use bounded exponential backoff with jitter. A pause preserves completed results and request attempts. Unknown limits mean conservative concurrency, not unlimited guaranteed throughput. Budgets are local to the plan; other processes/apps using the same account may still cause rate limits.

Transport timeouts or ambiguous server failures can have incurred charges. Such calls become uncertain and are not automatically resent. Quota/auth/configuration failures stop further sends. The journal reports known usage plus unknown attempts; it never treats unknown usage as zero.

## Batch jobs

```text
python <skill>/scripts/photography.py --state-dir <state> analyze --album-id <album> --all --mode batch
python <skill>/scripts/photography.py --state-dir <state> analysis-execute <plan-id> --confirm <digest>
python <skill>/scripts/photography.py --state-dir <state> analysis-job <plan-id> --refresh
python <skill>/scripts/photography.py --state-dir <state> analysis-collect <plan-id>
python <skill>/scripts/photography.py --state-dir <state> analysis-cleanup <plan-id>
```

Each Batch request analyzes one JPEG. JSONL partitions use encoded file bytes, request counts and a supplied queue budget. Task creation and file upload are network actions that happen only after confirmation. Requests have stable IDs; collection uses those IDs, never output order. Repeated collection is idempotent, and changed input results do not replace current analysis.

An ambiguous creation response is reconciled against remote metadata using a bounded read-only listing. No unique match means attention is required; the adapter does not infer that an unobserved task does not exist or blindly create another. `analysis-job --refresh` and collection are explicit, not an installed background monitor. An expired/failed request requires reviewing and confirming a new plan to retry; creating Batch tasks is deliberately not blindly retried.

`analysis-cancel` requests cancellation. It does not guarantee no work was performed or no charges occurred. Collect completed results afterwards. `analysis-cleanup` removes remote input/output/error files only once results have been collected (or a known submission rejected); cleanup errors remain visible. Upload files contain previews, although original paths and EXIF are excluded. Local request journals contain metadata, not data URLs. Files and Batch have a different lifecycle from a synchronous request with `store:false`.

## Recovery and reporting

```text
python <skill>/scripts/photography.py --state-dir <state> analysis-job <plan-id> --html <output>/progress.html
python <skill>/scripts/photography.py --state-dir <state> analysis-resume <plan-id>
```

`analysis-job` without `--refresh` only reads local records and updates their derived summary. `analysis-run <plan-id>` also reads execution checkpoints. Counts include cached, pending, analyzed, failed, stale and unresolved photos. Usage belongs to each request attempt; cached input is a subset of input, and multi-image input/output cannot be truthfully assigned to individual photos from provider totals. Codex usage includes turn context. Summed request durations can overlap under concurrency and do not represent wall-clock duration.

Confirmed plans, items, request attempts and claims are stored in SQLite schema v5. A backup precedes migration from v4 and earlier. Existing analyses, thumbnail BLOBs and embeddings are preserved. A transaction claims each input before sending, and no write transaction spans a network call. Uncertain requests retain their claims so a different plan cannot immediately pay for the same input again.

A crashed immediate worker may leave running/uncertain records. After the operator has stopped every active worker, `analysis-recover <plan-id> --acknowledge-no-active-worker` closes those calls locally as unresolved failures and releases their claims. It never resends them, and their usage stays unknown. Pending work can then resume, while failed photos require a new reviewed plan. Do not run recovery against a live worker. Remote Batch claims require reconciliation and collection instead.

Multi-image results use a separate recipe plus complete group-input fingerprint, preventing cache reuse across different shared image contexts. Valid mapped outputs save independently; missing/duplicated IDs are errors, never guessed. Multi-image quality and savings have not been live validated; use a separately approved small sample before recommending it over one-photo requests.

## Validation boundary and official sources

Regression validation uses synthetic photos, explicit test doubles and a loopback HTTP service. It does not validate real account access, asynchronous turnaround, live pricing or multi-image quality. Real model calls need a separately approved sample.

- [Batch API](https://developers.openai.com/api/docs/guides/batch)
- [Rate limits](https://developers.openai.com/api/docs/guides/rate-limits)
- [Images and vision](https://developers.openai.com/api/docs/guides/images-vision)
- [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

Capability documentation checked September 5, 2026. Recheck account limits and model-specific behavior when expanding support.
