"""Opt-in stage-one indexing commands; no new management search behavior."""
from __future__ import annotations

from .config import PhotographyError
from .feature_profiles import COMPONENTS, default_profile, profile_identity


SPECIAL_COMMANDS = frozenset(("result", "result-history", "register-profile", "prototypes", "compare", "pairs", "rebuild-fts"))


def add_commands(actions, shared):
    for name, parser in shared.items():
        parser.add_argument("--component", choices=("image_embedding", *COMPONENTS), default="image_embedding")
        if name in ("setup", "plan"):
            parser.add_argument("--worker-python", help="Absolute isolated worker interpreter; otherwise SMART_ALBUMS_FEATURE_PYTHON.")
    shared["setup"].add_argument("--dependency-profile-id", help="Explicit objects or image-embedding profile for a derived component.")
    registration = actions.add_parser("register-profile", help="Register an explicit immutable feature profile JSON, not a default.")
    registration.add_argument("profile_file")
    for name in ("result", "result-history"):
        result = actions.add_parser(name, help="Read a current feature result or its saved history; no inference.")
        result.add_argument("photo_id")
        result.add_argument("--component", required=True, choices=COMPONENTS)
        result.add_argument("--profile-id")
        if name == "result":
            result.add_argument("--result-id", help="Read this exact historical result with independent detail pagination.")
        result.add_argument("--details", action="store_true", help="Return typed details, including explicitly requested OCR blocks.")
        result.add_argument("--limit", type=int, default=100)
        result.add_argument("--after", type=int if name == "result" else str, default=0 if name == "result" else "")
    prototypes = actions.add_parser("prototypes", help="Plan scene text-prototype generation; execute its returned run with confirmation.")
    prototypes.add_argument("--profile-id")
    prototypes.add_argument("--dry-run", action="store_true")
    compare = actions.add_parser("compare", help="Plan exact-byte or perceptual-hash comparisons; does not execute them.")
    scope = compare.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--ids-file")
    compare.add_argument("--profile-id")
    compare.add_argument("--metric", choices=("hamming", "exact"), default="hamming")
    compare.add_argument("--max-distance", type=int, default=8)
    compare.add_argument("--dry-run", action="store_true")
    pairs = actions.add_parser("pairs", help="Page through saved comparison evidence, never automatically delete photos.")
    pairs.add_argument("run_id")
    pairs.add_argument("--limit", type=int, default=100)
    pairs.add_argument("--after", type=int, default=0)
    rebuild = actions.add_parser("rebuild-fts", help="Explicitly rebuild the OCR text index from saved result documents.")
    rebuild.add_argument("--confirm", action="store_true", required=True)


def handles(args):
    return (args.index_command in SPECIAL_COMMANDS
            or getattr(args, "component", "image_embedding") != "image_embedding"
            or getattr(args, "run_id", "").startswith("feature_"))


