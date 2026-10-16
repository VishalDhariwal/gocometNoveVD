# AI use

## Tools

- **Claude (Anthropic), in the Claude app**, linked to my local copy of this repo so it could read the files and run read-only commands on them. Model for the session: `claude-opus-5-5` (the app can serve a different model on some turns). Used so far to read and summarise the brief, the glossary, the worked examples, the data and the grader code, and to draft the architecture and build plan in `docs/ARCHITECTURE.md`.
- **Claude through the Anthropic API, inside the harness** (planned). The harness uses Claude to read documents. That's part of the product rather than a coding tool, and `DESIGN.md` describes it.

## How you worked with them

- I read `README.md` and the other explanation files myself first. Then I asked Claude to go back through every instruction file and give me the crux: what exists, what we have to build, and what's being asked. It wrote a report (a Claude doc called "GoComet Project Deep Dive").
- I asked it to explain the whole thing again in plain words, to check my understanding against its.
- Then I asked it to act as the engineer and work out a robust, real-world architecture one module at a time. Before writing anything, it read the actual contracts, amendments, invoices (including the scanned ones), statements, waybills, tracking exports, AP registers, every plain email in both mailboxes, and the grader's scoring and runner code. It did that in a scratch Python environment outside the repo, so the only repo files that changed are `docs/ARCHITECTURE.md` and this one.
- *To fill in as I review:* what I changed in the plan and why, and anything I checked against the files myself.
- Where it got things wrong: nothing caught yet. I'll log it here as it happens.

## Written without AI

No code yet. Parts I write by hand will be listed here.

## Prompt log

Every prompt I've sent, copied exactly as I typed it (typos and spacing included), in order. Times are IST. Under each is a one-line note of what the AI did. Nothing was removed because there were no secrets in them. The full chat export goes in `ai_log/` at the end.

### Session 1: Claude app, 2026-10-12

**Prompt 1** (22:15)

```text
Hey, below is a application we will be building. Uh, it is from GeoComet. I have uh, initially read the the files like readme.md and other kind of exlaination files.  I have an overall idea of what we will be building and what is the project about. So your job is to uh, go again and deep dive and give me the crux of the application like Then what currently exists first of all,what we have to build  . third what are the uh, requirement they are asking? So generate a report on that. so i will a full picture in the mind. read all the instruction and explanation files.
```

What it did: read `README.md`, `GLOSSARY.md`, `SUBMISSION.md`, `BRIEF.pdf` (and checked it matches the markdown files), the `DESIGN` / `SOLUTION` / `AI_USE` templates, all 8 worked examples, the report schema, the grader's module docs and a summary of the practice answers. Wrote the "GoComet Project Deep Dive" report: the crux, what exists, what to build, the requirements, gotchas and open questions.

**Prompt 2** (22:31)

```text
ok now tell me in your own words not the repo language i have read all this. make it simple and precise
```

What it did: a plain-language summary in chat covering what exists, the three jobs of the program, what they really want, and how grading works.

**Prompt 3** (22:42)

```text
ok fine. now you are an engineer and this thing is upto you. you have to fix it for a real billion dollar company which cant bear loses like these. so i want you to deep dive first and think on a module at a time and try to figure out what should be the robust archtecture. also create a file ai_use which is asked and log my every propmted their in the way their asked. although i will be inspecting it once it will be ready.  take as long as you want but the implementation plan should be very robust for real world not just for chasing the numbers for accuracy.
```

What it did: read the contracts, amendments, invoices (both layouts, including the scanned ones), statements, waybills, declarations, tracking exports, AP registers and plain emails in the practice and exam mailboxes, plus the grader's scoring code and runner. Wrote `docs/ARCHITECTURE.md` (module-by-module architecture, rules per class, safety, testing, a 16-hour build plan, risks, open questions) and this file.

**Prompt 4** (2026-10-13 00:34)

```text
give me a quick summary of what you have suggested and each why? what is the use of it here? its alternative approach? and also tell me where we can lack behind using this.
```

What it did: a chat summary of each design choice in the plan: why it's there, what it does for this task, the alternative, and where it can fall short.

**Prompt 5** (2026-10-13 00:43)

```text
now analyze and give me the format or formats in which we will recieve emails or documents and like what kind of emails  and documents. and tell me a mental model like what we need to build . how will be the end user using it and what does he want. tell me all these in a very clear way. after all this give me an example assuming me  a end user and then make me understand with a proper explanation with simple example
```

What it did: explained in chat the formats and kinds of emails and documents in the mailboxes, a mental model of the system, who the end user is and what they want, and a worked example with the user as the AP manager.

