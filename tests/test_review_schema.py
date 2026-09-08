from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib.config import PhotographyError
from photography_lib.review_schema import (
    DIMENSIONS, MAX_RESPONSE_BYTES, RESPONSE_SCHEMA, SCHEMA_VERSION, build_prompt,
    overall_score, parse_response, review_profile, validate_payload, validate_profile,
)


def response(image_ids=("image_1",), score=7):
    return {"schema_version": SCHEMA_VERSION, "reviews": [
        {"image_id": key, "description": "A balanced image.", "strengths": ["Clear subject."],
         "improvements": ["Simplify the background."], "limitations": ["Preview only; original detail is unknown."],
         "scores": {name: {"score": score, "reason": "Supported by visible preview evidence."}
                    for name in DIMENSIONS}}
        for key in image_ids]}


class ReviewSchemaTests(unittest.TestCase):
    def parse(self, value, ids=("image_1",)):
        return parse_response(json.dumps(value), ids)

    def test_complete_structured_payload_and_equal_weight_mean(self):
        value = response(("image_2", "image_1"))
        value["reviews"][1]["scores"]["technical"]["score"] = 6
        result = self.parse(value, ("image_1", "image_2"))
        self.assertEqual(list(result), ["image_1", "image_2"])
        self.assertEqual(result["image_1"]["overall_score"], 6.83)
        self.assertEqual(result["image_2"]["overall_score"], 7)
        self.assertNotIn("image_id", result["image_1"])
        self.assertEqual(validate_payload(result["image_1"]), result["image_1"])
        value["reviews"][0]["scores"]["color"]["score"] = 0
        self.assertEqual(result["image_2"]["scores"]["color"]["score"], 7)

    def test_invalid_shapes_fail_whole_batch(self):
        mutations = [
            lambda v: v.update(extra=True),
            lambda v: v.update(schema_version="future"),
            lambda v: v["reviews"].append(deepcopy(v["reviews"][0])),
            lambda v: v["reviews"][0].update(image_id="other"),
            lambda v: v["reviews"][0].update(extra="not allowed"),
            lambda v: v["reviews"][0].update(description=" "),
            lambda v: v["reviews"][0].update(strengths=[]),
            lambda v: v["reviews"][0].update(limitations=[]),
            lambda v: v["reviews"][0].update(improvements=["x"] * 9),
            lambda v: v["reviews"][0]["scores"].pop("color"),
            lambda v: v["reviews"][0]["scores"]["color"].update(reason=""),
            lambda v: v["reviews"][0]["scores"]["color"].update(score="8"),
            lambda v: v["reviews"][0]["scores"]["color"].update(score=True),
            lambda v: v["reviews"][0]["scores"]["color"].update(score=11),
            lambda v: v["reviews"][0]["scores"]["color"].update(score=-1),
            lambda v: v["reviews"][0]["scores"]["color"].update(score=float("nan")),
            lambda v: v["reviews"][0]["scores"]["color"].update(score=float("inf")),
        ]
        for mutate in mutations:
            value = response()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(PhotographyError):
                self.parse(value)

    def test_no_json_salvage_or_duplicate_keys(self):
        valid = json.dumps(response())
        for text in ("Here is ```json\n" + valid + "\n```", "Here is " + valid, valid + valid,
                     "```json\n" + valid + "\n```\nCommentary", "```python\n" + valid + "\n```",
                     "```json\n" + valid + "\n```\n```json\n" + valid + "\n```",
                     '{"schema_version":"x","schema_version":"y","reviews":[]}',
                     "[" * 2000, "x" * (MAX_RESPONSE_BYTES + 1), "\ud800"):
            with self.subTest(prefix=text[:50]), self.assertRaises(PhotographyError):
                parse_response(text, ["image_1"])

    def test_single_json_code_block_is_only_a_transport_wrapper(self):
        value = response(("image_1", "image_2", "image_3", "image_4"))
        ids = [item["image_id"] for item in value["reviews"]]
        plain = json.dumps(value)
        for tag in ("json", "JSON", ""):
            self.assertEqual(parse_response("```" + tag + "\n" + plain + "\n```", ids),
                             parse_response(plain, ids))
        value["reviews"][0]["scores"]["technical"]["score"] = True
        with self.assertRaises(PhotographyError):
            parse_response("```json\n" + json.dumps(value) + "\n```", ids)

    def test_duplicate_missing_ids_and_late_invalid_item(self):
        for value in (response(("image_1", "image_1")), response(("image_1",)),
                      response(("image_1", "image_3"))):
            with self.assertRaises(PhotographyError):
                self.parse(value, ("image_1", "image_2"))
        value = response(("image_1", "image_2"))
        value["reviews"][1]["scores"]["subject"]["score"] = False
        with self.assertRaises(PhotographyError):
            self.parse(value, ("image_1", "image_2"))

    def test_saved_projection_total_is_not_trusted(self):
        payload = self.parse(response())["image_1"]
        payload["overall_score"] = 9
        with self.assertRaises(PhotographyError):
            validate_payload(payload)
        payload = self.parse(response())["image_1"]
        payload["scores"]["technical"]["score"] = 7.03
        self.assertEqual(overall_score(payload["scores"]), 7.01)

    def test_versioned_profiles_and_prompt_are_stable_and_detached(self):
        profile = review_profile("vision-test")
        self.assertEqual(validate_profile(profile), profile)
        self.assertNotEqual(profile, review_profile("vision-test", "en"))
        prompt = build_prompt(profile, ["image_1", "image_2"])
        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("original", prompt)
        self.assertIn('"maxItems": 2', prompt)
        self.assertIn('"enum": ["image_1", "image_2"]', prompt)
        self.assertEqual(set(RESPONSE_SCHEMA["properties"]["reviews"]["items"]["properties"]["scores"]
                             ["properties"]), set(DIMENSIONS))
        changed = deepcopy(profile)
        changed["prompt_text"] += "changed"
        with self.assertRaises(PhotographyError):
            validate_profile(changed)
        for model in ("", "auto", "default", "bad model"):
            with self.assertRaises(PhotographyError):
                review_profile(model)


if __name__ == "__main__":
    unittest.main()
