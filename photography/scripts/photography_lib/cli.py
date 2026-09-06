from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .analyze import analyze
from .config import Config, PhotographyError
from .codex_vision import CodexConfig, CodexCLIProvider
from .ingest import ingest
from .report import analysis_report
from .sqlite_storage import SQLiteStorage
from .vision import AnalysisConfig, OpenAIResponsesProvider
from .status import analysis_status
from .thumbnails import stored_preview
from .exports import export_path


def parser():
    root = argparse.ArgumentParser(description="Smart Albums: photo ingestion, album management, visual analysis and saved result selection. Local indexing and new semantic queries are not yet available.")
    root.add_argument("--state-dir", help="State directory; defaults to PHOTOGRAPHY_STATE_DIR or ~/.photography-skill.")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("ingest", help="Incrementally scan a photo directory.")
    scan.add_argument("path", help="Absolute photo root path.")
    scan.add_argument("--album-name", help="Create/reuse this album and add successfully scanned photos; no model calls.")
    scan.add_argument("--thumbnail-size", type=int, default=1024)
    scan.add_argument("--thumbnail-quality", type=int, default=85)
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
    status = commands.add_parser("analysis-status", help="Read analysis status; no provider or credential checks.")
    scope = status.add_mutually_exclusive_group(required=True)
    scope.add_argument("--library-id")
    scope.add_argument("--album-id")
    status.add_argument("--status", choices=("never_analyzed", "saved", "cached", "needs_update"))
    status.add_argument("--limit", type=int, default=100)
    status.add_argument("--after", default="")
    status.add_argument("--provider", choices=("openai", "codex"))
    status.add_argument("--model")
    status.add_argument("--language", choices=("zh-CN", "en"), default="zh-CN")
    status.add_argument("--reasoning", default="low")
    scan_result = commands.add_parser("scan", help="Retrieve a saved scan summary.")
    scan_result.add_argument("scan_id")
    events = commands.add_parser("scan-events", help="Page through complete scan changes and errors.")
    events.add_argument("scan_id")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--changes-only", action="store_true")
    analysis = commands.add_parser("analyze", aliases=["analysis-plan"], help="Propose analysis; execution requires a separately confirmed plan.")
    analysis.add_argument("photo_ids", nargs="*")
    selection = analysis.add_mutually_exclusive_group()
    selection.add_argument("--ids-file", help="UTF-8 JSON array of photo IDs.")
    selection.add_argument("--library-id", help="Select photos from a library; defaults to a five-photo sample.")
    selection.add_argument("--album-id", help="Select photos from an album; defaults to a five-photo sample.")
    batch = analysis.add_mutually_exclusive_group()
    batch.add_argument("--limit", type=int, help="Maximum photos selected from the library (default 5).")
    batch.add_argument("--all", action="store_true", help="Explicitly select the entire library.")
    analysis.add_argument("--force", action="store_true")
    analysis.add_argument("--pending-only", action="store_true", help="Select only photos without matching saved results before applying the limit.")
    analysis.add_argument("--dry-run", action="store_true", help="Validate previews and inspect cache/configuration without model calls.")
    analysis.add_argument("--model", help="Override PHOTOGRAPHY_MODEL.")
    analysis.add_argument("--provider", choices=("openai", "codex"))
    analysis.add_argument("--reasoning", choices=("low", "medium", "high", "xhigh"))
    analysis.add_argument("--language", choices=("zh-CN", "en"))
    analysis.add_argument("--timeout", type=float, help="Per-attempt timeout; OpenAI 60s, Codex 240s by default.")
    analysis.add_argument("--retries", type=int)
    from .workflow_cli import add_commands
    add_commands(commands, analysis)
    record = commands.add_parser("analysis", help="Read a saved structured analysis.")
    record.add_argument("analysis_id")
    history = commands.add_parser("analyses", help="Page through a photo's analysis history, oldest first.")
    history.add_argument("photo_id")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--after", type=int, default=0)
    run = commands.add_parser("analysis-run", help="Read a saved analysis batch and per-photo results.")
    run.add_argument("run_id")
    report = commands.add_parser("analysis-report", help="Export a browsable HTML snapshot of saved analysis results.")
    scope = report.add_mutually_exclusive_group(required=True)
    scope.add_argument("--library-id")
    scope.add_argument("--album-id")
    report.add_argument("--output", required=True)
    report.add_argument("--model")
    report.add_argument("--provider", choices=("openai", "codex"), help="Optional cache profile filter; otherwise show saved results across models.")
    report.add_argument("--reasoning", choices=("low", "medium", "high", "xhigh"), default="low")
    report.add_argument("--language", choices=("zh-CN", "en"), default="zh-CN")
    selection = commands.add_parser("search-add", help="Add selected IDs from a saved search JSON to an album; no new search.")
    selection.add_argument("result_file")
    selection.add_argument("--album-name", required=True)
    selection.add_argument("photo_ids", nargs="*")
    selection.add_argument("--ids-file", help="JSON array exported from the search page.")
    return root


