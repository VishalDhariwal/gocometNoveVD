-- Nova state store, schema version 1. One SQLite file per state folder: <state-dir>/nova.db
--
-- Conventions
--   * Timestamps are UTC ISO text, 'YYYY-MM-DDTHH:MM:SSZ', so they sort correctly as text. Dates are 'YYYY-MM-DD'.
--   * Two clocks: known_at / received_at = when we learned something (the email's Date header);
--     valid_from / valid_to = when it is in force (inclusive dates, NULL = open-ended).
--   * Money and rates inside JSON are decimal strings ("1701.31"), never floats. nova/store/db.py refuses floats.
--   * Source tables are append-only: triggers refuse UPDATE and DELETE on blobs, receipts, facts, feedback and
--     emitted report items, so a bug can't rewrite history. Operational tables (documents, extractions, links,
--     reports, outbox, runs) carry status that changes.
--   * New schema versions are new files (schema_v2.sql, ...). Never edit this one after it has shipped.


-- ======================================= what arrived =======================================================

-- Raw bytes, content-addressed: a file sent twice is stored once. Kept inside the database (not a folder) so an
-- ingest step is one transaction: there's never a file on disk without its rows, or the other way round.
CREATE TABLE blobs (
    sha256      TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    data        BLOB NOT NULL
);

-- One row per .eml file the grader handed us.
CREATE TABLE receipts (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,              -- 'batch_01/0013.eml', relative to the mailbox root
    batch       TEXT NOT NULL,                     -- 'batch_01'
    sha256      TEXT NOT NULL REFERENCES blobs(sha256),
    message_id  TEXT,
    received_at TEXT NOT NULL,                     -- the Date header, UTC: knowledge time for everything inside
    ingested_at TEXT NOT NULL
);

-- The email itself and each of its attachments. doc_ref is exactly what evidence cites.
CREATE TABLE documents (
    id                    INTEGER PRIMARY KEY,
    doc_ref               TEXT NOT NULL UNIQUE,    -- 'batch_01/0013.eml' or 'batch_01/0013.eml#TX-US-2035-0189.pdf'
    receipt_id            INTEGER NOT NULL REFERENCES receipts(id),
    parent_id             INTEGER REFERENCES documents(id),   -- the email, for an attachment
    filename              TEXT,
    format                TEXT NOT NULL CHECK (format IN ('eml', 'pdf', 'xlsx', 'other')),
    sha256                TEXT NOT NULL REFERENCES blobs(sha256),
    received_at           TEXT NOT NULL,           -- copied from the receipt, for fast as_of filtering
    is_scan               INTEGER NOT NULL DEFAULT 0,
    doc_type              TEXT,                    -- the classifier's answer (msa, invoice, ...); NULL until classified
    doc_type_confidence   REAL,
    status                TEXT NOT NULL DEFAULT 'new'
                          CHECK (status IN ('new', 'read', 'extracted', 'failed', 'skipped')),
    current_extraction_id INTEGER REFERENCES extractions(id),
    error                 TEXT
);
CREATE INDEX documents_receipt ON documents(receipt_id);
CREATE INDEX documents_status ON documents(status);

-- Header fields of an email, and its body split into the sender's own words and the text it quotes.
CREATE TABLE emails (
    doc_id        INTEGER PRIMARY KEY REFERENCES documents(id),
    message_id    TEXT,
    from_addr     TEXT NOT NULL,                   -- lower-cased; identity is the address, never the display name
    from_name     TEXT,
    to_addrs      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(to_addrs)),
    cc_addrs      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cc_addrs)),
    subject       TEXT,
    in_reply_to   TEXT,
    authored_text TEXT NOT NULL,
    quoted_text   TEXT NOT NULL DEFAULT ''         -- replied-to or forwarded text: never carries authority
);
CREATE INDEX emails_from ON emails(from_addr);

