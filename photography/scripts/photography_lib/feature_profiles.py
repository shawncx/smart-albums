"""Versioned feature recipes and strict, pixel-free JSON result contracts."""
from __future__ import annotations

import copy
import json
import math
import re
import unicodedata

from .config import PhotographyError
from .fingerprints import fingerprint

COMPONENTS = ("ocr", "objects", "scene", "color", "composition", "perceptual_hash")
INPUT_SCOPES = {
    "ocr": "original", "objects": "stored_thumbnail", "scene": "image_embedding",
    "color": "stored_thumbnail", "composition": "object_result",
    "perceptual_hash": "stored_thumbnail",
}
COCO_CLASS_IDS = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)
_SCENES = (
    ("indoor", "Indoor", ("A photograph taken indoors.", "An indoor scene.")),
    ("outdoor", "Outdoor", ("A photograph taken outdoors.", "An outdoor scene.")),
    ("city", "City", ("A photograph of a city street.", "An urban city scene.")),
    ("beach", "Beach", ("A photograph of a beach.", "A sandy beach beside the sea.")),
    ("forest", "Forest", ("A photograph of a forest.", "A woodland scene with trees.")),
    ("mountain", "Mountain", ("A photograph of mountains.", "A mountainous landscape.")),
    ("night", "Night", ("A photograph taken at night.", "A nighttime scene.")),
    ("snow", "Snow", ("A photograph of a snowy landscape.", "A scene covered in snow.")),
)
_PARAMETERS = {
    "ocr": {
        "model": "PP-OCRv6-small", "orientation_model": "ch_ppocr_mobile_v2.0_cls",
        "orientation": "pillow-exif-transpose", "use_cls": False,
        "det_limit_side_len": 1280, "det_limit_type": "max",
        "det_thresh": 0.3, "det_box_thresh": 0.5, "det_unclip_ratio": 1.6,
        "text_score": 0.5, "max_blocks": 4096, "max_text_chars": 1000000,
        "max_input_pixels": 80000000, "normalization": "nfkc-casefold-whitespace-v1",
        "use_preprocess_img": False, "use_vertical_padding": False,
        "det_max_candidates": 2147483647, "dictionary": "embedded-in-rec.onnx",
    },
    "objects": {
        "model": "yolox-nano", "input_size": 416, "color_order": "BGR",
        "resize": "opencv-inter-linear", "letterbox": "top-left", "pad_value": 114,
        "dtype": "float32", "strides": [8, 16, 32], "score_threshold": 0.3,
        "nms_threshold": 0.45, "class_agnostic_nms": True, "max_detections": 300,
        "labels": list(COCO_CLASS_IDS), "preprocess": "bgr-top-left-letterbox-114",
        "orientation": "pillow-exif-transpose", "nms_coordinate_offset": 1.0,
    },
    "scene": {
        "catalog_version": "photography-scenes-v1",
        "catalog": [{"scene_id": sid, "label": label, "prompts": list(prompts)}
                    for sid, label, prompts in _SCENES],
        "aggregation": "mean-l2", "metric": "cosine",
    },
    "color": {
        "color_space": "srgb", "input_mode": "RGB", "max_sample_dimension": 256,
        "palette_size": 8, "quantization": "median-cut", "dither": "none",
        "resize": "lanczos", "hsv_encoding": "pillow-uint8",
        "hue_bins": 12, "low_saturation_threshold": 0.1,
    },
    "composition": {
        "subject_class": None, "subject_selection": "largest-area-score-index",
        "coordinates": "normalized-xyxy", "union": "rectangle-sweep",
        "thirds_distance": "euclidean-normalized",
    },
    "perceptual_hash": {
        "algorithm": "dhash", "bits": 64, "resize": "lanczos",
        "grayscale": "pillow-L", "width": 9, "height": 8,
        "comparison": "left-greater-than-right", "bit_order": "row-major-msb-first",
    },
}
_PROVIDERS = {
    "ocr": "rapidocr-3.9.2-onnx-v1", "objects": "yolox-nano-onnx-v1",
    "scene": "embedding-cosine-v1", "color": "pillow-color-v1",
    "composition": "box-geometry-v1", "perceptual_hash": "pillow-dhash-v1",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _fields(value, required, optional=()):
    _require(isinstance(value, dict), "Expected a JSON object.")
    keys = set(value)
    _require(set(required) <= keys and keys <= set(required) | set(optional),
             "Missing or unknown JSON fields.")


def _integer(value, low=0, high=None):
    _require(type(value) is int and value >= low and (high is None or value <= high),
             "Integer outside the supported range.")


def _number(value, low=0.0, high=1.0):
    _require(type(value) in (int, float) and math.isfinite(value)
             and low <= value <= high, "Expected a finite number in range.")


def _string(value, *, nonempty=True):
    _require(isinstance(value, str) and (not nonempty or bool(value.strip())),
             "Expected a string." if not nonempty else "Expected a nonblank string.")
    value.encode("utf-8")


def _boolean(value):
    _require(type(value) is bool, "Expected a boolean, not an integer.")


def _json(value):
    if isinstance(value, dict):
        for key, item in value.items():
            _string(key)
            _json(item)
    elif isinstance(value, list):
        for item in value:
            _json(item)
    elif isinstance(value, str):
        _string(value, nonempty=False)
    else:
        _require(value is None or type(value) is bool or type(value) is int
                 or (type(value) is float and math.isfinite(value)),
                 "Only finite JSON values are accepted.")


def _identity_text(value):
    _string(value)
    _require(not any(char in value for char in ("\\", "/", "\x00")),
             "Machine paths and source URLs are not profile identity fields.")


def _validate_runtime(runtime):
    strings = {"backend", "device", "dtype", "python", "architecture", "platform",
               "implementation", "provider", "version", "opencv", "execution_mode",
               "machine", "execution_provider"}
    integers = {"max_cpu_threads", "intra_op_num_threads", "inter_op_num_threads",
                "batch_size", "threads", "intra_op_threads", "inter_op_threads"}
    booleans = {"local_files_only", "trust_remote_code", "use_cuda"}
    _fields(runtime, (), strings | integers | booleans | {"packages", "providers"})
    for key, value in runtime.items():
        if key in strings:
            _identity_text(value)
        elif key in integers:
            _integer(value, 1, 1024)
        elif key in booleans:
            _boolean(value)
        elif key == "providers":
            _require(isinstance(value, list) and bool(value), "Runtime providers must be a list.")
            for provider in value:
                _identity_text(provider)
        elif key == "packages":
            _require(isinstance(value, dict), "Runtime packages must be a version map.")
            for package, version in value.items():
                _identity_text(package)
                _identity_text(version)
                _require(version.casefold() not in {"latest", "main", "master"}
                         and not any(char in version for char in "*<>=!~"),
                         "Runtime package versions must be pinned.")


def _validate_assets(assets):
    _require(isinstance(assets, list), "Assets must be a list.")
    names = set()
    for asset in assets:
        _fields(asset, {"sha256"}, {"name", "role", "filename", "size", "size_bytes",
                "revision", "license", "model", "version", "format"})
        _require(re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) is not None,
                 "Asset SHA-256 must be lowercase hexadecimal.")
        _require(any(key in asset for key in ("name", "role", "filename")),
                 "Every asset needs a stable name, filename or role.")
        for key, value in asset.items():
            if key in {"size", "size_bytes"}:
                _integer(value, 1)
            else:
                _identity_text(value)
        name = asset.get("role", asset.get("name", asset.get("filename")))
        _require(name not in names, "Duplicate asset identities.")
        names.add(name)


