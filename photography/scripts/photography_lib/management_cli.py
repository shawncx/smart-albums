"""Management commands for the one explicitly selected album file."""
from __future__ import annotations

import json
from pathlib import Path

from . import management
from .config import PhotographyError
from .exports import prepare_export, write_export


def _view_options(parser, *, page=True):
    parser.add_argument("--profile-id", help="Image-embedding profile; otherwise inspect the saved default, if any.")
    parser.add_argument("--output", help="Export a UTF-8 JSON snapshot.")
    parser.add_argument("--html", help="Export a standalone, read-only HTML snapshot.")
    if page:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after", default="")


def _scope_options(parser):
    parser.add_argument("--folder-id", dest="folder_ids", action="append",
                        help="Restrict to a virtual folder; repeat to combine folders.")
    parser.add_argument("--folder-match", choices=("union", "intersection"),
                        help="Required when selecting multiple distinct folders.")


FOLDER_WRITES = frozenset(("create", "rename", "delete", "add", "remove", "apply-date-plan"))


def _folder_commands(actions):
    folders = actions.add_parser("folders", help="Manage static virtual folders without moving photos.")
    commands = folders.add_subparsers(dest="folder_command", required=True)
    listing = commands.add_parser("list", help="List folders and saved member counts.")
    listing.add_argument("--query", help="Literal folder-name substring.")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--after", default="")
    create = commands.add_parser("create", help="Create an empty custom folder.")
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="")
    show = commands.add_parser("show", help="Show a folder and its saved embedding coverage.")
    show.add_argument("folder_id")
    show.add_argument("--profile-id")
    rename = commands.add_parser("rename", help="Rename a folder without changing its ID or members.")
    rename.add_argument("folder_id")
    rename.add_argument("--name", required=True)
    delete = commands.add_parser("delete", help="Delete a folder and memberships, never its photos.")
    delete.add_argument("folder_id")
    for name in ("add", "remove"):
        change = commands.add_parser(name, help=f"{name.capitalize()} explicit photo memberships; [] is no change.")
        change.add_argument("folder_id")
        change.add_argument("--ids-file", required=True, help="JSON array of photo IDs; a single ID is also an array.")
        if name == "add":
            source = change.add_mutually_exclusive_group()
            source.add_argument("--search-snapshot", help="Validate IDs against saved semantic candidates; no new search.")
            source.add_argument("--query-snapshot", help="Validate IDs against finalized condition results; no new query.")
            source.add_argument("--review-snapshot", help="Selected unified-search snapshot; --ids-file contains review numbers.")
            change.add_argument("--review-id", help="Bind unified review numbers to their original review.")
    organize = commands.add_parser("organize-date", help="Prepare a read-only EXIF capture-date grouping plan.")
    source = organize.add_mutually_exclusive_group(required=True)
    source.add_argument("--all", dest="all_photos", action="store_true")
    source.add_argument("--ids-file")
    organize.add_argument("--granularity", required=True, choices=("year", "month", "day"))
    organize.add_argument("--output", required=True, help="Write the date plan as UTF-8 JSON.")
    apply = commands.add_parser("apply-date-plan", help="Apply exactly a confirmed date grouping plan.")
    apply.add_argument("plan")
    apply.add_argument("--confirm", required=True, help="Digest of the reviewed plan.")


def add_commands(root_subparsers):
    root = root_subparsers.add_parser("management", help="Manage the selected album, browse and search its saved photos.")
    actions = root.add_subparsers(dest="management_command", required=True)
    for name, help_text in (
        ("create", "Create a new album file without overwriting an existing file."),
        ("open", "Validate an existing album read-only and show its identity and coverage."),
    ):
        _view_options(actions.add_parser(name, help=help_text), page=False)
    photos = actions.add_parser("photos", help="Browse saved photos in stable ID order.")
    _view_options(photos)
    _scope_options(photos)
    single = actions.add_parser("photo", help="Inspect saved metadata without accessing the original.")
    single.add_argument("photo_id")
    _view_options(single, page=False)
    search = actions.add_parser("search", help="Unified saved-evidence search with compact numbered review.")
    search.add_argument("query", nargs="?")
    search.add_argument("--query-file", help="Explicit unified-search-query-v1 with semantic and structured conditions.")
    search.add_argument("--visual-query", help="English visual intent prepared from the user's request.")
    search.add_argument("--mode", choices=("unified", "metadata", "semantic"), default="unified",
                        help="Unified is the default; explicit metadata/semantic retain legacy snapshot contracts.")
    _view_options(search, page=False)
    _scope_options(search)
    search.add_argument("--limit", type=_search_limit,
                        help="Unified: positive count or all, default 100, no upper cap. Legacy modes: 1-1000.")
    search.add_argument("--after", help="Stable photo ID cursor; metadata search only.")
    selected = actions.add_parser("show-results", help="Display explicit IDs from a saved candidate snapshot; no new search or classification.")
    selected.add_argument("snapshot")
    selected.add_argument("--ids-file", required=True, help="JSON array selected by the agent/user; [] means no suitable candidates.")
    selected.add_argument("--output", help="Write a separate selected-results JSON snapshot.")
    selected.add_argument("--html", help="Write a report containing only the selected candidates.")
    selected.add_argument("--review-id", help="Required for numbered decisions from a unified-search snapshot.")
    evidence = actions.add_parser("search-evidence", help="Read compact numbered evidence from a saved unified search.")
    evidence.add_argument("snapshot")
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
    _folder_commands(actions)
    from .condition_cli import add_commands as add_conditions

    add_conditions(actions)


