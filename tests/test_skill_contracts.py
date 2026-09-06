from pathlib import Path
import re
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]


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

if __name__ == "__main__":
    unittest.main()
