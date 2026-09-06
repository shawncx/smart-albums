"""Explicit image-index setup, configuration and confirmed execution."""
from pathlib import Path

from .fingerprints import fingerprint
from .config import PhotographyError


def add_commands(commands):
    root = commands.add_parser("index", help="Versioned local image embeddings; installation and execution are explicit.")
    actions = root.add_subparsers(dest="index_command", required=True)
    setup = actions.add_parser("setup", help="Explicitly download and verify the pinned image model.")
    setup.add_argument("--model-dir")
    actions.add_parser("profiles", help="List registered profiles without loading models.")
    configure = actions.add_parser("configure", help="Explicitly select the default index and query profile.")
    configure.add_argument("--default-profile", required=True)
    plan = actions.add_parser("plan", help="Prepare a reviewed scope; never perform inference.")
    scope = plan.add_mutually_exclusive_group(required=True)
    scope.add_argument("--album-id")
    scope.add_argument("--library-id")
    scope.add_argument("--ids-file")
    plan.add_argument("--profile-id")
    plan.add_argument("--model-dir")
    plan.add_argument("--limit", type=int)
    plan.add_argument("--dry-run", action="store_true")
    status = actions.add_parser("status", help="Inspect current input/vector metadata without loading a model.")
    scope = status.add_mutually_exclusive_group()
    scope.add_argument("--album-id")
    scope.add_argument("--library-id")
    status.add_argument("--profile-id")
    status.add_argument("--limit", type=int, default=100)
    status.add_argument("--after", default="")
    status.add_argument("--status", choices=("ready", "missing", "stale", "invalid_input", "invalid_vector"))
    job = actions.add_parser("job", help="Inspect a saved index plan and its progress.")
    job.add_argument("run_id")
    for action in ("execute", "resume"):
        cmd = actions.add_parser(action, help="Execute confirmed work without repeating valid results.")
        cmd.add_argument("run_id")
        cmd.add_argument("--model-dir")
        if action == "execute":
            cmd.add_argument("--confirm", required=True)


def model_directory(value, store, config):
    from .index_profiles import default_model_dir
    path = Path(value).expanduser().resolve() if value else default_model_dir(config.state_dir).resolve()
    if config.state_dir == path or config.state_dir.is_relative_to(path):
        raise PhotographyError("INVALID_ARGUMENT", "Model files must use a dedicated directory, not the state directory or its parent.")
    for library in store.libraries():
        source = Path(library["root_path"]).resolve()
        if path.is_relative_to(source) or source.is_relative_to(path):
            raise PhotographyError("INVALID_ARGUMENT", "Model and original photo directories must not overlap.")
    return path


def command(args, store, config):
    from .indexing import create_plan, execute_plan, index_status, job, resolve_profile
    action = args.index_command
    if action == "profiles":
        return {"profiles": store.index_profiles(), "default_profile_id": store.default_index_profile(),
                "model_calls": 0}
    if action == "configure":
        with store.transaction():
            store.set_default_index_profile(args.default_profile)
        return {"default_profile_id": args.default_profile, "model_calls": 0}
    if action == "job":
        return job(store, args.run_id)
    if action == "status":
        store._limit(args.limit)
        with store.read_snapshot():
            profile = resolve_profile(store, args.profile_id)
            photos = store.index_photos(album_id=args.album_id, library_id=args.library_id)
            result = index_status(photos, store, profile)
        matches = sorted((item for item in result["items"]
                          if item["photo_id"] > args.after and (not args.status or item["status"] == args.status)),
                         key=lambda item: item["photo_id"])
        result["items"] = matches[:args.limit]
        result["next_cursor"] = matches[args.limit - 1]["photo_id"] if len(matches) > args.limit else None
        return result
    from .siglip_embedding import SiglipEncoder, setup_model
    directory = model_directory(args.model_dir, store, config)
    if action == "setup":
        result = setup_model(directory)
        with store.transaction():
            profile_id = store.put_index_profile(result["profile"])
        return {**result, "profile_id": profile_id, "default_profile_id": store.default_index_profile(),
                "model_calls": 0}
    if action == "plan":
        if args.limit is not None and args.limit < 1:
            raise PhotographyError("INVALID_ARGUMENT", "Index limit must be positive.")
        profile = resolve_profile(store, args.profile_id)
        if args.ids_file:
            from .cli import read_json_file
            ids = read_json_file(args.ids_file)
        else:
            ids = [p["photo_id"] for p in sorted(
                store.index_photos(album_id=args.album_id, library_id=args.library_id),
                key=lambda p: (p["relative_path"].casefold(), p["photo_id"]))]
        if not isinstance(ids, list) or any(not isinstance(pid, str) or not pid.strip() for pid in ids):
            raise PhotographyError("INVALID_ARGUMENT", "Photo IDs must be a JSON array of non-empty strings.")
        ids = list(dict.fromkeys(ids))
        if args.limit is not None:
            ids = ids[:args.limit]
        encoder = SiglipEncoder(directory, profile=profile)
        return create_plan(ids, store=store, config=config, profile=profile,
                           persist=not args.dry_run, check_ready=encoder.check_ready)
    if action in ("execute", "resume"):
        saved = job(store, args.run_id)
        encoder = SiglipEncoder(directory, profile=saved["profile"])
        if fingerprint(encoder.profile()) != fingerprint(saved["profile"]):
            raise PhotographyError("INDEX_PROFILE_CHANGED", "Executor does not match the saved profile.")
        result = execute_plan(args.run_id, store=store, config=config, encoder=encoder,
                              confirm=getattr(args, "confirm", None), resume=action == "resume")
        result["model_load_seconds"] = getattr(encoder, "load_seconds", None)
        return result
    raise PhotographyError("INVALID_ARGUMENT", "Unknown index operation.")
