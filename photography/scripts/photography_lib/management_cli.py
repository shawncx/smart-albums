"""Argument handling for the read-only management command tree."""
from __future__ import annotations

import json

from . import management
from .config import PhotographyError
from .exports import export_path


def _scope(parser):
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--album-id")
    scope.add_argument("--library-id")


def _view_options(parser, *, page=True):
    parser.add_argument("--profile-id", help="Index profile to inspect; otherwise use the saved default, if any.")
    parser.add_argument("--output", help="Export a UTF-8 JSON snapshot outside state and source directories.")
    parser.add_argument("--html", help="Export a standalone, read-only HTML snapshot.")
    if page:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after", default="")


def add_commands(commands):
    root = commands.add_parser("management", help="Read-only album/photo browsing and explicit metadata or semantic search.")
    actions = root.add_subparsers(dest="management_command", required=True)
    _view_options(actions.add_parser("albums", help="List albums in stable ID order."))
    detail = actions.add_parser("album", help="Show an album and a page of its photos.")
    detail.add_argument("album_id")
    _view_options(detail)
    listing = actions.add_parser("photos", help="Browse a photo scope, or all stored photos.")
    _scope(listing)
    _view_options(listing)
    single = actions.add_parser("photo", help="Inspect one stored photo without reading its original.")
    single.add_argument("photo_id")
    _view_options(single, page=False)
    search = actions.add_parser("search", help="Literal Unicode metadata matching or exact image-index retrieval.")
    search.add_argument("query")
    search.add_argument("--mode", choices=("metadata", "semantic"), required=True)
    search.add_argument("--target", choices=("albums", "photos"))
    search.add_argument("--model-dir", help="Local model installation directory; semantic search only, never downloads.")
    _scope(search)
    _view_options(search, page=False)
    search.add_argument("--limit", type=int, help="1–1000; metadata default 100, semantic default 10.")
    search.add_argument("--after", help="Stable ID cursor; only valid for metadata search.")


def command(args, store, config):
    action = args.management_command
    profile = {"store": store, "profile_id": args.profile_id}
    if action == "search":
        if args.mode == "metadata" and args.target is None:
            raise PhotographyError("INVALID_ARGUMENT", "Metadata search requires --target albums or photos.")
        if args.mode == "metadata" and args.model_dir is not None:
            raise PhotographyError("INVALID_ARGUMENT", "--model-dir is only valid for semantic search.")
        if args.mode == "semantic" and args.target not in (None, "photos"):
            raise PhotographyError("INVALID_ARGUMENT", "Semantic search supports only --target photos.")
        if args.mode == "semantic" and args.after is not None:
            raise PhotographyError("INVALID_ARGUMENT", "Semantic search does not accept --after.")

    # Validate every output before any query encoding, preview decoding, or filesystem writes.
    output = export_path(args.output, config, store, (".json",)) if args.output else None
    html_output = export_path(args.html, config, store, (".html",)) if args.html else None
    if action == "albums":
        result = management.albums(**profile, limit=args.limit, after=args.after)
    elif action == "album":
        result = management.album(args.album_id, **profile, limit=args.limit, after=args.after)
    elif action == "photos":
        result = management.photos(**profile, album_id=args.album_id, library_id=args.library_id,
                                   limit=args.limit, after=args.after)
    elif action == "photo":
        result = management.photo(args.photo_id, **profile)
    elif action == "search":
        scope = {"album_id": args.album_id, "library_id": args.library_id}
        if args.mode == "metadata":
            result = management.metadata_search(args.query, **profile, **scope, target=args.target,
                                                limit=args.limit if args.limit is not None else 100,
                                                after=args.after if args.after is not None else "")
        else:
            result = management.semantic_search(args.query, **profile, **scope, config=config,
                                                limit=args.limit if args.limit is not None else 10,
                                                model_dir=args.model_dir)
    else:
        raise PhotographyError("INVALID_ARGUMENT", "Unknown management operation.")
    if html_output:
        from .management_report import management_report

        result["html_output"] = management_report(result, html_output, config=config, store=store)
    if output:
        result["output"] = str(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