def _validate_parameters(component, parameters):
    _fields(parameters, _PARAMETERS[component], {"dimensions"} if component == "scene" else ())
    p = parameters
    if component == "ocr":
        for key in ("model", "orientation_model", "det_limit_type", "normalization",
                    "orientation", "dictionary", "use_preprocess_img", "use_vertical_padding",
                    "det_max_candidates"):
            _require(p[key] == _PARAMETERS[component][key], "Unsupported OCR recipe.")
        for key in ("use_cls", "use_preprocess_img", "use_vertical_padding"):
            _boolean(p[key])
        _integer(p["det_limit_side_len"], 32, 4096)
        _integer(p["det_max_candidates"], 2147483647, 2147483647)
        for key in ("det_thresh", "det_box_thresh", "text_score"):
            _number(p[key])
        _number(p["det_unclip_ratio"], 0.1, 10)
        _integer(p["max_blocks"], 1, 100000)
        _integer(p["max_text_chars"], 1, 10000000)
        _integer(p["max_input_pixels"], 1, 1000000000)
    elif component == "objects":
        for key in ("model", "input_size", "color_order", "resize", "letterbox",
                    "pad_value", "dtype", "strides", "labels", "preprocess",
                    "orientation", "nms_coordinate_offset"):
            _require(p[key] == _PARAMETERS[component][key], "Unsupported object-detector recipe.")
        _integer(p["input_size"], 416, 416)
        _integer(p["pad_value"], 114, 114)
        _require(isinstance(p["strides"], list), "Strides must be a list.")
        for stride in p["strides"]:
            _integer(stride, 1)
        _number(p["score_threshold"])
        _number(p["nms_threshold"])
        _number(p["nms_coordinate_offset"], 1, 1)
        _boolean(p["class_agnostic_nms"])
        _integer(p["max_detections"], 1, 100000)
    elif component == "scene":
        _string(p["catalog_version"])
        _require(p["aggregation"] == "mean-l2" and p["metric"] == "cosine",
                 "Unsupported scene recipe.")
        if "dimensions" in p:
            _integer(p["dimensions"], 1, 4096)
        _require(isinstance(p["catalog"], list) and 1 <= len(p["catalog"]) <= 256,
                 "Scene catalog must have between 1 and 256 entries.")
        scene_ids = set()
        for scene in p["catalog"]:
            _fields(scene, ("scene_id", "label", "prompts"))
            _string(scene["scene_id"])
            _string(scene["label"])
            _require(scene["scene_id"] not in scene_ids, "Scene IDs must be unique.")
            scene_ids.add(scene["scene_id"])
            _require(isinstance(scene["prompts"], list) and 1 <= len(scene["prompts"]) <= 32,
                     "Each scene needs between 1 and 32 prompts.")
            for prompt in scene["prompts"]:
                _string(prompt)
            _require(len(scene["prompts"]) == len(set(scene["prompts"])),
                     "A scene cannot repeat a prompt.")
    elif component == "color":
        for key in ("color_space", "input_mode", "quantization", "dither", "resize",
                    "hsv_encoding", "hue_bins"):
            _require(p[key] == _PARAMETERS[component][key], "Unsupported color recipe.")
        _integer(p["hue_bins"], 12, 12)
        _integer(p["max_sample_dimension"], 1, 256)
        _integer(p["palette_size"], 1, 256)
        _number(p["low_saturation_threshold"])
    elif component == "composition":
        if p["subject_class"] is not None:
            _string(p["subject_class"])
        for key in ("subject_selection", "coordinates", "union", "thirds_distance"):
            _require(p[key] == _PARAMETERS[component][key], "Unsupported composition recipe.")
    else:
        _require(p == _PARAMETERS[component], "Only the declared dHash64 recipe is supported.")
        for key in ("bits", "width", "height"):
            _integer(p[key], 1)


