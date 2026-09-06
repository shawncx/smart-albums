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


if __name__ == "__main__":
    unittest.main()
