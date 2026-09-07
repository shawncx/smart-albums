"""One versioned visual-intent recipe shared by both semantic search entry points."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
import unicodedata

from .config import PhotographyError
from .image_vectors import validate_vector


STRATEGY = "english-visual-intent-v1"


def _text(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(unicodedata.category(char) in ("Cc", "Cs") for char in value)):
        raise PhotographyError("INVALID_ARGUMENT", f"{name} must be nonblank text without control characters.")
    return value.strip()


def prepare_query(query, visual_query=None):
    original = _text(query, "Original query")
    supplied = visual_query is not None
    visual = _text(visual_query if supplied else original, "Visual query")
    if any(char.isalpha() and "LATIN" not in unicodedata.name(char, "") for char in visual):
        raise PhotographyError(
            "VISUAL_QUERY_REQUIRED",
            "Provide an English visual description via --visual-query (or semantic condition visual_query). "
            "The Skill prepares it from the user's text only; Python does not silently translate.",
        )
    visual = unicodedata.normalize("NFC", visual)
    return {"strategy": STRATEGY, "query": original, "visual_query": visual,
            "prompts": [visual], "weights": [1.0]}


def validate_query_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"strategy", "query", "visual_query", "prompts", "weights"}:
        raise PhotographyError("INVALID_ARGUMENT", "Expected a frozen semantic query plan.")
    expected = prepare_query(plan["query"], plan["visual_query"])
    if (plan != expected or any(type(weight) not in (int, float) or not math.isfinite(weight)
                                for weight in plan["weights"])):
        raise PhotographyError("INVALID_ARGUMENT", "Semantic query prompts or recipe identity changed.")
    return expected


@dataclass(frozen=True)
class QueryEncoding:
    vector: list[float]
    model_calls: int
    token_counts: list[int | None]
    elapsed_seconds: float


def encode_query(plan, encoder):
    plan = validate_query_plan(plan)
    dimensions = encoder.profile()["dimensions"]
    started = time.perf_counter()
    encoded = encoder.encode_text(plan["visual_query"])
    vector = validate_vector(encoded.vector, dimensions)
    count = getattr(encoded, "token_count", None)
    if count is not None and (type(count) is not int or count < 0):
        raise PhotographyError("INDEX_ENCODING_FAILED", "The query encoder returned an invalid token count.")
    elapsed = getattr(encoded, "elapsed_seconds", None)
    if elapsed is None:
        elapsed = time.perf_counter() - started
    elif type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise PhotographyError("INDEX_ENCODING_FAILED", "The query encoder returned an invalid duration.")
    return QueryEncoding(vector, 1, [count], elapsed)
