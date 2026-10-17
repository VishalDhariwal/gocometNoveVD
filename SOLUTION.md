# My solution

## How to run it

`make setup`, then `make grade`, `make sheet` and `make eval` as the brief describes. The harness is
`harness/harness.py` (`ingest`, `report`, `feedback`) and needs nothing beyond `harness/requirements.txt`. It calls
Claude through the Anthropic SDK with `ANTHROPIC_API_KEY`; the models are set in `harness/nova/config.py` and can be
changed with `NOVA_READER_MODELS`, `NOVA_CONTRACT_MODELS`, `NOVA_CLASSIFY_MODELS`, `NOVA_VISION_MODELS` and
`NOVA_REVIEW_MODELS`.

I built and measured it with an OpenAI key (gpt-4o-mini) through a small local proxy that speaks the Anthropic
Messages API, so the harness runs unchanged:

```bash
export OPENAI_API_KEY=sk-...
bash tools/with_proxy.sh make grade        # or make sheet, make eval
BATCHES="batch_01 batch_02" MODE=both bash tools/live_run.sh   # timed replay: time, tokens, cost, score
PYTHONPATH=harness .venv/bin/python -m unittest discover -s harness/tests -t harness   # 247 unit tests
```

Each step keeps to the grader's 600 seconds by itself (`NOVA_STEP_LIMIT`, default 600 with a 60 s margin): `ingest`
stops starting new reads near the limit and `report` reads whatever is left before it reports, so nothing is pushed
to the next batch. `make clean` deletes `.state/`, which also holds the timed replays.

## What works

`make grade` (practice mailbox, both batches, both modes, gpt-4o-mini through the proxy) scores 99.7 / 100 with
every hard gate clear and the answer sheet check passing, 9 of 9 rows. Detection is 22 hits, 0 false, 0 missed;
every amount is exact (USD 14,140.09 recovered of 14,140.09, nothing wrongly disputed); adaptation (11/11),
learning from feedback (3/3), conflict and abstention (2/2), clean controls (16/16) and decoys (12/12) are at full
marks, and the action is right on 24 of 24 items in each mode. The conflict is escalated with both sources, the
missing Annex A rev 2 is a cannot_determine with its holder, and only 4% of items go to a person. Confidence has a
Brier score of 0.016; the only points lost are there. A timed replay of the same mailbox takes 3m46s and costs USD
0.14 for 163 documents.

Every number in a report comes from code, not from a model: models only read documents into quoted facts, and each
quote is checked on its page. The explanation and evidence on each item, the draft emails in `state/drafts/` and the
human review page in `state/human_queue/` show why each answer is what it is. The eval set is in `evals/cases/`: 12
cases of our own on mailboxes built by `evals/make_mailboxes.py`, each a worked example with one change (a sender
with no authority, another waybill, a planted instruction, a missing tracking export, feedback, an amendment
citing a missing annex, an amendment that must leave the rebate alone). All 14 cases pass, 12 of them ours
(`evals/results.json`).

## What doesn't

The quarterly rebate is not stable with gpt-4o-mini. The model reads the contract's rules in one long answer and,
from run to run, gets one of them wrong: an amendment read with rules it doesn't have ("rebate: not applicable",
quoting "All other terms of the Agreement are unchanged"), or the MSA's own rebate marked not applicable while its
quote is the rebate clause. Code now drops amendment rules whose quote doesn't speak of them and keeps only the
qualifying-spend charges the definition names. The `make grade` run got the rebate right, but it was missed in 3 of
the 5 timed replays before it, and a miss costs most of the amount score. The next fix is to check every yes/no rule against its own quote and ask again on a
contradiction.

The case reviewer (a second agent for the few cases code can't settle) rarely completes its decision protocol on
gpt-4o-mini; when it fails, code's verdict stands. The harness has only been run live with gpt-4o-mini through the
proxy, not with the Claude models it defaults to. Contract readings always come back "partial" because the coverage
check flags ordinary clause lines; this only affects status labels.

## The eval case that caught a bug

`evals/cases/amendment_missing_annex.yaml`. In practice run 3 the reading of amendment AMD-1 left out its document
reference ("Annex A rev 2 (attached) applies from 2035-06-04 to lane USS2-INN1"), so code priced invoice
TX-US-2035-0237 on the old Annex A and drafted, and in autonomous mode sent, a false USD 79.20 dispute instead of
asking Fintan Corrigan for the annex. The case rebuilds that trap on a mailbox of our own: a signed amendment that
replaces lane USE2-INE2's rates with an annex it calls attached and doesn't contain.

We fixed it in three layers: `doc_references` is now a required field of the contract reader; when an instrument
other than the MSA names a document "(attached)" or "(enclosed)" and no reference was recorded, the terms are read
once more with those lines named; and the case reviewer now checks any overcharge on a lane that a contract change
names without covering it. The practice replay has given cannot_determine on 0237 in every run since.
`amendment_keeps_rebate` and `qs_leaves_out_duties` pin the rebate bugs described above in the same way.
