"""Pure OR ranking over a complete, frozen condition-match matrix."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import random

from . import condition_queries
from .config import PhotographyError


RANKING_VERSION = "matched-count-pattern-score-random-v1"
MAX_SEED_LENGTH = 256
_RANK_FIELDS = {"matched_condition_ids", "matched_count", "pattern_score", "result_rank"}


def _invalid(message):
    raise PhotographyError("INVALID_CONDITION_RANKING", message)


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        _invalid(f"{label} must be a nonblank string.")


def _validate_seed(seed):
    _identifier(seed, "Ranking seed")
    if len(seed) > MAX_SEED_LENGTH:
        _invalid(f"Ranking seed must not exceed {MAX_SEED_LENGTH} characters.")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in seed):
        _invalid("Ranking seed must contain valid Unicode text.")


def _finite_number(value):
    return (type(value) in (int, float)
            and (type(value) is int or math.isfinite(value)))


def _validate_json(value, ancestors=None):
    """Reject non-JSON metadata without imposing the workflow's field whitelist."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (list, dict):
        _invalid("Ranking values must be finite JSON data.")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        _invalid("Ranking values must not contain cycles.")
    ancestors.add(identity)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            _invalid("Ranking object keys must be strings.")
        children = value.values()
    else:
        children = value
    for child in children:
        _validate_json(child, ancestors)
    ancestors.remove(identity)


def _prepare(rows, conditions, *, finalized):
    if not isinstance(conditions, (list, tuple)) or not conditions:
        _invalid("At least one condition is required.")
    by_id, descriptors = {}, {}
    for condition in conditions:
        if not isinstance(condition, dict):
            _invalid("Each condition must be an object.")
        _validate_json(condition)
        condition_id = condition.get("id")
        _identifier(condition_id, "Condition ID")
        if condition_id in by_id:
            _invalid("Duplicate condition IDs are not allowed.")
        by_id[condition_id] = deepcopy(condition)
        descriptor = condition_queries.descriptor(by_id[condition_id])
        if (not isinstance(descriptor, dict)
                or descriptor.get("direction") not in ("higher", "lower")
                or descriptor.get("score_type") not in ("exact", "cosine", "fraction", "hamming")):
            _invalid("Invalid condition score descriptor.")
        descriptors[condition_id] = descriptor
    if not isinstance(rows, (list, tuple)):
        _invalid("Ranking rows must be a sequence.")
    prepared, photo_ids = [], set()
    condition_ids = sorted(by_id)
    for row in rows:
        if not isinstance(row, dict):
            _invalid("Each ranking row must be an object.")
        _validate_json(row)
        photo_id = row.get("photo_id")
        _identifier(photo_id, "Photo ID")
        if photo_id in photo_ids:
            _invalid("Duplicate photo IDs are not allowed.")
        photo_ids.add(photo_id)
        cells = row.get("conditions")
        if not isinstance(cells, dict) or set(cells) != set(by_id):
            _invalid("Every row must contain exactly one cell for every condition ID.")
        if finalized and not _RANK_FIELDS.issubset(row):
            _invalid("Finalized rows must include every ranking field.")
        result = deepcopy(row)
        result["conditions"] = {}
        for condition_id in condition_ids:
            cell = cells[condition_id]
            if not isinstance(cell, dict):
                _invalid("Condition cells must be objects.")
            if cell.get("status") == "not_reviewed":
                _invalid("Every condition cell must be reviewed before ranking.")
            if finalized and "normalized_score" not in cell:
                _invalid("Finalized condition cells must include normalized_score.")
            validated = condition_queries.validate_cell(
                by_id[condition_id], deepcopy(cell), finalized=finalized,
            )
            descriptor = descriptors[condition_id]
            if any(validated.get(key) != descriptor[key] for key in ("score_type", "direction")):
                _invalid("Condition cell score descriptor does not match its condition.")
            if validated["status"] == "matched" and not _finite_number(validated.get("raw_score")):
                _invalid("Matched condition scores must be finite numbers, not booleans.")
            result["conditions"][condition_id] = validated
        prepared.append(result)
    return prepared, descriptors


def _normalize_population(population, *, score_type, direction):
    count = len(population)
    if not count:
        return {}
    if score_type == "exact" or count == 1:
        return {photo_id: 1.0 for photo_id, _ in population}
    ordered = sorted(population, key=lambda item: item[1], reverse=direction == "lower")
    if ordered[0][1] == ordered[-1][1]:
        return {photo_id: 1.0 for photo_id, _ in ordered}
    scores, start = {}, 0
    while start < count:
        end = start + 1
        while end < count and ordered[end][1] == ordered[start][1]:
            end += 1
        # Average one-based rank, minus one, expressed using zero-based tie bounds.
        normalized = (start + end - 1) / (2 * (count - 1))
        for photo_id, _ in ordered[start:end]:
            scores[photo_id] = normalized
        start = end
    return scores


