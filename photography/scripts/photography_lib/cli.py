"""Three capabilities operating on an explicitly selected SQLite album file."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .config import Config, PhotographyError
from .sqlite_storage import SQLiteStorage


def parser():
    root = argparse.ArgumentParser(description="Smart Albums: one SQLite file per album. Select or create an album before ingestion, image indexing or management.")
    root.add_argument("--database", required=True, help="Absolute .sqlite/.sqlite3/.db album file; never chosen implicitly.")
    root.add_argument("--model-cache-dir", help="Independent machine-local model cache root.")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("ingestion", help="Import originals and proportional previews into an existing album.")
    scan.add_argument("path", help="Absolute original photo directory.")
    scan.add_argument("--thumbnail-size", type=int, default=1024)
    scan.add_argument("--thumbnail-quality", type=int, default=85)
    from .index_cli import add_commands as add_index
    from .management_cli import add_commands as add_management
    add_index(commands)
    add_management(commands)
    return root


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        config = Config.from_env(args.database, args.model_cache_dir,
                                 thumbnail_size=getattr(args, "thumbnail_size", 1024),
                                 thumbnail_quality=getattr(args, "thumbnail_quality", 85))
        if args.command == "ingestion":
            from .ingest import ingestion
            result = ingestion(args.path, config=config)
        else:
            create = args.command == "management" and args.management_command == "create"
            writable = args.command == "index" and (
                args.index_command in ("setup", "configure", "execute", "resume", "register-profile", "rebuild-fts")
                or args.index_command in ("plan", "prototypes", "compare") and not args.dry_run)
            if args.command == "management" and args.management_command == "folders":
                from .management_cli import FOLDER_WRITES

                writable = args.folder_command in FOLDER_WRITES
            context = SQLiteStorage.create(config.database_path) if create else \
                SQLiteStorage.open(config.database_path, writable=writable)
            with context as store:
                if args.command == "index":
                    from .index_cli import command
                else:
                    from .management_cli import command
                result = command(args, store, config)
                if str(result.get("schema", "")).startswith("unified-search-"):
                    result.setdefault("album", {"id": store.album()["id"]})
                else:
                    result.setdefault("album", store.album())
        compact = str(result.get("schema", "")).startswith("unified-search-")
        print(json.dumps(result, ensure_ascii=False, indent=None if compact else 2,
                         separators=(",", ":") if compact else None, allow_nan=False))
        if result.get("interrupted"):
            return 130
        if result.get("status") == "blocked":
            return 2
        return 1 if result.get("status") in ("partial", "failed") else 0
    except PhotographyError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 130 if exc.code == "SCAN_INTERRUPTED" else 2
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
