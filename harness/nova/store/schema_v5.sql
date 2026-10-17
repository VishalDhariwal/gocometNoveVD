-- Nova state store, schema version 5: a document card on every document.
--
-- documents.summary: two or three sentences the reader wrote about the document (what it is, who it is from and to,
-- the identifiers and dates that matter, anything unusual). It helps the case reviewer and a person find their way;
-- it is the model's own description, never evidence, and nothing is decided from it.

ALTER TABLE documents ADD COLUMN summary TEXT;
