"""Management commands for the one explicitly selected album file."""
from __future__ import annotations

import json
from pathlib import Path

from . import management
from .config import PhotographyError
from .exports import export_path


def _view_options(parser, *, page=True):
    parser.add_argument("--profile-id", help="Image-embedding profile; otherwise inspect the saved default, if any.")
    parser.add_argument("--output", help="Export a UTF-8 JSON snapshot.")
    parser.add_argument("--html", help="Export a standalone, read-only HTML snapshot.")
    if page:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after", default="")


def add_commands(root_subparsers):
    root = root_subparsers.add_parser("management", help="Manage the selected album, browse and search its saved photos.")
    actions = root.add_subparsers(dest="management_command", required=True)
    for name, help_text in (
        ("create", "Create a new album file without overwriting an existing file."),
        ("open", "Validate an existing album read-only and show its identity and coverage."),
    ):
        _view_options(actions.add_parser(name, help=help_text), page=False)
    _view_options(actions.add_parser("photos", help="Browse saved photos in stable ID order."))
    single = actions.add_parser("photo", help="Inspect saved metadata without accessing the original.")
    single.add_argument("photo_id")
    _view_options(single, page=False)
    search = actions.add_parser("search", help="Literal filename/path matching or exact image-embedding retrieval.")
    search.add_argument("query")
    search.add_argument("--mode", choices=("metadata", "semantic"), required=True)
    _view_options(search, page=False)
    search.add_argument("--limit", type=int, help="1–1000; metadata default 100, semantic default 10.")
    search.add_argument("--after", help="Stable photo ID cursor; metadata search only.")
    selected = actions.add_parser("show-results", help="Display explicit IDs from a saved candidate snapshot; no new search or classification.")
    selected.add_argument("snapshot")
    selected.add_argument("--ids-file", required=True, help="JSON array selected by the agent/user; [] means no suitable candidates.")
    selected.add_argument("--output", help="Write a separate selected-results JSON snapshot.")
    selected.add_argument("--html", help="Write a report containing only the selected candidates.")
    locate = actions.add_parser("original", help="Locate an original and explicitly persist any path/status repair.")
    locate.add_argument("photo_id")
    relink = actions.add_parser("relink", help="Verify content and bind the photo to an explicit new original path.")
    relink.add_argument("photo_id")
    relink.add_argument("--path", required=True, help="Absolute path of an original with identical content.")
    backup = actions.add_parser("backup", help="Create a consistent SQLite backup at a new destination.")
    backup.add_argument("--output", required=True)
    thumbnail = actions.add_parser("thumbnail", help="Export a saved JPEG preview without reading its original.")
    thumbnail.add_argument("photo_id")
    thumbnail.add_argument("--output", required=True)
    scan = actions.add_parser("scan", help="Retrieve a saved ingestion summary.")
    scan.add_argument("scan_id")
    events = actions.add_parser("scan-events", help="Page through saved ingestion changes and errors.")
    events.add_argument("scan_id")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--changes-only", action="store_true")


def command(args, store, config):
    action = args.management_command
    if action == "original":
        return management.original(args.photo_id, store=store)
    if action == "relink":
        return management.relink(args.photo_id, args.path, store=store)
    if action == "backup":
        return management.backup(args.output, store=store, config=config)
    if action == "thumbnail":
        return management.thumbnail(args.photo_id, args.output, store=store, config=config)
    if action == "scan":
        return management.scan(args.scan_id, store=store)
    if action == "scan-events":
        return management.scan_events(args.scan_id, store=store, limit=args.limit,
                                      after=args.after, changes_only=args.changes_only)
    if action not in ("create", "open", "photos", "photo", "search", "show-results"):
        raise PhotographyError("INVALID_ARGUMENT", "Unknown management operation.")
    if action == "search" and args.mode == "semantic" and args.after is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Semantic search does not accept --after.")

    # Reject every unsafe target before query encoding, preview decoding or any export writes.
    output = export_path(args.output, config, store, (".json",)) if args.output else None
    html_output = export_path(args.html, config, store, (".html",)) if args.html else None
    profile = {"store": store, "profile_id": getattr(args, "profile_id", None)}
    if action == "show-results":
        from .cli import read_json_file
        inputs = [Path(path).expanduser().resolve() for path in (args.snapshot, args.ids_file)]
        for destination in (output, html_output):
            if destination is not None and any(destination == source or (
                    destination.exists() and source.exists() and destination.samefile(source)) for source in inputs):
                raise PhotographyError("INVALID_ARGUMENT", "Keep candidate and selection files unchanged; export to a different path.")
        result = management.select_search_results(read_json_file(args.snapshot), read_json_file(args.ids_file), store=store)
    elif action in ("create", "open"):
        result = management.album_info(**profile, view=action)
    elif action == "photos":
        result = management.photos(**profile, limit=args.limit, after=args.after)
    elif action == "photo":
        result = management.photo(args.photo_id, **profile)
    elif args.mode == "metadata":
        result = management.metadata_search(args.query, **profile,
                                            limit=args.limit if args.limit is not None else 100,
                                            after=args.after if args.after is not None else "")
    else:
        result = management.semantic_search(args.query, **profile, config=config,
                                            limit=args.limit if args.limit is not None else 10)
    if html_output:
        from .management_report import management_report

        result["html_output"] = management_report(result, html_output, config=config, store=store)
    if output:
        result["output"] = str(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
