"""Pure feature contracts, tested with generated images and three-dimensional vectors."""
from __future__ import annotations

import copy
import io
import json
import math
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from PIL import Image
from photography_lib.config import PhotographyError
from photography_lib.feature_algorithms import (
    compute_color, compute_composition, compute_hash, compute_scene, hash_distance,
    make_scene_prototypes, validate_scene_prototypes,
)
from photography_lib.feature_profiles import (
    COMPONENTS, COCO_CLASS_IDS, INPUT_SCOPES, default_profile, normalize_ocr_text,
    profile_identity, validate_payload,
)
from photography_lib.fingerprints import fingerprint


def preview(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def object_result(objects=(), *, complete=True):
    return {"width": 200, "height": 100, "complete": complete, "objects": list(objects)}


def detection(box, score=0.8, class_id="person"):
    return {"class_id": class_id, "score": score, "bbox": list(box)}


def ocr_result(text="ＡＢＣ　 Café\n中文"):
    return {
        "width": 100, "height": 50, "complete": True, "text": text,
        "normalized_text": normalize_ocr_text(text),
        "blocks": [{"text": text, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "recognition_score": 0.9, "detection_score": None}] if text else [],
    }


EMBEDDING_PROFILE = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "fixture", "revision": "v1", "dimensions": 3, "dtype": "float32-le", "normalized": True,
}


class FakeEncoder:
    def __init__(self, vectors=None):
        self.vectors = vectors or {"one": [1, 0, 0], "two": [0, 1, 0], "three": [-1, 0, 0]}
        self.calls = []
        self.identity = copy.deepcopy(EMBEDDING_PROFILE)

    def profile(self):
        return copy.deepcopy(self.identity)

    def encode_text(self, text):
        self.calls.append(text)
        return SimpleNamespace(vector=list(self.vectors[text]))


def scene_profile():
    profile = default_profile("scene", dependency_profile_id=fingerprint(EMBEDDING_PROFILE))
    profile["parameters"]["catalog"] = [
        {"scene_id": "mixed", "label": "Mixed scene", "prompts": ["one", "two"]},
        {"scene_id": "opposite", "label": "Opposite scene", "prompts": ["three"]},
    ]
    return profile