-- Page text as the grader reads it, or as Claude vision read a scan. Rebuildable from the blob, so not append-only.
CREATE TABLE pages (
    doc_id           INTEGER NOT NULL REFERENCES documents(id),
    page_no          INTEGER NOT NULL CHECK (page_no >= 1),
    text             TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('text', 'vision', 'unread', 'email', 'sheet')),
    model            TEXT,
    unreadable_marks INTEGER NOT NULL DEFAULT 0,
    ocr_agreement    REAL,
    notes            TEXT,
    error            TEXT,
    PRIMARY KEY (doc_id, page_no)
);


-- ======================================= what we read from it ===============================================

-- One reader pass over one document. A new prompt version means a new pass; the document then points at it.
CREATE TABLE extractions (
    id             INTEGER PRIMARY KEY,
    doc_id         INTEGER NOT NULL REFERENCES documents(id),
    reader         TEXT NOT NULL,                  -- 'contract', 'billing', 'records', 'tables', 'correspondence'
    reader_version TEXT NOT NULL,
    model          TEXT,
    status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
CREATE INDEX extractions_doc ON extractions(doc_id);

-- Everything the readers extracted, one fact per row, each with where it came from. The kinds, and which key
-- columns each must fill, are listed in nova/store/kinds.py. Key columns hold normalised values (nova/normalize.py).
CREATE TABLE facts (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    contract_id   TEXT,
    invoice_id    TEXT,
    awb           TEXT,
    lane          TEXT,
    party         TEXT,
    person_email  TEXT,
    period        TEXT,                            -- 'YYYY-MM', 'YYYY-Qn' or 'CYn'
    payload       TEXT NOT NULL CHECK (json_valid(payload)),
    valid_from    TEXT,                            -- business time: in force from / to (inclusive); NULL = open
    valid_to      TEXT,
    known_at      TEXT NOT NULL,                   -- knowledge time: when the source document arrived
    doc_id        INTEGER NOT NULL REFERENCES documents(id),
    extraction_id INTEGER NOT NULL REFERENCES extractions(id),
    page          INTEGER,
    quote         TEXT,                            -- an exact line from that page
    verification  TEXT NOT NULL
                  CHECK (verification IN ('verified', 'repaired', 'unverified', 'vision', 'structured')),
    created_at    TEXT NOT NULL
);
CREATE INDEX facts_kind_contract ON facts(kind, contract_id);
CREATE INDEX facts_kind_invoice ON facts(kind, invoice_id);
CREATE INDEX facts_kind_awb ON facts(kind, awb);
CREATE INDEX facts_kind_person ON facts(kind, person_email);
CREATE INDEX facts_extraction ON facts(extraction_id);

-- Only the facts from each document's current reader pass. Older passes stay in `facts` for the audit trail.
CREATE VIEW current_facts AS
    SELECT f.*, d.doc_ref
    FROM facts f
    JOIN documents d ON d.id = f.doc_id AND d.current_extraction_id = f.extraction_id;

-- Cross-document links the reconciler works out (invoice -> contract, billed label -> charge code, reference ->
-- held or missing document). Derived and rebuildable. known_at is the latest arrival among the inputs, so a report
-- for an earlier date never uses a link that depends on a later document.
CREATE TABLE links (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL,
    object     TEXT,
    method     TEXT NOT NULL CHECK (method IN ('exact', 'rule', 'llm')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    detail     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),
    known_at   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, subject, known_at)
);


-- ======================================= what people decided ================================================

CREATE TABLE feedback_files (
    id          INTEGER PRIMARY KEY,
    sha256      TEXT NOT NULL UNIQUE,              -- the same file twice is taken in once
    path        TEXT NOT NULL,
    world       TEXT,
    after_batch TEXT,
    decided_at  TEXT NOT NULL,
    decided_by  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(decided_by)),
    raw         TEXT NOT NULL CHECK (json_valid(raw)),
    ingested_at TEXT NOT NULL
);

-- One decision. decision and fact_type aren't constrained: an unfamiliar kind is stored and logged, never a crash.
CREATE TABLE feedback_items (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES feedback_files(id),
    item_id     TEXT NOT NULL,
    finding_key TEXT NOT NULL,                     -- normalised, e.g. 'overcharge|TX-US-2035-0210'
    solicited   INTEGER,
    decision    TEXT NOT NULL,
    fact_type   TEXT,
    fact        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(fact)),
    scope_kind  TEXT,                              -- 'standing' or 'one_off'
    applies_to  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(applies_to)),
    valid_from  TEXT,
    valid_to    TEXT,
    decided_at  TEXT NOT NULL,
    after_batch TEXT,
    note        TEXT,
    UNIQUE (file_id, item_id)
);
CREATE INDEX feedback_items_key ON feedback_items(finding_key);


