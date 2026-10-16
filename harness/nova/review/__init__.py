"""The case reviewer: a second Claude agent that decides the cases code can't settle, during `report`.

Code reads every document into facts and decides by the glossary's rules (decide/). A case goes to the reviewer only
when code is stuck or about to act on something it couldn't verify (triage.py): a waiver whose condition is in words,
a waiver with no scope code could read, a waiver seen only quoted, labels nobody mapped, lines code couldn't price,
documents nothing could be read out of that name our invoices, references a typo away from ours, and in autonomous
mode a finding about to be executed on an unverified read. Everything else never reaches it.

    triage (code)    picks those cases, risky executes first, then money; at most config.REVIEW_MAX_CASES
    agent (Claude)   decides each one (agent.py). Context comes in three layers: a fixed system prompt with the rules
                     and the criteria for bringing in a person, a case brief built from the store, and tools to pull
                     the rest (tools.py): documents, contract terms, tracking, the register, and code's own pricing
                     engine, a what-if repricer, late-fee and exact arithmetic tools. Every amount is a calculation
                     with an id; the decision names it.
    check (code)     five seat belts and nothing more: quotes on their pages, nothing resting on planted text, the
                     amount is the named calculation, a line claimed once, a code finding never dropped
    gate + route     the glossary's action gate as for every item; the reviewer's "send to a person" holds an
                     autonomous finding (A1), inside an escalation budget; the human review queue (route.py)

If the API isn't there or the time runs out, the case keeps the verdict code gave it. Every case is written to the
`reviews` table with the decision and what code did with it.
"""
from .run import review

__all__ = ["review"]
