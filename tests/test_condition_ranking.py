"""Pure ranking tests: no database, image access, model, or external service."""
from __future__ import annotations

from copy import deepcopy
import itertools
import json
import math
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import condition_queries, condition_ranking
from photography_lib.config import PhotographyError


def graded(condition_id):
    return {"id": condition_id, "kind": "semantic", "query": f"scene {condition_id}",
            "profile_id": "embedding-fixture", "scoring": "graded"}


def exact(condition_id):
    return {"id": condition_id, "kind": "ocr_contains", "text": f"word {condition_id}",
            "profile_id": "ocr-fixture", "scoring": "exact"}


def cell(condition, raw_score=None, *, status=None):
    return {
        "status": status or ("matched" if raw_score is not None else "not_matched"),
        "raw_score": raw_score,
        **condition_queries.descriptor(condition),
        "sources": [], "evidence": {}, "reason": None,
    }


def row(photo_id, conditions, matched=None):
    matched = {} if matched is None else matched
    return {
        "photo_id": photo_id, "content_version": f"content-{photo_id}",
        "thumbnail_profile": "preview-fixture", "input_image_hash": None,
        "conditions": {condition["id"]: cell(condition, matched.get(condition["id"]))
                       for condition in conditions},
    }


def validate(payload, conditions):
    return condition_ranking.validate_ranking(
        payload["results"], conditions, payload["normalization"],
        ranking_version=payload["ranking_version"], random_seed=payload["random_seed"],
    )


def ids(payload):
    return [item["photo_id"] for item in payload["results"]]


def by_id(payload):
    return {item["photo_id"]: item for item in payload["results"]}


def renumber(payload):
    for rank, item in enumerate(payload["results"], 1):
        item["result_rank"] = rank