**Prompt 6** (2026-10-13 01:16)

```text
ok everything is understood now. now tell me do we have a limitation of time like for a batch to preocess it takes time to preocess all the emails and pdfs and we can't it in a single go as it become very hard for llm to maintain the context for each documents? tell me how are they ,examiner, gonna do it like take a batch and then process a signle email in a single run or what? also tell me where does attach documents live like i only see .eml files.
```

What it did: explained in chat how the grader replays the mailbox (one ingest per batch, a time limit per step, fresh state per mode), the time budget and why the LLM never needs a whole batch in context, and showed where attachments live inside the .eml files.

**Prompt 7** (2026-10-13 01:33)

```text
so ok i have now complete picture in my mind. we can write our logic to process the pdfs and emails but within a time limit , for now we will not consern about the time limit. next is the db seletion. can we use any database for saving metadata and other things.
```

What it did: explained in chat which databases are possible under the grader's sandbox (no network except the Anthropic API, a fresh process per step, only the state folder persists), compared the options, and recommended SQLite inside the state folder.

**Prompt 8** (2026-10-13 01:41)

```text
ok so we have everything we just need to start with the worklow. but before that lets verify one more thing that if a pdf is scaned then do we need to run ocr or that is already implemented. as extracter.py have functions  which do rest of the job. analyze and find if we need something there or we should procede to our main worklow of harness?
```

What it did: ran `grader/extract.py` on both scanned invoices to show it returns no text (it only detects scans), test-ran the local OCR package on the scans, and explained what we need to build for scanned PDFs before moving on.

**Prompt 9** (2026-10-13 01:57)

```text
i think ocr will not be able to handle the complex document we need to use the claude vision here as spicified in the document. so start with the implemention of claude vision . first fix this broken piece then we will move to the main workflow with the acute planning as we are doing.
```

What it did: built the Claude vision reader for scanned PDFs (an API gateway with caching and model fallback, page rendering, a transcription prompt, an optional OCR cross-check) plus 11 offline unit tests (a fake client stands in for Claude) and a dry run on the exam scan. The live run needs my API key, so I run that step myself.

**Prompt 10** (2026-10-13 02:12)

```text
now ok we can extract the scanned pdf. next thing we will do is the sqlite db and schema design.  so do it now.
```

What it did: designed and built the SQLite state store (schema v1 with numbered migrations, append-only source tables, bitemporal facts, fact-kind registry, key normalisation, report history, outbox, LLM cache), moved the vision reader's cache into it, and added offline tests.

**Prompt 11** (2026-10-13 02:29)

```text
ok go on but never push anything to github i will do it myself no commit is done by you
```

What it did: confirmed it never runs git (no commits, no pushes; files are only written into the working folder). Wired `harness.py`'s three commands to the store, built the mail intake (email parsing, authored vs quoted text, attachments, page text identical to the grader's reader, vision for scans, retry of unread pages), with tests and a full run over both mailboxes.

**Prompt 12** (2026-10-13 02:40)

```text
yes lets begin the phase 2.
```

What it did: built phase 2, the readers. A classifier, then readers for contracts, bills, shipment records, spreadsheets and emails. Claude copies values as printed and code parses and verifies them: every value is checked against its quote on the page, invoice arithmetic is checked, and there's a completeness check for contract tables. Includes an orchestrator that runs readers in parallel with a time budget, a summary tool for the live run, and offline tests with canned answers taken from the real documents.

**Prompt 13** (2026-10-13 03:03)

```text
move to phase 3 and first tell me your appraoch to this i will verify first
```

What it did: explained the phase 3 approach in chat for review before any code: the as-of knowledge view, the authority levels, the terms engine with precedence and its four kinds of answer, the pricing engine, the label-to-code reconciler, the tests, and the decisions needing sign-off. No code written in this step.

**Prompt 14** (2026-10-13 03:09)

```text
but tell me one thing without the llm how can we get these numbers how can python code do this?
```

What it did: Python does not read the PDF; Python reads the database that Claude already filled during Phase 2.
Think of it like two different people:
Phase 2 (Claude) is the Data Entry Clerk: It reads the messy PDF documents and enters clean rows into your SQLite database (facts table).
Phase 3 (Python) is the Calculator/Accountant: It queries SQLite for those clean rows and does the exact multiplication.

**Prompt 15** (2026-10-13 03:15)

