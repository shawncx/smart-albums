# Authorization and recovery

This is the shared policy for local Smart Albums operations. Task-specific references define inputs and validation; [cloud review](review.md#confirm-the-whole-frozen-task-before-any-sdk-construction) has additional contact and retry requirements. User instructions and authorization already given in the conversation take precedence.

## Local requests authorize their concrete execution

A clear request to perform a local operation authorizes its necessary steps within the selected album, photo scope, component and configuration. For example, “compute color and hashes for these photos” permits planning and executing both named components. “Organize this album by month” permits inspecting a date plan and applying it. Do not ask again merely because the CLI requires a digest or a `--confirm` flag.

1. Resolve actual IDs and the explicitly selected or configured profile. Ask only if a required choice cannot be resolved from the request and conversation. “Index” selects the existing image-embedding default, not every feature component. Setup/registration does not change a default.
2. Prepare the local plan and inspect album, component/profile, exact scope, inputs, compute/reuse/skip counts, errors and any dependency work. Summarize the concrete action for the user before execution; this can be a progress update rather than a confirmation question.
3. Check that the plan implements the authorized request. Execute using the returned run ID/digest, or apply the raw date plan with its digest. Retain all programmatic validation and atomic writes. Neither a plan nor a digest is authorization by itself.
4. Inspect results and partial failures. Report the completed and remaining scope; do not assume success from dispatch or a saved run ID.

This applies to requested ingestion, local image embedding/feature computation (including non-ML color/hash/composition), prototypes/comparisons, folder writes and date organization. Read-only browsing, profile/status lookup and saved report generation within the task need no additional confirmation.

A request to explain, inspect or prepare a plan does not authorize execution. File selection alone does not authorize import/index/repair or folder changes. Search alone does not authorize generating missing indexes or organizing results.

## Dependencies and changed plans

Inspect missing dependencies and prepare their local plans without an extra question, when readiness permits. Use `--dry-run` for an informational-only request. If planning itself needs unavailable assets, report that blocker; do not install or download simply to obtain a plan.

Execute dependency work only when already covered by the request, such as explicitly named upstream components or an instruction to prepare the required local indexes. Otherwise present the prepared scope/configuration and ask for the additional computation. Do not infer all components, all photos, new defaults or a different model from a missing dependency.

Changed inputs invalidate the old digest: stop and prepare a new plan, never edit the saved one. Inspect it against the existing authorization. If the operation, photos, intended source version and configuration remain within that authorization, use the new validated plan without repeating consent. Ask when new photos/components, a different source/model/profile, an unresolved input change or another material choice changes what the user approved. Planned cache reuse cannot silently become compute inside an existing run.

## Boundaries that still need authorization

- Dependency installation and model/runtime downloads must be covered by an explicit request or prior permission. One request may authorize both, but a local computation request alone does not authorize provisioning. Prepare the concrete installation/download proposal before asking; use a compatible project environment and retain asset/license checks.
- Cloud authentication/model discovery, photo transfers and possible provider charges follow the review reference. Local authorization is never cloud authorization. Every cloud retry still needs fresh state-bound consent, including after uncertain sends.
- Destructive changes to originals are outside this version. Never delete/merge photos to complete duplicate discovery or silently substitute content during relink.

## Local recovery

Use `job` to inspect the failure, and resume the same authorized work after resolving its cause; reuse successful saved results. Do not repeatedly retry unchanged configuration, asset or input errors. New repair computation needs a new plan and the scope check above.

A run marked `running` requires evidence that all prior workers on all devices stopped before `--confirm-stopped`. Reuse evidence already established in the conversation; if remote-worker state is unknown, ask for that missing fact. Elapsed time or a missing local PID is insufficient. Never steal live locks, delete claims/sidecars or treat the stopped flag as computation approval.