class ConditionRankingTests(unittest.TestCase):
    def test_empty_rows_include_zero_population_for_every_condition(self):
        conditions = [graded("B"), exact("A")]
        payload = condition_ranking.rank_results([], conditions, "empty")
        self.assertEqual(payload["results"], [])
        self.assertEqual(list(payload["normalization"]), ["A", "B"])
        self.assertEqual(payload["normalization"], {
            "A": {"normalization_version": "exact-one-v1", "population_count": 0,
                  "score_type": "exact", "direction": "higher"},
            "B": {"normalization_version": "matched-average-rank-v1", "population_count": 0,
                  "score_type": "cosine", "direction": "higher"},
        })
        self.assertEqual(payload["random_seed"], "empty")
        self.assertEqual(payload["ranking_version"], "matched-count-pattern-score-random-v1")
        self.assertIsNone(validate(payload, conditions))

    def test_one_match_and_all_equal_matches_normalize_to_one(self):
        conditions = [graded("A")]
        for scores in ([0.0], [-0.3, -0.3, -0.3], [0, 0.0, -0.0]):
            rows = [row(str(index), conditions, {"A": score})
                    for index, score in reversed(list(enumerate(scores)))]
            with self.subTest(scores=scores):
                payload = condition_ranking.rank_results(rows, conditions, "equal")
                self.assertEqual(ids(payload), [str(index) for index in range(len(scores))])
                for item in payload["results"]:
                    self.assertEqual(item["conditions"]["A"]["normalized_score"], 1.0)
                    self.assertEqual(item["pattern_score"], 1.0)
                validate(payload, conditions)

    def test_average_ties_do_not_use_photo_id_to_break_percentile_ties(self):
        conditions = [graded("A")]
        rows = [row(name, conditions, {"A": score}) for name, score in
                (("worst", -0.9), ("tie-z", -0.3), ("tie-a", -0.3), ("best", 0.4))]
        payload = condition_ranking.rank_results(rows, conditions, "ties")
        self.assertEqual(ids(payload), ["best", "tie-a", "tie-z", "worst"])
        self.assertEqual({name: item["conditions"]["A"]["normalized_score"]
                          for name, item in by_id(payload).items()},
                         {"best": 1.0, "tie-a": 0.5, "tie-z": 0.5, "worst": 0.0})
        validate(payload, conditions)

    def test_negative_cosines_are_ranked_not_clamped_or_confidence_calibrated(self):
        conditions = [graded("A")]
        rows = [row(name, conditions, {"A": score}) for name, score in
                (("a", -0.9), ("b", -0.5), ("c", -0.1))]
        payload = condition_ranking.rank_results(rows, conditions, "negative")
        self.assertEqual(ids(payload), ["c", "b", "a"])
        self.assertEqual([item["pattern_score"] for item in payload["results"]], [1, 0.5, 0])
        self.assertEqual([item["conditions"]["A"]["raw_score"] for item in payload["results"]],
                         [-0.1, -0.5, -0.9])
        validate(payload, conditions)

    def test_distance_descriptor_uses_lower_raw_scores_as_better(self):
        population = [("eight-a", 8), ("zero", 0), ("eight-b", 8), ("sixteen", 16)]
        self.assertEqual(
            condition_ranking._normalize_population(population, score_type="hamming",
                                                    direction="lower"),
            {"sixteen": 0.0, "eight-a": 0.5, "eight-b": 0.5, "zero": 1.0},
        )
        self.assertEqual(condition_ranking._normalize_population(
            [("a", 64), ("b", 64)], score_type="hamming", direction="lower"),
            {"a": 1.0, "b": 1.0})

    def test_exact_scores_are_one_and_never_weighted_by_evidence_confidence(self):
        conditions = [graded("B"), {
            "id": "A", "kind": "color_fraction", "color": "blue", "minimum": 0,
            "profile_id": "color-fixture", "scoring": "exact",
        }]
        rows = [row("high-confidence", conditions, {"A": 1, "B": 0.1}),
                row("low-confidence", conditions, {"A": 1, "B": 0.9})]
        for item, confidence in zip(rows, (0.99, 0.01)):
            item["conditions"]["A"]["evidence"] = {"fraction": confidence}
        payload = condition_ranking.rank_results(rows, conditions, "exact")
        self.assertEqual(ids(payload), ["low-confidence", "high-confidence"])
        self.assertEqual([item["pattern_score"] for item in payload["results"]], [1.0, 0.5])
        for item in payload["results"]:
            self.assertEqual(item["matched_condition_ids"], ["A", "B"])
            self.assertEqual(item["conditions"]["A"]["normalized_score"], 1.0)
        validate(payload, conditions)

    def test_unknown_and_rejected_cells_do_not_contribute_or_enlarge_population(self):
        conditions = [graded("A"), graded("B")]
        rows = []
        statuses = ["not_matched", "unknown_missing_index", "unknown_stale",
                    "unknown_invalid", "unsupported"]
        for index, status in enumerate(statuses):
            item = row(f"photo-{index}", conditions, {"A": -0.8})
            item["conditions"]["B"] = cell(conditions[1], status=status)
            rows.append(item)
        rows[0]["conditions"]["B"]["raw_score"] = 0.99
        rows.append(row("no-match", conditions))
        payload = condition_ranking.rank_results(rows, conditions, "unknown")
        self.assertEqual(len(payload["results"]), len(statuses))
        self.assertNotIn("no-match", ids(payload))
        self.assertEqual(payload["normalization"]["A"]["population_count"], 5)
        self.assertEqual(payload["normalization"]["B"]["population_count"], 0)
        for item in payload["results"]:
            self.assertEqual(item["matched_condition_ids"], ["A"])
            self.assertEqual(item["matched_count"], 1)
            self.assertEqual(item["pattern_score"], 1)
            self.assertIsNone(item["conditions"]["B"]["normalized_score"])
        self.assertEqual(by_id(payload)["photo-0"]["conditions"]["B"]["raw_score"], 0.99)
        validate(payload, conditions)

    def test_not_reviewed_always_rejects_even_on_an_otherwise_ineligible_row(self):
        conditions = [graded("A"), exact("B")]
        for matches in ({}, {"B": 1}):
            item = row("pending", conditions, matches)
            item["conditions"]["A"] = cell(conditions[0], 0.5, status="not_reviewed")
            with self.subTest(matches=matches), self.assertRaises(PhotographyError):
                condition_ranking.rank_results([item], conditions, "pending")

    def test_match_count_dominates_and_seed_interleaving_preserves_each_pattern(self):
        conditions = [graded("A"), graded("B"), exact("C")]
        rows = [row(name, conditions, matches) for name, matches in (
            ("a-low", {"A": 0.1}), ("a-high", {"A": 0.9}), ("a-mid", {"A": 0.4}),
            ("b-low", {"B": -0.9}), ("b-high", {"B": -0.2}), ("c", {"C": 1}),
            ("ab-weak", {"A": -0.8, "B": -0.9}), ("abc", {"A": -0.9, "B": -1, "C": 1}),
        )]
        state = random.getstate()
        payload = condition_ranking.rank_results(rows, conditions, "interleave")
        self.assertEqual(random.getstate(), state)
        self.assertEqual(ids(payload)[:2], ["abc", "ab-weak"])
        self.assertEqual([name for name in ids(payload) if name.startswith("a-")],
                         ["a-high", "a-mid", "a-low"])
        self.assertEqual([name for name in ids(payload) if name.startswith("b-")],
                         ["b-high", "b-low"])
        self.assertEqual(payload, condition_ranking.rank_results(
            list(reversed(rows)), list(reversed(conditions)), "interleave"))
        sequences = {tuple(ids(condition_ranking.rank_results(rows, conditions, str(seed))))
                     for seed in range(12)}
        self.assertGreater(len(sequences), 1)
        validate(payload, conditions)

    def test_all_input_permutations_have_the_same_seeded_ranking(self):
        conditions = [graded("B"), exact("C"), graded("A")]
        rows = [row(name, conditions, matches) for name, matches in (
            ("a1", {"A": 0.2}), ("a2", {"A": 0.8}), ("b", {"B": -0.1}),
            ("ac", {"A": -0.5, "C": 1}), ("none", {}),
        )]
        expected = condition_ranking.rank_results(rows, conditions, "permutations")
        for condition_order in itertools.permutations(conditions):
            for row_order in itertools.permutations(rows):
                self.assertEqual(
                    condition_ranking.rank_results(row_order, condition_order, "permutations"),
                    expected,
                )

    def test_only_changed_matched_population_changes_percentiles(self):
        conditions = [graded("A"), exact("B")]
        rows = [row("middle", conditions, {"A": 0.2}), row("high", conditions, {"A": 0.8})]
        original = condition_ranking.rank_results(rows, conditions, "population")
        unchanged = condition_ranking.rank_results(
            rows + [row("none", conditions)], conditions, "population")
        self.assertEqual(original, unchanged)
        expanded = condition_ranking.rank_results(
            rows + [row("low-two-matches", conditions, {"A": 0.1, "B": 1})],
            conditions, "population",
        )
        self.assertEqual(by_id(original)["middle"]["pattern_score"], 0)
        self.assertEqual(by_id(expanded)["middle"]["pattern_score"], 0.5)
        self.assertEqual(ids(expanded)[0], "low-two-matches")
        self.assertEqual(expanded["normalization"]["A"]["population_count"], 3)
        validate(expanded, conditions)

    def test_full_union_has_no_pattern_quota_and_is_normalized_before_display_limit(self):
        conditions = [graded("A"), exact("B")]
        rows = [row(f"a-{index:03}", conditions, {"A": index / 200}) for index in range(125)]
        rows += [row("b-only", conditions, {"B": 1}),
                 row("both", conditions, {"A": -0.5, "B": 1}),
                 row("ineligible", conditions)]
        payload = condition_ranking.rank_results(rows, conditions, "no-quota")
        self.assertEqual(len(payload["results"]), 127)
        self.assertEqual(set(ids(payload)), {item["photo_id"] for item in rows[:-1]})
        self.assertEqual(ids(payload)[0], "both")
        self.assertEqual(payload["normalization"]["A"]["population_count"], 126)
        self.assertEqual(payload["normalization"]["B"]["population_count"], 2)
        self.assertAlmostEqual(by_id(payload)["a-000"]["pattern_score"], 1 / 125)
        validate(payload, conditions)
        first_page = deepcopy(payload)
        first_page["results"] = first_page["results"][:5]
        with self.assertRaises(PhotographyError):
            validate(first_page, conditions)

    def test_metadata_is_retained_detached_and_json_round_trip_is_valid(self):
        conditions = [graded("A"), exact("B")]
        item = row("写真-é", conditions, {"A": -0.3})
        item["workflow_metadata"] = {"flags": [True, False, None], "count": 2, "nested": {"x": "y"}}
        item["conditions"]["A"]["evidence"] = {"candidate_rank": 1, "score_gap_from_best": 0.3,
                                               "score_gap_to_next": None}
        item["conditions"]["A"]["sources"] = [{
            "kind": "photo", "photo_id": item["photo_id"],
            "content_version": item["content_version"], "ingest_state": "available",
        }]
        before_rows, before_conditions = deepcopy([item]), deepcopy(conditions)
        payload = condition_ranking.rank_results([item], conditions, "persisted-seed")
        saved = json.loads(json.dumps(payload, allow_nan=False))
        self.assertEqual(saved, payload)
        validate(saved, conditions)
        self.assertEqual(payload["results"][0]["workflow_metadata"], item["workflow_metadata"])
        self.assertEqual(payload["results"][0]["content_version"], item["content_version"])
        self.assertEqual([item], before_rows)
        self.assertEqual(conditions, before_conditions)
        payload["results"][0]["workflow_metadata"]["flags"].append("changed")
        payload["results"][0]["conditions"]["A"]["evidence"]["candidate_rank"] = 2
        payload["results"][0]["conditions"]["A"]["sources"][0]["content_version"] = "changed"
        self.assertEqual([item], before_rows)

    def test_randomized_population_and_order_invariants(self):
        generator = random.Random(73419)
        conditions = [graded("A"), exact("B"), graded("C"), exact("D"), graded("E")]
        for trial in range(60):
            rows = []
            for index in range(generator.randrange(0, 35)):
                matches = {}
                for condition in conditions:
                    if generator.random() < 0.6:
                        matches[condition["id"]] = (1 if condition["scoring"] == "exact"
                                                    else generator.choice([-1, -0.5, 0, 0.3, 0.9]))
                rows.append(row(f"p-{index:03}", conditions, matches))
            with self.subTest(trial=trial):
                payload = condition_ranking.rank_results(rows, conditions, f"trial-{trial}")
                validate(payload, conditions)
                expected_ids = {item["photo_id"] for item in rows
                                if any(c["status"] == "matched" for c in item["conditions"].values())}
                self.assertEqual(set(ids(payload)), expected_ids)
                self.assertEqual(len(ids(payload)), len(expected_ids))
                counts = [item["matched_count"] for item in payload["results"]]
                self.assertEqual(counts, sorted(counts, reverse=True))
                for condition in conditions:
                    condition_id = condition["id"]
                    population = [item["conditions"][condition_id]["raw_score"] for item in rows
                                  if item["conditions"][condition_id]["status"] == "matched"]
                    self.assertEqual(payload["normalization"][condition_id]["population_count"],
                                     len(population))
                    for item in payload["results"]:
                        entry = item["conditions"][condition_id]
                        if entry["status"] != "matched":
                            self.assertIsNone(entry["normalized_score"])
                            continue
                        if condition["scoring"] == "exact" or len(set(population)) == 1:
                            expected = 1
                        else:
                            smaller = sum(raw < entry["raw_score"] for raw in population)
                            tied = population.count(entry["raw_score"])
                            expected = (2 * smaller + tied - 1) / (2 * (len(population) - 1))
                        self.assertEqual(entry["normalized_score"], expected)
                patterns = {}
                for item in payload["results"]:
                    pattern = tuple(item["matched_condition_ids"])
                    patterns.setdefault(pattern, []).append((-item["pattern_score"], item["photo_id"]))
                    scores = [item["conditions"][condition_id]["normalized_score"]
                              for condition_id in pattern]
                    self.assertEqual(item["pattern_score"], math.fsum(scores) / len(scores))
                for ordering in patterns.values():
                    self.assertEqual(ordering, sorted(ordering))
                generator.shuffle(rows)
                self.assertEqual(payload, condition_ranking.rank_results(
                    rows, list(reversed(conditions)), f"trial-{trial}"))


