"""Explicit image-embedding operations for the selected album."""
from .config import PhotographyError
from .source_paths import absolute_candidate, relative_candidate


def add_commands(commands):
    root = commands.add_parser("index", help="Explicit image embeddings and opt-in local feature components.")
    actions = root.add_subparsers(dest="index_command", required=True)
    setup = actions.add_parser("setup", help="Explicitly install/verify model files and register their profile.")
    profiles = actions.add_parser("profiles", help="List this album's profiles for the selected component.")
    configure = actions.add_parser("configure", help="Explicitly select this album's default image/text profile.")
    configure.add_argument("--default-profile", required=True)
    plan = actions.add_parser("plan", help="Prepare a frozen scope; no model inference.")
    scope = plan.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Explicitly select every photo in this album.")
    scope.add_argument("--ids-file")
    plan.add_argument("--profile-id")
    plan.add_argument("--limit", type=int)
    plan.add_argument("--dry-run", action="store_true")
    status = actions.add_parser("status", help="Inspect image-embedding coverage without model loading.")
    status.add_argument("--profile-id")
    status.add_argument("--limit", type=int, default=100)
    status.add_argument("--after", default="")
    status.add_argument("--status", choices=("ready", "missing", "stale", "invalid_input", "invalid_vector",
                                           "invalid_result", "dependency_missing"))
    job = actions.add_parser("job", help="Inspect this album's saved embedding plan and attempts.")
    job.add_argument("run_id")
    execute = actions.add_parser("execute", help="Execute an explicitly approved plan.")
    execute.add_argument("run_id")
    execute.add_argument("--confirm", required=True)
    execute.add_argument("--worker-python", help="Isolated interpreter for a feature run.")
    resume = actions.add_parser("resume", help="Resume the existing approved scope.")
    resume.add_argument("run_id")
    resume.add_argument("--confirm-stopped", action="store_true",
                        help="Confirm a previously running worker, including on another device, has stopped.")
    resume.add_argument("--worker-python", help="Isolated interpreter for a feature run.")
    from .feature_cli import add_commands as add_features

    add_features(actions, {"setup": setup, "profiles": profiles, "configure": configure, "plan": plan, "status": status})


def model_directory(store, config):
    from .image_embedding_profiles import default_model_dir
    directory = default_model_dir(config.model_cache_root).resolve()
    if config.database_path == directory or config.database_path.is_relative_to(directory):
        raise PhotographyError("INVALID_ARGUMENT", "The model installation must not contain the album file.")
    for photo in store.photos():
        for candidate in (absolute_candidate(photo["original_absolute_path"]),
                          relative_candidate(photo["original_relative_path"], config.database_path)):
            if candidate is not None and (candidate == directory or candidate.is_relative_to(directory)):
                raise PhotographyError("INVALID_ARGUMENT", "The model installation must not contain original photo files.")
    return directory


def command(args, store, config):
    from .feature_cli import command as feature_command, handles

    if handles(args):
        return feature_command(args, store, config)
    if (getattr(args, "worker_python", None) is not None
            or getattr(args, "dependency_profile_id", None) is not None):
        raise PhotographyError("INVALID_ARGUMENT", "Feature worker/dependency options do not apply to image embeddings.")
    if args.index_command == "status" and args.status in ("invalid_result", "dependency_missing"):
        raise PhotographyError("INVALID_ARGUMENT", "This status applies to feature components, not image embeddings.")
    from .image_embedding import create_plan, execute_plan, embedding_status, job, resolve_profile
    action = args.index_command
    if action == "profiles":
        return {"component": "image_embedding", "profiles": store.embedding_profiles(),
                "default_profile_id": store.default_embedding_profile(), "model_calls": 0}
    if action == "configure":
        with store.transaction():
            store.set_default_embedding_profile(args.default_profile)
        return {"component": "image_embedding", "default_profile_id": args.default_profile, "model_calls": 0}
    if action == "job":
        return job(store, args.run_id)
    if action == "status":
        store._limit(args.limit)
        with store.read_snapshot():
            profile = resolve_profile(store, args.profile_id)
            result = embedding_status(store.photos(), store, profile)
        matches = [item for item in result["items"] if item["photo_id"] > args.after
                   and (args.status is None or item["status"] == args.status)]
        result["items"] = matches[:args.limit]
        result["next_cursor"] = matches[args.limit - 1]["photo_id"] if len(matches) > args.limit else None
        return result
    from .siglip_embedding import SiglipEncoder, setup_model
    directory = model_directory(store, config)
    if action == "setup":
        # Fail read-only/locked database writes before spending time downloading weights.
        with store.transaction():
            pass
        result = setup_model(directory)
        with store.transaction():
            profile_id = store.put_embedding_profile(result["profile"])
        return {**result, "component": "image_embedding", "profile_id": profile_id,
                "default_profile_id": store.default_embedding_profile(), "model_calls": 0}
    if action == "plan":
        if args.limit is not None and args.limit < 1:
            raise PhotographyError("INVALID_ARGUMENT", "Index limit must be positive.")
        profile = resolve_profile(store, args.profile_id)
        if args.ids_file:
            from .cli import read_json_file
            ids = read_json_file(args.ids_file)
        else:
            ids = [photo["photo_id"] for photo in store.photos()]
        if not isinstance(ids, list) or any(not isinstance(pid, str) or not pid.strip() for pid in ids):
            raise PhotographyError("INVALID_ARGUMENT", "Provide an array of nonempty photo IDs.")
        ids = list(dict.fromkeys(ids))
        if args.limit is not None:
            ids = ids[:args.limit]
        if not ids:
            raise PhotographyError("NO_PHOTOS_SELECTED", "There are no photos in the selected indexing scope.")
        encoder = SiglipEncoder(directory, profile=profile)
        return create_plan(ids, store=store, config=config, profile=profile, persist=not args.dry_run,
                           check_ready=encoder.check_ready)
    saved = job(store, args.run_id)
    encoder = SiglipEncoder(directory, profile=saved["profile"])
    result = execute_plan(args.run_id, store=store, config=config, encoder=encoder,
                          confirm=getattr(args, "confirm", None), resume=action == "resume",
                          confirm_stopped=getattr(args, "confirm_stopped", False))
    return {**result, "model_load_seconds": getattr(encoder, "load_seconds", None)}