def _validate_profile(profile):
    _fields(profile, ("schema", "component", "provider", "input_scope",
                      "output_schema_version", "parameters", "dependencies", "runtime", "assets"))
    _json(profile)
    _require(profile["schema"] == "image-feature-profile-v1", "Unsupported feature profile schema.")
    component = profile["component"]
    _require(isinstance(component, str) and component in COMPONENTS, "Unknown feature component.")
    _identity_text(profile["provider"])
    _require(profile["input_scope"] == INPUT_SCOPES[component], "Component and input scope disagree.")
    _integer(profile["output_schema_version"], 1, 1)
    dependency = {"scene": "image_embedding", "composition": "objects"}.get(component)
    _fields(profile["dependencies"], (dependency,) if dependency else ())
    if dependency:
        _string(profile["dependencies"][dependency])
    _validate_parameters(component, profile["parameters"])
    _validate_runtime(profile["runtime"])
    _validate_assets(profile["assets"])


def default_profile(component, *, dependency_profile_id=None):
    """Return an independent recipe; setup may bind verified runtime and asset records."""
    if not isinstance(component, str) or component not in COMPONENTS:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "Unknown feature component.")
    dependency = {"scene": "image_embedding", "composition": "objects"}.get(component)
    if dependency is None and dependency_profile_id is not None:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "This component has no profile dependency.")
    runtime = {}
    if component in {"color", "perceptual_hash"}:
        runtime = {"packages": {"Pillow": "12.3.0"}}
    elif component == "ocr":
        runtime = {"backend": "onnxruntime", "device": "cpu",
                   "packages": {"rapidocr": "3.9.2"}, "providers": ["CPUExecutionProvider"]}
    elif component == "objects":
        runtime = {"backend": "onnxruntime", "device": "cpu", "providers": ["CPUExecutionProvider"]}
    result = {
        "schema": "image-feature-profile-v1", "component": component,
        "provider": _PROVIDERS[component], "input_scope": INPUT_SCOPES[component],
        "output_schema_version": 1, "parameters": copy.deepcopy(_PARAMETERS[component]),
        "dependencies": {dependency: dependency_profile_id} if dependency else {},
        "runtime": runtime, "assets": [],
    }
    profile_identity(result)
    return result