class ConditionRankingValidationTests(unittest.TestCase):
    def setUp(self):
        self.conditions = [graded("A"), exact("B")]
        self.rows = [row(name, self.conditions, matches) for name, matches in (
            ("both", {"A": -0.5, "B": 1}), ("a-high", {"A": 0.9}),
            ("a-low", {"A": 0.1}), ("b-only", {"B": 1}),
        )]
        self.payload = condition_ranking.rank_results(self.rows, self.conditions, "validation")

    def test_invalid_seeds_and_empty_conditions_are_rejected(self):
        for seed in (None, True, False, 3, 0.5, "", "   ", "\0", "\ud800", "x" * 257, [], {}):
            with self.subTest(seed=seed), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(self.rows, self.conditions, seed)
        for conditions in ([], (), None, {}, "A"):
            with self.subTest(conditions=conditions), self.assertRaises(PhotographyError):
                condition_ranking.rank_results([], conditions, "seed")
        valid = condition_ranking.rank_results([], self.conditions, "x" * 256)
        self.assertIsNone(validate(valid, self.conditions))

    def test_duplicate_photo_or_condition_ids_are_errors_not_silent_deduplication(self):
        for rows, conditions in (
            (self.rows + [deepcopy(self.rows[0])], self.conditions),
            (self.rows, self.conditions + [deepcopy(self.conditions[0])]),
        ):
            with self.subTest(rows=len(rows), conditions=len(conditions)), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, conditions, "duplicates")

    def test_malformed_rows_conditions_and_cell_ids_are_rejected(self):
        for value in (None, True, 1, "", "   ", "\0"):
            rows = deepcopy(self.rows)
            rows[0]["photo_id"] = value
            with self.subTest(photo_id=value), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")
            conditions = deepcopy(self.conditions)
            conditions[0]["id"] = value
            with self.subTest(condition_id=value), self.assertRaises(PhotographyError):
                condition_ranking.rank_results([], conditions, "invalid")
        for cells in (None, [], {}, {"unknown": {}}):
            rows = deepcopy(self.rows)
            rows[0]["conditions"] = cells
            with self.subTest(cells=cells), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")
        for rows in (None, {}, "rows", [None], [False]):
            with self.subTest(rows=rows), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")
        rows = deepcopy(self.rows)
        rows[0]["conditions"]["unknown"] = deepcopy(rows[0]["conditions"]["A"])
        with self.assertRaises(PhotographyError):
            condition_ranking.rank_results(rows, self.conditions, "invalid")

    def test_nonfinite_boolean_and_wrong_descriptor_scores_are_rejected(self):
        for score in (None, True, False, float("nan"), float("inf"), -float("inf"), "0.3", []):
            rows = deepcopy(self.rows)
            rows[0]["conditions"]["A"]["raw_score"] = score
            with self.subTest(score=score), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")
        for field, value in (("score_type", "fraction"), ("direction", "lower"), ("status", "invented")):
            rows = deepcopy(self.rows)
            rows[0]["conditions"]["A"][field] = value
            with self.subTest(field=field), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")

    def test_metadata_rejects_non_json_nonfinite_and_cyclic_data(self):
        cycle = []
        cycle.append(cycle)
        for extra in (float("nan"), float("inf"), object(), b"bytes", {3: "non-string"}, {1, 2},
                      (1, 2), cycle):
            rows = deepcopy(self.rows)
            rows[0]["extra"] = extra
            with self.subTest(extra=type(extra).__name__), self.assertRaises(PhotographyError):
                condition_ranking.rank_results(rows, self.conditions, "invalid")

    def test_validator_recomputes_scores_sets_counts_and_ranks(self):
        mutations = [
            lambda item: item.update(matched_count=1),
            lambda item: item.update(matched_count=True),
            lambda item: item.update(matched_count=2.0),
            lambda item: item.update(matched_condition_ids=["B", "A"]),
            lambda item: item.update(matched_condition_ids=["A", "A"]),
            lambda item: item.update(matched_condition_ids=["unknown"]),
            lambda item: item.update(pattern_score=0.99),
            lambda item: item.update(pattern_score=True),
            lambda item: item.update(pattern_score=float("nan")),
            lambda item: item.update(result_rank=0),
            lambda item: item.update(result_rank=True),
            lambda item: item.update(result_rank=1.0),
            lambda item: item.pop("result_rank"),
            lambda item: item["conditions"]["A"].update(normalized_score=0.4),
            lambda item: item["conditions"]["A"].update(normalized_score=False),
            lambda item: item["conditions"]["A"].update(normalized_score=None),
            lambda item: item["conditions"]["A"].pop("normalized_score"),
            lambda item: item["conditions"]["B"].update(normalized_score=True),
            lambda item: item["conditions"]["A"].update(status="not_reviewed"),
        ]
        for index, mutate in enumerate(mutations):
            invalid = deepcopy(self.payload)
            mutate(invalid["results"][0])
            with self.subTest(mutation=index), self.assertRaises(PhotographyError):
                validate(invalid, self.conditions)
        invalid = deepcopy(self.payload)
        next(item for item in invalid["results"] if item["photo_id"] == "a-high")[
            "conditions"]["B"]["normalized_score"] = 0
        with self.assertRaises(PhotographyError):
            validate(invalid, self.conditions)

    def test_validator_recomputes_and_strictly_checks_normalization_metadata(self):
        mutations = [
            lambda value: value.clear(),
            lambda value: value.update(unknown=deepcopy(value["A"])),
            lambda value: value["A"].update(population_count=0),
            lambda value: value["A"].update(population_count=True),
            lambda value: value["A"].update(population_count=3.0),
            lambda value: value["A"].update(normalization_version="other"),
            lambda value: value["A"].update(score_type="hamming"),
            lambda value: value["A"].update(direction="lower"),
            lambda value: value["A"].update(unexpected="extra"),
        ]
        for index, mutate in enumerate(mutations):
            invalid = deepcopy(self.payload)
            mutate(invalid["normalization"])
            with self.subTest(mutation=index), self.assertRaises(PhotographyError):
                validate(invalid, self.conditions)
        for value in (None, [], True):
            invalid = deepcopy(self.payload)
            invalid["normalization"] = value
            with self.subTest(value=value), self.assertRaises(PhotographyError):
                validate(invalid, self.conditions)

    def test_validator_checks_versions_seeds_duplicates_and_no_match_rows(self):
        for version in (None, True, 1, "", "future-ranking-v2"):
            invalid = deepcopy(self.payload)
            invalid["ranking_version"] = version
            with self.subTest(version=version), self.assertRaises(PhotographyError):
                validate(invalid, self.conditions)
        for seed in (None, True, 1, "", "x" * 257):
            invalid = deepcopy(self.payload)
            invalid["random_seed"] = seed
            with self.subTest(seed=seed), self.assertRaises(PhotographyError):
                validate(invalid, self.conditions)
        invalid = deepcopy(self.payload)
        invalid["results"].append(deepcopy(invalid["results"][0]))
        renumber(invalid)
        with self.assertRaises(PhotographyError):
            validate(invalid, self.conditions)
        invalid = deepcopy(self.payload)
        unmatched = row("unmatched", self.conditions)
        unmatched.update(matched_condition_ids=[], matched_count=0, pattern_score=0, result_rank=5)
        for value in unmatched["conditions"].values():
            value["normalized_score"] = None
        invalid["results"].append(unmatched)
        with self.assertRaises(PhotographyError):
            validate(invalid, self.conditions)

    def test_validator_rejects_count_increase_and_within_pattern_score_inversion(self):
        invalid = deepcopy(self.payload)
        invalid["results"][0], invalid["results"][-1] = invalid["results"][-1], invalid["results"][0]
        renumber(invalid)
        with self.assertRaises(PhotographyError):
            validate(invalid, self.conditions)
        invalid = deepcopy(self.payload)
        high, low = ids(invalid).index("a-high"), ids(invalid).index("a-low")
        invalid["results"][high], invalid["results"][low] = invalid["results"][low], invalid["results"][high]
        renumber(invalid)
        with self.assertRaises(PhotographyError):
            validate(invalid, self.conditions)

    def test_validator_checks_photo_id_ties_without_replaying_random_interleaving(self):
        conditions = [exact("A"), exact("B")]
        rows = [row(name, conditions, matches) for name, matches in (
            ("z", {"A": 1}), ("a", {"A": 1}), ("b", {"B": 1}),
        )]
        payload = condition_ranking.rank_results(rows, conditions, "persisted")
        existing = by_id(payload)
        for order in (("a", "z", "b"), ("a", "b", "z"), ("b", "a", "z")):
            saved = deepcopy(payload)
            saved["results"] = [deepcopy(existing[name]) for name in order]
            renumber(saved)
            before = deepcopy(saved)
            with patch.object(condition_ranking.random, "Random",
                              side_effect=AssertionError("Validation must not replay shuffles")):
                self.assertIsNone(validate(saved, conditions))
            self.assertEqual(before, saved)
        payload["results"] = [existing[name] for name in ("z", "b", "a")]
        renumber(payload)
        with self.assertRaises(PhotographyError):
            validate(payload, conditions)


if __name__ == "__main__":
    unittest.main()
