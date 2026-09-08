"""Versioned unified requests, reusing the existing saved-predicate normalization."""
from __future__ import annotations

from .condition_queries import _fields, _require, _text, normalize_query as normalize_conditions


QUERY_SCHEMA = "unified-search-query-v1"


def normalize_query(raw, *, store):
    _fields(raw, {"schema", "query", "operator", "conditions", "evidence_conditions", "scope", "candidate_limit"},
            ("query", "conditions"))
    _require(raw.get("schema", QUERY_SCHEMA) == QUERY_SCHEMA, "Unsupported unified-query schema.")
    original = _text(raw["query"], "Original query")
    conditions = raw["conditions"]
    _require(isinstance(conditions, list) and bool(conditions), "Provide a nonempty condition array.")
    evidence = raw.get("evidence_conditions", [])
    _require(isinstance(evidence, list), "Supporting evidence conditions must be an array.")
    _require(all(isinstance(condition, dict) and condition.get("kind") != "semantic" for condition in evidence),
             "Supporting evidence accepts saved structured conditions only.")
    _require(len(conditions) == 1 or "operator" in raw,
             "Multiple original conditions require an explicit AND or OR operator.")
    operator = raw.get("operator", "and")
    _require(operator in ("and", "or"), "Unified queries require an AND or OR operator.")
    limit = raw.get("candidate_limit", 100)
    _require(limit == "all" or type(limit) is int and limit > 0,
             "Candidate limit must be a positive integer or 'all'.")
    normalized = normalize_conditions(
        {"conditions": conditions + evidence, "scope": raw.get("scope", {}), "semantic_candidates": "all"},
        store=store)
    required_ids = {normalized["aliases"][condition["id"]] for condition in conditions}
    canonical = normalized["query"]["conditions"]
    return {"query": {"schema": QUERY_SCHEMA, "query": original, "operator": operator,
                      "conditions": [condition for condition in canonical if condition["id"] in required_ids],
                      "evidence_conditions": [condition for condition in canonical if condition["id"] not in required_ids],
                      "scope": normalized["query"]["scope"], "candidate_limit": limit},
            "aliases": normalized["aliases"]}