```text
ok but is it sure we always have these things like numbers pricese and with always proper structure in the db? as we have emails and pdf from different types?
```
What it did: Claude fits the messy document into a rigid JSON tool schema.
Python verifies every number against the raw page text.
Python normalizes all keys (IDs, lanes, dates).
If anything is missing or unparseable, Python safely abstains (no_finding / cannot_determine) instead of hallucinating money.

**Prompt 16** (2026-10-13 03:18)

```text
ok continue with the phase 3
```

What it did: built phase 3 as approved. Covers the as-of knowledge view, authority levels (Schedule 1 by address and category, quoted text, outside domains, feedback grants), and the terms engine (rates with precedence, conflicts and missing documents; fuel, FX, fees, charge codes, clause terms). Also the pricing engine (pickup-date sources, weight bands and minimums, fuel on correct freight, DG vs dry ice, storage, delivery, customs, peak, monthly fees, GST) plus conflict options, the charge-name reconciler and a per-invoice trace command, with tests that reproduce the worked examples' arithmetic.

**Prompt 17** (2026-10-13 03:43)

```text
we will do it later as i have no antropic key now telll me about phase 4
```

What it did: explained the phase 4 approach in chat for review before any code. Covers the six detectors (overcharge, duplicate, waived but billed, late fees, cashback, expiry), the adjudicator that gives each key one verdict, the claim ledger that stops the same money being claimed twice, golden tests built from the worked examples, and the decisions needing sign-off. No code written in this step.

**Prompt 18** (2026-10-13 11:01)

```text
i want your opinion here - 
i think better is using claude as a llm and it can call tools to do the maths instead of writing so many functions manually? if some where needed we can keep them but as we have complex documents we should use the claude? what is your opinion?
if something diffucult / mislenious which can not handled by python there we can use claude. ?
the thing is not every document have the same format and also not all emails and documents related to this only there can be any other domain also. how python can manage it?
```

What it did: gave an opinion on Claude-as-auditor vs Python rules. Claude already handles every format and off-topic document at ingest, and Python only sees normalised facts, so format variety never reaches the Python code. It recommended keeping the money decisions in Python (same answer every run, no as_of leaks, auditable, safe from planted instructions, no API cost at report time). It agreed with the user that the fuzzy judgement calls (precedence wording, waiver conditions, customs cause, GST scope, free days) should move from regexes to Claude at ingest, as fixed-choice fields. No code changed.

**Prompt 19** (2026-10-13 11:11)

```text
so lets first  fix the regex with claude. with proper prompts.
```

What it did: replaced every place where code was interpreting wording with fixed-choice answers from Claude at ingest. Each answer carries a quote that is checked against the page:
- contract rules: which of a rate notice and an amendment prevails, free storage days and their start and end events, GST scope and rate, customs-hold exemption;
- accessorial basis units and seasonal months;
- weight bands, read as from/to with a check against the printed band;
- tracking-event, ledger-status and reason-code meanings for spreadsheets, using every distinct value in the sheet;
- status-update events, customs-hold causes, waiver conditions and copy markers in emails;
- dry-ice items in declarations;
- line units and copy markers on bills.

Reader versions were bumped so documents are re-read. The terms and pricing engines now read these answers instead of regexes, and abstain when a question isn't answered. Tests were updated and added (112 passing).

**Prompt 21** (2026-10-13 11:59)

```text
yes continue to the phase
```

What it did: built phase 4 with the five decisions as approved.
- **Detectors:** overcharge, duplicate (resent invoice number, and the same charge on two invoice numbers), waived but billed, late fee not owed, late-fee risk, cashback (quarterly rebate, annual improvement credit, margin step-down) and expiry.
- **Adjudicator and claim ledger:** one verdict per key, and no billed line is ever claimed twice.
- **Contract reader:** the clauses these checks depend on are now fixed-choice answers: late fee, payment terms, rebate tiers, improvement credit, the qualifying-spend definition, handling margin, renewal and extension. An amendment's new end date is stored as an extension.
- **Waivers:** the reader now records which invoice a payment condition names.
- **Tools and tests:** an audit command prints every verdict. Tests rebuild all eight worked examples, each with its decoy, plus edge cases (143 tests passing).

**Prompt 22** (2026-10-13 12:20)

```text
yes - phase 5
```

What it did: started phase 5 by reading how the grader scores actions, escalation, confidence, citations and its hard gates. The user stopped it partway; no code was written in this step.

**Prompt 23** (2026-10-16 17:07)

```text
start phase 5
```

