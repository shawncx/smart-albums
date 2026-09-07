from contextlib import closing
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "photography" / "scripts"))

from photography_lib.cli import parser
from photography_lib.feature_profiles import COMPONENTS
from photography_lib.image_embedding_storage import IMAGE_EMBEDDING_SCHEMA, IMAGE_EMBEDDING_TABLES
from photography_lib.image_feature_storage import (
    FEATURE_ALL_TABLES, FEATURE_FTS_SHADOW_TABLES, FEATURE_FTS_TABLE,
    IMAGE_FEATURE_SCHEMA, IMAGE_FEATURE_TABLES,
)
from photography_lib.sqlite_storage import SCHEMA, SCHEMA_VERSION
from photography_lib.virtual_folder_storage import VIRTUAL_FOLDER_SCHEMA


class SkillContractTests(unittest.TestCase):
    def test_skill_exposes_three_named_capabilities(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: smart-albums", text.split("---", 2)[1])
        rows = [line for line in text.splitlines() if line.startswith("|")]
        names = []
        for row in rows:
            first = row.split("|")[1].strip().strip("`*").lower()
            if first in ("ingestion", "index", "management"):
                names.append(first)
        self.assertCountEqual(names, ["ingestion", "index", "management"])
        self.assertNotIn("requirements-embedding.txt", text)
        self.assertNotIn("multilingual-e5", text.lower())

    def test_current_guides_do_not_restore_retired_runtime(self):
        for name in ("README.md", "photography/SKILL.md", "photography/references/index.md",
                     "photography/references/management.md", "docs/index-design.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(path=name):
                self.assertNotIn("pip install -r photography/requirements-embedding.txt", text)
                self.assertNotIn("intfloat/multilingual-e5-small", text)
                self.assertNotIn("Xenova/multilingual-e5-small", text)

    def test_local_markdown_links_exist(self):
        documents = [ROOT / "README.md", ROOT / "photography" / "SKILL.md"]
        documents += list((ROOT / "docs").glob("*.md"))
        documents += list((ROOT / "photography" / "references").glob("*.md"))
        for document in documents:
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                target = target.strip().strip("<>")
                if target.startswith(("http:", "https:", "mailto:", "#")):
                    continue
                target = unquote(target.split("#", 1)[0])
                if target:
                    with self.subTest(document=document.name, target=target):
                        self.assertTrue((document.parent / target).exists())

    def test_required_followups_are_explicit(self):
        text = (ROOT / "docs" / "TODO.md").read_text(encoding="utf-8")
        for term in ("NaFlex", "技术参数", "ONNX"):
            self.assertIn(term, text)
        self.assertFalse((ROOT / "photography" / "requirements-embedding.txt").exists())
        for name in ("embedding_model.py", "embedding_text.py", "embeddings.py"):
            self.assertFalse((ROOT / "photography" / "scripts" / "photography_lib" / name).exists())

    def test_ingestion_requires_explicit_index_invitation(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("## ingestion", 1)[1].split("## index", 1)[0]
        self.assertIn("index_prompt", section)
        self.assertIn("explicitly ask the user whether to create an index", section)
        self.assertIn("index_prompt.photo_ids", section)
        self.assertIn("configuration_required", section)
        self.assertIn("**Without an index:**", section)
        self.assertIn("**With a valid index and its compatible local model:**", section)
        self.assertIn("not guaranteed detections or exact filters", section)
        self.assertIn("do not ask the same intent question again", section)
        self.assertIn("wait for the user's answer", section)

    def test_skill_requires_explicit_album_lifecycle_without_legacy_fallback(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        heading = "## Select or create the SQLite album file first"
        self.assertLess(text.index(heading), text.index("## ingestion"))
        section = text.split(heading, 1)[1].split("## Runtime and safety", 1)[0]
        for expected in ("SQLite album file", "one album per SQLite file", "open an existing album", "create a new album",
                         "missing file is an error", "creation requires an explicit choice",
                         "clear the previous photo/profile/run selections", "no second selection of an internal album",
                         "--database", "management create", "management open"):
            self.assertIn(expected, section)
        self.assertIn("references/library.md", section)
        self.assertNotIn("planned but not implemented", section)
        self.assertNotIn("otherwise `PHOTOGRAPHY_STATE_DIR`", text)

    def test_semantic_display_decisions_never_send_images_to_agent(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Default semantic display: embedding-only selection", 1)[1].split(
            "### Explicit original-path maintenance", 1)[0]
        for phrase in ("Do not pass image or thumbnail data to the agent",
                       "embedding-derived similarity information only",
                       "management show-results", "use `[]`", "not claims of visual verification",
                       "do not read its HTML image payloads"):
            self.assertIn(phrase, section)

    def test_current_guides_describe_actual_schema_ten_without_migration(self):
        with closing(sqlite3.connect(":memory:")) as database:
            for statement in (*SCHEMA, *IMAGE_EMBEDDING_SCHEMA, *VIRTUAL_FOLDER_SCHEMA, *IMAGE_FEATURE_SCHEMA):
                database.execute(statement)
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(len(IMAGE_EMBEDDING_TABLES), 6)
        self.assertEqual(len(tables), 13 + len(FEATURE_ALL_TABLES))
        ordinary_count = len(tables) - 1 - len(FEATURE_FTS_SHADOW_TABLES)
        documents = [ROOT / "README.md", ROOT / "photography" / "SKILL.md",
                     ROOT / "docs" / "index-design.md", ROOT / "docs" / "TODO.md"]
        documents += list((ROOT / "photography" / "references").glob("*.md"))
        for document in documents:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                self.assertIn(f"schema {SCHEMA_VERSION}", text.lower())
                self.assertRegex(text, rf"{len(tables)} (?:registered tables|张注册表)")
                self.assertIn("v1–v9", text)
                self.assertRegex(text, r"rejected unchanged|原样拒绝")
                self.assertRegex(text, r"no (?:automatic )?migration|not migrated|not reinitialized or migrated|不迁移")
                self.assertIn("virtual_folders", text)
                self.assertIn("virtual_folder_photos", text)
                self.assertIn("album-snapshot-v2", text)
                self.assertNotIn("album-snapshot-v1", text)
        design = (ROOT / "docs" / "index-design.md").read_text(encoding="utf-8")
        self.assertIn(f"{ordinary_count} ordinary tables", design)
        self.assertIn("external-content FTS5", design)
        self.assertIn("sqlite_sequence", design)
        documented_tables = set(re.findall(r"^\| `([^`]+)` \|", design, re.MULTILINE))
        self.assertTrue(tables <= documented_tables, tables - documented_tables)
        self.assertIn("content='image_ocr_documents'", design)
        self.assertIn("content_rowid='document_id'", design)
        self.assertIn("tokenize='trigram'", design)
        self.assertTrue(set(IMAGE_FEATURE_TABLES) <= tables)
        self.assertTrue({FEATURE_FTS_TABLE, *FEATURE_FTS_SHADOW_TABLES} <= documented_tables)
        for columns in (
                "virtual_folders(folder_id, name, name_key, description, created_at, updated_at)",
                "virtual_folder_photos(folder_id, photo_id, added_at)"):
            self.assertIn(columns, design)
        self.assertIn("primary key `(folder_id, photo_id)`", design)
        self.assertIn("reverse index `(photo_id, folder_id)`", design)

    def test_manual_custom_folders_are_primary_and_support_one_photo_without_index(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Manual custom virtual folders: primary workflow", 1)[1].split(
            "### Scoped browsing and search", 1)[0]
        for phrase in ("primary workflow", "require no index", "empty custom folders",
                       "when explicitly requested", "do not infer deletion permission from a browse/search request",
                       "one photo in multiple folders", "actual folder/photo IDs",
                       "the Skill writes a one-element JSON ID array",
                       '["<actual-photo-id>"]', "`[]` means zero changes, never the entire album",
                       "invalid IDs roll back the entire batch", "duplicate and unchanged counts",
                       "unique NFC + casefold", "stable `folder_id`",
                       "never deletes photo records, originals, thumbnails or embeddings",
                       "never removes other folder memberships", "list/show/browse remain read-only"):
            self.assertIn(phrase, section)
        self.assertLess(text.index("### Manual custom virtual folders: primary workflow"),
                        text.index("### Optional one-time organization"))
        self.assertIn("static, flat many-to-many collections inside one SQLite album", text)
        self.assertIn("folder selections and pending confirmations", text)
        self.assertIn("not a fourth capability", text)

    def test_skill_and_management_reference_include_complete_folder_commands(self):
        commands = (
            "management folders list [--query <name>] [--limit N] [--after <folder-id>]",
            "management folders create --name <name> [--description <text>]",
            "management folders show <folder-id> [--profile-id <profile-id>]",
            "management folders rename <folder-id> --name <new-name>",
            "management folders delete <folder-id>",
            "management folders add <folder-id> --ids-file <photo-ids.json> [--search-snapshot <candidates.json>]",
            "management folders add <folder-id> --ids-file <photo-ids.json> --query-snapshot <ranked.json>",
            "management folders remove <folder-id> --ids-file <photo-ids.json>",
            "management folders organize-date --all --granularity year|month|day --output <date-plan.json>",
            "management folders organize-date --ids-file <photo-ids.json> --granularity year|month|day --output <date-plan.json>",
            "management folders apply-date-plan <date-plan.json> --confirm <digest>",
        )
        for document in (ROOT / "photography" / "SKILL.md",
                         ROOT / "photography" / "references" / "management.md"):
            text = document.read_text(encoding="utf-8")
            for command in commands:
                with self.subTest(document=document.name, command=command):
                    self.assertIn(command, text)

    def test_folder_scope_is_explicit_and_precedes_semantic_ranking(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Scoped browsing and search", 1)[1].split(
            "### Default semantic display", 1)[0]
        for phrase in ("`management photos` and both `management search` modes",
                       "repeatable `--folder-id <id>`", "--folder-match union|intersection",
                       "Multiple distinct folder IDs require an explicit match operator",
                       "repeating the same ID is not a second folder",
                       "No folder IDs means the entire album",
                       "An invalid folder is an error, never a fallback",
                       "empty folder/intersection returns empty results without loading an encoder",
                       "semantic search still requires an explicit or configured valid profile",
                       "before vector inspection, ranking and top-K", "deduplicate union members",
                       "Counts, coverage, pagination and score gaps describe the selected scope",
                       "Retain folder IDs/operator", "`album_total` is the actual whole-album count",
                       "`scope_total`", '"kind":"album"', '"kind":"virtual_folders"',
                       '"folder_id"', '"name"', "coverage_scope: entire_album|selected_folders"):
            self.assertIn(phrase, section)

    def test_snapshots_preserve_historical_folder_scope_but_reject_stale_photo_identity(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Scoped browsing and search", 1)[1].split(
            "### Optional one-time organization", 1)[0]
        for phrase in ("album-snapshot-v2", "Candidates, `show-results` and reports",
                       "preserve historical scope and folder names",
                       "folders are renamed/deleted or memberships change",
                       "do not re-query current membership",
                       "Folder changes alone do not stale a historical result",
                       "photo's input/result identity has changed", "SEARCH_SNAPSHOT_STALE",
                       "preserves validated saved scores, candidate ranks and score gaps verbatim",
                       "gap to the next candidate outside returned top-K",
                       "do not recompute gaps from the selected subset"):
            self.assertIn(phrase, section)

    def test_search_selected_add_reuses_validation_without_inference(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("**Search-selected add:**", 1)[1].split("**Date organization:**", 1)[0]
        for phrase in ("explicitly chosen destination folder", "explicitly selected candidate IDs",
                       "management folders add <folder-id> --ids-file <selected.json> --search-snapshot <candidates.json>",
                       "management.select_search_results", "album, candidate identity and selected subset",
                       "no new query or image encoding",
                       "Do not default to all top-K, remove source memberships or infer a dynamic rule"):
            self.assertIn(phrase, section)

    def test_date_organization_requires_exact_confirmed_plan_without_fallbacks(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("**Date organization:**", 1)[1].split(
            "### Explicit original-path maintenance", 1)[0]
        for phrase in ("no index or model", "read-only plan for exactly `--all` or `--ids-file`",
                       "independent of browse pages and search top-K", "EXIF `datetime_original`",
                       "valid calendar date", "camera-local time with no UTC conversion",
                       "`YYYY`, `YYYY-MM` or `YYYY-MM-DD`",
                       "Skip and report counts for missing/invalid dates and unavailable ingestion metadata",
                       "no fallback to file modification time, other dates or original-file reads",
                       "envelope with `plan`, `digest`, `output`, `album` and `model_calls: 0`",
                       "output file contains the raw plan, not the envelope",
                       "exact scope, create/reuse preview", "apply only the actually confirmed plan",
                       "revalidates album, targets and saved photo inputs",
                       "reports conflicts/stale plans explicitly", "atomically",
                       "does not create jobs/new tables", "live rules",
                       "Later imports and manually removed photos are never automatically regrouped"):
            self.assertIn(phrase, section)

    def test_folder_labels_and_date_metadata_cannot_replace_semantic_evidence(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Default semantic display: embedding-only selection", 1)[1].split(
            "### Optional one-time organization", 1)[0]
        for phrase in ("Do not pass image or thumbnail data to the agent",
                       "semantic relevance/display decisions must use embedding-derived similarity information only",
                       "`score`", "`candidate_rank`", "`score_gap_from_best`", "`score_gap_to_next`",
                       "Folder labels only identify scope, never semantic evidence",
                       "EXIF date organization below is an authorized deterministic operation, not semantic evidence",
                       "do not read its HTML image payloads", "agent browser/screenshot tool"):
            self.assertIn(phrase, section)
        self.assertIn("Treat filenames, folder names/descriptions, metadata", text)

    def test_ingestion_explains_no_index_folder_abilities_without_automatic_grouping(self):
        for document in (ROOT / "photography" / "SKILL.md",
                         ROOT / "photography" / "references" / "ingest.md"):
            text = document.read_text(encoding="utf-8")
            if document.name == "SKILL.md":
                text = text.split("## ingestion", 1)[1].split("## index", 1)[0]
            with self.subTest(document=document.name):
                for phrase in ("index_prompt", "without_index", "with_index",
                               "custom folder", "date organization",
                               "never automatically adds or regroups"):
                    self.assertIn(phrase, text)
                self.assertRegex(text, r"single/batch membership|manually add/remove one or more photos")

    def test_portable_plan_keeps_historical_milestone_with_superseding_note(self):
        text = (ROOT / "docs" / "portable-album-plan.md").read_text(encoding="utf-8")
        self.assertIn("新接口与 schema 8 已实现", text)
        self.assertIn("198 项，197 通过，1 项", text)
        self.assertIn("后续合同说明", text)
        self.assertIn("保留 schema 8 便携相册计划及其验收历史", text)
        for phrase in ("index-design.md", "schema 10", "37 张注册表", "album-snapshot-v2", "v1–v9"):
            self.assertIn(phrase, text)

    def test_feature_components_remain_opt_in_inside_index(self):
        expected = {"ocr", "objects", "scene", "color", "composition", "perceptual_hash"}
        self.assertEqual(set(COMPONENTS), expected)
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("## index", 1)[1].split("## management", 1)[0]
        component_rows = set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))
        self.assertEqual(component_rows, expected)
        for token in ("--component", "image_embedding", "opt-in", "not new public capabilities",
                      "register-profile", "never select a default", "independently per component"):
            self.assertIn(token, section)
        cli = parser()
        prefix = ["--database", str(ROOT / "contract-only.sqlite"), "index"]
        for arguments in (["setup"], ["profiles"], ["configure", "--default-profile", "profile"],
                          ["plan", "--all"], ["status"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(cli.parse_args(prefix + arguments).component, "image_embedding")
                for component in COMPONENTS:
                    self.assertEqual(
                        cli.parse_args(prefix + arguments + ["--component", component]).component, component)

    def test_documented_feature_commands_parse_without_execution(self):
        cli = parser()
        values = {
            "component": "color", "status": "ready", "offset": "0", "pair-id": "0",
            "feature-run-id": "feature_contract", "run-id": "run_contract",
            "absolute-python": str(ROOT / ".venv-features" / "Scripts" / "python.exe"),
            "absolute-worker-python": str(ROOT / ".venv-features" / "Scripts" / "python.exe"),
        }
        required = {
            "setup", "profiles", "configure", "register-profile", "plan", "execute", "status",
            "result", "result-history", "job", "resume", "prototypes", "compare", "pairs", "rebuild-fts",
        }
        for name in ("photography/SKILL.md", "photography/references/index.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            commands = re.findall(r"^index .+$", text, re.MULTILINE)
            observed = set()
            for command in commands:
                for include_optional in (False, True):
                    expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if include_optional else "", command)
                    expanded = re.sub(r"<([^>]+)>", lambda match: values.get(match[1], match[1]), expanded)
                    expanded = re.sub(r"\bN\b", "20", expanded)
                    for metric in ("exact", "hamming"):
                        arguments = shlex.split(expanded.replace("exact|hamming", metric), posix=False)
                        with self.subTest(document=name, arguments=arguments):
                            parsed = cli.parse_args(["--database", str(ROOT / "contract-only.sqlite"), *arguments])
                            observed.add(parsed.index_command)
                            if parsed.index_command == "result":
                                self.assertIsInstance(parsed.after, int)
                            elif parsed.index_command == "result-history":
                                self.assertIsInstance(parsed.after, str)
            self.assertTrue(required <= observed, required - observed)

    def test_feature_input_history_and_dependency_contracts_are_explicit(self):
        for name in ("photography/SKILL.md", "photography/references/index.md", "docs/index-design.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                for token in ("input_fingerprint", "profile_id", "1024", "original",
                              "thumbnail", "prototype", "objects", "dependency_missing",
                              "--dependency-profile-id", "history"):
                    self.assertIn(token, text)
                self.assertRegex(text, r"(?i)paths[^.\n]{0,100}not (?:profile identity|content)|not paths")
                self.assertRegex(text, r"do not stat originals|without[^.\n]{0,100}original stat")
                self.assertRegex(text, r"(?i)(?:never|not)[^.\n]{0,100}(?:latest|newest) timestamp")
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("### Stage 1:", 1)[1].split("## management", 1)[0]
        for token in ("Do not automatically index all photos", "input_unavailable",
                      "never silently substitutes a thumbnail", "preserve historical"):
            self.assertIn(token.casefold(), section.casefold())

    def test_historical_result_details_have_independent_paging_without_default(self):
        documents = ("README.md", "photography/SKILL.md", "photography/references/index.md",
                     "photography/references/management.md", "docs/index-design.md")
        for name in documents:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                for token in ("--result-id", "--details", "--after", "--limit", "--profile-id",
                              "FEATURE_RESULT_MISMATCH", "historical: true"):
                    self.assertIn(token, text)
                self.assertRegex(text, r"(?i)no configured default is needed")
                self.assertRegex(text, r"photo/component")
                self.assertRegex(text, r"(?i)not (?:a )?current[- ]coverage")
        cli = parser()
        for component in COMPONENTS:
            arguments = ["--database", str(ROOT / "contract-only.sqlite"), "index", "result",
                         "photo-contract", "--component", component, "--result-id", "historical-contract",
                         "--details", "--after", "20", "--limit", "10"]
            with self.subTest(component=component):
                parsed = cli.parse_args(arguments)
                self.assertIsNone(parsed.profile_id)
                self.assertEqual(parsed.result_id, "historical-contract")
                self.assertEqual(parsed.photo_id, "photo-contract")
                self.assertEqual(parsed.component, component)
                self.assertTrue(parsed.details)
                self.assertEqual((parsed.after, parsed.limit), (20, 10))
                self.assertEqual(cli.parse_args(arguments + ["--profile-id", "profile-contract"]).profile_id,
                                 "profile-contract")

    def test_feature_plans_cover_non_ml_prototypes_compare_and_explicit_fts(self):
        for name in ("photography/SKILL.md", "photography/references/index.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            section = text.split("Stage 1:", 1)[1]
            with self.subTest(document=name):
                for token in ("non-ML", "exact", "digest", "compute/reuse/skip", "feature_",
                              "--dry-run", "--confirm-stopped", "prototypes", "compare",
                              "--metric exact|hamming", "N×N dense matrix", "pairs",
                              "rebuild-fts --confirm"):
                    self.assertIn(token, section)
                self.assertRegex(section, r"(?i)only after execute approval")
                self.assertRegex(section, r"(?i)(?:never|no)[^.\n]{0,80}(?:steal|stealing)")
                self.assertRegex(section, r"(?i)never automatically delete/merge photos")
                self.assertRegex(section, r"(?i)(?:explicitly writes|separately requested write)")
                self.assertRegex(section, r"(?i)(?:uncomputed|incomplete)[^.\n]{0,180}(?:prove|establish)")

    def test_feature_read_status_empty_results_and_ocr_privacy_are_separate(self):
        coverage = {"ready", "missing", "stale", "invalid_input", "invalid_result", "dependency_missing"}
        for name in ("photography/SKILL.md", "photography/references/index.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            section = text.split("Stage 1:", 1)[1]
            with self.subTest(document=name):
                documented_states = [set(value.split("|")) for value in re.findall(
                    r"`([a-z_]+(?:\|[a-z_]+)+)`", section)]
                self.assertIn(coverage, documented_states)
                for token in ("execution-item state", "complete: false", "empty", "not computed",
                              "text_length", "detail_count", "--details", "offset", "result ID",
                              "1–1000", "whole-album OCR", "untrusted data", "Base64",
                              "pixel", "HTML", "embedding-only"):
                    self.assertIn(token.casefold(), section.casefold())
                self.assertRegex(section, r"(?i)(?:no|not) full (?:OCR )?text")
                self.assertRegex(section, r"(?i)(?:never|do not)[^.\n]{0,180}(?:send|pass)[^.\n]{0,180}agent")

    def test_feature_runtime_download_and_acceptance_are_not_implicit(self):
        for name in ("README.md", "photography/SKILL.md", "photography/references/index.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                for token in (".venv-features", "requirements-features.txt", "--worker-python",
                              "SMART_ALBUMS_FEATURE_PYTHON", "checksum", "offline",
                              "synthetic", "35.4", "performance"):
                    self.assertIn(token.casefold(), text.casefold())
                self.assertRegex(text, r"(?i)(?:unmeasured[^.\n]{0,160}performance"
                                       r"|performance[^.\n]{0,180}do not follow)")
                self.assertRegex(text, r"(?i)licens(?:e|ing)")
                self.assertRegex(text, r"(?i)(?:never|not) (?:SQLite|SQL)|no weights[^.\n]{0,100}SQL")
                self.assertRegex(text, r"(?i)(?:no implicit|without[^.\n]{0,100}download"
                                      r"|(?:not|never)[^.\n]{0,100}automatic downloads)")
        self.assertTrue((ROOT / "photography" / "requirements-features.txt").is_file())

    def test_stage_two_search_records_targeted_acceptance_without_changing_defaults(self):
        documents = [ROOT / "README.md", ROOT / "photography" / "SKILL.md", ROOT / "docs" / "index-design.md"]
        documents += list((ROOT / "photography" / "references").glob("*.md"))
        for document in documents:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                self.assertRegex(text, r"Stage 2 OR search")
                self.assertIn("Stage 2 OR search is implemented", text)
                self.assertNotIn("planned, not implemented", text)
                self.assertRegex(text, r"(?i)targeted integration checks have passed")
                self.assertRegex(text, r"(?i)metadata/semantic")
                self.assertRegex(text, r"(?i)(?:unchanged|remain unchanged)")
        text = (ROOT / "docs" / "TODO.md").read_text(encoding="utf-8")
        for token in ("第二阶段 OR 搜索", "代码已实现", "定向集成验收已通过", "未验证", "35.4"):
            self.assertIn(token, text)

    def test_stage_two_skill_uses_only_numeric_evidence_not_private_matrices_or_images(self):
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("### Stage 2: OR condition queries and private semantic review", 1)[1].split(
            "### Optional one-time organization", 1)[0]
        for phrase in ("Do not read the private snapshot file or its feature matrix",
                       "Do not use OCR snippets, scene labels, feature cells",
                       "query-evidence", "candidate_rank", "score_gap_from_best", "score_gap_to_next",
                       "condition-decisions-v1", "matched_photo_ids", "page_id",
                       "Full semantic review before match counts and ranking",
                       "top-K is not matched", "unknown", "input", "evaluated_coverage",
                       "global `result_rank`", "opaque", "only for the same matched-condition ID set",
                       "frozen random seed", "index profiles", "aliases",
                       "not pHash", "1–2 characters", "3+", "literal `INSTR`",
                       "do not open it in an agent browser/screenshot tool",
                       "model_calls: 0", "query_model_calls", "all", "alias/hardlink",
                       "query-pairs", "condition-duplicate-pairs-v1", "photo_id_a", "photo_id_b",
                       "Pairs are not transitive groups", "Never automatically delete/merge photos",
                       "no `--html` option"):
            self.assertIn(phrase, section)
        self.assertIn("JSON `null` is an error", skill)
        self.assertIn("source_query", skill)

    def test_documented_condition_commands_parse_without_execution(self):
        commands = {
            "management query --query-file <query.json> --output <private-snapshot.json>",
            "management query-evidence <private-snapshot.json> --condition-id A [--page N]",
            "management finalize-query <private-snapshot.json> --decisions-file <decisions.json> --output <ranked.json>",
            "management show-query-results <ranked.json> [--limit N] [--after <opaque-cursor>] [--output <page.json>] [--html <report.html>]",
            "management query-pairs <ranked.json> --condition-id D [--limit N] [--after <opaque-cursor>] [--output <pairs.json>]",
        }
        root = parser()
        for name in ("photography/SKILL.md", "photography/references/management.md", "photography/references/search.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for command in commands:
                self.assertIn(command, text)
                for optional in (False, True):
                    expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if optional else "", command)
                    expanded = re.sub(r"<([^>]+)>", r"\1", expanded)
                    expanded = re.sub(r"\bN\b", "2", expanded)
                    with self.subTest(document=name, command=expanded):
                        args = root.parse_args(["--database", str(ROOT / "contract-only.sqlite"),
                                                *shlex.split(expanded)])
                        self.assertIn(args.management_command,
                                      ("query", "query-evidence", "finalize-query", "show-query-results", "query-pairs"))


if __name__ == "__main__":
    unittest.main()
