-- DESIGN ONLY: additive schema draft; not a complete migration program.
-- Requires existing v5 photos, analyses and embedding_encoders tables.
-- Do not execute against the live database without the migration workflow.
-- Backfill selections and run validation before setting user_version=6.

CREATE TABLE model_artifacts (
    artifact_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX analyses_photo_analysis
ON analyses(photo_id, analysis_id);

CREATE TABLE photo_analysis_selections (
    photo_id TEXT PRIMARY KEY REFERENCES photos(photo_id),
    analysis_id TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    FOREIGN KEY(photo_id, analysis_id)
        REFERENCES analyses(photo_id, analysis_id)
);

CREATE TABLE embedding_documents (
    document_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
    encoder_id TEXT NOT NULL REFERENCES embedding_encoders(encoder_id),
    recipe_version TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    retrieval_text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK(token_count >= 0),
    trimming_json TEXT NOT NULL CHECK(json_valid(trimming_json)),
    created_at TEXT NOT NULL,
    UNIQUE(analysis_id, encoder_id, text_hash)
);
