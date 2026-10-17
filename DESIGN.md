# Design

## Fact model

Models read, code decides. Each document is stored raw with its pages and, for an email, its own words apart from
quoted text. Readers (classify, contract, billing, records, tables, correspondence, and a general reader for any
other layout) turn it into facts in a bitemporal SQLite store: a kind (rate, invoice_line, waiver, contract_term,
tracking_event ...), a JSON payload, keys (contract, invoice, waybill, lane, party, person, period), validity dates,
and the document, page and quote it came from. A quote must be on its page or the fact is marked unverified.
`report --as-of` rebuilds every answer from the facts known by that date, so reports are reproducible and order of
arrival doesn't matter. Every reader also writes a four- or five-sentence card of the document for people and the
reviewer; it is never evidence.

## Conflicts

The terms engine answers each lookup (rate on a lane, band and pickup date; fuel; fees; FX; clauses) as Decided,
Conflict, Missing or Undefined, with its sources. Precedence comes from the contract's own clause: signed amendment
over MSA, later amendment over earlier, notices under clause 3.5. Authority comes from Schedule 1 and the signatures;
quoted text, outsiders and planted text decide nothing. Two authorised sources that disagree and that the contract
does not rank are a conflict, escalated with both quotes and the range. A held document that cites one we don't hold
is cannot_determine. One document read two ways is not a conflict: we claim only what is overcharged under every
reading. An amendment's rule counts only when its quote speaks of that rule.

## Confidence

A logistic score: a starting value per class by how much rests on a model's reading (an overcharge on a rate cell,
band and period starts below a duplicate), minus penalties for a pickup date not from tracking, weak verification,
quotes lost in the check, labels mapped by a model, weight disagreements, spend near a tier, an undecided reading,
and a case the reviewer didn't settle. The first practice run scored every item about 0.92, so all wrong items
outranked right ones (AUROC 0); after re-basing, the latest run has a Brier score of 0.014. A penalty for partly read
documents was tried and dropped because every contract reads as partial.

## People in the loop

Actions follow the glossary only. Conflicts are escalated. An outbound remedy is drafted in supervised mode and
becomes `human_review` at USD 5,000 or more per invoice. Autonomous mode executes a dispute, credit claim or document
request only when all five unattended checks pass (evidence, no open conflict, threshold, reversibility, Schedule 1
authority and recipient); otherwise a person approves it. Nothing that can't be undone is ever done. A case reviewer
(a second agent with read-only tools, money only from code's calculation ledger, five code checks on its answer)
looks at the few cases code can't settle. A human review page per report says what needs a person and why.

## Feedback

Each feedback item is stored with its scope. `close`, `reject` and `override_amount` act on that key alone.
`sender_authority`, `rate_accepted` and `precedence` become facts the terms engine uses inside the contract, lane,
person, category and dates the scope names, so they reach later invoices but nothing outside the scope. A one-off
approval covers only its key. Changed answers carry `learned_at`. Practice: 3 of 3 learning checks.

## New documents

Ingest only adds. Contracts are read first, then everything else, in parallel; a failed read is retried once in the
same run, told what went wrong. A document of unknown type or layout goes to the general reader, whose notes reach
the reviewer when they name our invoices. When a new document changes an answer, the item gets a new id that
supersedes the old one, `retro` marks one created by a backdated document, and stale keys drop out. Practice: 11 of
11 changes right.

## Cost

Measured with gpt-4o-mini through the proxy's call log on the practice replay (163 documents, both batches and
modes): 781k input tokens (535k cached) and 108k output, USD 0.142, so about USD 0.87 per 1,000 documents. That is
about 4.8M input and 0.66M output tokens per 1,000 documents, which prices any other model. Ingest took 3.5 minutes
for 163 documents with 8 documents read in parallel, about 21 minutes per 1,000; contracts are the long pole (60-85 s each).

## What I'd do next

Make contract rules hold run after run: check each yes/no rule against its own quote, ask again on a contradiction,
and read the rules twice where money depends on them. Then replay each mailbox several times before trusting a
pass, run it live with the Claude models it defaults to, calibrate confidence on the evals, and quiet the contract
coverage warnings. I'm least sure about rule reading on long contracts with a small model.
