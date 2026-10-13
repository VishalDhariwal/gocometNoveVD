"""Which term applies to a lane, a month or a charge on a given date, and on whose authority.

Every question gets one of four answers:
  Decided    one value, with the documents behind it
  Conflict   two sources with authority disagree and the contract doesn't rank them (each option is a Decided)
  Missing    the answer depends on a document a held document names and we don't hold; says who holds it
  Undefined  nothing we hold says; the line isn't judged

Rules (GLOSSARY "Which document wins"):
- Signed amendment > MSA; between amendments, the later effective date wins for what it changes.
- A rate notice counts only from the Schedule 1 rates person (or a feedback grant); it beats the MSA's Annex A.
- Notice against amendment: the contract's precedence clause decides; when it doesn't rank them (the usual case,
  and our default when no clause says otherwise) it's a Conflict.
- Feedback: rate_accepted re-admits the named notice for that lane inside its dates; resolve_conflict picks a side;
  a signed amendment received after the decision still beats either.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Union

from .. import normalize as norm
from ..store import Fact
from .authority import Authorities, Authority
from .feedback import Feedback
from .view import View, dec, day

LOOSE_BAND = Decimal("0.5")
RANKING = {"unranked": "unranked", "amendment_prevails": "amendment", "notice_prevails": "notice",
           "later_prevails": "later"}   # a printed weight between two bands (44.8 between 0-44.5 and 45-99.5) takes the lower


# ---- answers --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    fact: Fact
    level: int
    why: str
    feedback: str | None = None     # the feedback item that gave this source its authority, if one did

    def cite(self) -> dict:
        return {"doc": self.fact.doc_ref, "page": self.fact.page or 1, "quote": self.fact.quote}


@dataclass
class Decided:
    value: Any
    sources: list[Source]
    label: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> list[str]:
        return sorted({s.feedback for s in self.sources if s.feedback})


@dataclass
class Conflict:
    options: list[Decided]
    why: str
    sources: list[Source] = field(default_factory=list)   # the clause that leaves them unranked, if one does


@dataclass
class Missing:
    name: str                 # as cited, without "(attached)"
    holder: dict              # {party, person, role, email}
    why: str
    sources: list[Source] = field(default_factory=list)


@dataclass
class Undefined:
    why: str
    notes: list[str] = field(default_factory=list)


Result = Union[Decided, Conflict, Missing, Undefined]


@dataclass(frozen=True)
class Precedence:
    rule: str                  # unranked | amendment | notice | later
    source: Source | None
    from_contract: bool


# ---- helpers --------------------------------------------------------------------------------------------------
def covers(fact: Fact, on: date) -> bool:
    start, end = day(fact.valid_from), day(fact.valid_to)
    return (start is None or start <= on) and (end is None or on <= end)


def _band_match(fact: Fact, weight: Decimal | None) -> int:
    """2 = the weight is inside the band, 1 = just above it (in the gap before the next band), 0 = no."""
    if weight is None:
        return 0
    lo, hi = dec(fact.payload.get("band_min")), dec(fact.payload.get("band_max"))
    if lo is None or weight < lo:
        return 0
    if hi is None or weight <= hi:
        return 2
    return 1 if weight < hi + LOOSE_BAND else 0


def _best_band(facts: list[Fact], weight: Decimal | None) -> list[Fact]:
    scored = [(f, _band_match(f, weight)) for f in facts]
    best = max((s for _, s in scored), default=0)
    return [f for f, s in scored if s == best and s > 0]


def cited_name(name: str | None) -> str:
    """'Annex A rev 2 (attached)' -> 'Annex A rev 2'."""
    return re.sub(r"\s*\((?:attached|enclosed|herewith|annexed)[^)]*\)\s*$", "", str(name or ""), flags=re.I).strip()


def _newest(sources: list[Source]) -> Source:
    return max(sources, key=lambda s: (s.fact.valid_from or "", s.fact.known_at, s.fact.id))


def _describe(src: Source) -> str:
    p = src.fact.payload
    inst = p.get("instrument")
    if inst == "rate_notice":
        return f"rate notice {p.get('instrument_ref') or ''} ({src.fact.doc_ref})".replace("  ", " ")
    return f"{inst} {p.get('instrument_ref') or ''}".strip()


# ---- the engine -----------------------------------------------------------------------------------------------
class Terms:
    def __init__(self, view: View):
        self.view = view
        self.feedback = Feedback(view)
        self.auth = Authorities(view, self.feedback)
        self._levels: dict[int, Authority] = {}

    def level(self, fact: Fact) -> Authority:
        """Authority of a fact from a contract document (cached per fact)."""
        if fact.id not in self._levels:
            self._levels[fact.id] = self.auth.of_contract_doc(fact)
        return self._levels[fact.id]

    def _formal(self, facts: list[Fact], on: date | None, max_level: int = 1) -> list[Source]:
        out = []
        for f in facts:
            if on is not None and not covers(f, on):
                continue
            a = self.level(f)
            if a.level <= max_level:
                out.append(Source(f, a.level, a.why))
        return out

    # ---- precedence -------------------------------------------------------------------------------------------
    def precedence(self, contract: str) -> Precedence:
        """How a rate notice ranks against an amendment under this contract, as the contract reader answered it
        (rules.notice_vs_amendment). No answer: unranked, the glossary's usual case, marked as not from the contract."""
        rule = self.rule(contract, "notice_vs_amendment")
        if isinstance(rule, Decided):
            rule_name = RANKING.get(str(rule.value.get("ranking")))
            if rule_name:
                return Precedence(rule_name, rule.sources[0], True)
        return Precedence("unranked", None, False)

    # ---- rates ------------------------------------------------------------------------------------------------
    def rate(self, contract: str, lane: str, on: date, *, weight: Decimal | None = None,
             kind: str = "per_kg") -> Result:
        """The rate for this lane and weight on `on` (kind='minimum' for the per-shipment minimum)."""
        rows = [f for f in self.view.get("rate", contract_id=contract, lane=lane) if covers(f, on)]
        rows = [f for f in rows if f.payload.get("kind") == kind]
        ignored: list[str] = []
        base, over, notices = [], [], []
        groups: dict[str, list[Fact]] = {"base": [], "over": [], "notice": []}
        for f in rows:
            inst = f.payload.get("instrument")
            if inst == "rate_notice":
                groups["notice"].append(f)
            elif inst == "msa":
                groups["base"].append(f)
            else:
                groups["over"].append(f)
        if kind == "per_kg":
            groups = {k: _best_band(v, weight) for k, v in groups.items()}

        for f in groups["base"] + groups["over"]:
            a = self.level(f)
            if a.level == 1:
                (base if f.payload.get("instrument") == "msa" else over).append(Source(f, 1, a.why))
            elif a.level == 2:   # an annex update sent by the rates person: as good as a notice
                notices.append(Source(f, 2, a.why))
            else:
                ignored.append(f"{_describe(Source(f, a.level, a.why))}: {a.why}")
        for f in groups["notice"]:
            a = self.auth.of_email_fact(f, contract, "rates", on)
            accepted = self._accepted(contract, lane, on, f)
            if accepted is not None:
                notices.append(Source(f, 2, f"feedback {accepted['item_id']} accepts this notice", accepted["item_id"]))
            elif a.level <= 2:
                notices.append(Source(f, a.level, a.why, a.feedback_item))
            else:
                ignored.append(f"rate notice in {f.doc_ref} ignored: {a.why}")

        missing = self._missing_rates_doc(contract, lane, on, over)
        if missing is not None:
            return missing

        def decided(src: Source, label: str, *notes: str) -> Decided:
            return Decided(dec(src.fact.payload.get("rate")), [src], label, [*notes, *ignored])

        formal = _newest(over) if over else (_newest(base) if base else None)
        notice = _newest(notices) if notices else None
        if notice is None and formal is None:
            return Undefined(f"no {kind.replace('_', ' ')} rate for {lane} on {on}" +
                             (f" in band {weight} kg" if weight is not None else ""), ignored)
        if notice is None:
            return decided(formal, _describe(formal))
        if formal is None or not over:      # a notice from the rates person replaces the MSA's Annex A rate
            return decided(notice, _describe(notice))

        # an amendment and a notice are both in force on the pickup date
        a_val, n_val = dec(formal.fact.payload.get("rate")), dec(notice.fact.payload.get("rate"))
        if a_val == n_val:
            return decided(formal, _describe(formal), f"the rate notice in {notice.fact.doc_ref} says the same")
        later_signed = lambda decided_at: formal.fact.known_at > decided_at  # noqa: E731
        if notice.feedback:                   # rate_accepted: the named notice is the rate inside its dates
            item = next(it for it in self.view.feedback if it["item_id"] == notice.feedback)
            if not later_signed(item["decided_at"]):
                return decided(notice, _describe(notice), f"feedback {notice.feedback} accepts this notice")
        choice = self.feedback.precedence(contract, lane, on)
        if choice is not None and not later_signed(choice["decided_at"]):
            use = str((choice.get("fact") or {}).get("value", {}).get("use") or "").lower()
            fb = choice["item_id"]
            pick = formal if "amendment" in use else notice if "notice" in use else None
            if pick is not None:
                src = Source(pick.fact, pick.level, f"{pick.why}; feedback {fb} says to use the {use}", fb)
                return decided(src, _describe(pick), f"feedback {fb} resolves the conflict")
        prec = self.precedence(contract)
        if prec.rule == "amendment":
            return decided(formal, _describe(formal), "the contract ranks amendments above rate notices")
        if prec.rule == "notice":
            return decided(notice, _describe(notice), "the contract ranks rate notices above amendments")
        if prec.rule == "later":
            later = _newest([formal, notice])
            return decided(later, _describe(later), "the contract says the later instrument prevails")
        why = ("the contract doesn't rank a rate notice against an amendment" if prec.from_contract else
               "no clause ranks a rate notice against an amendment")
        return Conflict([decided(formal, _describe(formal)), decided(notice, _describe(notice))], why,
                        [prec.source] if prec.source else [])

    def _accepted(self, contract: str, lane: str, on: date, fact: Fact) -> dict | None:
        """A rate_accepted feedback item naming this notice, for this contract and lane, in force on `on`."""
        _, name = self.view.sender(fact)
        for it in self.feedback.accepted_rates(contract, lane, on):
            val = (it.get("fact") or {}).get("value") or {}
            by, eff = val.get("sent_by"), val.get("effective_from")
            if by and any(Authorities.name_matches(by, n) for n in (name, fact.payload.get("original_sender"))):
                return it
            if eff and fact.valid_from == str(eff)[:10]:
                return it
            if not by and not eff:
                return it
        return None

    # ---- documents we don't hold ------------------------------------------------------------------------------
    def held(self, name: str | None) -> bool:
        """Do we hold a document by this name? Matched on instrument references and titles, file names, subjects."""
        key = norm.label(cited_name(name))
        if len(key) < 4:
            return False
        for f in self.view.kind("instrument"):
            if key in norm.label(f.payload.get("reference") or "") or key in norm.label(f.payload.get("title") or ""):
                return True
        return any(key in norm.label(d.filename or "") for d in self.view.docs.values())

    def _missing_rates_doc(self, contract: str, lane: str, on: date, over: list[Source]) -> Missing | None:
        for ref in self.view.get("doc_reference", contract_id=contract):
            p = ref.payload
            name = cited_name(p.get("name_as_cited"))
            text = f"{name} {p.get('governs') or ''}".lower()
            if p.get("category") != "rates" and "rate" not in text and "annex a" not in text:
                continue
            lanes = p.get("lanes") or []
            if norm.lane(lane) not in lanes and norm.lane(lane).lower() not in text:
                continue
            eff = day(p.get("effective_from"))
            if (eff and on < eff) or self.held(name) or self.level(ref).level != 1:
                continue
            if any(s.fact.doc_id != ref.doc_id and eff and (day(s.fact.valid_from) or date.min) >= eff for s in over):
                continue    # a later signed amendment sets this lane's rate itself
            return Missing(name, self.holder(contract, "rates", p.get("issued_by")),
                           f"{p.get('cited_in') or ref.doc_ref} says {name} governs {lane} from {eff or 'its date'}, "
                           f"and we don't hold it", [Source(ref, 1, self.level(ref).why)])
        return None

    def holder(self, contract: str, category: str, issued_by: str | None = None) -> dict:
        """Who holds a document of this category: the issuing party's Schedule 1 contact for it."""
        carrier = self.auth.carrier_party(contract)
        parties = {p.party for p in self.view.get("schedule1_person", contract_id=contract)}
        shipper = next((p for p in parties if p != carrier), None)
        party = carrier
        who = (issued_by or "").lower()
        if shipper and ("shipper" in who or (shipper and shipper.lower() in who.replace(" ", ""))):
            party = shipper
        person = self.auth.contact(contract, party, category)
        if person is None:
            return {"party": party, "person": None, "role": None, "email": None}
        return {"party": party, "person": person.payload.get("name"), "role": person.payload.get("title"),
                "email": person.person_email}

    # ---- monthly tables ---------------------------------------------------------------------------------------
    def _monthly(self, kind: str, contract: str, month: str) -> list[Source]:
        return self._formal(self.view.get(kind, contract_id=contract, period=month), None, max_level=2)

    def fuel(self, contract: str, month: str) -> Result:
        found = self._monthly("fuel_rate", contract, month)
        if not found:
            return Undefined(f"{contract} has no fuel surcharge for {month}")
        src = max(found, key=lambda s: (-s.level, s.fact.known_at, s.fact.id))
        return Decided(dec(src.fact.payload.get("pct")), [src], f"Annex C {month}")

    def fx(self, contract: str, month: str) -> Result:
        """INR per 1 USD for the month of the invoice date. No row -> Missing, held by the treasury contact."""
        found = self._monthly("fx_rate", contract, month)
        if found:
            src = max(found, key=lambda s: (-s.level, s.fact.known_at, s.fact.id))
            return Decided(dec(src.fact.payload.get("rate")), [src], f"Annex F {month}")
        refs = [r for r in self.view.get("doc_reference", contract_id=contract)
                if r.payload.get("category") == "treasury" or "annex f" in str(r.payload.get("name_as_cited")).lower()]
        name = cited_name(refs[-1].payload.get("name_as_cited")) if refs else "Annex F"
        return Missing(name, self.holder(contract, "treasury"), f"{contract} has no exchange rate for {month}",
                       [Source(r, self.level(r).level, "names the exchange-rate document") for r in refs[-1:]])

    # ---- fees, codes, clauses ---------------------------------------------------------------------------------
    def accessorial(self, contract: str, code: str, on: date) -> Result:
        code = norm.code(code)
        rows = [f for f in self.view.get("accessorial", contract_id=contract)
                if norm.code(f.payload.get("code") or self.charge_code(contract, f.payload.get("name")) or "") == code]
        found = self._formal(rows, on)
        if not found:
            return Undefined(f"{contract} has no Annex D fee for {code} on {on}")
        src = _newest(found)
        return Decided(src.fact.payload, [src], f"Annex D {code}")

    def charge_code(self, contract: str | None, label: str | None) -> str | None:
        """Annex E: the code a billed label belongs to; else the reconciler's mapping; else None."""
        if not label:
            return None
        key = norm.label(label)
        for f in self.view.kind("charge_alias") if contract is None else self.view.get("charge_alias", contract_id=contract):
            if key in (f.payload.get("label_keys") or []) or key == norm.label(f.payload.get("code") or ""):
                return norm.code(f.payload.get("code"))
        link = self.view.links.get("label_code", {}).get(f"{contract}|{key}")
        if link is not None and link["object"] and float(link["confidence"]) >= 0.6:
            return norm.code(link["object"])
        return None

    def term(self, contract: str, name: str, on: date | None = None) -> Result:
        """A modelled clause (contract_term with this name) from the highest instrument in force on `on`."""
        rows = [f for f in self.view.get("contract_term", contract_id=contract) if f.payload.get("term") == name]
        found = self._formal(rows, on)
        if not found:
            return Undefined(f"{contract} has no {name} clause")
        src = _newest(found)
        return Decided(src.fact.payload.get("value") or {}, [src], f"clause {src.fact.payload.get('clause') or name}")

    def rule(self, contract: str, name: str, on: date | None = None) -> Result:
        """A clause the contract reader answered as fixed choices (contract_term 'rule:<name>'), from the highest
        instrument in force on `on` that answers it."""
        t = self.term(contract, f"rule:{name}", on)
        if isinstance(t, Decided) and not isinstance(t.value, dict):
            return Undefined(f"{contract}'s {name} rule isn't readable")
        return t

    def clause_source(self, contract: str, name: str, on: date | None = None) -> Source | None:
        t = self.term(contract, name, on)
        return t.sources[0] if isinstance(t, Decided) else None

    def storage_free_days(self, contract: str, on: date) -> tuple[int | None, Source | None]:
        rule = self.rule(contract, "storage", on)
        if not isinstance(rule, Decided):
            return None, None
        free = rule.value.get("free_days")
        return (free if isinstance(free, int) and not isinstance(free, bool) else None), rule.sources[0]

    def contract_currency(self, contract: str) -> str | None:
        for c in self.view.get("contract", contract_id=contract):
            if c.payload.get("currency"):
                return str(c.payload["currency"]).upper()
        return None