def profile_config(args):
    if args.provider == "codex":
        return CodexConfig(model=args.model or CodexConfig.model, language=args.language, reasoning=args.reasoning)
    return AnalysisConfig.from_env(model=args.model, language=args.language) if args.provider or args.model else None


def scoped_photos(args, store):
    if args.album_id:
        return store.photos_for_album(args.album_id)
    store.photos(args.library_id, 1)
    return store.photos_for_library(args.library_id)


def selected_ids(args, store, analysis_config=None):
    if args.photo_ids and (args.ids_file or args.library_id or args.album_id):
        raise PhotographyError("INVALID_ARGUMENT", "Use explicit IDs, --ids-file or --library-id, not multiple selections.")
    if (args.all or args.limit is not None or args.pending_only) and not (args.library_id or args.album_id):
        raise PhotographyError("INVALID_ARGUMENT", "Batch selection options require a library or album.")
    if args.ids_file:
        try:
            return json.loads(Path(args.ids_file).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            raise PhotographyError("INVALID_ARGUMENT", "IDs file must be a readable UTF-8 JSON array.") from None
    if args.library_id or args.album_id:
        limit = args.limit if args.limit is not None else 5
        if limit < 1:
            raise PhotographyError("INVALID_ARGUMENT", "Photo limit must be positive.")
        photos = sorted(scoped_photos(args, store), key=lambda item: (item["relative_path"].casefold(), item["photo_id"]))
        if args.pending_only:
            if args.force:
                raise PhotographyError("INVALID_ARGUMENT", "--pending-only and --force cannot be combined.")
            states = analysis_status(photos, store, analysis_config)["items"]
            pending = {e["photo_id"] for e in states if e["status"] != "cached" or e["source_status"] != "available" or e["preview_error"]}
            # A metadata cache match must not hide a corrupted BLOB from preflight.
            for photo in photos:
                if photo["photo_id"] not in pending:
                    try:
                        stored_preview(photo, store)
                    except PhotographyError:
                        pending.add(photo["photo_id"])
            photos = [p for p in photos if p["photo_id"] in pending]
        return [item["photo_id"] for item in (photos if args.all else photos[:limit])]
    return args.photo_ids


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = Config.from_env(args.state_dir,
                                 thumbnail_size=getattr(args, "thumbnail_size", 1024),
                                 thumbnail_quality=getattr(args, "thumbnail_quality", 85))
        if args.command == "ingest":
            result = ingest(args.path, config=config, album_name=args.album_name)
        else:
            with SQLiteStorage(config.state_dir) as store:
                if args.command == "search-add":
                    result = search_selection_command(args, store)
                elif args.command in ("analyze", "analysis-plan", "analysis-config", "analysis-confirm", "analysis-execute", "analysis-resume", "analysis-job", "analysis-collect", "analysis-cancel", "analysis-cleanup", "analysis-recover"):
                    from .workflow_cli import command
                    result = command(args, store, config)
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
                elif args.command == "analysis-status":
                    store._limit(args.limit)
                    result = analysis_status(scoped_photos(args, store), store, profile_config(args))
                    matches = sorted((e for e in result["items"] if e["photo_id"] > args.after
                                      and (args.status is None or e["status"] == args.status)), key=lambda e: e["photo_id"])
                    result["items"] = matches[:args.limit]
                    result["next_cursor"] = matches[args.limit - 1]["photo_id"] if len(matches) > args.limit else None
                    result["summary_scope"] = "entire_selected_album_or_library"
                elif args.command == "scan":
                    result = store.scan(args.scan_id)
                elif args.command == "scan-events":
                    result = store.events(args.scan_id, args.limit, args.after, args.changes_only)
                elif args.command == "analysis":
                    result = store.analysis(args.analysis_id)
                elif args.command == "analyses":
                    result = store.analyses(args.photo_id, args.limit, args.after)
                elif args.command == "analysis-run":
                    result = store.analysis_run(args.run_id)
                elif args.command == "analysis-report":
                    result = analysis_report(args.library_id, args.output, config=config, store=store,
                                             analysis_config=profile_config(args), album_id=args.album_id)
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
