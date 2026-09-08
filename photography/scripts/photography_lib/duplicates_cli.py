"""One management entry for complete duplicate scans and frozen result views."""
from __future__ import annotations

from . import duplicates
from .config import PhotographyError
from .exports import prepare_export


def add_commands(actions):
    from .management_cli import _search_limit

    root = actions.add_parser("duplicates", help="Find all exact and suspected duplicate photos within an explicit scope.")
    commands = root.add_subparsers(dest="duplicates_command", required=True)
    scan = commands.add_parser("scan", help="Save a complete, read-only duplicate snapshot; no models or original reads.")
    scope = scan.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", dest="all_photos", action="store_true")
    scope.add_argument("--folder-id", dest="folder_ids", action="append")
    scope.add_argument("--ids-file", help="JSON array of photo IDs; [] selects no photos.")
    scan.add_argument("--folder-match", choices=("union", "intersection"))
    scan.add_argument("--mode", choices=("exact", "similar", "all"), default="all")
    scan.add_argument("--profile-id", help="Perceptual-hash profile; otherwise use that component's saved default.")
    scan.add_argument("--max-distance", type=int, help="Hamming distance 0-64, default 8; not accepted in exact mode.")
    scan.add_argument("--output", required=True, help="Complete JSON snapshot, independent of display page sizes.")
    scan.add_argument("--html", help="Optional HTML containing every matched photo, grouped for comparison.")
    for name in ("groups", "group", "pairs"):
        view = commands.add_parser(name, help=f"Read frozen duplicate {name}; no new album scan.")
        view.add_argument("snapshot")
        if name != "groups":
            view.add_argument("--group-id", required=True)
        view.add_argument("--limit", type=_search_limit, default=50 if name == "groups" else 100,
                          help="Positive page size or all; no fixed upper count.")
        view.add_argument("--after", default="", help="Cursor returned by this snapshot and view.")
    report = commands.add_parser("report", help="Render every group from a frozen snapshot using matching stored previews.")
    report.add_argument("snapshot")
    report.add_argument("--output", required=True, help="Standalone HTML report.")


def command(args, store, config):
    from .cli import read_json_file
    from .duplicates_report import duplicate_report
    from .management_cli import _protect_inputs, _write_json

    action = args.duplicates_command
    if action == "scan":
        output = prepare_export(args.output, config, store, (".json",))
        html_output = prepare_export(args.html, config, store, (".html",)) if args.html else None
        sources = [args.ids_file] if args.ids_file else []
        _protect_inputs((output, html_output), sources)
        if html_output is not None:
            _protect_inputs((output,), (html_output,))
        snapshot = duplicates.scan(store=store, all_photos=args.all_photos, folder_ids=args.folder_ids,
                                   folder_match=args.folder_match,
                                   photo_ids=read_json_file(args.ids_file) if args.ids_file else None,
                                   mode=args.mode, profile_id=args.profile_id, max_distance=args.max_distance)
        _write_json(snapshot, output)
        result = {**duplicates.summary(snapshot), "output": str(output)}
        if html_output is not None:
            try:
                result["html_output"] = duplicate_report(snapshot, html_output, config=config, store=store)
            except (PhotographyError, OSError, KeyboardInterrupt) as exc:
                raise PhotographyError("SCAN_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "DUPLICATE_REPORT_FAILED",
                                       "The JSON snapshot was saved; retry duplicates report to render its HTML.",
                                       details={"output": str(output), "report_error": str(exc)}) from exc
        return result
    if action == "report":
        output = prepare_export(args.output, config, store, (".html",))
        _protect_inputs((output,), (args.snapshot,))
        snapshot = read_json_file(args.snapshot)
        html_output = duplicate_report(snapshot, output, config=config, store=store)
        return {**duplicates.summary(snapshot), "html_output": html_output}
    return duplicates.page(read_json_file(args.snapshot), store=store, view=action,
                           group_id=getattr(args, "group_id", None), limit=args.limit, after=args.after)