What it did: built phase 5.
- **Action gate:** draft, execute, human_review, escalate or internal action per item and mode. It runs the five unattended-action checks with their reasons, applies the $5,000 threshold summed per invoice, and blocks autonomous action while a conflict is open on the same lane and period. Recipients come only from Schedule 1.
- **Confidence:** a hand-set score built from the pickup-date source, extraction quality, lost citations, Claude-mapped labels, weight disagreement and closeness to a rebate tier.
- **Citation check:** mirrors the grader's quote check; quotes on scanned pages are checked by page number only.
- **Report writer:** stable ids, versioning with supersedes, retro for backdated documents, learned_at, a recipient field and a schema check. Items are recorded for the next report.
- **Clerk:** template-only drafts and an idempotent outbox.
- **Tests:** 156 passing.

**Prompt 24** (2026-10-16 17:32)

```text
do phase 6
```

What it did: built phase 6, feedback inside its scope.
- **Single-key decisions:** close and reject drop that key only; override_amount sets that key's amount.
- **Authority grants:** a sender_authority grant now also has to list the category.
- **Waiver approvals:** a one-off waiver approval covers the named months on its own key and nothing else.
- **Strict learned_at:** every report is also computed with no feedback, and learned_at is attached only where feedback changed the verdict, amount, deadline or action.
- **Tests:** mirror each feedback kind against a sibling it must not reach (165 passing).

**Prompt 25** (2026-10-16 17:38)

```text
so now we have to create our own environment using any llm like gemini api key and we willl test with it. the officials will try with the anthropic so create a .py file which do this
```

What it did: wrote `tools/llm_proxy.py`, a local stand-in for the Anthropic Messages API answered by Gemini, used only for testing. The harness stays unchanged and is pointed at the proxy through ANTHROPIC_BASE_URL, the one switch the grader passes through.
- **Translation:** the system prompt, text, images and PDFs, the forced tool, the JSON schemas (with a fallback to the older schema form), the repair round, token usage, stop reasons, and error codes so that model fallback and retries behave as they do against Anthropic.
- **Model mapping:** Opus requests go to a "strong" Gemini model, everything else to a "fast" one.
- **Rate limiting and retries:** for free-tier keys.
- **Gateway change:** the gateway's answer cache now keys on the API endpoint, so stand-in answers are never served to a real run.
- **Tests:** go through the real SDK and gateway against a faked Gemini (174 tests passing).

**Prompt 26** (2026-10-16 18:03)

```text
Tell me two things. First is confidence. Like, uh, the content should not be a hard-coded value. Like, uh, it is always showing 75 or 90 or 100 or 75, 90, 100. Not like this. And second is, uh, that my doubt is about Uh, and second is how will you provide the actual text or sentence or paragraph like whatever it is asked like like the facts on the basis of which we have divided or participated we have uh, divided or classified the email or the outcome we have provided on what basis how will we do that
```

What it did: explained how confidence is computed today. It's a formula, not fixed numbers, but its inputs are coarse, so clean items cluster on a few values. It proposed continuous evidence features with calibration on labelled runs. It also explained how evidence is traced: every fact carries the exact quote from its page, and every verdict carries the facts it used, re-checked against the page before printing. It offered to add a reasoning trail to each report item. No code changed.

**Prompt 27** (2026-10-16 18:10)

```text
first fix for explanation part , we will do for confidence later
```

What it did: every report item now carries an `explanation` list. The first line is a plain conclusion: what was found, the amount, and for conflicts and missing documents what a person must do and whom to ask. It's followed by the working in order (each billed line against what it should be and why, the arithmetic, the pickup date and its source, the total, any currency conversion). Each evidence entry now carries a `role` (billed line, contract rate, pickup date from tracking, contract clause, authority, register...) and a `detail` saying why it was used. The step text for overcharges, duplicates and waivers was rewritten to read as sentences, and the draft letters use the same working. Confidence is left for later (176 tests passing).

**Prompt 28** (2026-10-16 18:30)

```text
now tell me what is left
```

What it did: listed what's left. The live practice run (through the Gemini stand-in or a real key), fixing prompts against real reads, calibrated confidence, our own eval cases and stress tests, the exam answer sheet, the submission check, and the DESIGN / SOLUTION / AI_USE write-ups. No code changed.


**Prompt 29** (2026-10-16 19:20)

```text
I think after collecting all facts , evidence . we should make a llm call to give a report ?  I think after doing money calculations we can use llm. as llm are better in decisions. what u think? we will not do this for all the emails in a batch but for some classs which is very hard to be left to python code only.
i want your opinion in it. take your time and think about it
```

