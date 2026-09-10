from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib.config import PhotographyError
from photography_lib.review_schema import (
    DIMENSIONS, MAX_RESPONSE_BYTES, RESPONSE_SCHEMA, SCHEMA_VERSION, build_prompt,
    overall_score, parse_response, response_diagnostics, review_profile, validate_payload, validate_profile,
)


def response(image_ids=("image_1",), score=7):
    return {"results": [
        {"image_id": key, "review_status": "reviewed", "description": "A balanced image.",
         "strengths": ["Clear subject."], "improvements": [
             {"kind": "edit", "action": "Crop the bright edge.",
              "rationale": "Removing the bright edge reduces distraction from the subject.", "tradeoff": None}],
         "limitations": ["The downsampled JPEG preview cannot reliably establish original focus accuracy, "
                         "original noise levels, compression versus preview-generation artifacts, or fine detail."],
         "dimensions": {name: {"score": score, "reason": "Supported by visible preview evidence."}
                        for name in DIMENSIONS}}
        for key in image_ids]}


class ReviewSchemaTests(unittest.TestCase):
    def parse(self, value, ids=("image_1",)):
        return parse_response(json.dumps(value), ids)

    def test_complete_structured_payload_and_equal_weight_mean(self):
        value = response(("image_1", "image_2"))
        value["results"][0]["dimensions"]["technical"]["score"] = 6
        result = self.parse(value, ("image_1", "image_2"))
        self.assertEqual(list(result), ["image_1", "image_2"])
        self.assertEqual(result["image_1"]["overall_score"], 6.83)
        self.assertEqual(result["image_2"]["overall_score"], 7)
        self.assertNotIn("image_id", result["image_1"])
        self.assertEqual(validate_payload(result["image_1"]), result["image_1"])
        value["results"][1]["dimensions"]["color"]["score"] = 0
        self.assertEqual(result["image_2"]["dimensions"]["color"]["score"], 7)

    def test_invalid_shapes_fail_whole_batch(self):
        mutations = [
            lambda v: v.update(extra=True),
            lambda v: v.update(schema_version="photo-review-v2"),
            lambda v: v["results"].append(deepcopy(v["results"][0])),
            lambda v: v["results"][0].update(image_id="other"),
            lambda v: v["results"][0].update(extra="not allowed"),
            lambda v: v["results"][0].update(description=" "),
            lambda v: v["results"][0].update(strengths=["x"] * 4),
            lambda v: v["results"][0].update(limitations=[]),
            lambda v: v["results"][0].update(improvements=["x"]),
            lambda v: v["results"][0]["improvements"].extend(v["results"][0]["improvements"] * 3),
            lambda v: v["results"][0]["dimensions"].pop("color"),
            lambda v: v["results"][0]["dimensions"]["color"].update(reason=""),
            lambda v: v["results"][0]["dimensions"]["color"].update(score="8"),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=True),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=11),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=-1),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=7.03),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=None),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=float("nan")),
            lambda v: v["results"][0]["dimensions"]["color"].update(score=float("inf")),
            lambda v: v["results"][0].update(review_status="partial"),
            lambda v: v["results"][0].update(review_status=[]),
            lambda v: v["results"][0]["improvements"][0].update(kind="select"),
            lambda v: v["results"][0]["improvements"][0].update(tradeoff=" "),
            lambda v: v["results"][0]["improvements"][0].update(action=False),
            lambda v: v["results"][0]["improvements"][0].pop("tradeoff"),
            lambda v: v["results"][0]["improvements"][0].update(extra=True),
            lambda v: v["results"][0].update(overall_score=7),
        ]
        for mutate in mutations:
            value = response()
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(PhotographyError):
                self.parse(value)

    def test_no_json_salvage_or_duplicate_keys(self):
        valid = json.dumps(response())
        for text in ("Here is " + valid, valid + valid,
                     '{"results":[],"results":[]}', valid.replace('"score": 7', '"score": 7, "score": 8'),
                     "[" * 2000, "x" * (MAX_RESPONSE_BYTES + 1), "\ud800"):
            with self.subTest(prefix=text[:50]), self.assertRaises(PhotographyError):
                parse_response(text, ["image_1"])

    def test_single_json_transport_wrapper_preserves_the_validated_payload(self):
        plain = json.dumps(response())
        fence = chr(96) * 3
        expected = parse_response(plain, ["image_1"])
        for tag in ("json", "JSON", ""):
            for newline in ("\n", "\r\n"):
                wrapped = " \t" + fence + tag + " \t" + newline + plain + newline + fence + "\r\n"
                with self.subTest(tag=tag, newline=newline):
                    self.assertEqual(parse_response(wrapped, ["image_1"]), expected)

    def test_code_block_never_salvages_ambiguous_or_invalid_responses(self):
        plain = json.dumps(response())
        block = "```json\n" + plain + "\n```"
        invalid_score = plain.replace('"score": 7', '"score": true')
        for text in ("Here is " + block, block + "\nDone.", block + "\n" + block,
                     "```python\n" + plain + "\n```", "```json\n" + plain,
                     "```json\n" + block + "\n```", "```json\n" + plain[:-1] + "\n```",
                     '```json\n{"results":[],"results":[]}\n```',
                     "```json\n" + invalid_score + "\n```",
                     "```json\n" + plain.replace('"score": 7', '"score": NaN') + "\n```",
                     "```json\n" + plain.replace('"image_id": "image_1"', '"image_id": "other"') + "\n```",
                     "```json\n" + plain + " " * (MAX_RESPONSE_BYTES - len(plain)) + "\n```"):
            with self.subTest(prefix=text[:50]), self.assertRaises(PhotographyError):
                parse_response(text, ["image_1"])

    def test_format_diagnostics_are_content_free_and_attached_to_parse_errors(self):
        text = "Private model commentary, not JSON."
        expected = {"response_format": "other", "response_bytes": len(text.encode()),
                    "response_chars": len(text),
                    "response_sha256": hashlib.sha256(text.encode()).hexdigest()}
        self.assertEqual(response_diagnostics(text), expected)
        with self.assertRaises(PhotographyError) as failed:
            parse_response(text, ["image_1"])
        self.assertEqual(failed.exception.details, {
            **expected, "line": 1, "column": 1, "json_error_code": "expected_value",
            "json_error_offset": 0, "json_error_character": "other", "json_error_at_end": False,
            "json_body_chars": len(text), "json_remaining_chars": len(text),
        })
        self.assertNotIn(text, json.dumps(failed.exception.to_dict()))
        for value, kind in ((None, "non_text"), ("\ud800", "invalid_utf8"),
                            ("x" * (MAX_RESPONSE_BYTES + 1), "oversized"),
                            ("```json\n{}\n```", "json_code_block"),
                            ("```\n{}\n```", "unlabeled_code_block")):
            with self.subTest(kind=kind):
                self.assertEqual(response_diagnostics(value)["response_format"], kind)

    def test_syntax_diagnostics_distinguish_errors_without_retaining_text(self):
        cases = (
            ('{"private":"PRIVATE_VALUE"', "expected_comma", "end_of_input", True),
            ('{"private":"PRIVATE_VALUE" "next":1}', "expected_comma", "quote", False),
            ('{"private" "PRIVATE_VALUE"}', "expected_colon", "quote", False),
            ('{"private":"PRIVATE_VALUE}', "unterminated_string", "quote", False),
            ('{"private":"PRIVATE_VALUE\\q"}', "invalid_escape", "backslash", False),
            ('{"private":"PRIVATE_VALUE"} {"other":1}', "extra_data", "object_start", False),
        )
        for text, code, character, at_end in cases:
            for wrapper in ("{}", "```json\n{}\n```"):
                with self.subTest(code=code, wrapper=wrapper), self.assertRaises(PhotographyError) as failed:
                    parse_response(wrapper.format(text), ["image_1"])
                details = failed.exception.details
                self.assertEqual(details["json_error_code"], code)
                self.assertEqual(details["json_error_character"], character)
                self.assertIs(details["json_error_at_end"], at_end)
                self.assertEqual(details["json_body_chars"], len(text))
                self.assertEqual(details["json_remaining_chars"], len(text) - details["json_error_offset"])
                self.assertNotIn("PRIVATE_VALUE", json.dumps(failed.exception.to_dict()))
                self.assertNotIn("private", json.dumps(details))

    def test_syntax_offsets_count_characters_not_utf8_bytes(self):
        text = '{"private":"\u96ea\u5c71"'
        with self.assertRaises(PhotographyError) as failed:
            parse_response(text, ["image_1"])
        details = failed.exception.details
        self.assertEqual(details["response_chars"], len(text))
        self.assertEqual(details["response_bytes"], len(text.encode("utf-8")))
        self.assertEqual(details["json_error_offset"], len(text))
        self.assertTrue(details["json_error_at_end"])

    def test_missing_result_closing_brace_is_diagnosed_not_repaired(self):
        valid = json.dumps(response(), separators=(",", ":"))
        self.assertTrue(valid.endswith("}}]}"))
        text = valid[:-3] + valid[-2:]
        with self.assertRaises(PhotographyError) as failed:
            parse_response(text, ["image_1"])
        details = failed.exception.details
        self.assertEqual(details["json_error_code"], "expected_comma")
        self.assertEqual(details["json_error_character"], "array_end")
        self.assertEqual(details["json_remaining_chars"], 2)
        self.assertFalse(details["json_error_at_end"])

    def test_prompt_syntax_example_is_complete_and_not_a_numeric_score_default(self):
        profile = review_profile("vision-test")
        example = profile["prompt_text"].split("SYNTAX EXAMPLE (unreadable attachment only)\n", 1)[1]
        example = example.split("\nEND SYNTAX EXAMPLE", 1)[0]
        payload = parse_response(example, ["example_image"])["example_image"]
        self.assertEqual(payload["review_status"], "unreviewable")
        self.assertIsNone(payload["overall_score"])
        self.assertEqual(set(payload["dimensions"]), set(DIMENSIONS))
        self.assertIn("Do not omit the result object's closing brace.", profile["prompt_text"])
        self.assertIn("Readable photos require their own evidence-based scores", profile["prompt_text"])

    def test_duplicate_missing_reordered_ids_and_late_invalid_item(self):
        for value in (response(("image_1", "image_1")), response(("image_1",)),
                      response(("image_1", "image_3")), response(("image_2", "image_1"))):
            with self.assertRaises(PhotographyError):
                self.parse(value, ("image_1", "image_2"))
        value = response(("image_1", "image_2"))
        value["results"][1]["dimensions"]["subject"]["score"] = False
        with self.assertRaises(PhotographyError):
            self.parse(value, ("image_1", "image_2"))

    def test_saved_projection_total_is_not_trusted(self):
        payload = self.parse(response())["image_1"]
        payload["overall_score"] = 9
        with self.assertRaises(PhotographyError):
            validate_payload(payload)
        payload = self.parse(response())["image_1"]
        payload["dimensions"]["technical"]["score"] = 7.03
        payload["overall_score"] = overall_score(payload["dimensions"])
        with self.assertRaises(PhotographyError):
            validate_payload(payload)

    def test_partial_and_unreviewable_keep_unknowns_and_empty_lists(self):
        for unknown in range(7):
            value = response()
            entry = value["results"][0]
            entry.update(strengths=[], improvements=[], review_status=(
                "reviewed" if unknown == 0 else "unreviewable" if unknown == 6 else "partial"))
            for name in DIMENSIONS[:unknown]:
                entry["dimensions"][name] = {"score": None, "reason": "No interpretable visual evidence."}
            result = self.parse(value)["image_1"]
            self.assertEqual(result["overall_score"], None if unknown else 7)
            self.assertEqual(result["review_status"], entry["review_status"])
            for status in ("reviewed", "partial", "unreviewable"):
                if status != entry["review_status"]:
                    invalid = deepcopy(value)
                    invalid["results"][0]["review_status"] = status
                    with self.assertRaises(PhotographyError):
                        self.parse(invalid)

    def test_opaque_ids_empty_manifest_half_steps_and_structured_suggestions(self):
        ids = ["opaque id/雪", "", "image-01"]
        self.assertEqual(list(self.parse(response(ids), ids)), ids)
        self.assertEqual(self.parse({"results": []}, []), {})
        for score in (0, 0.5, 9.5, 10):
            self.assertEqual(self.parse(response(score=score))["image_1"]["overall_score"], score)
        value = response()
        value["results"][0]["improvements"][0].update(kind="reshoot", tradeoff="Less surrounding context.")
        self.parse(value)
        for ids in (["a", "a"], [None], None):
            with self.assertRaises(PhotographyError):
                parse_response('{"results":[]}', ids)

    def test_versioned_profiles_and_prompt_are_stable_and_detached(self):
        profile = review_profile("vision-test")
        self.assertEqual(profile["rubric_version"], "photo-review-v2")
        self.assertEqual(validate_profile(profile), profile)
        self.assertNotEqual(profile, review_profile("vision-test", "en"))
        prompt = build_prompt(profile, ["image_1", "image_2"])
        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("original", prompt)
        self.assertIn('"maxItems": 2', prompt)
        self.assertIn('"const": "image_1"', prompt)
        self.assertIn('"attachment_index": 1', prompt)
        self.assertNotIn("attachment_position", prompt)
        self.assertIn('"output_language": "Simplified Chinese"', prompt)
        self.assertEqual(set(RESPONSE_SCHEMA["properties"]["results"]["items"]["properties"]["dimensions"]
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
