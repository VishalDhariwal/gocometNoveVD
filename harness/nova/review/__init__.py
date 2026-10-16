"""The case reviewer: a Claude agent that takes a second look at the cases code finds hard, during `report`.

Code reads every document into facts and decides by the glossary's rules (decide/). A few cases need judgement across
several documents that those rules can't give, and there code either stays silent or acts with nobody looking:

    waiver_condition  a waiver whose condition is in words ("provided you confirm the Q3 forecast")
    waiver_scope      a waiver from an authorised person where the reader found no waybill, invoice or month
    quoted_waiver     a waiver seen only quoted, when the original may be held under other wording
    unmapped_labels   billed lines whose label nobody could map to an Annex E code (so no detector saw them)
    unread_email      an email no reader understood that names one of our invoices or its waybills
    second_look       autonomous mode: a dispute or claim about to be executed with no person looking

The flow, one case at a time (cases run in parallel):

    triage (code)    picks the cases, money first, at most config.REVIEW_MAX_CASES
    agent (Claude)   reads the original documents with read-only tools and answers in a fixed shape, with quotes
    check (code)     every quote must be on its page; every source must have the authority the glossary asks for;
                     nothing may rest on quoted text, outsiders or text aimed at automated readers; the money is
                     worked out by code from the billed lines; only two upgrades exist (waived_but_billed and
                     duplicate), each with its own mechanical test
    gate             anything that rests on the reviewer is never executed: draft in supervised, human_review in
                     autonomous. A reviewer's cited disagreement holds an autonomous execute for a person.

Open in what it reads, closed in what it can do. If the API isn't there, the time runs out or an answer doesn't hold
up, the case keeps the verdict code gave it. Every case is written to the `reviews` table with what code did and why.
"""
from .run import review

__all__ = ["review"]