What it did: gave an opinion. It agreed there is a real gap: judgements that need several documents read together. It advised against letting Claude pick verdicts, amounts or actions at report time, because of the injection hard gate, answers changing between runs (which breaks supersedes), the rule-based answer key, and the report having to work without the API. Instead it proposed a cross-document judgement step at ingest that asks closed questions, for example: is a waiver's condition met, which shipments does a vague waiver cover, is a quoted waiver the same as an original we hold. Each answer is a fixed choice citing fact ids, checked by code and stored as a fact with its own known_at. It also suggested an optional second opinion that can only hold back an autonomous execute, and letting the live run's abstentions decide which questions to add first. No code changed.

**Prompt 30** (2026-10-16 19:40)

```text
but why we have to use the same agent we can introduce a new agent which will handle these difficult scenarios because adding it to ingestion layer and giving question whihc python ask has no use as they are hard coded and become vague. for these critical cases we can send the documents if needed and all we have and use claude for better results and which can help us to safe our company money. i want your opinion about hthis think and tell me
```

What it did: agreed that a separate case-reviewer agent at report time beats hard-coded questions at ingest, since fixed questions only cover cases we foresaw. Proposed this shape:
- Code picks the hard cases: abstentions with a "can't check" reason, conflicts, $5,000 and over, and unread emails naming our invoices.
- The agent reads the full documents plus our facts and working. It has tools to find facts, read a page, price a line and check the payment register.
- It answers in a fixed format: an opinion, the lines affected, its reasons and its citations.
- Code checks the citations and the authority of each source, rejects anything resting on injected or quoted text, and recomputes the money.
- Items the agent backs go to a draft or human review, never execute.
- Results are cached per case and evidence so ids stay stable.

No code changed.

**Prompt 31** (2026-10-16 20:23)

```text
all good now create a reviewer
```

What it did: built the case reviewer (`harness/nova/review/`), a separate Claude agent that runs inside `report` for the cases code finds hard.

- **Triage (code) picks the cases:**
  - a waiver whose condition is in words;
  - a waiver the reader couldn't pin to a waybill, invoice or month;
  - a waiver seen only quoted;
  - billed lines whose label nobody mapped;
  - an email no reader understood that names one of our invoices;
  - in autonomous mode, a second look at every dispute about to be executed.

  At most 20 cases, money first, with a 300 s budget.
