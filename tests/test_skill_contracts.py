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
    DIMENSIONS, RUNTIME_VERSION, SDK_VERSION, SCHEMA_VERSION as REVIEW_SCHEMA_VERSION, parse_response,
)
from photography_lib.review_storage import REVIEW_SCHEMA, REVIEW_TABLES
from photography_lib.sqlite_storage import SCHEMA, SCHEMA_VERSION
from photography_lib.virtual_folder_storage import VIRTUAL_FOLDER_SCHEMA


QUERY_GUIDES = (
    ROOT / "README.md",
    ROOT / "photography" / "SKILL.md",
    ROOT / "photography" / "references" / "search.md",
    ROOT / "photography" / "references" / "management.md",
    ROOT / "docs" / "index-design.md",
)
UNIFIED_GUIDES = QUERY_GUIDES[:-1]
REVIEW_GUIDES = (
    ROOT / "README.md",
    ROOT / "photography" / "SKILL.md",
    ROOT / "photography" / "references" / "review.md",
)


class SkillContractTests(unittest.TestCase):
    def test_skill_exposes_four_named_capabilities(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: smart-albums", text.split("---", 2)[1])
        rows = [line for line in text.splitlines() if line.startswith("|")]
        names = []
        for row in rows:
            first = row.split("|")[1].strip().strip("`*").lower()
            if first in ("ingestion", "index", "management", "review"):
                names.append(first)
        self.assertCountEqual(names, ["ingestion", "index", "management", "review"])
        self.assertIn("exactly four photography capabilities", text.split("---", 2)[1])
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

    def test_semantic_guides_require_text_only_intent_without_literal_translation(self):
        for document in QUERY_GUIDES:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for token in ("text only", "English visual intent", "query", "--visual-query",
                              "visual_query", "scene", "actions", "colors", "negation", "count constraints",
                              "search verbs", "sky", "blue", "clear", "dominant", "outdoor", "clarify",
                              "Python does not translate", "VISUAL_QUERY_REQUIRED", "QUERY_TOO_LONG",
                              "64", "EOS", "without truncation", "one fixed recipe",
                              semantic_query.STRATEGY, "no image reindexing is required",
                              "768", "schema 11"):
                    self.assertIn(token, text)
                self.assertIn("Never translate literal metadata or OCR searches", text)
                self.assertIn("`--visual-query` is rejected in metadata mode", text)
                self.assertRegex(text, r"(?i)already-English visual input may omit|already-English visual input[^.;\n]+may omit")
                self.assertNotIn("Do not translate automatically", text)
                self.assertNotIn("without automatic translation", text)
                self.assertNotIn("without translation or retired retrieval prefixes", text)
                self.assertNotIn("no translation/retrieval prefixes", text)
                self.assertNotRegex(text, r"--(?:query-strategy|strategy|prompts|weights)\b")

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
        for document in QUERY_GUIDES:
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

    def test_query_guides_preserve_frozen_recipe_and_actual_call_accounting(self):
        fields = set(semantic_query.prepare_query("搜索带有天空的图片", "sky"))
        self.assertEqual(fields, {"strategy", "query", "visual_query", "prompts", "weights"})
        for document in QUERY_GUIDES:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for field in fields:
                    self.assertIn(f"`{field}`", text)
                for token in ("query_encoding", "query_encodings", "show-results", "folder-add provenance",
                              "actual encoded text", "historical raw-query snapshots",
                              "page_id", "final validation", "no additional encoding",
                              "model_calls", "zero", "eligible", "matched_count",
                              "one logical semantic condition", "prepared visual phrase",
                              "profile", "`id`", "`scoring`", "nfc normalization applies both",
                              "no prefix, caption template or ensemble",
                              "prompts: [visual_query]", "weights: [1.0]",
                              "one text encoder call per unique semantic condition with eligible vectors"):
                    self.assertIn(token, text.casefold())
                self.assertNotRegex(text, r"number of prepared `prompts`|calls follow prompt count")

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

    def test_unified_guides_route_new_requests_to_global_uncapped_candidates(self):
        for document in UNIFIED_GUIDES:
            text = document.read_text(encoding="utf-8").casefold()
            with self.subTest(document=document.name):
                for token in ("unified-search-query-v1", "natural-content requests", "legacy",
                              "100", "deduplicat", "globally", "candidate_limit",
                              "any positive integer", "no fixed upper count", "no 4 kib cap",
                              "no byte-budget reduction", "beyond the requested 100",
                              "--limit", "all", "--query-file", "--visual-query", "--profile-id",
                              "scope", "more than one condition", '"and"', '"or"',
                              "whole query", "not boolean truth", "unknown is not false",
                              "hard clauses", "before semantic ranking", "top-k",
                              "or must not filter other branches"):
                    self.assertIn(token, text)
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        route = skill.split("### Default unified search: numbered evidence review", 1)[1].split(
            "### Fixed semantic text preparation", 1)[0]
        self.assertIn("`--mode` defaults to `unified`", route)
        self.assertIn("Do not route them to the legacy semantic or per-condition OR workflows", route)
        self.assertLess(skill.index("### Default unified search"), skill.index("### Legacy semantic display"))
        self.assertIn("Plain input creates one semantic condition", route)
        self.assertIn("those belong in the file", route)
        self.assertIn("no database schema change", route)

    def test_unified_guides_allow_requested_facts_but_only_numbered_private_review(self):
        for document in UNIFIED_GUIDES:
            text = document.read_text(encoding="utf-8").casefold()
            with self.subTest(document=document.name):
                for token in ("private snapshot", "compact", "number", "review_id",
                              "requested saved structural facts", "hit state, not raw text",
                              "coverage_items", "long hashes/profile ids", "paths", "pictures",
                              "only a json array of integer candidate numbers", "[1,4]", "[]",
                              "search-evidence", "--review-id", "--output <selected.json>",
                              "summary only", "not full selected rows", "not visual verification",
                              "subset of previously selected numbers", "--review-snapshot",
                              "--search-snapshot", "--query-snapshot", "mutually exclusive",
                              "thumbnails", "pixels", "untrusted data"):
                    self.assertIn(token, text)
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        unified = skill.split("### Default unified search: numbered evidence review", 1)[1].split(
            "### Fixed semantic text preparation", 1)[0]
        for token in ("legacy numeric-only restriction does not apply",
                      "Do not read private snapshots", "never blindly query all indexes",
                      "Literal metadata and OCR do not translate or require a text encoder",
                      "never automatic setup", "Do not pass image or thumbnail data to the agent"):
            self.assertIn(token, unified)
        self.assertNotIn("embedding-derived similarity information only", unified)
        self.assertNotIn("condition-decisions-v1", unified)

    def test_unified_guides_separate_optional_facts_from_logical_filters(self):
        for document in UNIFIED_GUIDES:
            text = document.read_text(encoding="utf-8").casefold()
            with self.subTest(document=document.name):
                for token in ("evidence_conditions", "nonsemantic typed conditions",
                              "supporting facts", "not hard filters", "globally unique across both arrays",
                              "missing or unknown optional evidence does not exclude candidates",
                              "multiple-condition operator", "unrequested",
                              "do not affect candidate eligibility or programmatic ranking"):
                    self.assertIn(token, text)
        text = (ROOT / "photography" / "references" / "search.md").read_text(encoding="utf-8")
        section = text.split("## Unified query JSON", 1)[1].split("## Unified evidence", 1)[0]
        self.assertIn("not a user-required filter", section)
        self.assertIn("does not locate sky pixels", section)
        self.assertIn("supporting entries neither add retrieval branches", section)
        self.assertIn("Omission is equivalent to no supporting conditions", section)

    def test_documented_unified_commands_parse_without_execution(self):
        cli = parser()
        commands = {
            'management search "<query>" --visual-query "<English visual intent>" --output <private.json> [--limit N|all] [--profile-id <id>]': "search",
            "management search --query-file <query.json> --output <private.json> [--limit N|all]": "search",
            "management search-evidence <private.json>": "search-evidence",
            "management show-results <private.json> --ids-file <numbers.json> --review-id <returned-id> --output <selected.json> [--html <report.html>]": "show-results",
            "management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>": "folders",
        }
        values = {"query": "搜索带有天空的图片", "English visual intent": "sky"}
        for document in UNIFIED_GUIDES[1:]:
            text = document.read_text(encoding="utf-8")
            for command, action in commands.items():
                self.assertIn(command, text, document.name)
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

    def test_unified_approval_does_not_approve_unrelated_deferred_repairs(self):
        text = (ROOT / "docs" / "code-review-follow-up.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("已另行批准：统一检索与紧凑证据选择", text)
        self.assertIn("无固定数量上限、无 4 KiB 字节上限、无按字节自动减量", text)
        self.assertIn("不再将它们整体标为延期", text)
        for heading in ("## 3. 延期：R2", "## 4. 延期：R4", "## 5. 延期：R6"):
            section = text.split(heading, 1)[1].split("\n## ", 1)[0]
            self.assertIn("状态：未批准", section)

    def test_benchmark_guides_limit_claims_to_proxy_labels_and_tested_queries(self):
        for document in QUERY_GUIDES:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for term in ("101", "SegFormer proxy labels, not human ground truth",
                             "user labels were unavailable", "arbitrary-query translation quality",
                             "not tested"):
                    self.assertIn(term.casefold(), text.casefold())
        for document in (ROOT / "README.md", ROOT / "docs" / "index-design.md"):
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for term in ("8 strategies, 6 concepts and 101 provided photos", "preregistered",
                             "holdout", "content", "sensitivity gates passed",
                             "0.5694", "0.8031", "0.7431", "0.8998"):
                    self.assertIn(term.casefold(), text.casefold())
        design = (ROOT / "docs" / "index-design.md").read_text(encoding="utf-8")
        for term in ("0.8452", "0.8811", "0.7376", "0.9034", "not runtime options",
                     "validation of embedding-only display decisions"):
            self.assertIn(term, design)

    def test_documented_semantic_condition_aliases_do_not_add_matches(self):
        text = (ROOT / "photography" / "references" / "search.md").read_text(encoding="utf-8")
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
                if target.startswith(("http:", "https:", "mailto:", "#")):
                    continue
                target = unquote(target.split("#", 1)[0])
                if target:
                    with self.subTest(document=document.name, target=target):
                        resolved = (document.parent / target).resolve()
                        if bundle is not None:
                            self.assertTrue(resolved.is_relative_to(bundle), "Link escapes installed Skill")
                        self.assertTrue(resolved.exists(), f"Missing Markdown target: {resolved}")

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
            self.assertTrue((installed / "prompts" / "photo-review-v1.txt").is_file())
            self.assertEqual((installed / "requirements-review.txt").read_text(encoding="utf-8"),
                             (ROOT / "photography" / "requirements-review.txt").read_text(encoding="utf-8"))
            self.assert_local_markdown_links(sorted(installed.rglob("*.md")), bundle=installed.resolve())
            runtime_reference = (installed / "references" / "index.md").read_text(encoding="utf-8")
            self.assertIn("absolute path via `--worker-python`", runtime_reference)
            self.assertIn("SMART_ALBUMS_FEATURE_PYTHON", runtime_reference)
            self.assertIn("an installed Skill must not rely on its parent's layout", runtime_reference)
            self.assertIn(r"<skill-directory>\requirements-features.txt", runtime_reference)
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
assert PROMPT_PATH == entrypoint.parent.parent / "prompts" / "photo-review-v1.txt"
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

    def test_legacy_semantic_display_decisions_never_send_images_to_agent(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Legacy semantic display: embedding-only selection", 1)[1].split(
            "### Explicit original-path maintenance", 1)[0]
        for phrase in ("Do not pass image or thumbnail data to the agent",
                       "embedding-derived similarity information only",
                       "management show-results", "use `[]`", "not claims of visual verification",
                       "do not read its HTML image payloads"):
            self.assertIn(phrase, section)

    def test_current_guides_describe_actual_schema_eleven_without_migration(self):
        with closing(sqlite3.connect(":memory:")) as database:
            for statement in (*SCHEMA, *IMAGE_EMBEDDING_SCHEMA, *VIRTUAL_FOLDER_SCHEMA,
                              *IMAGE_FEATURE_SCHEMA, *REVIEW_SCHEMA):
                database.execute(statement)
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(SCHEMA_VERSION, 11)
        self.assertEqual(len(IMAGE_EMBEDDING_TABLES), 6)
        self.assertEqual(set(REVIEW_TABLES), {"ai_review_results", "ai_review_runs", "ai_review_batches"})
        self.assertEqual(len(tables), 13 + len(FEATURE_ALL_TABLES) + len(REVIEW_TABLES))
        self.assertEqual(len(tables), 40)
        ordinary_count = len(tables) - 1 - len(FEATURE_FTS_SHADOW_TABLES)
        self.assertEqual(ordinary_count, 35)
        self.assertEqual(len(FEATURE_FTS_SHADOW_TABLES), 4)
        documents = [ROOT / "README.md", ROOT / "photography" / "SKILL.md",
                     ROOT / "docs" / "index-design.md", ROOT / "docs" / "TODO.md"]
        documents += list((ROOT / "photography" / "references").glob("*.md"))
        for document in documents:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                self.assertIn(f"schema {SCHEMA_VERSION}", text.lower())
                self.assertRegex(text, rf"{len(tables)} (?:registered tables|张注册表)")
                self.assertIn("v1–v10", text)
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
        self.assertTrue(set(REVIEW_TABLES) <= tables)
        self.assertTrue({FEATURE_FTS_TABLE, *FEATURE_FTS_SHADOW_TABLES} <= documented_tables)
        for columns in (
                "virtual_folders(folder_id, name, name_key, description, created_at, updated_at)",
                "virtual_folder_photos(folder_id, photo_id, added_at)"):
            self.assertIn(columns, design)
        self.assertIn("primary key `(folder_id, photo_id)`", design)
        self.assertIn("reverse index `(photo_id, folder_id)`", design)

    def test_review_guides_require_whole_task_and_fresh_retry_approval(self):
        for document in REVIEW_GUIDES:
            text = document.read_text(encoding="utf-8").casefold()
            with self.subTest(document=document.name):
                for token in (
                        "whole", "task", "before sdk construction", "authentication/model",
                        "contact", "cloud-transfer", "--confirm-provider-access",
                        "confirmation_required", "model-list approval", "does not approve",
                        "help/planning/read commands never construct the sdk",
                        "fresh state-bound approval", "retry", "consumed", "digest",
                        "--confirm-stopped", "no automatic retry", "fallback",
                        "batch", "4", "independently", "atomically", "--force", "history",
                        "authorized live copilot validation completed",
                        "trial requires explicit approval of its frozen plan before provider contact"):
                    self.assertIn(token, text)
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("## review\n", 1)[1].split("## Format and unverified work", 1)[0]
        for token in ("code-plan approval", "earlier tasks", "in the user's language",
                      "never authorizes cloud image transfer", "Never copy credentials",
                      "actual dimensions/bytes", "No automatic retry or fallback"):
            self.assertIn(token, section)

    def test_review_guides_keep_runtime_optional_and_authentication_scoped(self):
        for document in REVIEW_GUIDES:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for token in ("requirements-review.txt", f"github-copilot-sdk=={SDK_VERSION}",
                              RUNTIME_VERSION, 'mode="copilot-cli"', "use_logged_in_user=True",
                              "credential home", "owned working/session state", "Child-only",
                              "no-logs", "OS sandbox", "runtime download"):
                    self.assertIn(token, text)
                self.assertRegex(text.casefold(), r"no separate token (?:is )?required|no separate token required")
                self.assertIn("existing local Copilot", text)
        dependencies = (ROOT / "photography" / "requirements-review.txt").read_text(encoding="utf-8")
        reference = (ROOT / "photography" / "references" / "review.md").read_text(encoding="utf-8")
        self.assertIn(
            rf"<absolute-project-environment>\Scripts\python.exe -m copilot download-runtime --version {RUNTIME_VERSION}",
            reference,
        )
        self.assertIn("runtime.node", reference)
        self.assertIn(".hostless-runtime-assets-v2", reference)
        self.assertIn("COPILOT_CLI_EXTRACT_DIR", reference)
        self.assertIn("entire version-specific cache root", reference)
        self.assertIn("an existing `COPILOT_HOME` override", reference)
        self.assertIn('authType="user"', reference)
        self.assertIn("https://github.com", reference)
        self.assertIn("first use, this adapter must fail instead", reference)
        self.assertEqual(
            [line.strip() for line in dependencies.splitlines() if line.strip() and not line.lstrip().startswith("#")],
            [f"github-copilot-sdk=={SDK_VERSION}"],
        )
        for filename in ("requirements.txt", "requirements-index.txt", "requirements-features.txt"):
            text = (ROOT / "photography" / filename).read_text(encoding="utf-8").casefold()
            self.assertNotIn("github-copilot-sdk", text)

    def test_review_guides_document_versioned_queryable_results_not_search(self):
        self.assertEqual(DIMENSIONS, ("composition", "lighting", "color", "subject", "storytelling", "technical"))
        for document in REVIEW_GUIDES:
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for token in (REVIEW_SCHEMA_VERSION, *DIMENSIONS, "description", "strengths",
                              "improvements", "limitations", "reason", "equal-weight mean",
                              "two decimal places", "decimal half-up", "strict", "JPEG",
                              "no review-aware search entry in v1", "fixed", "JSON", "FTS",
                              "ai_review_results", "ai_review_runs", "ai_review_batches",
                              "local ingestion/index/search behavior is unchanged"):
                    self.assertIn(token, text)
        reference = (ROOT / "photography" / "references" / "review.md").read_text(encoding="utf-8")
        for token in ("$.scores.composition.reason", "$.strengths[0]", "$.improvements[0]",
                      "$.limitations[0]", "payload_json", "description", "overall_score",
                      "duplicate JSON keys", "booleans", "NaN/infinity", "unknown/duplicate/missing image IDs",
                      "4,000", "1 MiB", "1–8", "not original-file measurements",
                      "not persisted", "never raw assistant text"):
            self.assertIn(token, reference)
        for dimension in DIMENSIONS:
            self.assertIn(f"{dimension}_score", reference)

    def test_documented_review_response_validates_against_shared_contract(self):
        text = (ROOT / "photography" / "references" / "review.md").read_text(encoding="utf-8")
        examples = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example):
                response = json.loads(example)
                self.assertEqual(response["schema_version"], REVIEW_SCHEMA_VERSION)
                self.assertEqual([item["image_id"] for item in response["reviews"]], ["image_1"])
                parsed = parse_response(example, ["image_1"])
                self.assertEqual(parsed["image_1"]["overall_score"], 6.17)
                self.assertNotIn("image_id", parsed["image_1"])

    def test_documented_review_commands_parse_without_execution(self):
        cli = parser()
        values = {"absolute-json-file": str(ROOT / "contract-only-photo-ids.json"),
                  "absolute-new.html": str(ROOT / "contract-only-new.html"),
                  "photo-id": "photo-id", "model-id": "explicit-vision-model"}
        actions = {"rubric", "models", "plan", "execute", "job", "resume", "result", "history", "report"}
        for document in REVIEW_GUIDES:
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

    def test_review_report_guides_separate_local_exports_and_observed_usage(self):
        for document in REVIEW_GUIDES:
            text = document.read_text(encoding="utf-8").casefold()
            with self.subTest(document=document.name):
                for token in ("review report <run-id> --output <absolute-new.html>",
                              "read-only album operation", "no-overwrite", "self-contained html",
                              "no external network", "active execution time excludes user confirmation waits",
                              "report generation", "provider-reported", "missing usage is not zero",
                              "unknown", "copilot credits are not azure credits", "per-photo",
                              "--batch-size 1", "default remains 4", "random 10-photo",
                              "user-provided source folder", "frozen plan", "retry"):
                    self.assertIn(token, text)
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("do not use an agent browser/screenshot tool to inspect its image payloads", skill)
        reference = (ROOT / "photography" / "references" / "review.md").read_text(encoding="utf-8")
        for token in ("existing destination is an error", "Not reported", "incomplete", "source and unit",
                      "not pure model inference latency", "no scripts or write controls",
                      "single-photo trial does not validate four-image delivery",
                      "authorized 10-photo trial completed with batches 4+4+2", "assistant.usage:per_call_sum",
                      "assistant.usage:unavailable", "explicitly reported zero is preserved",
                      "Duplicate event UUIDs count once", "subset of output tokens",
                      "session.usage_info", "not token consumption", "assistant.usage.cost",
                      "premium multiplier, not credits/currency", "copilot_usage.total_nano_aiu",
                      'credits_unit="copilot_nano_aiu"'):
            self.assertIn(token, reference)

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
        self.assertIn("explicit management writes, not additional capabilities", text)

    def test_skill_and_management_reference_include_complete_folder_commands(self):
        commands = (
            "management folders list [--query <name>] [--limit N] [--after <folder-id>]",
            "management folders create --name <name> [--description <text>]",
            "management folders show <folder-id> [--profile-id <profile-id>]",
            "management folders rename <folder-id> --name <new-name>",
            "management folders delete <folder-id>",
            "management folders add <folder-id> --ids-file <photo-ids.json> [--search-snapshot <candidates.json>]",
            "management folders add <folder-id> --ids-file <photo-ids.json> --query-snapshot <ranked.json>",
            "management folders add <folder-id> --review-snapshot <selected.json> --review-id <returned-id> --ids-file <numbers.json>",
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
            "### Default unified search", 1)[0]
        for phrase in ("`management photos` and all `management search` modes",
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

    def test_legacy_folder_labels_and_date_metadata_cannot_replace_semantic_evidence(self):
        text = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Legacy semantic display: embedding-only selection", 1)[1].split(
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

    def test_stage_two_search_records_targeted_acceptance_and_legacy_compatibility(self):
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

    def test_ocr_short_query_guides_do_not_deny_implemented_stage_two_search(self):
        for document in (ROOT / "photography" / "references" / "index.md",
                         ROOT / "docs" / "index-design.md"):
            text = document.read_text(encoding="utf-8")
            with self.subTest(document=document.name):
                for token in ("management query", "ocr_contains", "1–2 characters", "3+ characters",
                              "literal `INSTR`", "trigram FTS5 MATCH"):
                    self.assertIn(token, text)
                self.assertNotIn("future short-query path", text)
                self.assertNotIn("does **not** implement OCR/OR search now", text)

    def test_legacy_stage_two_skill_uses_only_numeric_evidence_not_private_matrices_or_images(self):
        skill = (ROOT / "photography" / "SKILL.md").read_text(encoding="utf-8")
        section = skill.split("### Stage 2: OR condition queries and private semantic review", 1)[1].split(
            "### Optional one-time organization", 1)[0]
        self.assertIn("Legacy only, not the route for new natural-content requests", section)
        self.assertIn("policies in this section apply only to these legacy commands", section)
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
