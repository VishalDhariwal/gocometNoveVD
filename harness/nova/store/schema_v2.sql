-- Nova state store, schema version 2: the case reviewer's audit trail (nova/review).
--
-- One row per case the reviewer looked at in a report: why code sent it, what the reviewer said, and what code did
-- with that (taken, held for a person, or rejected and why). The reviewer's whole conversation is in llm_cache.

CREATE TABLE reviews (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    finding_key TEXT NOT NULL,
    reason      TEXT NOT NULL,                     -- why triage picked it: waiver_condition, unread_email, ...
    opinion     TEXT CHECK (opinion IS NULL OR json_valid(opinion)),   -- the reviewer's answer, as given
    outcome     TEXT NOT NULL,                     -- upgraded | held | no_change | rejected | failed | skipped
    detail      TEXT NOT NULL DEFAULT '',          -- what code checked, in words
    model       TEXT,
    turns       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX reviews_report ON reviews(report_id);

CREATE TRIGGER reviews_no_update BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
