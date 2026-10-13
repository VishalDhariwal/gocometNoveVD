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

