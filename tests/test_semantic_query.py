"""The shipping text-query recipe is fixed; experimental strategies are not public modes."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import semantic_query
from photography_lib.config import PhotographyError
from photography_lib.fingerprints import fingerprint


class Encoder:
    def __init__(self, vectors=None):
        self.calls = []
        self.vectors = vectors or [[1., 0., 0.]]

    def profile(self):
        return {"dimensions": 3, "model": "query-fixture"}

    def encode_text(self, text):
        self.calls.append(text)
        return SimpleNamespace(vector=self.vectors[(len(self.calls) - 1) % len(self.vectors)],
                               elapsed_seconds=.01, token_count=len(text.split()) + 1)


class SemanticQueryTests(unittest.TestCase):
    def test_original_request_and_visual_description_are_distinct_and_frozen(self):
        plan = semantic_query.prepare_query("搜索带有天空的图片", "sky")
        self.assertEqual(plan["query"], "搜索带有天空的图片")
        self.assertEqual(plan["visual_query"], "sky")
        self.assertEqual(plan["strategy"], semantic_query.STRATEGY)
        self.assertEqual(sum(plan["weights"]), 1)
        self.assertEqual(len(plan["prompts"]), len(plan["weights"]))
        self.assertEqual(semantic_query.validate_query_plan(plan), plan)

    def test_query_validation_does_not_invent_a_translation_or_discard_constraints(self):
        for text in ("", "  ", 23, "sky\0", "sky\nblue"):
            with self.subTest(value=text), self.assertRaises(PhotographyError):
                semantic_query.prepare_query("request", text)
        for visual in ("cloudy sky", "people with red umbrellas", "a person without a hat"):
            plan = semantic_query.prepare_query("原始请求", visual)
            self.assertEqual(plan["visual_query"], visual)
        with self.assertRaises(PhotographyError):
            semantic_query.prepare_query("搜索带有天空的图片")

    def test_already_prepared_english_query_can_use_the_same_fixed_recipe(self):
        self.assertEqual(semantic_query.prepare_query("sky")["visual_query"], "sky")
        self.assertEqual(semantic_query.prepare_query("request", "  sky  ")["visual_query"], "sky")

    def test_explicit_plan_cannot_choose_another_strategy_or_inject_prompts(self):
        plan = semantic_query.prepare_query("original request", "sky")
        for change in ({"strategy": "original"}, {"prompts": ["a different meaning"]},
                       {"weights": [2.]}, {"image_payload": "NOT_ALLOWED"}):
            with self.subTest(change=change), self.assertRaises(PhotographyError):
                semantic_query.validate_query_plan({**plan, **change})

    def test_encoding_reports_actual_text_calls_and_never_mutates_recipe(self):
        plan = semantic_query.prepare_query("original request", "sky")
        before = deepcopy(plan)
        encoder = Encoder()
        result = semantic_query.encode_query(plan, encoder)
        self.assertEqual(encoder.calls, plan["prompts"])
        self.assertEqual(result.model_calls, len(plan["prompts"]))
        self.assertEqual(result.vector, [1., 0., 0.])
        self.assertEqual(len(result.token_counts), result.model_calls)
        self.assertEqual(result.elapsed_seconds, .01)
        self.assertEqual(plan, before)
        self.assertEqual(fingerprint(plan), fingerprint(before))

    def test_invalid_vectors_or_token_limit_errors_are_not_silently_skipped(self):
        for vector in ([0., 0., 0.], [1.], [float("nan"), 0., 0.]):
            with self.subTest(vector=vector), self.assertRaises(PhotographyError):
                semantic_query.encode_query(semantic_query.prepare_query("sky"), Encoder([vector]))

        class TooLong(Encoder):
            def encode_text(self, text):
                raise PhotographyError("QUERY_TOO_LONG", "The prepared prompt exceeds the model context.")

        with self.assertRaises(PhotographyError) as error:
            semantic_query.encode_query(semantic_query.prepare_query("sky"), TooLong())
        self.assertEqual(error.exception.code, "QUERY_TOO_LONG")


if __name__ == "__main__":
    unittest.main()