def _search_limit(value):
    import argparse

    if value == "all":
        return value
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a positive candidate count or all.") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive candidate count or all.")
    return number


def _protect_inputs(destinations, sources):
    inputs = [Path(path).expanduser().resolve() for path in sources]
    for destination in destinations:
        destination = Path(destination) if destination is not None else None
        if destination is not None and any(destination == source or (
                destination.exists() and source.exists() and destination.samefile(source)) for source in inputs):
            raise PhotographyError("INVALID_ARGUMENT", "Keep input files unchanged; export to a different path.")


def _write_json(result, output):
    data = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    write_export(output, data)


def _folder_command(args, store, config):
    from . import virtual_folders
    from .cli import read_json_file

    action = args.folder_command
    if action == "list":
        return virtual_folders.list_folders(store=store, query=args.query, limit=args.limit, after=args.after)
    if action == "create":
        return virtual_folders.create_folder(args.name, store=store, description=args.description)
    if action == "show":
        return virtual_folders.show_folder(args.folder_id, store=store, profile_id=args.profile_id)
    if action == "rename":
        return virtual_folders.rename_folder(args.folder_id, args.name, store=store)
    if action == "delete":
        return virtual_folders.delete_folder(args.folder_id, store=store)
    if action in ("add", "remove"):
        ids = read_json_file(args.ids_file)
        if action == "remove":
            return virtual_folders.remove_photos(args.folder_id, ids, store=store)
        snapshot = read_json_file(args.search_snapshot) if args.search_snapshot else None
        if args.search_snapshot is not None and not isinstance(snapshot, dict):
            raise PhotographyError("INVALID_ARGUMENT", "A supplied search snapshot must be a candidate JSON object.")
        query_snapshot = read_json_file(args.query_snapshot) if args.query_snapshot else None
        if args.query_snapshot is not None and not isinstance(query_snapshot, dict):
            raise PhotographyError("INVALID_ARGUMENT", "A supplied query snapshot must be a finalized JSON object.")
        review_snapshot = read_json_file(args.review_snapshot) if args.review_snapshot else None
        if args.review_snapshot is not None and not isinstance(review_snapshot, dict):
            raise PhotographyError("INVALID_ARGUMENT", "A supplied review snapshot must be a selected JSON object.")
        return virtual_folders.add_photos(args.folder_id, ids, store=store, search_snapshot=snapshot,
                                         query_snapshot=query_snapshot, review_snapshot=review_snapshot,
                                         review_id=args.review_id)
    if action == "organize-date":
        from .date_folders import plan_date_organization

        output = prepare_export(args.output, config, store, (".json",))
        _protect_inputs([output], [args.ids_file] if args.ids_file else [])
        ids = read_json_file(args.ids_file) if args.ids_file else None
        plan = plan_date_organization(store=store, photo_ids=ids, all_photos=args.all_photos,
                                      granularity=args.granularity)
        _write_json(plan, output)
        return {"album": plan["album"], "plan": plan, "digest": plan["digest"],
                "output": str(output), "model_calls": 0, "image_model_calls": 0}
    if action == "apply-date-plan":
        from .date_folders import apply_date_plan

        return apply_date_plan(read_json_file(args.plan), args.confirm, store=store)
    raise PhotographyError("INVALID_ARGUMENT", "Unknown virtual-folder operation.")


