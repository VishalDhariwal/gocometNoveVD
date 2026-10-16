-- Nova state store, schema version 3: the human review queue (nova/review/route.py).
--
-- One row per item a person should look at, per report: the rule that sent it (G1-G3 the glossary's, R1-R6 ours),
-- why, how urgent, the action the report gave it, and the case reviewer's note and quotes when it has them. The same
-- queue is written as a readable page to <state>/human_queue/<mode>/<as_of>.md.

CREATE TABLE human_queue (
    id          INTEGER PRIMARY KEY,
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    mode        TEXT NOT NULL CHECK (mode IN ('supervised', 'autonomous')),
    as_of       TEXT NOT NULL,
    item_key    TEXT NOT NULL,                     -- a finding key, or document|<doc_ref> for a document case
    rule        TEXT NOT NULL,                     -- 'G1 threshold', 'R2 reviewer holds', ...
    rules       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(rules)),
    reason      TEXT NOT NULL,
    priority    TEXT NOT NULL CHECK (priority IN ('high', 'normal', 'low')),
    action      TEXT NOT NULL,
    amount_usd  TEXT,
    note        TEXT NOT NULL DEFAULT '',
    quotes      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(quotes)),
    created_at  TEXT NOT NULL
);
CREATE INDEX human_queue_report ON human_queue(report_id);

CREATE TRIGGER human_queue_no_update BEFORE UPDATE ON human_queue
    BEGIN SELECT RAISE(ABORT, 'the human queue is append-only'); END;
CREATE TRIGGER human_queue_no_delete BEFORE DELETE ON human_queue
    BEGIN SELECT RAISE(ABORT, 'the human queue is append-only'); END;