def profile_identity(profile):
    """Validate a recipe and return its SHA-256 and canonical finite JSON."""
    try:
        _validate_profile(profile)
        encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return fingerprint(profile), encoded
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "Invalid feature profile: " + str(exc)) from exc


def normalize_ocr_text(text):
    """Normalize only the search field; raw OCR text remains unchanged."""
    if not isinstance(text, str):
        raise PhotographyError("FEATURE_PAYLOAD_INVALID", "OCR text must be a string.")
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _size(payload):
    _integer(payload["width"], 1, 1000000)
    _integer(payload["height"], 1, 1000000)


def _bbox(box):
    _require(isinstance(box, list) and len(box) == 4, "Expected an xyxy bounding box.")
    for coordinate in box:
        _number(coordinate)
    _require(box[0] < box[2] and box[1] < box[3], "Bounding boxes must have positive area.")


def _polygon(polygon):
    _require(isinstance(polygon, list) and len(polygon) == 4, "Expected a quadrilateral.")
    for point in polygon:
        _require(isinstance(point, list) and len(point) == 2, "Expected xy polygon vertices.")
        for coordinate in point:
            _number(coordinate)
    crosses = []
    for index in range(4):
        a, b, c = (polygon[(index + offset) % 4] for offset in range(3))
        crosses.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    _require(all(cross > 0 for cross in crosses) or all(cross < 0 for cross in crosses),
             "OCR polygons must be convex, ordered and have positive area.")


def _objects(payload, *, class_ids=None, max_objects=100000, score_threshold=0.0):
    _fields(payload, ("width", "height", "complete", "objects"))
    _size(payload)
    _boolean(payload["complete"])
    _require(isinstance(payload["objects"], list) and len(payload["objects"]) <= max_objects,
             "Object result exceeds its declared cardinality.")
    for item in payload["objects"]:
        _fields(item, ("class_id", "score", "bbox"))
        _string(item["class_id"])
        _require(class_ids is None or item["class_id"] in class_ids, "Unknown object class.")
        _number(item["score"], score_threshold, 1)
        _bbox(item["bbox"])


def _distribution(values, count):
    _require(isinstance(values, list) and len(values) == count, "Distribution has incorrect cardinality.")
    for number in values:
        _number(number)
    _require(math.isclose(math.fsum(values), 1.0, abs_tol=1e-6, rel_tol=0),
             "Distribution fractions must sum to one.")