- **The agent** reads the original documents with read-only tools (read a document, search the mailbox, find facts, payment register, code's pricing) and answers in a fixed shape with quotes. It never gives amounts, actions or recipients.
- **Code checks the answer.** Each quote must be on its page and gets its document's authority level. An answer citing text aimed at automated readers is thrown out. Only two upgrades exist, each with a mechanical test:
  - waived_but_billed: the Schedule 1 waivers contact in their own words, the condition met with a checked record, and the email naming each line's waybill, invoice or month;
  - duplicate: the same waybill and amount on an earlier invoice, with no credit note touching either.

  Code works out the money.
- **What rests on the reviewer is never executed:** a draft in supervised mode, human_review in autonomous, with lower confidence. A cited disagreement holds an autonomous execute for a person.
- **Supporting changes:**
  - every case is logged in a new `reviews` table (schema v2);
  - conversations are cached turn by turn, so re-runs are free and ids don't churn;
  - the Gemini proxy now replays tool turns as real function calls and keeps Gemini 3's thought signatures;
  - ARCHITECTURE.md has a new M19 section.

194 tests pass, including 15 new ones for the reviewer.

**Prompt 32** (2026-10-16 22:06)

```text
as an independent invigilator i want you to see these hard coded roles and their potential drwbacks. i wnat to hear from you.

In this architecture, the LLM reads documents into structured facts at ingest, but all monetary decisions, precedence rankings, claim deduplication, confidence scoring, and action gates are hardcoded Python rules.

1. Action Gate & Policy Rules
File: 

harness/nova/decide/policy.py

$5,000 Financial Threshold:
THRESHOLD = Decimal("5000.00")
In supervised mode: amounts $\ge $5,000$ on an invoice or cashback key are diverted to human_review; under $$5,000$ become draft:<remedy>.
In autonomous mode: amounts $\ge $5,000$ fail the unattended check and force human_review.
The 5 Unattended Autonomous Execution Checks:
Evidence Check:
Repriced lines must take pickup dates strictly from DATED_SOURCES = {"tracking", "status_email"}. Waybill (awb) or invoice-printed dates are hardcoded as insufficient for autonomous execution.
At least one citation must survive the quote check (n_cited > 0).
Conflict Check:
Only checks conflict windows for FREIGHT_CODES = {"FRT", "FUEL"}.
If an open conflict exists on the same contract, lane, and period, autonomous action is blocked.
Threshold Check: Outbound total for the invoice or cashback key must be $< $5,000.00$.
Reversibility Check: Hardcoded to True strictly for dispute, claim_credit, and request_document.
Authority Check: Contract terms must be Level 1–2 (TERM_KINDS); factual evidence must be Level 3 or better.
Recipient Selection Rule:
Outbound dispute/claim emails must only go to Schedule 1 contacts in category billing_disputes or notices (Key Account Manager). Any email address found inside the body of an invoice or email is hard-blocked.
2. Confidence Scoring Formula & Weights
File: 

harness/nova/decide/confidence.py

Confidence is computed using a hardcoded logistic regression formula: $$\text{confidence} = \text{round}\left(\min\left(0.98, \max\left(0.05, \frac{1}{1 + e^{-\text{logit}}}\right)\right), 2\right)$$

Base Logits by Finding Class:
overcharge: +2.4, duplicate: +2.3, waived_but_billed: +2.1, late_fee_not_owed: +2.1, late_fee_risk: +2.2, cashback: +1.7, expiry: +2.8 (default: +2.0).
Verdict Penalty:
finding: 0.0, conflict: -0.3, cannot_determine: -0.4.
Pickup Date Source Penalty:
tracking: 0.0, status_email: -0.1, awb: -0.5, invoice: -0.9, none: -1.5.
Fact Verification Penalty:
verified: 0.0, structured: 0.0, repaired: -0.2, vision: -0.4, unverified: -1.0.
Hardcoded Heuristic Deductions:
Any citation dropped during quote verification: -0.6
Zero citations surviving: -1.5
Charge label mapped by LLM (not an exact word match in Annex E): -0.5
Weight discrepancy between invoice and waybill: -0.5
Line items omitted because they fall outside Annex E: -0.6
Rebate spend within 2% of a tier threshold (gap < 0.02): -0.5
Human feedback confirmed part of reasoning: +0.2
3. Claim Priority & Deduplication Ledger
Files: 

harness/nova/decide/adjudicate.py
, 

common.py

Hardcoded Detector Order: $$\text{duplicate} \longrightarrow \text{waiver} \longrightarrow \text{late_fee_not_owed} \longrightarrow \text{overcharge}$$
Claim Locking: Once a line fact ID is claimed by an earlier detector in Claims, it is marked as taken. Subsequent detectors are strictly prohibited from touching or claiming that line.
Late Fee Interdependence: late_fees.risks is evaluated after invoice disputes so that disputed amounts on an invoice are deducted from the outstanding principal before calculating fee risk.
First-Verdict-Wins: If multiple detectors generate verdicts for the exact same finding_key, only the first is retained; subsequent ones are dropped.
4. Duplicate Billing Rules
File: 

harness/nova/decide/duplicate.py

Resend Rules:
Finding is only raised if the payment register shows the first copy was status in ("paid", "scheduled") on or before the arrival date of the resend (paid_date <= arrived).
The resend must explicitly request payment (asks_for_payment == True).
If the resend contains copy markers in {"copy", "duplicate", "for_your_records"}, it is suppressed (no_finding).
Cross-Invoice Line Duplicate Rules:
Matches line signatures by exact tuple: ("awb", f.awb, code, amount) or ("period", period, code, amount).
Excludes charges coded as TAX and LATE.
The invoice with the earlier date is deemed the original; the invoice with the later date carries the duplicate finding.
Credit Note Logic: If the earlier invoice was credited in full (credits >= total), re-billing is allowed (no_finding). If partially credited, credit is deducted from the duplicate claim amount.
5. Waiver Rules
File: 

harness/nova/decide/waiver.py

Authority Check: The email sender must match Schedule 1 for waivers (or have an explicit feedback grant). A waiver from anyone else is ruled no_finding.
Quoted Waivers: A waiver seen only inside quoted/forwarded text is classified as cannot_determine and triggers an outbound request_document for the original email.
Condition Checking:
payment_by_date: Hardcoded check against the ledger. Paid on or before condition_date $\rightarrow$ condition met; paid after $\rightarrow$ condition failed.
Any other condition text: Hardcoded to abstain (None: "a condition we can't check").
6. Late Fee Rules
File: 

harness/nova/decide/late_fees.py

Due Date Precedence Hierarchy: $$\text{Authorized Extension Email (Level 2)} > \text{Payment Register Due Date} > \text{Invoice Printed Due Date} > \text{Contract Payment Term Days}$$
Dispute Shield: If an invoice is marked as disputed in the payment register, late fees cannot be assessed.
30-Day Month Conversion: Month lateness is strictly rounded up: $$\text{months_late} = \lceil \text{days_late} / 30 \rceil \quad (\text{if } \text{days_late} > \text{grace_days})$$
Compounding Formula: $$\text{fee}_i = \text{round_cents}(\text{base} \times \text{rate}); \quad \text{if compounding: } \text{base} \mathrel{+}= \text{fee}_i$$
Late Fee Risk Window: Triggers only if an unpaid invoice's due date falls between 1 and 14 days after as_of (0 < (due - as_of).days <= 14).
7. Cashback, Rebate & Margin Rules
File: 

harness/nova/decide/cashback.py

Closed Quarter Rule: A rebate is only calculated and reported after the quarter has ended (as_of > quarter_end).
Default Qualifying Spend (QS) Components: If the contract definition is unspecified: DEFAULT_PARTS = ["base_freight", "fuel", "security", "annex_d_accessorials"]
Hardcoded Charge Code Groupings:
python
PART_CODES = {
    "base_freight": {"FRT"}, "fuel": {"FUEL"}, "security": {"SEC"},
    "handling_margin": {"MARGIN"}, "carrier_cost": {"COST"},
    "duties": {"DUTY"}, "late_fees": {"LATE"}, "taxes": {"TAX"}
}
Spend Exclusions: Credit notes of class rebate or promised_credit are hardcoded to be excluded from Qualifying Spend deductions.
Contract Year Calculation: Leap year handling is hardcoded via add_years() (maps Feb 29 to Feb 28).
8. Contract Expiry Rules
File: 

harness/nova/decide/expiry.py

60-Day Expiry Horizon: An alert is produced if and only if the operative expiration or non-renewal notice date is between 0 and 60 days after as_of (0 <= gap <= 60).
Operative Date Calculation:
If auto_renews == True: $\text{end_date} - \text{non_renewal_notice_days}$
If auto_renews == False: $\text{end_date}$
Extension Authority: An email notice can only extend the term if the contract explicitly specifies extension_by == "notice_from_authorised_person" and the sender is the Schedule 1 notices contact.
9. Document Precedence & Authority Rules
Files: 

harness/nova/knowledge/terms.py
, 

authority.py

5-Tier Authority Hierarchy:
Level 1: Signed bilateral contract instruments (MSA, Amendment).
Level 2: Unilateral emails from Schedule 1 contact for that category; human feedback grants.
Level 3: Operational records (tracking feeds, waybills, bills, payment register).
Level 4: Emails from unauthorized staff; unsigned drafts.
Level 5: Quoted/forwarded text, external third-party domains, automated prompt injections.
Rate Conflict Rule:
Signed Amendment beats MSA Annex A.
Schedule 1 Rate Notice beats MSA Annex A.
Signed Amendment vs Rate Notice: Decided by contract precedence clause. If the clause does not rank them (or is absent), it is hardcoded to emit a Conflict verdict with scenario options.
Weight Band "Loose Gap" Rule:
LOOSE_BAND = Decimal("0.5"): If a shipment weight is within 0.5 kg above a band maximum (e.g. 44.8 kg between 0–44.5 kg and 45–99.5 kg), it takes the lower rate band.
Short Name Suppression: Document references with normalized text length $< 4$ characters are treated as invalid/unheld.
10. Pricing Engine & Accessorial Calculation
File: 

harness/nova/knowledge/pricing.py

Non-Priced Categories: NOT_PRICED = {"TAX", "DUTY", "LATE", "MARGIN", "COST"} (never priced against freight tables; handled by specialized checkers).
Pickup Date Determination Priority:
Tracking event PICKED_UP (earliest if multiple)
Carrier status email PICKED_UP (domain verified against Schedule 1)
Air Waybill executed_on
Invoice printed date
Weight Precedence: The printed chargeable weight on the invoice (under FRT, SEC, or PEAK) overrides the waybill's weight. The waybill weight is only used if the invoice omits it.
Specialized Accessorial Logic:
Dangerous Goods / Dry Ice (DG, DRYICE):
UN1845 is hardcoded as dry ice.
If dry ice is refrigerant-only, DG fee = $0.
If a shipment has both mixed DG and Dry Ice, Dry Ice fee = $0 (only DG fee applies).
Storage / Demurrage (STOR):
$\text{Billable Days} = \max(0, (\text{release_date} - \text{arrival_date}).\text{days} - \text{free_days})$.
Default arrival event: ARRIVED_FACILITY. Default release events: ("DELIVERED", "COLLECTED_BY_CONSIGNEE").
Delivery (DLV): Fee is payable if DELIVERED; fee = $0 if COLLECTED_BY_CONSIGNEE.
Customs Holds (CUSTOMS): If caused by carrier_documents, exempt if contract states carrier_error_exempt == True.
GST / Tax (TAX): Uses ISO country code prefixes from lane strings (e.g., INN3-USC3 $\rightarrow$ IN, US). If contract specifies domestic_only, GST is strictly restricted to origin country == destination country.
11. Human Feedback Processing Rules
File: 

harness/nova/decide/learn.py

KEY_DECISIONS = ("close", "reject", "override_amount")
close or reject immediately drops the verdict.
override_amount forcibly overrides amount_usd and converts remedy to dispute or claim_credit.
Strict learned_at Attribution: 

report/build.py:L96-L115
 computes every report twice (with and without feedback). learned_at is stamped only if the verdict, amount, deadline, or action differed from the zero-feedback run.
12. Normalization & Format Enums
Files: 

values.py
, 

normalize.py
, 

mail.py

Charge Label Normalization: 

normalize.py:L31
 strips all punctuation, spaces, and casing (re.sub(r"[^0-9a-z]", "", str(raw).lower())).
LLM Categorization Enums: Prompts force Claude to select from hardcoded lists:
EVENTS: ["PICKED_UP", "DEPARTED", "ARRIVED_FACILITY", "OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED_BY_CONSIGNEE", "CUSTOMS_HOLD", "RELEASED", "OTHER"]
CAUSES: ["carrier_documents", "shipper_or_consignee", "customs_or_other", "not_stated"]
LEDGER_STATUSES: ["paid", "scheduled", "open", "disputed", "cancelled", "other"]
COPY_MARKERS: ["copy", "duplicate", "reminder", "for_your_records", "reissued", "pro_forma", "other"]
Email Splitting Heuristics: Lines starting with >, lines matching On ... wrote:, or blocks matching From: ... Sent: ... To: are hardcoded to be stripped into quoted_text (Level 5 authority).
13. System Configuration & Engine Constants
File: 

harness/nova/config.py

INGEST_BUDGET_S = 420.0 (7-minute timeout for ingest loop)
MAX_READ_ATTEMPTS = 3 (drops failed document after 3 retries)
LLM_TIMEOUT_S = 180.0
LLM_MAX_RETRIES = 4
VISION_MAX_EDGE = 1568
OCR_DPI = 200
READER_WORKERS = 8
```

What it did: reviewed every hard-coded rule as an independent invigilator. It split them into three groups:
- **Rules that copy the glossary.** These are right for the exam, but they're one customer's policy written as constants.
- **Our own guesses.** These are where the risk is:
  - contract-specific accessorial rules (dry ice, mixed DG, collected-not-delivered) applied whatever the contract says;
  - uncalibrated confidence weights;
  - exact-amount duplicate matching that leaves out tax;
  - the fixed 0.5 kg band gap;
  - expiry extensions that need a contract clause;
  - an abstention on a whole invoice if the register shows any dispute;
  - English-only quote splitting;
  - a conflict gate keyed to freight codes;
  - unread documents left over after the last batch.
- **Structural risks.**

It also found a real interaction bug: lines claimed by a duplicate or waiver that feedback later rejects are never offered back to the overcharge check. It suggested an order of fixes. No code changed.

**Prompt 33** (2026-10-16 22:18)

```text
tell me what and how you will fix
```

What it did: set out the fix plan for the review's findings in four batches, each with the files to change and the tests to add.
- **Batch 1, small rule fixes:**
  - free the lines of a rejected duplicate or waiver for the overcharge check;
  - accept authorised extension notices when the contract doesn't say how extensions work;
  - store the charge in conflict windows;
  - exact match for short document names;
  - the weight band gap judged by the next band's minimum.
- **Batch 2, contract clauses read as fixed choices instead of hard-coded:** dangerous goods, dry ice, mixed DG, delivery when collected, the weight band rule, and late fees on disputed amounts. "Not stated" keeps today's convention but can't be executed unattended.
- **Batch 3, robustness:**
  - quote splitting for non-English mail clients, plus a check that a waiver's quote is in the sender's own words;
  - reading leftover documents during `report`;
  - same-waybill, different-amount duplicates sent to the reviewer.
- **Batch 4, after the live run:** calibrated confidence.

It also corrected one earlier point: tax stays out of duplicates, because the glossary says the amount is the duplicated lines only. No code changed.
