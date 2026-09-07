"""Local, deterministic feature calculations without models, SQL or source paths."""
from __future__ import annotations

from contextlib import contextmanager
import io
import math

from .config import PhotographyError
from .feature_profiles import (
    _fields, _integer, _json, _objects, _PROVIDERS, _require,
    default_profile, profile_identity, validate_payload,
)
from .fingerprints import fingerprint
from .image_vectors import validate_vector


def _profile(profile, component, *, executable=True):
    profile_identity(profile)
    if profile["component"] != component:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "Algorithm and profile component disagree.")
    if executable and profile["provider"] != _PROVIDERS[component]:
        raise PhotographyError("FEATURE_PROVIDER_UNSUPPORTED", "This algorithm cannot execute the declared feature provider.")
    if executable and component in ("color", "perceptual_hash"):
        from PIL import __version__

        if profile["runtime"].get("packages", {}).get("Pillow") != __version__:
            raise PhotographyError("FEATURE_RUNTIME_MISMATCH", "Pillow does not match this immutable feature profile.")
    return profile["parameters"]


@contextmanager
def _preview(image_bytes):
    if not isinstance(image_bytes, (bytes, bytearray, memoryview)) or not image_bytes:
        raise PhotographyError("FEATURE_INPUT_INVALID", "A stored RGB preview byte buffer is required.")
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.mode != "RGB":
                raise PhotographyError("FEATURE_INPUT_INVALID", "Preview must already be direction-corrected sRGB RGB.")
            image.load()
            yield image
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise PhotographyError("FEATURE_INPUT_INVALID", "Cannot decode the stored RGB preview.") from exc