def _unified_search(args, store, config):
    from . import unified_search
    from .cli import read_json_file

    if not args.output:
        raise PhotographyError("INVALID_ARGUMENT", "Unified search requires --output for its private source snapshot.")
    if args.html or args.after is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Review numbered candidates before rendering; unified search has no --after.")
    if args.query_file:
        if (args.query is not None or args.visual_query is not None or args.profile_id is not None
                or args.folder_ids is not None or args.folder_match is not None):
            raise PhotographyError("INVALID_ARGUMENT", "Put query text, profiles and scope in --query-file; do not override them.")
        raw = read_json_file(args.query_file)
        if not isinstance(raw, dict):
            raise PhotographyError("INVALID_ARGUMENT", "Expected a unified-search query JSON object.")
    else:
        if args.query is None:
            raise PhotographyError("INVALID_ARGUMENT", "Provide a search query or --query-file.")
        condition = {"id": "semantic", "kind": "semantic", "query": args.query}
        if args.visual_query is not None:
            condition["visual_query"] = args.visual_query
        if args.profile_id is not None:
            condition["profile_id"] = args.profile_id
        raw = {"schema": unified_search.QUERY_SCHEMA, "query": args.query, "operator": "and",
               "conditions": [condition],
               "scope": {"folder_ids": args.folder_ids or [], "match": args.folder_match}}
    if args.limit is not None:
        raw = {**raw, "candidate_limit": args.limit}
    output = prepare_export(args.output, config, store, (".json",))
    _protect_inputs((output,), [args.query_file] if args.query_file else [])
    snapshot = unified_search.query(raw, store=store, config=config)
    evidence = unified_search.review(snapshot, store=store)
    _write_json(snapshot, output)
    return {**evidence, "model_calls": snapshot["query_model_calls"], "output": str(output)}


def _unified_selection(args, snapshot, store, config):
    from . import unified_search
    from .cli import read_json_file

    if args.review_id is None or not args.output:
        raise PhotographyError("INVALID_ARGUMENT", "Unified selection requires --review-id and a separate --output snapshot.")
    output = prepare_export(args.output, config, store, (".json",))
    html_output = prepare_export(args.html, config, store, (".html",)) if args.html else None
    _protect_inputs((output, html_output), (args.snapshot, args.ids_file))
    selected = unified_search.select(snapshot, read_json_file(args.ids_file), review_id=args.review_id, store=store)
    result = unified_search.summary(selected)
    if html_output:
        from .unified_report import unified_report

        result["html_output"] = unified_report(selected, html_output, config=config, store=store)
    _write_json(selected, output)
    return {**result, "output": str(output)}


def command(args, store, config):
    action = args.management_command
    if action in ("query", "query-evidence", "finalize-query", "show-query-results", "query-pairs"):
        from .condition_cli import command as condition_command

        return condition_command(args, store, config)
    if action == "folders":
        return _folder_command(args, store, config)
    if action == "search-evidence":
        from .cli import read_json_file
        from .unified_search import review

        return review(read_json_file(args.snapshot), store=store)
    if action == "search":
        if args.mode == "unified":
            return _unified_search(args, store, config)
        if args.query_file is not None or args.query is None:
            raise PhotographyError("INVALID_ARGUMENT", "Legacy search requires query text, not --query-file.")
        if args.limit == "all":
            raise PhotographyError("INVALID_ARGUMENT", "Legacy search accepts limits 1-1000; use unified search for all candidates.")
    if action == "show-results":
        from .cli import read_json_file
        from .unified_search import SNAPSHOT_SCHEMA

        snapshot = read_json_file(args.snapshot)
        if isinstance(snapshot, dict) and snapshot.get("schema") == SNAPSHOT_SCHEMA:
            return _unified_selection(args, snapshot, store, config)
        if args.review_id is not None:
            raise PhotographyError("INVALID_ARGUMENT", "--review-id applies only to unified-search numbered selections.")
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
    if action == "search" and args.mode != "semantic" and args.visual_query is not None:
        raise PhotographyError("INVALID_ARGUMENT", "--visual-query applies only to semantic search, never literal metadata.")

    # Reject every unsafe target before query encoding, preview decoding or any export writes.
    output = prepare_export(args.output, config, store, (".json",)) if args.output else None
    html_output = prepare_export(args.html, config, store, (".html",)) if args.html else None
    profile = {"store": store, "profile_id": getattr(args, "profile_id", None)}
    scope = {"folder_ids": getattr(args, "folder_ids", None), "folder_match": getattr(args, "folder_match", None)}
    if action == "show-results":
        from .cli import read_json_file
        _protect_inputs((output, html_output), (args.snapshot, args.ids_file))
        result = management.select_search_results(snapshot, read_json_file(args.ids_file), store=store)
    elif action in ("create", "open"):
        result = management.album_info(**profile, view=action)
    elif action == "photos":
        result = management.photos(**profile, **scope, limit=args.limit, after=args.after)
    elif action == "photo":
        result = management.photo(args.photo_id, **profile)
    elif args.mode == "metadata":
        result = management.metadata_search(args.query, **profile, **scope,
                                            limit=args.limit if args.limit is not None else 100,
                                            after=args.after if args.after is not None else "")
    else:
        result = management.semantic_search(args.query, **profile, **scope, config=config,
                                            visual_query=args.visual_query,
                                            limit=args.limit if args.limit is not None else 10)
    if html_output:
        from .management_report import management_report

        result["html_output"] = management_report(result, html_output, config=config, store=store)
    if output:
        result["output"] = str(output)
        _write_json(result, output)
    return result