def _selected_ids(args, store):
    if args.ids_file is not None:
        from .cli import read_json_file

        ids = read_json_file(args.ids_file)
    else:
        ids = [photo["photo_id"] for photo in store.photos()]
    if (not isinstance(ids, list) or any(not isinstance(pid, str) or not pid.strip() for pid in ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Provide a JSON array of real photo IDs.")
    selected = list(dict.fromkeys(ids))
    limit = getattr(args, "limit", None)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise PhotographyError("INVALID_ARGUMENT", "Index limit must be positive.")
        selected = selected[:limit]
    return selected


def _check_dependencies(profile, store):
    component = profile["component"]
    if component == "scene":
        store.embedding_profile(profile["dependencies"]["image_embedding"])
    if component == "composition":
        source = store.feature_profile(profile["dependencies"]["objects"])
        if source["component"] != "objects":
            raise PhotographyError("FEATURE_PROFILE_MISMATCH", "Composition requires an objects source profile.")


def command(args, store, config):
    from . import feature_index

    action = args.index_command
    component = getattr(args, "component", None)
    if action == "register-profile":
        from .cli import read_json_file

        profile = read_json_file(args.profile_file)
        profile_identity(profile)
        with store.transaction():
            _check_dependencies(profile, store)
            profile_id = store.put_feature_profile(profile)
        return {"component": profile["component"], "profile_id": profile_id, "profile": profile,
                "default_profile_id": store.default_feature_profile(profile["component"]), "model_calls": 0}
    if action == "setup":
        if component in ("scene", "composition"):
            if args.dependency_profile_id is None:
                raise PhotographyError("INVALID_ARGUMENT", "Explicitly select --dependency-profile-id for this component.")
        elif args.dependency_profile_id is not None:
            raise PhotographyError("INVALID_ARGUMENT", "This component has no upstream profile dependency.")
        with store.transaction():
            pass
        if component in ("ocr", "objects"):
            from .feature_models import setup_component

            setup = setup_component(component, config=config, python_path=args.worker_python)
            profile = setup["profile"]
        else:
            profile = default_profile(component, dependency_profile_id=args.dependency_profile_id)
            setup = {"status": "ready", "profile": profile, "model_calls": 0}
        with store.transaction():
            _check_dependencies(profile, store)
            profile_id = store.put_feature_profile(profile)
        return {**setup, "component": component, "profile_id": profile_id,
                "default_profile_id": store.default_feature_profile(component)}
    if action == "profiles":
        return {"component": component, "profiles": store.feature_profiles(component),
                "default_profile_id": store.default_feature_profile(component), "model_calls": 0}
    if action == "configure":
        with store.transaction():
            store.set_default_feature_profile(component, args.default_profile)
        return {"component": component, "default_profile_id": args.default_profile, "model_calls": 0}
    if action in ("job", "pairs"):
        saved = feature_index.job(store, args.run_id)
        if action == "pairs":
            if saved["work_kind"] != "compare":
                raise PhotographyError("INVALID_ARGUMENT", "Pairs requires a comparison run.")
            return {"album": store.album(), "run_id": args.run_id, "status": saved["status"],
                    "options": saved["options"], **store.similarity_pairs(args.run_id, limit=args.limit, after=args.after),
                    "model_calls": 0, "historical": True}
        return saved
    if action in ("execute", "resume"):
        return feature_index.execute_plan(args.run_id, store=store, config=config,
                                           confirm=getattr(args, "confirm", None), resume=action == "resume",
                                           confirm_stopped=getattr(args, "confirm_stopped", False),
                                           python_path=getattr(args, "worker_python", None))
    if action == "rebuild-fts":
        return {"album": store.album(), **store.rebuild_feature_fts(), "model_calls": 0}
    if action == "result" and args.result_id is not None:
        with store.read_snapshot():
            record = store.feature_result(args.result_id)
            if (record["photo_id"] != args.photo_id or record["component"] != component
                    or args.profile_id is not None and args.profile_id != record["profile_id"]):
                raise PhotographyError("FEATURE_RESULT_MISMATCH", "Historical result does not match the requested photo/component/profile.")
            return {"album": store.album(), "component": component, "photo_id": args.photo_id,
                    "profile_id": record["profile_id"], "historical": True,
                    "result": feature_index.result_summary(record, details=args.details, limit=args.limit, after=args.after),
                    "model_calls": 0, "original_verification": "not_checked"}
    if action == "prototypes":
        component = "scene"
    elif action == "compare":
        component = "perceptual_hash"
    profile = feature_index.resolve_profile(component, store=store, profile_id=getattr(args, "profile_id", None))
    if action == "plan":
        return feature_index.create_plan(_selected_ids(args, store), store=store, config=config, profile=profile,
                                         python_path=args.worker_python, persist=not args.dry_run)
    if action == "prototypes":
        return feature_index.create_prototype_plan(store=store, config=config, profile=profile, persist=not args.dry_run)
    if action == "compare":
        return feature_index.create_compare_plan(_selected_ids(args, store), store=store, config=config,
                                                 profile=profile, metric=args.metric, max_distance=args.max_distance,
                                                 persist=not args.dry_run)
    if action == "status":
        return feature_index.status(store=store, profile=profile, limit=args.limit, after=args.after,
                                    status_filter=args.status)
    if action == "result":
        return feature_index.current_result(args.photo_id, store=store, profile=profile,
                                            details=args.details, limit=args.limit, after=args.after)
    if action == "result-history":
        with store.read_snapshot():
            store.photo(args.photo_id)
            records = store.feature_history(args.photo_id, fingerprint_profile(profile), limit=args.limit, after=args.after)
            more = bool(records and len(records) == args.limit and store.feature_history(
                args.photo_id, fingerprint_profile(profile), limit=1, after=records[-1]["result_id"]))
            return {"album": store.album(), "photo_id": args.photo_id, "component": component,
                    "profile_id": fingerprint_profile(profile),
                    "items": [feature_index.result_summary(record, details=args.details) for record in records],
                    "next_cursor": records[-1]["result_id"] if more else None,
                    "historical": True, "model_calls": 0}
    raise PhotographyError("INVALID_ARGUMENT", "Unknown feature operation.")


def fingerprint_profile(profile):
    return profile_identity(profile)[0]
