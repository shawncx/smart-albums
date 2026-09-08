from contextlib import closing
from pathlib import Path
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "photography" / "scripts"))

from photography_lib.cli import parser
from photography_lib import condition_queries, condition_ranking, semantic_query
from photography_lib.feature_profiles import COMPONENTS
from photography_lib.image_embedding_storage import IMAGE_EMBEDDING_SCHEMA, IMAGE_EMBEDDING_TABLES
from photography_lib.image_feature_storage import (
    FEATURE_ALL_TABLES, FEATURE_FTS_SHADOW_TABLES, FEATURE_FTS_TABLE,
    IMAGE_FEATURE_SCHEMA, IMAGE_FEATURE_TABLES,
)
from photography_lib.review_schema import (
    parse_response,
)
from photography_lib.review_storage import REVIEW_SCHEMA, REVIEW_TABLES
from photography_lib.sqlite_storage import SCHEMA, SCHEMA_VERSION
from photography_lib.virtual_folder_storage import VIRTUAL_FOLDER_SCHEMA


# Each executable example is validated at its authoritative reference. Entry and
# overview documents can link there without duplicating schemas or command lists.
SKILL = ROOT / "photography" / "SKILL.md"
REFERENCES = ROOT / "photography" / "references"
SEARCH_GUIDE = REFERENCES / "search.md"
LEGACY_GUIDE = REFERENCES / "search-legacy.md"
REVIEW_GUIDE = REFERENCES / "review.md"