def _annotate(rows, descriptors):
    normalization, scores = {}, {}
    for condition_id in sorted(descriptors):
        descriptor = descriptors[condition_id]
        population = [
            (row["photo_id"], row["conditions"][condition_id]["raw_score"])
            for row in rows if row["conditions"][condition_id]["status"] == "matched"
        ]
        scores[condition_id] = _normalize_population(population, **descriptor)
        normalization[condition_id] = {
            "normalization_version": ("exact-one-v1" if descriptor["score_type"] == "exact"
                                      else "matched-average-rank-v1"),
            "population_count": len(population),
            "score_type": descriptor["score_type"],
            "direction": descriptor["direction"],
        }
    eligible = []
    for row in rows:
        matched_ids = []
        matched_scores = []
        for condition_id in sorted(descriptors):
            cell = row["conditions"][condition_id]
            normalized = scores[condition_id].get(row["photo_id"])
            cell["normalized_score"] = normalized
            if cell["status"] == "matched":
                matched_ids.append(condition_id)
                matched_scores.append(normalized)
        row["matched_condition_ids"] = matched_ids
        row["matched_count"] = len(matched_ids)
        if matched_ids:
            row["pattern_score"] = math.fsum(matched_scores) / len(matched_scores)
            eligible.append(row)
    return eligible, normalization


def rank_results(rows, conditions, seed):
    """Rank the entire union before display limits, without modifying any input."""
    _validate_seed(seed)
    prepared, descriptors = _prepare(rows, conditions, finalized=False)
    eligible, normalization = _annotate(prepared, descriptors)
    buckets = {}
    for row in eligible:
        pattern = tuple(row["matched_condition_ids"])
        buckets.setdefault(row["matched_count"], {}).setdefault(pattern, []).append(row)
    rng, results = random.Random(seed), []
    for count in sorted(buckets, reverse=True):
        queues, labels = {}, []
        for pattern in sorted(buckets[count]):
            group = sorted(buckets[count][pattern],
                           key=lambda row: (-row["pattern_score"], row["photo_id"]))
            queues[pattern] = deque(group)
            labels.extend([pattern] * len(group))
        # Only group labels are shuffled: each group's score order stays intact.
        rng.shuffle(labels)
        for pattern in labels:
            row = queues[pattern].popleft()
            row["result_rank"] = len(results) + 1
            results.append(row)
    return {
        "results": results,
        "normalization": normalization,
        "ranking_version": RANKING_VERSION,
        "random_seed": seed,
    }


def validate_ranking(results, conditions, normalization, *, ranking_version, random_seed):
    """Validate stored order, not a Python-version-dependent shuffle replay."""
    if not isinstance(ranking_version, str) or ranking_version != RANKING_VERSION:
        _invalid("Unsupported condition ranking version.")
    _validate_seed(random_seed)
    prepared, descriptors = _prepare(results, conditions, finalized=True)
    expected_rows, expected_normalization = _annotate(prepared, descriptors)
    _validate_json(normalization)
    if not isinstance(normalization, dict) or set(normalization) != set(expected_normalization):
        _invalid("Normalization must describe exactly the query's conditions.")
    for condition_id, expected in expected_normalization.items():
        actual = normalization[condition_id]
        if (not isinstance(actual, dict) or type(actual.get("population_count")) is not int
                or actual != expected):
            _invalid("Normalization metadata does not match the frozen matched population.")
    if len(expected_rows) != len(results):
        _invalid("Finalized results must contain only matching photos.")
    previous_count, previous_patterns = None, {}
    for rank, (actual, expected) in enumerate(zip(results, expected_rows), 1):
        if type(actual["result_rank"]) is not int or actual["result_rank"] != rank:
            _invalid("Result ranks must be consecutive and one-based.")
        if (type(actual["matched_count"]) is not int
                or actual["matched_count"] != expected["matched_count"]):
            _invalid("Matched count does not agree with condition cells.")
        if (not isinstance(actual["matched_condition_ids"], list)
                or actual["matched_condition_ids"] != expected["matched_condition_ids"]):
            _invalid("Matched condition IDs must be the exact, sorted matched set.")
        if (not _finite_number(actual["pattern_score"])
                or actual["pattern_score"] != expected["pattern_score"]):
            _invalid("Pattern score must be the equal-weight mean of matched scores.")
        for condition_id, cell in actual["conditions"].items():
            score = cell["normalized_score"]
            expected_score = expected["conditions"][condition_id]["normalized_score"]
            if expected_score is None:
                if score is not None:
                    _invalid("Unmatched conditions must have null normalized scores.")
            elif not _finite_number(score) or score != expected_score:
                _invalid("Normalized score does not match its condition's matched population.")
        count = expected["matched_count"]
        if previous_count is not None and count > previous_count:
            _invalid("Matched count must be descending.")
        previous_count = count
        pattern = tuple(expected["matched_condition_ids"])
        key = (-expected["pattern_score"], expected["photo_id"])
        if pattern in previous_patterns and key < previous_patterns[pattern]:
            _invalid("Identical matched patterns must retain score and photo-ID order.")
        previous_patterns[pattern] = key
