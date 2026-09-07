"""Read-only condition queries with private snapshots and explicit review pages."""
from __future__ import annotations

from .config import PhotographyError
from .exports import prepare_export


def add_commands(actions):
    query = actions.add_parser("query", help="Query saved indexes; print only a safe summary, not the private matrix.")
    query.add_argument("--query-file", required=True, help="multi-condition-query-v1 JSON.")
    query.add_argument("--output", required=True, help="Private condition snapshot JSON; do not inspect for semantic review.")
    evidence = actions.add_parser("query-evidence", help="Read one semantic condition's numeric-only review page.")
    evidence.add_argument("snapshot")
    evidence.add_argument("--condition-id", required=True)
    evidence.add_argument("--page", type=int, default=0)
    finalize = actions.add_parser("finalize-query", help="Apply complete explicit semantic decisions and freeze ranking.")
    finalize.add_argument("snapshot")
    finalize.add_argument("--decisions-file", required=True, help="condition-decisions-v1 matched-ID pages.")
    finalize.add_argument("--output", required=True, help="Separate finalized condition snapshot JSON.")
    show = actions.add_parser("show-query-results", help="Page finalized results without repeating the query.")
    show.add_argument("snapshot")
    show.add_argument("--limit", type=int, default=100)
    show.add_argument("--after", default="", help="Opaque next_cursor returned by a previous page.")
    show.add_argument("--output", help="Export this validated result page as JSON.")
    show.add_argument("--html", help="Export only this page's saved previews for the user, never agent inspection.")
    pairs = actions.add_parser("query-pairs", help="Page actual duplicate pairs from a finalized query; never delete or group photos.")
    pairs.add_argument("snapshot")
    pairs.add_argument("--condition-id", required=True)
    pairs.add_argument("--limit", type=int, default=100)
    pairs.add_argument("--after", default="", help="Opaque next_cursor returned by a previous pair page.")
    pairs.add_argument("--output", help="Export this duplicate-pair page as JSON.")


def command(args, store, config):
    from . import condition_search
    from .cli import read_json_file
    from .management_cli import _protect_inputs, _write_json

    action = args.management_command
    if action == "query-evidence":
        evidence = condition_search.semantic_evidence(
            read_json_file(args.snapshot), args.condition_id, args.page, store=store)
        # The root CLI supplies a full album by default; evidence permits identity only.
        return {**evidence, "album": {"id": store.album()["id"]}}
    if action not in ("query", "finalize-query", "show-query-results", "query-pairs"):
        raise PhotographyError("INVALID_ARGUMENT", "Unknown condition-query operation.")

    # Check every target and input alias before model calls, validation reads or exports.
    output = prepare_export(args.output, config, store, (".json",)) if args.output else None
    html_name = getattr(args, "html", None)
    html_output = prepare_export(html_name, config, store, (".html",)) if html_name else None
    sources = ([args.query_file] if action == "query" else
               [args.snapshot, args.decisions_file] if action == "finalize-query" else [args.snapshot])
    _protect_inputs((output, html_output), sources)
    if action == "query":
        snapshot = condition_search.query(read_json_file(args.query_file), store=store, config=config)
        result = condition_search.summary(snapshot, model_calls=snapshot["query_model_calls"])
        _write_json(snapshot, output)
        return {**result, "output": str(output)}
    if action == "finalize-query":
        snapshot = condition_search.finalize_query(
            read_json_file(args.snapshot), read_json_file(args.decisions_file), store=store)
        result = condition_search.summary(snapshot, model_calls=0)
        _write_json(snapshot, output)
        return {**result, "output": str(output)}

    snapshot = read_json_file(args.snapshot)
    if action == "query-pairs":
        result = condition_search.duplicate_pairs(
            snapshot, args.condition_id, store=store, limit=args.limit, after=args.after)
    else:
        result = condition_search.show_results(snapshot, store=store, limit=args.limit, after=args.after)
    if html_output:
        from .condition_report import condition_report

        result["html_output"] = condition_report(result, html_output, config=config, store=store)
    if output:
        result["output"] = str(output)
        _write_json(result, output)
    return result