def compute_color(jpeg_bytes, profile):
    p = _profile(profile, "color")
    from PIL import Image

    with _preview(jpeg_bytes) as image:
        width, height = image.size
        sample = image.copy()
    sample.thumbnail((p["max_sample_dimension"], p["max_sample_dimension"]), Image.Resampling.LANCZOS)
    hsv = list(sample.convert("HSV").get_flattened_data())
    count = len(hsv)
    hue_counts = [0] * p["hue_bins"]
    low_saturation = 0
    for hue, saturation, _ in hsv:
        hue_counts[min(p["hue_bins"] - 1, hue * p["hue_bins"] // 255)] += 1
        low_saturation += saturation / 255 < p["low_saturation_threshold"]
    quantized = sample.quantize(colors=p["palette_size"], method=Image.Quantize.MEDIANCUT,
                                kmeans=0, dither=Image.Dither.NONE)
    palette = quantized.getpalette()
    colors = {}
    for frequency, index in quantized.getcolors():
        rgb = tuple(palette[index * 3:index * 3 + 3])
        colors[rgb] = colors.get(rgb, 0) + frequency
    payload = {
        "width": width, "height": height, "complete": True,
        "features": {
            "mean_saturation": math.fsum(saturation for _, saturation, _ in hsv) / (255 * count),
            "low_saturation_fraction": low_saturation / count,
            "hue_histogram": [frequency / count for frequency in hue_counts],
        },
        "palette": [{"rgb": list(rgb), "fraction": frequency / count}
                    for rgb, frequency in sorted(colors.items(), key=lambda item: (-item[1], item[0]))],
    }
    return validate_payload(profile, payload)


def compute_hash(jpeg_bytes, profile):
    p = _profile(profile, "perceptual_hash")
    from PIL import Image

    with _preview(jpeg_bytes) as image:
        pixels = list(image.convert("L").resize((p["width"], p["height"]),
                                               Image.Resampling.LANCZOS).get_flattened_data())
    value = 0
    for row in range(p["height"]):
        for column in range(p["width"] - 1):
            index = row * p["width"] + column
            value = (value << 1) | int(pixels[index] > pixels[index + 1])
    return validate_payload(profile, {
        "complete": True, "algorithm": p["algorithm"], "bits": p["bits"],
        "hash_hex": f"{value:016x}",
    })


def hash_distance(left_payload, right_payload):
    """Compare compatible, validated hashes; similarity is not transitive."""
    profile = default_profile("perceptual_hash")
    left = validate_payload(profile, left_payload)
    right = validate_payload(profile, right_payload)
    if (left["algorithm"], left["bits"]) != (right["algorithm"], right["bits"]):
        raise PhotographyError("FEATURE_PAYLOAD_INVALID", "Hash algorithm and bit count must agree.")
    return (int(left["hash_hex"], 16) ^ int(right["hash_hex"], 16)).bit_count()


def _rectangle_union(boxes):
    xs = sorted({coordinate for box in boxes for coordinate in (box[0], box[2])})
    strips = []
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((y1, y2) for x1, y1, x2, y2 in boxes if x1 < right and x2 > left)
        covered = 0.0
        end = 0.0
        for bottom, top in intervals:
            covered += max(0.0, top - max(bottom, end))
            end = max(end, top)
        strips.append((right - left) * covered)
    return min(1.0, max(0.0, math.fsum(strips)))


def compute_composition(objects_payload, profile):
    p = _profile(profile, "composition")
    try:
        _objects(objects_payload)
        _json(objects_payload)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("FEATURE_PAYLOAD_INVALID", "Composition requires a valid object result.") from exc
    objects = objects_payload["objects"]
    boxes = [item["bbox"] for item in objects]
    union = _rectangle_union(boxes)
    bounding = ((max(box[2] for box in boxes) - min(box[0] for box in boxes))
                * (max(box[3] for box in boxes) - min(box[1] for box in boxes))) if boxes else 0.0
    candidates = [index for index, item in enumerate(objects)
                  if p["subject_class"] is None or item["class_id"] == p["subject_class"]]

    def rank(index):
        item = objects[index]
        left, top, right, bottom = item["bbox"]
        return (-(right - left) * (bottom - top), -item["score"], index)

    features = {
        "subject_index": None, "center_x": None, "center_y": None, "subject_area": None,
        "thirds_distance": None, "union_area": union, "bounding_area": bounding,
        "uncovered_fraction": 1 - union,
    }
    if candidates:
        index = min(candidates, key=rank)
        left, top, right, bottom = boxes[index]
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        features.update(
            subject_index=index, center_x=center_x, center_y=center_y,
            subject_area=(right - left) * (bottom - top),
            thirds_distance=min(math.hypot(center_x - x, center_y - y)
                                for x in (1 / 3, 2 / 3) for y in (1 / 3, 2 / 3)),
        )
    return validate_payload(profile, {
        "width": objects_payload["width"], "height": objects_payload["height"],
        "complete": objects_payload["complete"], "features": features,
    })


def validate_scene_prototypes(profile, prototypes, *, dimensions=None):
    """Validate portable catalog records, preserving their declared ordering."""
    p = _profile(profile, "scene", executable=False)
    try:
        _require(isinstance(prototypes, list) and len(prototypes) == len(p["catalog"]),
                 "Prototypes must cover the entire scene catalog.")
        configured_dimensions = p.get("dimensions")
        if dimensions is None:
            dimensions = configured_dimensions
        if dimensions is None:
            vector = prototypes[0].get("vector") if isinstance(prototypes[0], dict) else None
            _require(isinstance(vector, list), "Prototype vectors must be JSON arrays.")
            dimensions = len(vector)
        _integer(dimensions, 1, 4096)
        _require(configured_dimensions is None or dimensions == configured_dimensions,
                 "Prototype dimensions disagree with the scene profile.")
        result = []
        for prototype, scene in zip(prototypes, p["catalog"]):
            _fields(prototype, ("scene_id", "label", "prompts", "vector"))
            _require(all(prototype[key] == scene[key] for key in ("scene_id", "label", "prompts")),
                     "Prototype identity and prompts must match the catalog.")
            _require(isinstance(prototype["vector"], list), "Prototype vectors must be JSON arrays.")
            _json(prototype)
            result.append({
                "scene_id": scene["scene_id"], "label": scene["label"], "prompts": list(scene["prompts"]),
                "vector": validate_vector(prototype["vector"], dimensions),
            })
        return result
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("FEATURE_PAYLOAD_INVALID", "Invalid scene prototype set: " + str(exc)) from exc


def compute_scene(image_vector, prototypes, profile):
    p = _profile(profile, "scene")
    try:
        dimensions = p.get("dimensions", len(image_vector))
        _integer(dimensions, 1, 4096)
    except (TypeError, ValueError) as exc:
        raise PhotographyError("INDEX_VECTOR_INVALID", "Scene input must be a finite normalized vector.") from exc
    vector = validate_vector(image_vector, dimensions)
    prototypes = validate_scene_prototypes(profile, prototypes, dimensions=dimensions)
    return validate_payload(profile, {
        "complete": True,
        "scores": [{"scene_id": prototype["scene_id"],
                    "score": min(1.0, max(-1.0, math.fsum(a * b for a, b in zip(vector, prototype["vector"]))))}
                   for prototype in prototypes],
    })


def make_scene_prototypes(profile, encoder):
    p = _profile(profile, "scene")
    encoder_profile = encoder.profile()
    try:
        _require(isinstance(encoder_profile, dict), "Encoder profile must be an object.")
        _json(encoder_profile)
        encoder_id = fingerprint(encoder_profile)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "Invalid scene encoder profile.") from exc
    if encoder_id != profile["dependencies"]["image_embedding"]:
        raise PhotographyError("FEATURE_DEPENDENCY_MISMATCH", "Scene text encoder profile does not match its dependency.")
    dimensions = encoder_profile.get("dimensions")
    try:
        _integer(dimensions, 1, 4096)
        _require("dimensions" not in p or p["dimensions"] == dimensions,
                 "Scene and encoder dimensions disagree.")
    except (TypeError, ValueError) as exc:
        raise PhotographyError("FEATURE_PROFILE_INVALID", str(exc)) from exc
    vectors, prototypes = {}, []
    for scene in p["catalog"]:
        for prompt in scene["prompts"]:
            if prompt not in vectors:
                encoding = encoder.encode_text(prompt)
                values = encoding.vector if hasattr(encoding, "vector") else encoding
                vectors[prompt] = validate_vector(values, dimensions)
        count = len(scene["prompts"])
        mean = [math.fsum(vectors[prompt][index] for prompt in scene["prompts"]) / count
                for index in range(dimensions)]
        mean = validate_vector(mean, dimensions, normalized=False)
        norm = math.hypot(*mean)
        prototypes.append({
            "scene_id": scene["scene_id"], "label": scene["label"], "prompts": list(scene["prompts"]),
            "vector": [value / norm for value in mean],
        })
    return validate_scene_prototypes(profile, prototypes, dimensions=dimensions)
