"""Natural-language agents use these commands to plan, confirm, then execute."""
from .analysis_settings import DEFAULTS, resolve_settings, make_provider
from .analysis_planner import create_plan, confirm_plan
from .analysis_execution import execute_plan, summarize
from .config import PhotographyError


def setting_arguments(parser):
    existing = {a.dest for a in parser._actions}
    choices = dict(provider=("openai", "codex"), mode=("auto", "immediate", "batch"),
        wait_preference=("immediate", "flexible"), language=("zh-CN", "en"),
        detail=("low", "high", "auto"), reasoning=("low", "medium", "high", "xhigh"))
    for key in DEFAULTS:
        if key in existing:
            continue
        kwargs = {}
        if key in choices:
            kwargs["choices"] = choices[key]
        elif key in ("retries", "max_output_tokens", "images_per_request", "concurrency", "batch_max_requests", "batch_max_bytes", "max_wait_seconds"):
            kwargs["type"] = int
        elif key in ("rpm", "tpm", "queue_tokens", "input_tokens_per_photo", "input_price", "output_price", "timeout"):
            kwargs["type"] = float
        parser.add_argument("--" + key.replace("_", "-"), **kwargs)


def add_commands(commands, analysis):
    setting_arguments(analysis)
    analysis.add_argument("--remember", action="store_true", help="Save this selection's non-secret model and execution defaults.")
    analysis.add_argument("--html", help="Export an offline proposal snapshot; never executes analysis.")
    cfg = commands.add_parser("analysis-config", help="Show defaults, or explicitly save supplied non-secret settings.")
    setting_arguments(cfg)
    cfg.add_argument("--save", action="store_true")
    cfg.add_argument("--reset", action="store_true")
    for name in ("analysis-confirm", "analysis-execute", "analysis-resume", "analysis-job", "analysis-collect", "analysis-cancel", "analysis-cleanup", "analysis-recover"):
        cmd = commands.add_parser(name)
        cmd.add_argument("plan_id")
        if name in ("analysis-confirm", "analysis-execute"):
            cmd.add_argument("--confirm", required=name == "analysis-confirm", help="Exact digest of the user-reviewed plan.")
        if name == "analysis-job":
            cmd.add_argument("--refresh", action="store_true", help="Fetch remote Batch status; otherwise read local records only.")
            cmd.add_argument("--html", help="Export a progress snapshot.")
        if name == "analysis-recover":
            cmd.add_argument("--acknowledge-no-active-worker", action="store_true", required=True,
                help="Operator has stopped all workers. Uncertain calls are failed locally, never automatically resent.")


def overrides(args):
    return {k: getattr(args, k) for k in DEFAULTS if getattr(args, k, None) is not None}


def command(args, store, config):
    name = args.command
    if name == "analysis-config":
        settings = resolve_settings({} if args.reset else store.settings(), overrides(args))
        if args.save or args.reset:
            with store.transaction():
                store.save_settings(settings)
        return {"settings": settings, "saved": args.save or args.reset, "model_calls": 0}
    if name in ("analyze", "analysis-plan"):
        from .cli import selected_ids
        settings = resolve_settings(store.settings(), overrides(args))
        provider = make_provider(settings)
        ids = selected_ids(args, store, provider)
        if not ids and not (args.album_id or args.library_id):
            raise PhotographyError("INVALID_ARGUMENT", "Provide indexed photo IDs or an album/library selection.")
        result = create_plan(ids, store=store, config=config, settings=settings, force=args.force, persist=not args.dry_run)
        result["dry_run"] = args.dry_run
        if args.remember and not args.dry_run:
            with store.transaction():
                store.save_settings(settings)
        if args.html:
            from .workflow_report import workflow_report
            result["html_output"] = workflow_report(result, args.html, config=config, store=store)
        return result
    if name == "analysis-confirm":
        return confirm_plan(store, args.plan_id, args.confirm)
    if name in ("analysis-execute", "analysis-resume"):
        if getattr(args, "confirm", None):
            confirm_plan(store, args.plan_id, args.confirm)
        return execute_plan(args.plan_id, store=store, config=config, resume=name == "analysis-resume")
    if name == "analysis-recover":
        return recover_interrupted(args.plan_id, store)
    if name == "analysis-job" and not args.refresh:
        result = summarize(store, args.plan_id)
        if args.html:
            from .workflow_report import workflow_report
            result["html_output"] = workflow_report(result, args.html, config=config, store=store)
        return result
    from .openai_batch import batch_action
    action = {"analysis-job": "status", "analysis-collect": "collect", "analysis-cancel": "cancel", "analysis-cleanup": "cleanup"}[name]
    result = batch_action(args.plan_id, store=store, config=config, action=action)
    if getattr(args, "html", None):
        from .workflow_report import workflow_report
        result["html_output"] = workflow_report(result, args.html, config=config, store=store)
    return result


def recover_interrupted(plan_id, store):
    from .analysis_planner import require_confirmed
    from .analysis_execution import fail_request
    with store.transaction():
        plan = store.plan(plan_id)
        require_confirmed(plan)
        if plan["execution"]["mode"] == "batch":
            # Remote submitted work must be reconciled/collected before claims release.
            if any(r["status"] in ("running", "submitting", "submitted", "unknown") for r in store.requests(plan_id)):
                raise PhotographyError("RECONCILE_BATCH_FIRST", "Use remote status reconciliation and collection; local recovery must not abandon a possibly active Batch.")
        for req in store.requests(plan_id):
            if req["status"] in ("running", "unknown"):
                if req["attempts"]:
                    req["attempts"][-1]["status"] = "unknown"
                fail_request(plan, req, store, {"code": "UNRESOLVED_CALL", "message": "Operator closed an uncertain call. Usage remains unknown; a new plan is required to retry this photo."})
        plan["status"] = "paused"
        store.save_plan(plan)
    return summarize(store, plan_id)
