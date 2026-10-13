"""Who is allowed to say what, as levels 1 (strongest) to 5 (none), following the glossary.

1  a contract instrument signed by both parties (MSA, amendment, annex update), with its annexes and schedules
2  an email in the sender's own words from the Schedule 1 person for that category on that contract; a feedback
   decision inside its scope (including a feedback grant of authority to a person)
3  records: tracking, carrier status emails, waybills, declarations, customs notices, the payment register, bills
4  anyone else's email from either company, unsigned drafts
5  quoted or forwarded text, text aimed at automated readers, anything from outside the two companies

Identity is the email address matched to Schedule 1, never a display name or a signature. A company's domains are
learned from its own people's addresses in Schedule 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..store import Fact
from .feedback import Feedback
from .view import View, day, domain

CATEGORY = {"rate": "rates", "waiver": "waivers", "due_date_extension": "payment_terms",
            "contract_notice": "notices", "fx_rate": "treasury"}


@dataclass(frozen=True)
class Authority:
    level: int
    why: str
    person: Fact | None = None
    feedback_item: str | None = None


def _name_key(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


class Authorities:
    def __init__(self, view: View, feedback: Feedback):
        self.view = view
        self.feedback = feedback
        self.party_domains: dict[str, set[str]] = {}
        for p in view.kind("schedule1_person"):
            self.party_domains.setdefault(p.party or "", set()).add(domain(p.person_email))
        self.known_domains = set().union(*self.party_domains.values()) if self.party_domains else set()

    # ---- Schedule 1 -------------------------------------------------------------------------------------------
    def schedule1(self, contract: str, email: str | None, category: str) -> Fact | None:
        if not email:
            return None
        for p in self.view.get("schedule1_person", contract_id=contract, person_email=email):
            if category in (p.payload.get("categories") or []):
                return p
        return None

    def contact(self, contract: str, party: str | None, category: str) -> Fact | None:
        """The Schedule 1 person of `party` for `category` on `contract` (the holder of a missing document, the
        recipient of a dispute)."""
        for p in self.view.get("schedule1_person", contract_id=contract):
            if category in (p.payload.get("categories") or []) and (party is None or p.party == party):
                return p
        return None

    def carrier_party(self, contract: str) -> str | None:
        """The carrier's party code: from the contract's parties if the reader found it, else the Schedule 1 party
        whose people share a domain with whoever sends this contract's bills."""
        for c in self.view.get("contract", contract_id=contract):
            for party in c.payload.get("parties") or []:
                if party.get("role") == "carrier" and party.get("code"):
                    return re.sub(r"\s", "", str(party["code"])).upper()
        billing_domains = {domain(self.view.sender(f)[0]) for f in self.view.get("invoice", contract_id=contract)}
        for p in self.view.get("schedule1_person", contract_id=contract):
            if domain(p.person_email) in billing_domains:
                return p.party
        return None

    # ---- levels -----------------------------------------------------------------------------------------------
    def of_contract_doc(self, fact: Fact) -> Authority:
        """Level of a fact that came from a contract document (MSA, amendment, annex update)."""
        inst = next(iter(self.view.from_same_doc(fact, "instrument")), None)
        if inst is None:
            return Authority(4, "no instrument found for this document")
        kind = inst.payload.get("type")
        if kind in ("msa", "amendment", "annex_update") and inst.payload.get("signed_by_both"):
            return Authority(1, f"signed {kind} {inst.payload.get('reference')}")
        if kind == "annex_update":
            email, _ = self.view.sender(inst)
            for category in ("rates", "treasury"):
                person = self.schedule1(inst.contract_id, email, category)
                if person is not None:
                    return Authority(2, f"annex update sent by {person.payload.get('name')} ({category})", person)
        return Authority(4, f"{kind} {inst.payload.get('reference')} isn't signed by both parties")

    def of_email_fact(self, fact: Fact, contract: str, category: str, on=None) -> Authority:
        """Level of something an email says (a rate notice, waiver, extension, notice) for `category` on
        `contract`."""
        p = fact.payload
        if p.get("from_quoted_text"):
            return Authority(5, "only seen in quoted or forwarded text")
        email = fact.person_email or p.get("sender_email")
        if not email:
            return Authority(4, "no sender address")
        if self.known_domains and domain(email) not in self.known_domains:
            return Authority(5, f"{email} is outside both companies")
        person = self.schedule1(contract, email, category)
        if person is not None:
            return Authority(2, f"{person.payload.get('name')} is on Schedule 1 for {category}", person)
        _, name = self.view.sender(fact)
        when = on or day(fact.known_at)
        grant = self.feedback.grant(contract=contract, person=name, category=category, on=when)
        if grant is not None:
            return Authority(2, f"feedback {grant['item_id']} confirms {name} for {category}",
                             feedback_item=grant["item_id"])
        return Authority(4, f"{email} isn't on Schedule 1 for {category} on {contract}")

    @staticmethod
    def name_matches(a: str | None, b: str | None) -> bool:
        return bool(a) and _name_key(a) == _name_key(b)