def _validate_payload(profile, payload):
    component, p = profile["component"], profile["parameters"]
    _require(isinstance(payload, dict), "Expected a result object.")
    if component == "objects":
        _objects(payload, class_ids=p["labels"], max_objects=p["max_detections"],
                 score_threshold=p["score_threshold"])
    elif component == "ocr":
        _fields(payload, ("width", "height", "complete", "text", "normalized_text", "blocks"))
        _size(payload)
        _require(payload["width"] * payload["height"] <= p["max_input_pixels"], "OCR input exceeds pixel limit.")
        _string(payload["text"], nonempty=False)
        _string(payload["normalized_text"], nonempty=False)
        _require(len(payload["text"]) <= p["max_text_chars"], "OCR text exceeds its declared limit.")
        _require(isinstance(payload["blocks"], list) and len(payload["blocks"]) <= p["max_blocks"],
                 "OCR block count exceeds its declared limit.")
        for block in payload["blocks"]:
            _fields(block, ("text", "polygon", "recognition_score", "detection_score"))
            _string(block["text"], nonempty=False)
            _polygon(block["polygon"])
            for key in ("recognition_score", "detection_score"):
                if block[key] is not None:
                    _number(block[key])
        _require(payload["text"] == "\n".join(block["text"] for block in payload["blocks"]),
                 "Raw OCR text must be the newline-joined block text.")
        _require(payload["normalized_text"] == normalize_ocr_text(payload["text"]),
                 "Normalized OCR text does not match its raw text.")
    elif component == "scene":
        _fields(payload, ("complete", "scores"))
        _require(isinstance(payload["scores"], list)
                 and len(payload["scores"]) == len(p["catalog"]), "Scene scores must cover the entire catalog.")
        for score, scene in zip(payload["scores"], p["catalog"]):
            _fields(score, ("scene_id", "score"))
            _require(score["scene_id"] == scene["scene_id"], "Scene scores must follow catalog order.")
            _number(score["score"], -1, 1)
    elif component == "color":
        _fields(payload, ("width", "height", "complete", "features", "palette"))
        _size(payload)
        features = payload["features"]
        _fields(features, ("mean_saturation", "low_saturation_fraction", "hue_histogram"))
        _number(features["mean_saturation"])
        _number(features["low_saturation_fraction"])
        _distribution(features["hue_histogram"], p["hue_bins"])
        _require(isinstance(payload["palette"], list)
                 and 1 <= len(payload["palette"]) <= p["palette_size"], "Invalid palette cardinality.")
        colors, fractions = set(), []
        for entry in payload["palette"]:
            _fields(entry, ("rgb", "fraction"))
            _require(isinstance(entry["rgb"], list) and len(entry["rgb"]) == 3, "Expected an RGB triple.")
            for channel in entry["rgb"]:
                _integer(channel, 0, 255)
            rgb = tuple(entry["rgb"])
            _require(rgb not in colors, "Duplicate palette color.")
            colors.add(rgb)
            _number(entry["fraction"])
            _require(entry["fraction"] > 0, "Palette entries must represent pixels.")
            fractions.append(entry["fraction"])
        _distribution(fractions, len(fractions))
    elif component == "composition":
        _fields(payload, ("width", "height", "complete", "features"))
        _size(payload)
        features = payload["features"]
        subject_fields = ("center_x", "center_y", "subject_area", "thirds_distance")
        _fields(features, ("subject_index", *subject_fields, "union_area", "bounding_area", "uncovered_fraction"))
        for field in ("union_area", "bounding_area", "uncovered_fraction"):
            _number(features[field])
        _require(features["union_area"] <= features["bounding_area"] + 1e-12,
                 "Union area cannot exceed bounding area.")
        _require(math.isclose(features["uncovered_fraction"], 1 - features["union_area"],
                              abs_tol=1e-12, rel_tol=0), "Uncovered fraction must complement union area.")
        if features["subject_index"] is None:
            _require(all(features[field] is None for field in subject_fields), "Absent subject geometry must be null.")
        else:
            _integer(features["subject_index"])
            for field in subject_fields:
                _number(features[field])
            _require(features["subject_area"] > 0
                     and features["subject_area"] <= features["union_area"] + 1e-12,
                     "Subject area must be positive and within the union.")
            distance = min(math.hypot(features["center_x"] - x, features["center_y"] - y)
                           for x in (1 / 3, 2 / 3) for y in (1 / 3, 2 / 3))
            _require(math.isclose(features["thirds_distance"], distance, abs_tol=1e-12, rel_tol=0),
                     "Thirds distance does not match subject center.")
    else:
        _fields(payload, ("complete", "algorithm", "bits", "hash_hex"))
        _require(payload["algorithm"] == p["algorithm"], "Hash algorithm does not match the recipe.")
        _integer(payload["bits"], p["bits"], p["bits"])
        _require(isinstance(payload["hash_hex"], str)
                 and re.fullmatch(r"[0-9a-f]{16}", payload["hash_hex"]) is not None,
                 "dHash64 requires exactly 16 lowercase hexadecimal digits.")
    _boolean(payload["complete"])


def validate_payload(profile, payload):
    """Reject unknown fields at every result level; return only detached typed JSON."""
    profile_identity(profile)
    try:
        _validate_payload(profile, payload)
        _json(payload)
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("FEATURE_PAYLOAD_INVALID", "Invalid feature payload: " + str(exc)) from exc
