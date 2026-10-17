-- Nova state store, schema version 4: how long each read and each reviewer case took, and what a case cost.
--
-- extractions.seconds: wall time to read one document (its classification included), queueing for the API included.
-- reviews.seconds / input_tokens / output_tokens: one reviewer case, all its turns (answers served from the cache
-- count no tokens).

ALTER TABLE extractions ADD COLUMN seconds REAL;
ALTER TABLE reviews ADD COLUMN seconds REAL;
ALTER TABLE reviews ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reviews ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
