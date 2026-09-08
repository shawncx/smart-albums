"""Independent AI review commands with explicit remote-operation consent."""
from pathlib import Path

from .config import PhotographyError


def add_commands(commands):
    root = commands.add_parser("review", help="Explicit cloud review of selected saved photo previews.")
    actions = root.add_subparsers(dest="review_command", required=True)
    actions.add_parser("rubric", help="Show the bundled scoring rubric without contacting Copilot.")
    upgrade = actions.add_parser("upgrade", help="Create a schema-12 album copy for v2 reviews; preserve the source.")
    upgrade.add_argument("--output", required=True, help="Unused absolute album file destination.")
    models = actions.add_parser("models", help="Contact Copilot to list models only after explicit confirmation.")
    models.add_argument("--confirm-provider-access", action="store_true")
    plan = actions.add_parser("plan", help="Freeze a photo selection locally; no Copilot calls.")
    selection = plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--photo-id")
    selection.add_argument("--ids-file", help="Absolute UTF-8 JSON array of existing photo IDs.")
    plan.add_argument("--model", required=True, help="Explicit Copilot vision model ID, not auto.")
    plan.add_argument("--batch-size", type=int, default=4)
    plan.add_argument("--language", choices=("zh-CN", "en"), default="zh-CN")
    plan.add_argument("--force", action="store_true", help="Request a fresh review and preserve history.")
    plan.add_argument("--dry-run", action="store_true")
    for name in ("execute", "resume"):
        action = actions.add_parser(name, help="Execute an explicitly confirmed task or retry scope.")
        action.add_argument("run_id")
        action.add_argument("--confirm", required=True, help="Whole-task digest, or fresh retry digest for resume.")
        if name == "resume":
            action.add_argument("--confirm-stopped", action="store_true")
    job = actions.add_parser("job", help="Inspect saved progress and the next retry confirmation without Copilot.")
    job.add_argument("run_id")
    result = actions.add_parser("result", help="Read the latest structured photo review locally.")
    result.add_argument("photo_id")
    history = actions.add_parser("history", help="Read paginated photo review history locally.")
    history.add_argument("photo_id")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--after", default="")
    report = actions.add_parser("report", help="Export a self-contained, read-only HTML review report.")
    report.add_argument("run_id")
    report.add_argument("--output", required=True, help="Unused absolute .html destination.")


def command(args, store, config):
    from . import review
    action = args.review_command
    if action == "upgrade":
        if not Path(args.output).is_absolute():
            raise PhotographyError("INVALID_ARGUMENT", "Album upgrade requires an absolute destination.")
        return store.upgrade_review(args.output)
    if action == "rubric":
        from .review_schema import DIMENSIONS, RESPONSE_SCHEMA, review_profile
        profile = review_profile("explicit-model-required")
        return {"rubric_version": profile["rubric_version"], "dimensions": list(DIMENSIONS),
                "prompt": profile["prompt_text"], "response_schema": RESPONSE_SCHEMA,
                "overall_score": "Application-only equal-weight mean, decimal half-up to two places; null if any dimension is null.",
                "validation_scope": "Structural contract; visual evidence, output language and preview-limit wording require semantic review.",
                "provider_calls_this_operation": 0}
    if action == "models":
        if not args.confirm_provider_access:
            raise PhotographyError("CONFIRMATION_REQUIRED",
                                   "Confirm contacting Copilot with the current login before listing models; no images are sent.")
        return {"models": review._provider().models(), "images_sent": 0}
    if action == "plan":
        if args.ids_file:
            from .cli import read_json_file
            if not Path(args.ids_file).is_absolute():
                raise PhotographyError("INVALID_ARGUMENT", "Review photo IDs file must have an absolute path.")
            ids = read_json_file(args.ids_file)
        else:
            ids = [args.photo_id]
        return review.create_plan(ids, store=store, model=args.model, language=args.language,
                                  batch_size=args.batch_size, force=args.force, persist=not args.dry_run)
    if action == "job":
        return review.job(store, args.run_id)
    if action == "result":
        return review.result(store, args.photo_id)
    if action == "history":
        return review.history(store, args.photo_id, limit=args.limit, after=args.after)
    if action == "report":
        from .review_report import review_report
        return review_report(args.run_id, args.output, store=store, config=config)
    return review.execute_plan(args.run_id, store=store, confirm=args.confirm, resume=action == "resume",
                               confirm_stopped=getattr(args, "confirm_stopped", False))