class SkillContractTests(unittest.TestCase):
    def test_entrypoint_metadata_and_context_budget(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: smart-albums$")
        self.assertRegex(frontmatter, r"(?m)^description: .+")
        # The agreed lightweight entry budget measures bytes, not deceptively short lines.
        self.assertLessEqual(len(text.encode("utf-8")), 10_000)

    def test_all_bundled_references_are_reachable_from_entrypoint(self):
        pending = [SKILL.resolve()]
        visited = set()
        while pending:
            document = pending.pop()
            if document in visited:
                continue
            visited.add(document)
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                target = unquote(target.split("#", 1)[0].strip("<>"))
                if target and not target.startswith(("https:", "http:", "mailto:")):
                    resolved = (document.parent / target).resolve()
                    if resolved.suffix == ".md":
                        self.assertTrue(resolved.is_relative_to((ROOT / "photography").resolve()))
                        pending.append(resolved)
        expected = {path.resolve() for path in REFERENCES.glob("*.md")}
        self.assertTrue(expected <= visited, expected - visited)

    def test_documented_management_commands_parse_without_execution(self):
        cli = parser()
        text = (REFERENCES / "management.md").read_text(encoding="utf-8")
        commands = [line for block in re.findall(r"```text\n(.*?)```", text, re.DOTALL)
                    for line in block.splitlines() if line.startswith("management ")]
        self.assertTrue(commands)
        folder_actions = set()
        for command in commands:
            for optional in (False, True):
                for granularity in ("year", "month", "day"):
                    expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if optional else "", command)
                    expanded = re.sub(r"<([^>]+)>",
                                      lambda item: {"event-id": "0"}.get(item[1], item[1]), expanded)
                    expanded = re.sub(r"\bN\b", "20", expanded).replace("year|month|day", granularity)
                    expanded = expanded.replace("20|all", "all")
                    arguments = [arg[1:-1] if arg.startswith('"') and arg.endswith('"') else arg
                                 for arg in shlex.split(expanded, posix=False)]
                    with self.subTest(command=expanded):
                        parsed = cli.parse_args(["--database", str(ROOT / "contract-only.sqlite"), *arguments])
                        self.assertEqual(parsed.command, "management")
                        if parsed.management_command == "folders":
                            folder_actions.add(parsed.folder_command)
        self.assertEqual(folder_actions, {"list", "create", "show", "rename", "delete", "add", "remove",
                                         "organize-date", "apply-date-plan"})

    def test_documented_semantic_commands_parse_and_keep_original_and_visual_text(self):
        cli = parser()
        expected = {
            "搜索带有天空的图片": "sky",
            "黑白的枯树": "black and white leafless trees",
            "有人物的照片": "people",
        }
        observed = set()
        values = {
            "query": "sky", "original query": "sky", "English visual intent": "sky",
            "output-directory": str(ROOT / "contract-only-output"),
        }
        for document in (LEGACY_GUIDE,):
            text = document.read_text(encoding="utf-8")
            blocks = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
            commands = [line[line.index("management search "):]
                        for block in blocks for line in block.splitlines()
                        if "management search " in line and "--mode semantic" in line]
            self.assertTrue(commands, document.name)
            for command in commands:
                for optional in (False, True):
                    expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if optional else "", command)
                    expanded = re.sub(r"<([^>]+)>", lambda match: values.get(match[1], match[1]), expanded)
                    expanded = re.sub(r"\bN\b", "20", expanded)
                    arguments = [arg[1:-1] if arg.startswith(('"', "'")) and arg[-1] == arg[0] else arg
                                 for arg in shlex.split(expanded, posix=False)]
                    with self.subTest(document=document.name, command=expanded):
                        parsed = cli.parse_args(["--database", str(ROOT / "contract-only.sqlite"), *arguments])
                        plan = semantic_query.prepare_query(parsed.query, parsed.visual_query)
                        self.assertEqual(plan["query"], parsed.query)
                        self.assertEqual(plan["strategy"], semantic_query.STRATEGY)
                        self.assertEqual(semantic_query.validate_query_plan(plan), plan)
                        if parsed.query in expected:
                            observed.add(parsed.query)
                            self.assertEqual(parsed.visual_query, expected[parsed.query])
                            self.assertEqual(plan["visual_query"], expected[parsed.query])
                        else:
                            self.assertTrue(parsed.query.isascii())
        self.assertEqual(observed, set(expected))


    def test_final_shipping_recipe_encodes_only_the_visual_phrase_once(self):
        plan = semantic_query.prepare_query("搜索带有天空的图片", "sky")
        self.assertEqual(plan, {
            "strategy": "english-visual-intent-v1", "query": "搜索带有天空的图片",
            "visual_query": "sky", "prompts": ["sky"], "weights": [1.0],
        })
        encoder = Mock()
        encoder.profile.return_value = {"dimensions": 3}
        encoder.encode_text.return_value = SimpleNamespace(vector=[0.6, 0.8, 0.0], token_count=2)
        encoded = semantic_query.encode_query(plan, encoder)
        encoder.encode_text.assert_called_once_with("sky")
        self.assertEqual(encoded.vector, [0.6, 0.8, 0.0])
        self.assertEqual(encoded.model_calls, 1)
        self.assertEqual(encoded.token_counts, [2])


    def test_documented_unified_commands_parse_without_execution(self):
        cli = parser()
        values = {"query": "搜索带有天空的图片", "English visual intent": "sky"}
        for document in (SEARCH_GUIDE,):
            text = document.read_text(encoding="utf-8")
            commands = [line for block in re.findall(r"```text\n(.*?)```", text, re.DOTALL)
                        for line in block.splitlines() if line.startswith("management ")
                        and "--mode metadata" not in line]
            self.assertTrue(commands)
            for command in commands:
                action = command.split()[1]
                for optional in (False, True):
                    for limit in ("1", "100", "10000000000000000", "all"):
                        expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if optional else "", command)
                        expanded = re.sub(r"<([^>]+)>", lambda item: values.get(item[1], item[1]), expanded)
                        expanded = expanded.replace("N|all", limit)
                        arguments = [arg[1:-1] if arg.startswith('"') and arg.endswith('"') else arg
                                     for arg in shlex.split(expanded, posix=False)]
                        with self.subTest(document=document.name, command=expanded):
                            parsed = cli.parse_args(["--database", str(ROOT / "contract-only.sqlite"), *arguments])
                            self.assertEqual(parsed.management_command, action)
                            if action == "search":
                                self.assertEqual(parsed.mode, "unified")
                                self.assertEqual(parsed.output, "private.json")
                                self.assertEqual(parsed.limit, (int(limit) if limit != "all" else "all")
                                                 if optional else None)
                                if parsed.query_file:
                                    self.assertIsNone(parsed.query)
                                    self.assertIsNone(parsed.visual_query)
                                    self.assertIsNone(parsed.profile_id)
                                else:
                                    plan = semantic_query.prepare_query(parsed.query, parsed.visual_query)
                                    self.assertEqual(plan["prompts"], ["sky"])
                            elif action in ("show-results", "folders"):
                                self.assertEqual(parsed.review_id, "returned-id")
                                self.assertEqual(parsed.ids_file, "numbers.json")
        for mode in ("semantic", "metadata", "unified"):
            self.assertEqual(cli.parse_args([
                "--database", str(ROOT / "contract-only.sqlite"), "management", "search", "sky",
                "--mode", mode, "--output", "contract-only.json",
            ]).mode, mode)


    def test_documented_unified_json_preserves_original_typed_clauses_and_uncapped_limits(self):
        from photography_lib import unified_queries
        from photography_lib.config import PhotographyError

        text = (ROOT / "photography" / "references" / "search.md").read_text(encoding="utf-8")
        examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL)]
        query = next(example for example in examples if example.get("schema") == unified_queries.QUERY_SCHEMA)

        def profile(condition, store, cache):
            return condition["profile_id"], {"parameters": {"labels": ["bird"], "score_threshold": 0.3}}

        with patch.object(condition_queries, "_profile", side_effect=profile):
            normalized = unified_queries.normalize_query(query, store=None)["query"]
            self.assertEqual(normalized, query)
            semantic, count = normalized["conditions"]
            self.assertEqual(semantic["query"], query["query"])
            self.assertEqual(semantic["visual_query"], "two birds in a blue sky")
            self.assertEqual((count["kind"], count["operator"], count["value"]), ("object_count", "eq", 2))
            supporting = normalized["evidence_conditions"]
            self.assertEqual(len(supporting), 1)
            self.assertEqual((supporting[0]["kind"], supporting[0]["color"]), ("color_fraction", "blue"))
            self.assertNotIn(supporting[0], normalized["conditions"])
            single = {key: value for key, value in query.items() if key != "operator"}
            single["conditions"] = [semantic]
            result = unified_queries.normalize_query(single, store=None)["query"]
            self.assertEqual(result["conditions"], [semantic])
            self.assertEqual(result["evidence_conditions"], supporting)
            without_support = {key: value for key, value in query.items() if key != "evidence_conditions"}
            result = unified_queries.normalize_query(without_support, store=None)["query"]
            self.assertEqual(result.get("evidence_conditions", []), [])
            for invalid_support in ([{**supporting[0], "id": semantic["id"]}],
                                    [{**semantic, "id": "supporting-semantic"}]):
                with self.subTest(invalid_support=invalid_support), self.assertRaises(PhotographyError):
                    unified_queries.normalize_query({**query, "evidence_conditions": invalid_support}, store=None)
            without_limit = {key: value for key, value in query.items() if key != "candidate_limit"}
            self.assertEqual(unified_queries.normalize_query(without_limit, store=None)["query"]["candidate_limit"],
                             100)
            for limit in (1, 100, 1001, 10 ** 16, "all"):
                with self.subTest(limit=limit):
                    result = unified_queries.normalize_query({**query, "candidate_limit": limit}, store=None)
                    self.assertEqual(result["query"]["candidate_limit"], limit)
            for operator in ("and", "or"):
                result = unified_queries.normalize_query({**query, "operator": operator}, store=None)
                self.assertEqual(result["query"]["operator"], operator)
            with self.assertRaises(PhotographyError):
                unified_queries.normalize_query({key: value for key, value in query.items() if key != "operator"},
                                                store=None)
            for limit in (0, -1, True, 1.5, None, "100", "ALL"):
                with self.subTest(invalid_limit=limit), self.assertRaises(PhotographyError):
                    unified_queries.normalize_query({**query, "candidate_limit": limit}, store=None)


    def test_documented_semantic_condition_aliases_do_not_add_matches(self):
        text = (ROOT / "photography" / "references" / "search-legacy.md").read_text(encoding="utf-8")
        examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL)]
        query = next(example for example in examples if example.get("schema") == condition_queries.SCHEMA)
        condition = next(item for item in query["conditions"] if item["kind"] == "semantic")
        self.assertEqual(condition["query"], "搜索带有天空的图片")
        self.assertEqual(condition["visual_query"], "sky")
        original = {**condition, "profile_id": "embedding-contract"}
        alias = {**original, "id": "alias", "query": "有天空的照片"}
        alias.pop("scoring")
        direct = {**original, "id": "direct", "query": original["visual_query"]}
        direct.pop("visual_query")
        with patch.object(condition_queries, "_profile", return_value=("embedding-contract", {})):
            normalized = condition_queries.normalize_query(
                {**query, "conditions": [original, alias, direct]}, store=None)
        conditions = normalized["query"]["conditions"]
        self.assertEqual(conditions, [original])
        self.assertEqual(normalized["aliases"],
                         {original["id"]: original["id"], "alias": original["id"], "direct": original["id"]})
        plan = semantic_query.prepare_query(original["query"], original["visual_query"])
        self.assertEqual(plan["prompts"], ["sky"])
        self.assertEqual(plan["weights"], [1.0])
        row = {"photo_id": "synthetic-contract-id", "conditions": {original["id"]: {
            "status": "matched", "raw_score": 0.5, **condition_queries.descriptor(original),
            "sources": [], "evidence": {}, "reason": None,
        }}}
        ranked = condition_ranking.rank_results([row], conditions, "contract-seed")
        self.assertEqual(ranked["results"][0]["matched_count"], 1)
        self.assertEqual(ranked["results"][0]["matched_condition_ids"], [original["id"]])


    def test_visual_condition_identity_normalizes_nfc_for_both_routes(self):
        explicit = {
            "id": "explicit", "kind": "semantic", "query": "咖啡馆露台",
            "visual_query": "café terrace", "profile_id": "embedding-contract", "scoring": "graded",
        }
        direct = {"id": "direct", "kind": "semantic", "query": "cafe\u0301 terrace",
                  "profile_id": "embedding-contract", "scoring": "graded"}
        alias = {**explicit, "id": "alias", "visual_query": "cafe\u0301 terrace"}
        for conditions in ([explicit, direct, alias], [direct, alias, explicit]):
            with self.subTest(first=conditions[0]["id"]):
                with patch.object(condition_queries, "_profile", return_value=("embedding-contract", {})):
                    normalized = condition_queries.normalize_query({"conditions": conditions}, store=None)
                canonical = normalized["query"]["conditions"]
                self.assertEqual(len(canonical), 1)
                self.assertEqual(canonical[0]["query"], conditions[0]["query"])
                self.assertEqual(normalized["aliases"],
                                 {item["id"]: conditions[0]["id"] for item in conditions})
                plan = semantic_query.prepare_query(canonical[0]["query"], canonical[0].get("visual_query"))
                self.assertEqual(plan["prompts"], ["café terrace"])


    def assert_local_markdown_links(self, documents, *, bundle=None):
        for document in documents:
            text = document.read_text(encoding="utf-8")
            targets = re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
            targets += re.findall(r"(?m)^\s{0,3}\[[^\]]+\]:\s*(<[^>\n]+>|\S+)", text)
            for target in targets:
                target = target.strip().strip("<>")
                if target.startswith(("http:", "https:", "mailto:")):
                    continue
                filename, _, fragment = target.partition("#")
                with self.subTest(document=document.name, target=target):
                    resolved = (document.parent / unquote(filename)).resolve() if filename else document.resolve()
                    if bundle is not None:
                        self.assertTrue(resolved.is_relative_to(bundle), "Link escapes installed Skill")
                    self.assertTrue(resolved.exists(), f"Missing Markdown target: {resolved}")
                    if fragment and resolved.suffix == ".md":
                        # Validate routing anchors, including same-file links and duplicate headings.
                        content = resolved.read_text(encoding="utf-8")
                        content = re.sub(r"(?ms)^```.*?^```[^\n]*", "", content)
                        anchors = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', content))
                        counts = {}
                        for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", content):
                            heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
                            slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                            count = counts.get(slug, 0)
                            counts[slug] = count + 1
                            anchors.add(f"{slug}-{count}" if count else slug)
                        self.assertIn(unquote(fragment), anchors, f"Missing anchor in {resolved}")


    def test_local_markdown_links_exist(self):
        documents = [ROOT / "README.md", ROOT / "photography" / "SKILL.md"]
        documents += list((ROOT / "docs").glob("*.md"))
        documents += list((ROOT / "photography" / "references").glob("*.md"))
        self.assert_local_markdown_links(documents)


    def test_installed_skill_links_and_help_without_repository_or_models(self):
        with tempfile.TemporaryDirectory(prefix=".skill-contract-", dir=ROOT) as directory:
            staging = Path(directory)
            installed = staging / "host-skills" / "smart-albums"
            shutil.copytree(ROOT / "photography", installed,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            workspace = staging / "unrelated-workspace"
            workspace.mkdir()
            self.assertFalse((installed.parent / "docs").exists())
            self.assertFalse((installed.parent / ".venv-features").exists())
            self.assertFalse((installed.parent / ".venv-review").exists())
            self.assertTrue((installed / "prompts" / "photo-review-v2.txt").is_file())
            self.assertEqual((installed / "requirements-review.txt").read_text(encoding="utf-8"),
                             (ROOT / "photography" / "requirements-review.txt").read_text(encoding="utf-8"))
            self.assert_local_markdown_links(sorted(installed.rglob("*.md")), bundle=installed.resolve())
            entrypoint = installed / "scripts" / "photography.py"
            probe = """
from pathlib import Path
import runpy
import sys

class NoModelImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {
            "torch", "transformers", "rapidocr", "onnxruntime", "cv2", "numpy", "huggingface_hub",
            "copilot", "copilot_sdk_runtime",
        }:
            raise AssertionError("Installed help must not import optional models: " + fullname)

sys.meta_path.insert(0, NoModelImports())
entrypoint = Path(sys.argv[1])
sys.path.insert(0, str(entrypoint.parent))
from photography_lib.cli import parser
from photography_lib.feature_models import worker_python
from photography_lib.review_schema import PROMPT_PATH, review_profile
assert PROMPT_PATH == entrypoint.parent.parent / "prompts" / "photo-review-v2.txt"
assert review_profile("explicit-test-vision-model")["prompt_text"] == PROMPT_PATH.read_text(encoding="utf-8")
arguments = parser().parse_args([
    "--database", str(Path.cwd() / "unopened.sqlite"), "index", "setup", "--component", "ocr",
    "--worker-python", sys.executable,
])
assert worker_python(arguments.worker_python) == Path(sys.executable).resolve()
sys.argv = sys.argv[1:]
runpy.run_path(str(entrypoint), run_name="__main__")
"""
            for command in ([], ["ingestion"], ["index"], ["management"], ["management", "search"],
                            ["management", "search-evidence"], ["management", "show-results"],
                            ["management", "folders", "add"], ["review"], ["review", "rubric"],
                            ["review", "models"], ["review", "plan"], ["review", "execute"],
                            ["review", "resume"], ["review", "job"], ["review", "result"],
                            ["review", "history"], ["review", "report"]):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-I", "-B", "-c", probe, str(entrypoint), *command, "--help"],
                        cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("usage:", result.stdout)
            direct = subprocess.run(
                [sys.executable, "-E", "-s", "-B", str(entrypoint), "--help"],
                cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
            self.assertIn("usage:", direct.stdout)
            self.assertEqual(list(workspace.iterdir()), [])


    def test_schema_inventory_matches_registered_tables(self):
        with closing(sqlite3.connect(":memory:")) as database:
            for statement in (*SCHEMA, *IMAGE_EMBEDDING_SCHEMA, *VIRTUAL_FOLDER_SCHEMA,
                              *IMAGE_FEATURE_SCHEMA, *REVIEW_SCHEMA):
                database.execute(statement)
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(SCHEMA_VERSION, 12)
        self.assertEqual(len(IMAGE_EMBEDDING_TABLES), 6)
        self.assertEqual(set(REVIEW_TABLES), {"ai_review_results", "ai_review_runs", "ai_review_batches"})
        self.assertEqual(len(tables), 13 + len(FEATURE_ALL_TABLES) + len(REVIEW_TABLES))
        self.assertEqual(len(tables), 40)
        ordinary_count = len(tables) - 1 - len(FEATURE_FTS_SHADOW_TABLES)
        self.assertEqual(ordinary_count, 35)
        self.assertEqual(len(FEATURE_FTS_SHADOW_TABLES), 4)
        design = (ROOT / "docs" / "index-design.md").read_text(encoding="utf-8")
        documented_tables = set(re.findall(r"^\| `([^`]+)` \|", design, re.MULTILINE))
        self.assertTrue(tables <= documented_tables, tables - documented_tables)
        self.assertIn("content='image_ocr_documents'", design)
        self.assertIn("content_rowid='document_id'", design)
        self.assertIn("tokenize='trigram'", design)
        self.assertTrue(set(IMAGE_FEATURE_TABLES) <= tables)
        self.assertTrue(set(REVIEW_TABLES) <= tables)
        self.assertTrue({FEATURE_FTS_TABLE, *FEATURE_FTS_SHADOW_TABLES} <= documented_tables)
        for columns in (
                "virtual_folders(folder_id, name, name_key, description, created_at, updated_at)",
                "virtual_folder_photos(folder_id, photo_id, added_at)"):
            self.assertIn(columns, design)
        self.assertIn("primary key `(folder_id, photo_id)`", design)
        self.assertIn("reverse index `(photo_id, folder_id)`", design)


    def test_documented_review_response_validates_against_shared_contract(self):
        text = (ROOT / "photography" / "references" / "review.md").read_text(encoding="utf-8")
        examples = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example):
                response = json.loads(example)
                self.assertEqual(set(response), {"results"})
                self.assertEqual([item["image_id"] for item in response["results"]], ["image_1"])
                parsed = parse_response(example, ["image_1"])
                self.assertEqual(parsed["image_1"]["overall_score"], 6.17)
                self.assertNotIn("image_id", parsed["image_1"])


    def test_documented_review_commands_parse_without_execution(self):
        cli = parser()
        values = {"absolute-json-file": str(ROOT / "contract-only-photo-ids.json"),
                  "absolute-new.html": str(ROOT / "contract-only-new.html"),
                  "photo-id": "photo-id", "model-id": "explicit-vision-model"}
        actions = {"rubric", "models", "plan", "execute", "job", "resume", "result", "history", "report", "upgrade"}
        for document in (REVIEW_GUIDE,):
            text = document.read_text(encoding="utf-8")
            commands = [line for block in re.findall(r"```text\n(.*?)```", text, re.DOTALL)
                        for line in block.splitlines() if line.startswith("review ")]
            self.assertEqual({command.split()[1] for command in commands}, actions, document.name)
            for command in commands:
                for optional in (False, True):
                    expanded = re.sub(r"\[([^\[\]]*)\]", r"\1" if optional else "", command)
                    expanded = re.sub(r"<([^>]+)>", lambda item: values.get(item[1], item[1]), expanded)
                    expanded = re.sub(r"\bN\b", "20", expanded).replace("zh-CN|en", "en")
                    arguments = shlex.split(expanded, posix=False)
                    with self.subTest(document=document.name, command=expanded):
                        parsed = cli.parse_args(["--database", str(ROOT / "contract-only.sqlite"), *arguments])
                        self.assertEqual(parsed.command, "review")
                        self.assertEqual(parsed.review_command, command.split()[1])
                        if parsed.review_command == "plan":
                            self.assertEqual(parsed.model, "explicit-vision-model")
                            self.assertEqual(parsed.batch_size, 4)
                            self.assertNotEqual(bool(parsed.photo_id), bool(parsed.ids_file))
                            if parsed.ids_file:
                                self.assertTrue(Path(parsed.ids_file).is_absolute())
                                self.assertEqual(parsed.language, "en" if optional else "zh-CN")
                                self.assertEqual(parsed.force, optional)
                                self.assertEqual(parsed.dry_run, optional)
                        if parsed.review_command == "models":
                            self.assertTrue(parsed.confirm_provider_access)
                        if parsed.review_command == "resume":
                            self.assertEqual(parsed.confirm, "retry-digest")
                            self.assertEqual(parsed.confirm_stopped, optional)
                        if parsed.review_command == "report":
                            self.assertEqual(parsed.run_id, "run-id")
                            self.assertEqual(Path(parsed.output), ROOT / "contract-only-new.html")
                            self.assertTrue(Path(parsed.output).is_absolute())


    def test_documented_models_confirmation_fails_before_provider_construction(self):
        from photography_lib.config import PhotographyError
        from photography_lib.review_cli import command

        args = parser().parse_args(["--database", str(ROOT / "contract-only.sqlite"), "review", "models"])
        self.assertFalse(args.confirm_provider_access)
        with patch("photography_lib.review._provider", side_effect=AssertionError("Unapproved provider")) as provider:
            with self.assertRaises(PhotographyError) as raised:
                command(args, Mock(), Mock())
            self.assertEqual(raised.exception.code, "CONFIRMATION_REQUIRED")
            provider.assert_not_called()


    def test_feature_components_remain_opt_in_inside_index(self):
        expected = {"ocr", "objects", "scene", "color", "composition", "perceptual_hash"}
        self.assertEqual(set(COMPONENTS), expected)
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
        for name in ("photography/references/index.md",):
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


    def test_historical_result_details_have_independent_paging_without_default(self):
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


    def test_documented_condition_commands_parse_without_execution(self):
        root = parser()
        for name in ("photography/references/search-legacy.md",):
            text = (ROOT / name).read_text(encoding="utf-8")
            actions = {"query", "query-evidence", "finalize-query", "show-query-results", "query-pairs"}
            commands = [line for block in re.findall(r"```text\n(.*?)```", text, re.DOTALL)
                        for line in block.splitlines() if line.startswith("management ")
                        and line.split()[1] in actions]
            self.assertEqual({line.split()[1] for line in commands}, actions)
            for command in commands:
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