class FeatureProfileTests(unittest.TestCase):
    def test_six_profiles_are_stable_independent_and_json(self):
        for component in COMPONENTS:
            kwargs = {"dependency_profile_id": "fixture-profile-v1"} if component in {"scene", "composition"} else {}
            with self.subTest(component=component):
                profile = default_profile(component, **kwargs)
                identity, encoded = profile_identity(profile)
                self.assertEqual(json.loads(encoded), profile)
                self.assertEqual(identity, fingerprint(profile))
                self.assertEqual(profile["input_scope"], INPUT_SCOPES[component])
                self.assertEqual(profile_identity(dict(reversed(list(profile.items()))))[0], identity)
                profile["runtime"]["packages"] = {"fixture": "1.0.0"}
                self.assertNotEqual(profile_identity(profile)[0], identity)
                self.assertEqual(profile_identity(default_profile(component, **kwargs))[0], identity)

    def test_dependency_ids_are_nonblank_not_hex_only(self):
        for component, dependency in (("scene", "image_embedding"), ("composition", "objects")):
            for invalid in (None, "", "   ", 5, True):
                with self.subTest(component=component, invalid=invalid), self.assertRaises(PhotographyError):
                    default_profile(component, dependency_profile_id=invalid)
            self.assertEqual(default_profile(component, dependency_profile_id="model-v1")["dependencies"],
                             {dependency: "model-v1"})
        with self.assertRaises(PhotographyError):
            default_profile("color", dependency_profile_id="unexpected")

    def test_profile_unknown_fields_and_pixel_payloads_are_rejected(self):
        for location in ("root", "parameters", "runtime"):
            profile = default_profile("color")
            target = profile if location == "root" else profile[location]
            target["pixels"] = [[[12, 34, 56]]]
            with self.subTest(location=location), self.assertRaises(PhotographyError):
                profile_identity(profile)
        for scope in (None, "original", "", False):
            profile = default_profile("color")
            profile["input_scope"] = scope
            with self.subTest(scope=scope), self.assertRaises(PhotographyError):
                profile_identity(profile)

    def test_asset_runtime_pins_are_mutable_before_registration_not_paths(self):
        profile = default_profile("ocr")
        profile["runtime"]["packages"].update({"onnxruntime": "1.24.2", "numpy": "2.4.2"})
        profile["runtime"].update({"python": "cpython-3.14", "intra_op_num_threads": 2})
        profile["assets"] = [{"role": "det", "filename": "det.onnx", "sha256": "a" * 64,
                              "size": 123, "license": "Apache-2.0", "revision": "v6"}]
        profile_identity(profile)
        for key, value in (("path", "C:\\models\\det.onnx"), ("source", "https://example.test/det")):
            invalid = copy.deepcopy(profile)
            invalid["assets"][0][key] = value
            with self.subTest(key=key), self.assertRaises(PhotographyError):
                profile_identity(invalid)
        for invalid in ("latest", ">=1.0", "*"):
            profile["runtime"]["packages"]["numpy"] = invalid
            with self.subTest(version=invalid), self.assertRaises(PhotographyError):
                profile_identity(profile)

    def test_strict_types_finiteness_and_parameter_ranges(self):
        mutations = [
            ("color", "palette_size", True), ("color", "hue_bins", 12.0),
            ("color", "palette_size", 0), ("color", "max_sample_dimension", 257),
            ("color", "low_saturation_threshold", float("nan")),
            ("ocr", "use_cls", 1), ("ocr", "max_blocks", 0),
            ("objects", "input_size", 416.0), ("objects", "class_agnostic_nms", 0),
            ("objects", "score_threshold", float("inf")),
            ("perceptual_hash", "bits", 64.0), ("perceptual_hash", "algorithm", "phash"),
        ]
        for component, key, value in mutations:
            profile = default_profile(component)
            profile["parameters"][key] = value
            with self.subTest(component=component, key=key, value=value), self.assertRaises(PhotographyError):
                profile_identity(profile)
        for value in (True, 1.0, 2):
            profile = default_profile("color")
            profile["output_schema_version"] = value
            with self.subTest(version=value), self.assertRaises(PhotographyError):
                profile_identity(profile)

    def test_scene_catalog_identity_and_dimensions_are_explicit(self):
        profile = scene_profile()
        self.assertNotIn("dimensions", profile["parameters"])
        before = profile_identity(profile)[0]
        profile["parameters"]["dimensions"] = 3
        self.assertNotEqual(before, profile_identity(profile)[0])
        for bad in (True, 0, 4097, None):
            profile["parameters"]["dimensions"] = bad
            with self.subTest(dimensions=bad), self.assertRaises(PhotographyError):
                profile_identity(profile)
        profile = scene_profile()
        profile["parameters"]["catalog"][1]["scene_id"] = "mixed"
        with self.assertRaises(PhotographyError):
            profile_identity(profile)

    def test_importing_contracts_adds_no_image_or_inference_runtime_imports(self):
        script = (
            "import sys; sys.path.insert(0, r'photography\\scripts'); "
            "import photography_lib; before = set(sys.modules); "
            "from photography_lib import feature_profiles, feature_algorithms; "
            "feature_profiles.default_profile('ocr'); "
            "added = set(sys.modules) - before; "
            "assert not any(n.split('.')[0] in "
            "('PIL', 'numpy', 'torch', 'transformers', 'rapidocr', 'onnxruntime', 'scipy') for n in added)"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=PROJECT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class PayloadTests(unittest.TestCase):
    def test_ocr_normalization_raw_text_and_nullable_confidence(self):
        profile, payload = default_profile("ocr"), ocr_result()
        saved = validate_payload(profile, payload)
        self.assertEqual(saved["normalized_text"], "abc café 中文")
        self.assertEqual(saved["text"], payload["text"])
        saved["blocks"][0]["polygon"][0][0] = 0.2
        self.assertEqual(payload["blocks"][0]["polygon"][0][0], 0)
        payload["blocks"][0]["recognition_score"] = None
        validate_payload(profile, payload)
        validate_payload(profile, ocr_result(""))
        self.assertEqual(normalize_ocr_text("Straße\tＡＢＣ\u2003中国\n中文"), "strasse abc 中国 中文")

    def test_ocr_text_and_blocks_must_agree(self):
        for key, value in (("text", "unrelated"), ("normalized_text", "wrong"), ("complete", 1)):
            payload = ocr_result()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(PhotographyError):
                validate_payload(default_profile("ocr"), payload)
        payload = ocr_result("first")
        payload["blocks"].append({**payload["blocks"][0], "text": "second"})
        payload.update(text="first\nsecond", normalized_text="first second")
        validate_payload(default_profile("ocr"), payload)

    def test_ocr_polygon_and_confidence_validation(self):
        polygons = [
            [[0, 0], [1, 0], [1, 1]],
            [[0, 0], [1, 1], [1, 0], [0, 1]],
            [[0, 0], [0, 0], [1, 1], [0, 1]],
            [[0, 0], [2, 0], [1, 1], [0, 1]],
            [[True, 0], [1, 0], [1, 1], [0, 1]],
        ]
        for polygon in polygons:
            payload = ocr_result()
            payload["blocks"][0]["polygon"] = polygon
            with self.subTest(polygon=polygon), self.assertRaises(PhotographyError):
                validate_payload(default_profile("ocr"), payload)
        for score in (float("nan"), float("inf"), True, -0.1, 1.1):
            payload = ocr_result()
            payload["blocks"][0]["detection_score"] = score
            with self.subTest(score=score), self.assertRaises(PhotographyError):
                validate_payload(default_profile("ocr"), payload)

    def test_ocr_limits_and_pixel_fields_are_rejected(self):
        profile = default_profile("ocr")
        profile["parameters"]["max_text_chars"] = 2
        with self.assertRaises(PhotographyError):
            validate_payload(profile, ocr_result("long"))
        for location in ("root", "block"):
            payload = ocr_result()
            (payload if location == "root" else payload["blocks"][0])["image_base64"] = "AA=="
            with self.subTest(location=location), self.assertRaises(PhotographyError):
                validate_payload(default_profile("ocr"), payload)

    def test_objects_classes_boxes_scores_and_cardinality(self):
        profile = default_profile("objects")
        self.assertEqual(len(COCO_CLASS_IDS), 80)
        validate_payload(profile, object_result())
        valid = detection([0, 0, 1, 1])
        for key, value in (("class_id", "fictional"), ("score", 0.1), ("score", None),
                           ("score", True), ("bbox", [0, 0, 0, 1]),
                           ("bbox", [0, 0, 1, float("nan")]), ("bbox", [0, False, 1, 1])):
            item = {**valid, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(PhotographyError):
                validate_payload(profile, object_result([item]))
        profile["parameters"]["max_detections"] = 1
        with self.assertRaises(PhotographyError):
            validate_payload(profile, object_result([valid, valid], complete=False))

    def test_unknown_nested_result_fields_reject_without_forwarding_pixels(self):
        payload = object_result([detection([0, 0, 1, 1])])
        payload["objects"][0]["pixels"] = [[1, 2, 3]]
        with self.assertRaises(PhotographyError) as raised:
            validate_payload(default_profile("objects"), payload)
        self.assertNotIn("[1, 2, 3]", str(raised.exception))
        self.assertEqual(raised.exception.code, "FEATURE_PAYLOAD_INVALID")


class ProviderExecutionTests(unittest.TestCase):
    def test_unknown_and_wrong_providers_remain_readable_but_cannot_compute(self):
        data = preview(Image.new("RGB", (9, 8), "red"))
        scene = scene_profile()
        prototypes = make_scene_prototypes(scene, FakeEncoder())
        cases = [
            (default_profile("color"), lambda p: compute_color(data, p), "pillow-dhash-v1"),
            (default_profile("perceptual_hash"), lambda p: compute_hash(data, p), "pillow-color-v1"),
            (default_profile("composition", dependency_profile_id="fixture"),
             lambda p: compute_composition(object_result(), p), "embedding-cosine-v1"),
            (scene, lambda p: compute_scene([1, 0, 0], prototypes, p), "box-geometry-v1"),
        ]
        for profile, compute, wrong_provider in cases:
            payload = compute(profile)
            for provider in ("unknown-provider-v99", wrong_provider):
                stored = copy.deepcopy(profile)
                stored["provider"] = provider
                with self.subTest(component=profile["component"], provider=provider):
                    self.assertNotEqual(profile_identity(stored), profile_identity(profile))
                    self.assertEqual(validate_payload(stored, payload), payload)
                    if stored["component"] == "scene":
                        self.assertEqual(validate_scene_prototypes(stored, prototypes), prototypes)
                    with self.assertRaises(PhotographyError) as caught:
                        compute(stored)
                    self.assertEqual(caught.exception.code, "FEATURE_PROVIDER_UNSUPPORTED")

    def test_unknown_scene_provider_cannot_encode_prototypes(self):
        for provider in ("unknown-provider-v99", "box-geometry-v1"):
            profile, encoder = scene_profile(), FakeEncoder()
            profile["provider"] = provider
            with self.subTest(provider=provider), self.assertRaises(PhotographyError) as caught:
                make_scene_prototypes(profile, encoder)
            self.assertEqual(caught.exception.code, "FEATURE_PROVIDER_UNSUPPORTED")
            self.assertEqual(encoder.calls, [])

    def test_workflow_rejects_unknown_provider_without_persisting_result_provenance(self):
        from functools import partial
        import tempfile
        from unittest.mock import patch
        from tests.test_feature_index import FeatureIndexTests
        from photography_lib import feature_index

        state = PROJECT / ".photography-state"
        state.mkdir(exist_ok=True)
        fixture = FeatureIndexTests()
        self.addCleanup(fixture.doCleanups)
        with patch("tempfile.TemporaryDirectory", partial(tempfile.TemporaryDirectory, dir=state)):
            fixture.setUp()
        for component in ("color", "perceptual_hash"):
            with self.subTest(component=component):
                profile = default_profile(component)
                profile["provider"] = "unknown-provider-v99"
                profile_id = fixture.store.put_feature_profile(profile)
                pid = fixture.ids[0]
                result = fixture.execute(fixture.plan(profile, [pid]))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["items"][0]["error"]["code"], "FEATURE_PROVIDER_UNSUPPORTED")
                self.assertEqual(result["model_calls"], 0)
                self.assertFalse(fixture.store.has_feature_results(pid, profile_id))
                self.assertEqual(fixture.store.feature_history(pid, profile_id), [])
                current = feature_index.current_result(pid, store=fixture.store, profile=profile)
                self.assertEqual(current["status"], "missing")


class ColorTests(unittest.TestCase):
    def test_uniform_primary_colors_and_achromatic_images(self):
        profile = default_profile("color")
        for rgb, hue_bin in (((255, 0, 0), 0), ((0, 255, 0), 4), ((0, 0, 255), 8)):
            result = compute_color(preview(Image.new("RGB", (20, 10), rgb)), profile)
            with self.subTest(rgb=rgb):
                self.assertEqual(result["palette"], [{"rgb": list(rgb), "fraction": 1.0}])
                self.assertEqual(result["features"]["hue_histogram"][hue_bin], 1.0)
                self.assertEqual(result["features"]["mean_saturation"], 1.0)
                self.assertEqual(result["features"]["low_saturation_fraction"], 0.0)
        for rgb in ((0, 0, 0), (255, 255, 255), (128, 128, 128)):
            result = compute_color(preview(Image.new("RGB", (1, 1), rgb)), profile)
            self.assertEqual(result["features"]["mean_saturation"], 0.0)
            self.assertEqual(result["features"]["low_saturation_fraction"], 1.0)

    def test_palette_proportions_sorting_and_determinism(self):
        image = Image.new("RGB", (8, 8), "blue")
        image.paste("red", (0, 0, 4, 8))
        data, profile = preview(image), default_profile("color")
        first = compute_color(data, profile)
        self.assertEqual(first, compute_color(data, profile))
        self.assertEqual(first["palette"], [
            {"rgb": [0, 0, 255], "fraction": 0.5},
            {"rgb": [255, 0, 0], "fraction": 0.5},
        ])
        profile["parameters"]["palette_size"] = 1
        self.assertEqual(len(compute_color(data, profile)["palette"]), 1)

    def test_large_non_square_sampling_preserves_input_dimensions(self):
        result = compute_color(preview(Image.new("RGB", (1024, 100), "blue")), default_profile("color"))
        self.assertEqual((result["width"], result["height"]), (1024, 100))
        self.assertEqual(result["palette"], [{"rgb": [0, 0, 255], "fraction": 1.0}])

    def test_distribution_cardinality_totals_and_types(self):
        profile = default_profile("color")
        result = compute_color(preview(Image.new("RGB", (8, 8), "blue")), profile)
        for key, value in (("hue_histogram", [1.0] * 12), ("hue_histogram", [1.0] * 11),
                           ("mean_saturation", True), ("low_saturation_fraction", float("nan"))):
            bad = copy.deepcopy(result)
            bad["features"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PhotographyError):
                validate_payload(profile, bad)
        for key, value in (("fraction", 0.5), ("rgb", [False, 0, 255]), ("rgb", [0, 0, 256])):
            bad = copy.deepcopy(result)
            bad["palette"][0][key] = value
            with self.subTest(key=key), self.assertRaises(PhotographyError):
                validate_payload(profile, bad)

    def test_image_inputs_do_not_accept_paths_urls_pixels_or_implicit_conversion(self):
        for function, component in ((compute_color, "color"), (compute_hash, "perceptual_hash")):
            for data in ("C:\\photo.jpg", "https://example.test/photo.jpg", [[0, 0, 0]], b"invalid",
                         preview(Image.new("RGBA", (3, 3), "red"))):
                with self.subTest(function=function.__name__, input_type=type(data)), self.assertRaises(PhotographyError):
                    function(data, default_profile(component))


class HashTests(unittest.TestCase):
    def test_hash_bit_order_and_strict_comparison(self):
        profile = default_profile("perceptual_hash")
        image = Image.new("RGB", (9, 8), (128, 128, 128))
        self.assertEqual(compute_hash(preview(image), profile)["hash_hex"], "0000000000000000")
        image.putpixel((0, 0), (255, 255, 255))
        self.assertEqual(compute_hash(preview(image), profile)["hash_hex"], "8000000000000000")
        image.putpixel((7, 7), (255, 255, 255))
        self.assertEqual(compute_hash(preview(image), profile)["hash_hex"], "8000000000000001")

    def test_gradients_and_exact_hamming_distance(self):
        profile = default_profile("perceptual_hash")
        image = Image.new("RGB", (9, 8))
        image.putdata([(255 - x * 25,) * 3 for _ in range(8) for x in range(9)])
        descending = compute_hash(preview(image), profile)
        image.putdata([(x * 25,) * 3 for _ in range(8) for x in range(9)])
        ascending = compute_hash(preview(image), profile)
        self.assertEqual(descending["hash_hex"], "ffffffffffffffff")
        self.assertEqual(ascending["hash_hex"], "0000000000000000")
        self.assertEqual(hash_distance(descending, ascending), 64)
        self.assertEqual(hash_distance(descending, descending), 0)
        changed = {**ascending, "hash_hex": "0000000000000003"}
        self.assertEqual(hash_distance(changed, ascending), 2)
        self.assertEqual(hash_distance(ascending, changed), 2)

    def test_hash_type_algorithm_length_and_optional_phash_rejection(self):
        profile = default_profile("perceptual_hash")
        valid = compute_hash(preview(Image.new("RGB", (9, 8))), profile)
        for key, value in (("bits", True), ("bits", 63), ("algorithm", "phash"),
                           ("hash_hex", "FF" * 8), ("hash_hex", "0" * 15), ("hash_hex", 0)):
            with self.subTest(key=key, value=value), self.assertRaises(PhotographyError):
                hash_distance(valid, {**valid, key: value})
        profile["parameters"]["algorithm"] = "phash"
        with self.assertRaises(PhotographyError):
            compute_hash(preview(Image.new("RGB", (9, 8))), profile)

    def test_nontransitive_similarity_chain(self):
        a = {"complete": True, "algorithm": "dhash", "bits": 64, "hash_hex": "0000000000000000"}
        b, c = {**a, "hash_hex": "0000000000000001"}, {**a, "hash_hex": "0000000000000003"}
        self.assertEqual((hash_distance(a, b), hash_distance(b, c), hash_distance(a, c)), (1, 1, 2))


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.profile = default_profile("composition", dependency_profile_id="objects-fixture")

    def test_overlap_union_bounding_and_incomplete_source(self):
        payload = object_result([detection([0, 0, 0.5, 0.5]), detection([0.25, 0.25, 0.75, 0.75])],
                                complete=False)
        result = compute_composition(payload, self.profile)
        features = result["features"]
        self.assertFalse(result["complete"])
        self.assertEqual(features["union_area"], 0.4375)
        self.assertEqual(features["bounding_area"], 0.5625)
        self.assertEqual(features["uncovered_fraction"], 0.5625)
        self.assertEqual(features["subject_index"], 0)
        self.assertEqual((features["center_x"], features["center_y"], features["subject_area"]), (.25, .25, .25))
        self.assertAlmostEqual(features["thirds_distance"], math.hypot(.25 - 1 / 3, .25 - 1 / 3))

    def test_nested_disjoint_and_touching_rectangles(self):
        cases = [
            ([[0, 0, 1, 1], [.2, .2, .8, .8]], 1),
            ([[0, 0, .25, .25], [.75, .75, 1, 1]], .125),
            ([[0, 0, .5, 1], [.5, 0, 1, 1]], 1),
            ([[0, 0, 1, .5], [0, .5, 1, 1]], 1),
            ([[0, 0, .5, .5]] * 3, .25),
        ]
        for boxes, expected in cases:
            result = compute_composition(object_result([detection(box) for box in boxes]), self.profile)
            with self.subTest(boxes=boxes):
                self.assertEqual(result["features"]["union_area"], expected)

    def test_subject_selection_area_then_score_then_stable_index(self):
        objects = [detection([0, 0, .25, .25], .99), detection([0, 0, .5, .5], .7, "dog"),
                   detection([0, 0, .5, .5], .8, "cat"), detection([.5, .5, 1, 1], .8, "dog")]
        payload = object_result(objects)
        self.assertEqual(compute_composition(payload, self.profile)["features"]["subject_index"], 2)
        self.profile["parameters"]["subject_class"] = "dog"
        self.assertEqual(compute_composition(payload, self.profile)["features"]["subject_index"], 3)
        self.profile["parameters"]["subject_class"] = "person"
        self.assertEqual(compute_composition(payload, self.profile)["features"]["subject_index"], 0)

    def test_empty_results_and_no_matching_subject_are_nullable(self):
        result = compute_composition(object_result(), self.profile)
        self.assertEqual(result["features"], {
            "subject_index": None, "center_x": None, "center_y": None, "subject_area": None,
            "thirds_distance": None, "union_area": 0.0, "bounding_area": 0.0, "uncovered_fraction": 1.0,
        })
        self.profile["parameters"]["subject_class"] = "cat"
        result = compute_composition(object_result([detection([0, 0, 1, 1])]), self.profile)
        self.assertIsNone(result["features"]["subject_index"])
        self.assertEqual(result["features"]["union_area"], 1)

    def test_invalid_source_and_inconsistent_derived_geometry_are_rejected(self):
        source = object_result([detection([0, 0, 1, 1])])
        result = compute_composition(source, self.profile)
        for key, value in (("union_area", 1.1), ("bounding_area", .5),
                           ("uncovered_fraction", .5), ("thirds_distance", 0),
                           ("subject_index", True), ("subject_area", None)):
            bad = copy.deepcopy(result)
            bad["features"][key] = value
            with self.subTest(key=key), self.assertRaises(PhotographyError):
                validate_payload(self.profile, bad)
        source["pixels"] = [[1, 2, 3]]
        with self.assertRaises(PhotographyError):
            compute_composition(source, self.profile)


class SceneTests(unittest.TestCase):
    def test_prototypes_average_normalize_and_preserve_catalog(self):
        encoder, profile = FakeEncoder(), scene_profile()
        prototypes = make_scene_prototypes(profile, encoder)
        self.assertEqual(encoder.calls, ["one", "two", "three"])
        self.assertEqual(prototypes[0]["prompts"], ["one", "two"])
        self.assertEqual(prototypes[0]["label"], "Mixed scene")
        self.assertEqual(len(prototypes[0]["vector"]), encoder.profile()["dimensions"])
        self.assertAlmostEqual(prototypes[0]["vector"][0], 1 / math.sqrt(2))
        self.assertAlmostEqual(math.hypot(*prototypes[0]["vector"]), 1)
        self.assertEqual(prototypes[1]["vector"], [-1.0, 0.0, 0.0])

    def test_each_prompt_is_encoded_once_across_catalog(self):
        encoder, profile = FakeEncoder(), scene_profile()
        profile["parameters"]["catalog"][1]["prompts"] = ["one"]
        make_scene_prototypes(profile, encoder)
        self.assertEqual(encoder.calls, ["one", "two"])

    def test_full_cosines_are_not_probabilities(self):
        profile = scene_profile()
        prototypes = make_scene_prototypes(profile, FakeEncoder())
        result = compute_scene([1, 0, 0], prototypes, profile)
        self.assertEqual([score["scene_id"] for score in result["scores"]], ["mixed", "opposite"])
        self.assertAlmostEqual(result["scores"][0]["score"], 1 / math.sqrt(2))
        self.assertEqual(result["scores"][1]["score"], -1)
        self.assertTrue(result["complete"])
        self.assertEqual(result, compute_scene([1, 0, 0], prototypes, profile))

    def test_wrong_dependency_and_dimensions_do_not_encode_any_prompt(self):
        for mutation in ("dependency", "model", "dimensions"):
            profile, encoder = scene_profile(), FakeEncoder()
            if mutation == "dependency":
                profile["dependencies"]["image_embedding"] = "different-profile"
            elif mutation == "model":
                encoder.identity["model"] = "different-encoder-same-dimensions"
            else:
                profile["parameters"]["dimensions"] = 4
            with self.subTest(mutation=mutation), self.assertRaises(PhotographyError):
                make_scene_prototypes(profile, encoder)
            self.assertEqual(encoder.calls, [])

    def test_zero_nonunit_nonfinite_wrong_dimension_and_boolean_vectors_fail(self):
        profile = scene_profile()
        prototypes = make_scene_prototypes(profile, FakeEncoder())
        for vector in ([0, 0, 0], [2, 0, 0], [True, 0, 0], [float("nan"), 0, 0], [1, 0],
                       [float("inf"), 0, 0], ["1", 0, 0]):
            with self.subTest(vector=vector), self.assertRaises(PhotographyError):
                compute_scene(vector, prototypes, profile)
        with self.assertRaises(PhotographyError):
            make_scene_prototypes(profile, FakeEncoder({"one": [1, 0, 0], "two": [-1, 0, 0]}))

    def test_prototype_and_score_cardinality_identity_and_detachment(self):
        profile = scene_profile()
        prototypes = make_scene_prototypes(profile, FakeEncoder())
        for change in ("missing", "label", "prompt", "vector", "pixels", "order"):
            bad = copy.deepcopy(prototypes)
            if change == "missing":
                bad.pop()
            elif change == "label":
                bad[0]["label"] = "wrong"
            elif change == "prompt":
                bad[0]["prompts"] = ["wrong"]
            elif change == "vector":
                bad[0]["vector"] = [1, 0]
            elif change == "pixels":
                bad[0]["pixels"] = [[255, 0, 0]]
            else:
                bad.reverse()
            with self.subTest(change=change), self.assertRaises(PhotographyError):
                compute_scene([1, 0, 0], bad, profile)
        detached = validate_scene_prototypes(profile, prototypes)
        detached[0]["vector"][0] = 100
        self.assertNotEqual(detached, prototypes)
        payload = compute_scene([1, 0, 0], prototypes, profile)
        payload["scores"].pop()
        with self.assertRaises(PhotographyError):
            validate_payload(profile, payload)

    def test_default_catalog_is_small_versioned_and_stable(self):
        profile = default_profile("scene", dependency_profile_id="fixture")
        self.assertEqual(profile["parameters"]["catalog_version"], "photography-scenes-v1")
        self.assertEqual([entry["scene_id"] for entry in profile["parameters"]["catalog"]],
                         ["indoor", "outdoor", "city", "beach", "forest", "mountain", "night", "snow"])


if __name__ == "__main__":
    unittest.main()