-- ======================================= what we said and did ===============================================

-- Every report we printed, kept whole for the audit.
CREATE TABLE reports (
    id         INTEGER PRIMARY KEY,
    as_of      TEXT NOT NULL,
    mode       TEXT NOT NULL CHECK (mode IN ('supervised', 'autonomous')),
    status     TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'printed', 'failed')),
    report     TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at TEXT NOT NULL
);

-- One row per item in a report. The next report compares against these to set supersedes and retro.
CREATE TABLE emitted_items (
    report_id   INTEGER NOT NULL REFERENCES reports(id),
    finding_key TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    signature   TEXT NOT NULL,                     -- verdict|amount|range|deadline: a change means a new version
    verdict     TEXT NOT NULL,
    action      TEXT NOT NULL,
    item        TEXT NOT NULL CHECK (json_valid(item)),
    PRIMARY KEY (report_id, finding_key)
);
CREATE INDEX emitted_items_key ON emitted_items(finding_key);

-- Drafts and sends. The idempotency key (item id + version) makes re-running a report safe: nothing goes out twice.
CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    report_id       INTEGER NOT NULL REFERENCES reports(id),
    mode            TEXT NOT NULL CHECK (mode IN ('supervised', 'autonomous')),
    action          TEXT NOT NULL,                 -- 'draft:dispute', 'execute:claim_credit', ...
    finding_key     TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    recipient_email TEXT,                          -- from Schedule 1 only, never from an email body
    recipient_name  TEXT,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('drafted', 'queued', 'sent', 'superseded', 'cancelled')),
    created_at      TEXT NOT NULL
);


-- ======================================= housekeeping =======================================================

-- Claude answers keyed by the full request (see nova/llm/gateway.py): re-runs are free and identical.
CREATE TABLE llm_cache (
    key           TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,
    version       TEXT NOT NULL,
    model         TEXT NOT NULL,
    data          TEXT NOT NULL CHECK (json_valid(data)),
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- One row per command the grader ran, with what it cost.
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY,
    command       TEXT NOT NULL,
    args          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args)),
    nova_version  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    api_calls     INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

-- Things worth keeping beyond stderr: failed reads, injection attempts, skipped documents.
CREATE TABLE run_log (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER REFERENCES runs(id),
    at      TEXT NOT NULL,
    level   TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    doc_id  INTEGER REFERENCES documents(id),
    message TEXT NOT NULL
);


-- ======================================= append-only guards =================================================

CREATE TRIGGER blobs_no_update BEFORE UPDATE ON blobs BEGIN SELECT RAISE(ABORT, 'blobs are append-only'); END;
CREATE TRIGGER blobs_no_delete BEFORE DELETE ON blobs BEGIN SELECT RAISE(ABORT, 'blobs are append-only'); END;
CREATE TRIGGER receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER facts_no_update BEFORE UPDATE ON facts BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER facts_no_delete BEFORE DELETE ON facts BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER feedback_files_no_update BEFORE UPDATE ON feedback_files
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER feedback_files_no_delete BEFORE DELETE ON feedback_files
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER feedback_items_no_update BEFORE UPDATE ON feedback_items
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER feedback_items_no_delete BEFORE DELETE ON feedback_items
    BEGIN SELECT RAISE(ABORT, 'feedback is append-only'); END;
CREATE TRIGGER emitted_items_no_update BEFORE UPDATE ON emitted_items
    BEGIN SELECT RAISE(ABORT, 'emitted items are append-only'); END;
CREATE TRIGGER emitted_items_no_delete BEFORE DELETE ON emitted_items
    BEGIN SELECT RAISE(ABORT, 'emitted items are append-only'); END;
