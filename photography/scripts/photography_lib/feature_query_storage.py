"""Read-only, parameterized lookups over the stage-one saved OCR index."""
from __future__ import annotations

from .config import PhotographyError
from .feature_profiles import normalize_ocr_text


def ocr_matches(text, result_ids, *, store):
    """Return result-ID -> bounded normalized snippet, restricted to caller-validated results."""
    if (not isinstance(text, str) or not text or "\x00" in text
            or normalize_ocr_text(text) != text
            or not isinstance(result_ids, (list, tuple))
            or any(not isinstance(value, str) or not value for value in result_ids)):
        raise PhotographyError("INVALID_ARGUMENT", "OCR lookup requires normalized literal text and saved result IDs.")
    ids = list(dict.fromkeys(result_ids))
    matches = {}
    phrase = '"' + text.replace('"', '""') + '"'
    # Batches stay below SQLite's historical 999-parameter limit; only placeholders are interpolated.
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        placeholders = ",".join("?" for _ in batch)
        if len(text) >= 3:
            source = """image_ocr_fts JOIN image_ocr_documents d
                        ON d.document_id=image_ocr_fts.rowid"""
            restriction = "image_ocr_fts MATCH ? AND "
            parameters = [text, phrase, *batch, text]
        else:
            source = "image_ocr_documents d"
            restriction = ""
            parameters = [text, *batch, text]
        rows = store.db.execute(
            "SELECT d.result_id, substr(d.normalized_text, max(1,instr(d.normalized_text,?)-40),160) AS snippet "
            f"FROM {source} WHERE {restriction}d.result_id IN ({placeholders}) "
            "AND instr(d.normalized_text,?)>0", parameters)
        matches.update((row["result_id"], row["snippet"]) for row in rows)
    return matches
