from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import Config, PhotographyError
from .ingest import ingest
from .sqlite_storage import SQLiteStorage
from .thumbnails import stored_preview
from .exports import export_path


def parser():
    root = argparse.ArgumentParser(description="Smart Albums: ingestion, local image index and read-only management. Legacy album and saved-selection commands remain available.")
    root.add_argument("--state-dir", help="State directory; defaults to PHOTOGRAPHY_STATE_DIR or ~/.photography-skill.")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("ingestion", aliases=["ingest"], help="Import photo records and proportional previews without model calls.")
    scan.add_argument("path", help="Absolute photo root path.")
    scan.add_argument("--album-name", help="Create/reuse this album and add successfully scanned photos; no model calls.")
    scan.add_argument("--thumbnail-size", type=int, default=1024)
    scan.add_argument("--thumbnail-quality", type=int, default=85)
    from .index_cli import add_commands as add_index_commands
    from .management_cli import add_commands as add_management_commands
    add_index_commands(commands)
    add_management_commands(commands)
    commands.add_parser("libraries", help="List indexed libraries and their counts.")
    photos = commands.add_parser("photos", help="Page through a library's indexed photos.")
    scope = photos.add_mutually_exclusive_group(required=True)
    scope.add_argument("--library-id")
    scope.add_argument("--album-id")
    photos.add_argument("--limit", type=int, default=100)
    photos.add_argument("--after", default="")
    photo = commands.add_parser("photo", help="Get one photo and stored thumbnail metadata (no image bytes).")
    photo.add_argument("photo_id")
    commands.add_parser("albums", help="List albums and member counts.")
    create = commands.add_parser("album-create", help="Create an album or reuse its unique name.")
    create.add_argument("name")
    rename = commands.add_parser("album-rename")
    rename.add_argument("album_id")
    rename.add_argument("name")
    for name in ("album-add", "album-remove"):
        members = commands.add_parser(name, help="Change album membership without changing photo records.")
        members.add_argument("album_id")
        members.add_argument("photo_ids", nargs="+")
    thumbnail = commands.add_parser("thumbnail", help="Export a stored JPEG without reading the original.")
    thumbnail.add_argument("photo_id")
    thumbnail.add_argument("--output", required=True)
    scan_result = commands.add_parser("scan", help="Retrieve a saved scan summary.")
    scan_result.add_argument("scan_id")
    events = commands.add_parser("scan-events", help="Page through complete scan changes and errors.")
    events.add_argument("scan_id")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--changes-only", action="store_true")
    selection = commands.add_parser("search-add", help="Add selected IDs from a saved search JSON to an album; no new search.")
    selection.add_argument("result_file")
    selection.add_argument("--album-name", required=True)
    selection.add_argument("photo_ids", nargs="*")
    selection.add_argument("--ids-file", help="JSON array exported from the search page.")
    return root


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = Config.from_env(args.state_dir,
                                 thumbnail_size=getattr(args, "thumbnail_size", 1024),
                                 thumbnail_quality=getattr(args, "thumbnail_quality", 85))
        if args.command in ("ingestion", "ingest"):
            result = ingest(args.path, config=config, album_name=args.album_name)
        else:
            with SQLiteStorage(config.state_dir) as store:
                if args.command == "index":
                    from .index_cli import command
                    result = command(args, store, config)
                elif args.command == "management":
                    from .management_cli import command
                    result = command(args, store, config)
                elif args.command == "search-add":
                    result = search_selection_command(args, store)
                elif args.command == "libraries":
                    result = {"libraries": store.libraries()}
                elif args.command == "albums":
                    result = {"albums": store.albums()}
                elif args.command == "album-create":
                    result = store.create_album(args.name)
                elif args.command == "album-rename":
                    result = store.rename_album(args.album_id, args.name)
                elif args.command in ("album-add", "album-remove"):
                    result = store.change_members(args.album_id, args.photo_ids, remove=args.command == "album-remove")
                elif args.command == "photos":
                    result = (store.album_photos(args.album_id, args.limit, args.after) if args.album_id else
                              store.photos(args.library_id, args.limit, args.after))
                elif args.command == "photo":
                    result = store.photo(args.photo_id)
                    try:
                        result["thumbnail"] = store.thumbnail(args.photo_id, include_data=False)
                    except PhotographyError as exc:
                        result["thumbnail_error"] = exc.to_dict()
                elif args.command == "thumbnail":
                    output = export_path(args.output, config, store, (".jpg", ".jpeg"))
                    data = stored_preview(store.photo(args.photo_id), store)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(data)
                    result = {"photo_id": args.photo_id, "output": str(output), "bytes": len(data)}
                elif args.command == "scan":
                    result = store.scan(args.scan_id)
                elif args.command == "scan-events":
                    result = store.events(args.scan_id, args.limit, args.after, args.changes_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("interrupted"):
            return 130
        if result.get("status") == "blocked":
            return 2
        return 1 if result.get("status") in ("partial", "failed") else 0
    except PhotographyError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": {"code": "STORAGE_UNAVAILABLE", "message": str(exc)}}, ensure_ascii=False, indent=2))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "INTERRUPTED", "message": "Operation interrupted."}}))
        return 130


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise PhotographyError("INVALID_ARGUMENT", "Expected a readable UTF-8 JSON file.") from None


def search_selection_command(args, store):
    from .search import add_search_selection
    if args.photo_ids and args.ids_file:
        raise PhotographyError("INVALID_ARGUMENT", "Use explicit photo IDs or --ids-file.")
    ids = read_json_file(args.ids_file) if args.ids_file else args.photo_ids
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
        raise PhotographyError("INVALID_ARGUMENT", "Selection must be an array of non-empty photo IDs.")
    return add_search_selection(read_json_file(args.result_file), ids, store=store, album_name=args.album_name)
