"""Waived but billed (GLOSSARY "Waivers").

A waiver counts when the Schedule 1 waivers person for that contract (or someone a feedback decision confirmed for
waivers) writes it in their own email, it names the shipment, the invoice or the month, the billed charge is one it
names (matched by Annex E code, not wording), and any condition it sets was met:
- a "paid by a date" condition is checked against the payment register;
- any other condition can't be checked, so it isn't a finding (logged).
A waiver from anyone else: no_finding. A waiver seen only quoted inside someone else's email: cannot_determine, and
we ask for the original from its sender if they're on Schedule 1 for waivers, else from the carrier's Key Account
Manager. Amount: the whole billed lines the waiver covers.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .. import normalize as norm
from ..knowledge.authority import Authorities, Authority
from ..knowledge.terms import Source, Terms
from ..knowledge.view import day
from .common import ZERO, Claims, Verdict, amount_of, contract_of, invoice_date, ledger, lines_of, to_usd


def _month(line, header) -> str | None:
    p = line.payload
    period = str(p.get("period") or "")
    if len(period) == 7 and period[4] == "-":
        return period
    d = day(p.get("pickup_date")) or invoice_date(header)
    return norm.month(d) if d else None


def detect(terms: Terms, claims: Claims, headers: dict) -> list[Verdict]:
    view = terms.view
    billed = []
    for h in headers.values():
        contract = contract_of(terms, h)
        for f in lines_of(view, h):
            code = terms.charge_code(contract, f.payload.get("label"))
            if code is not None and not claims.taken(f.id):
                billed.append((h, f, code, contract))
    authored = [w for w in view.kind("waiver") if not w.payload.get("from_quoted_text")]
    hits: dict[str, list] = defaultdict(list)
    for w in view.kind("waiver"):
        p = w.payload
        awbs, invs, months = set(p.get("awbs") or []), set(p.get("invoices") or []), set(p.get("months") or [])
        if not (awbs or invs or months):
            continue
        for h, f, code, contract in billed:
            if not (f.awb in awbs or h.invoice_id in invs or _month(f, h) in months):
                continue
            if code not in {terms.charge_code(contract, c) for c in p.get("charges") or []}:
                continue
            if p.get("from_quoted_text") and any(_same_waiver(a, w) for a in authored):
                continue   # we hold the original: it decides on its own
            hits[h.invoice_id].append((w, f, contract, None))
    for inv_id in headers:   # a person approved a waiver for named months on this one key (and no other)
        for it in terms.feedback.for_key(f"waived_but_billed|{inv_id}"):
            fact = it.get("fact") or {}
            if it.get("decision") != "one_off_approval" or fact.get("type") != "waiver_scope":
                continue
            person = (fact.get("subject") or {}).get("person")
            months = set((fact.get("value") or {}).get("months") or [])
            theirs = [w for w in view.kind("waiver") if _from_person(terms, w, person)]
            for h, f, code, contract in billed:
                if h.invoice_id != inv_id or _month(f, h) not in months:
                    continue
                w = next((w for w in theirs if code in {terms.charge_code(contract, c)
                                                        for c in w.payload.get("charges") or []}), None)
                if w is not None:
                    hits[inv_id].append((w, f, contract, it["item_id"]))
    out = []
    for inv_id, found in sorted(hits.items()):
        v = _judge(terms, headers[inv_id], found)
        if v.verdict == "finding":
            claims.claim("waived_but_billed", v.lines)
        out.append(v)
    return out


def _from_person(terms: Terms, w, person: str | None) -> bool:
    if not person:
        return False
    names = [terms.view.sender(w)[1], w.payload.get("original_sender")]
    names += [p.payload.get("name") for p in terms.view.get("schedule1_person", person_email=w.person_email)]
    return any(Authorities.name_matches(person, n) for n in names)


def _same_waiver(a, quoted) -> bool:
    return (a.person_email == quoted.person_email and set(a.payload.get("awbs") or []) >= set(quoted.payload.get("awbs") or [])
            and set(a.payload.get("charge_keys") or []) >= set(quoted.payload.get("charge_keys") or []))


def _condition(view, w, line_invoice: str) -> tuple[bool | None, str]:
    p = w.payload
    kind = p.get("condition_kind") or ("other" if p.get("condition") else "none")
    if kind == "none":
        return True, "unconditional"
    if kind == "payment_by_date":
        by = day(p.get("condition_date"))
        target = p.get("condition_invoice") or (p.get("invoices") or [line_invoice])[0]
        row = ledger(view, target)
        paid = day(row.payload.get("paid_date")) if row and row.payload.get("status") == "paid" else None
        if by is None:
            return None, "a payment condition without a readable date"
        if paid is not None and paid <= by:
            return True, f"condition met: {target} paid {paid}, by {by}"
        return False, f"condition not met: {target} not paid by {by}"
    return None, f"a condition we can't check: {p.get('condition')!r}"


def _judge(terms: Terms, header, found: list) -> Verdict:
    view = terms.view
    contract = found[0][2]
    v = Verdict("waived_but_billed", {"invoice_id": header.invoice_id}, "no_finding", contract=contract,
                currency=header.payload.get("currency"))
    waived, quoted, reasons = {}, [], []
    for w, f, _, approved in found:
        if approved:
            waived.setdefault(f.id, (w, f, Authority(2, f"feedback {approved} approves this waiver for this invoice",
                                                     feedback_item=approved)))
    for w, f, _, approved in found:
        if approved or f.id in waived:
            continue
        if w.payload.get("from_quoted_text"):
            quoted.append((w, f))
            continue
        auth = terms.auth.of_email_fact(w, contract, "waivers", day(w.known_at))
        if auth.level > 2:
            reasons.append(f"waiver in {w.doc_ref} doesn't count: {auth.why}")
            continue
        met, why = _condition(view, w, header.invoice_id)
        if not met:
            reasons.append(f"waiver in {w.doc_ref}: {why}")
            continue
        waived.setdefault(f.id, (w, f, auth))
    v.steps += reasons
    if quoted and not waived:
        w, f = quoted[0]
        holder = _original_holder(terms, contract, w)
        name = f"original email from {w.payload.get('original_sender') or w.person_email}"
        v.verdict, v.remedy, v.why = "cannot_determine", "request_document", "the waiver is only seen quoted"
        v.missing_doc = {"name": name, "holder": {k: holder[k] for k in ("party", "person", "role") if holder.get(k)}}
        v.cite(Source(w, 5, "quoted waiver"), Source(f, 3, "the billed line"))
        v.checks.update(missing_holder=holder)
        return v
    if not waived:
        v.why = reasons[0] if reasons else "no waiver covers these lines"
        return v
    native = sum((amount_of(f) for _, f, _ in waived.values()), ZERO)
    usd, fx = to_usd(terms, contract, v.currency, native, invoice_date(header))
    if usd is None:
        v.why = "the amount can't be converted to USD"
        return v
    v.verdict, v.remedy, v.why = "finding", "dispute", "billed a charge the carrier waived"
    v.amount_native, v.amount_usd = native, usd
    v.lines = sorted(waived)
    for w, f, auth in waived.values():
        who = terms.view.sender(w)[1] or w.person_email
        v.steps.append(f"{f.payload.get('label')} on {f.awb or f.payload.get('period') or 'the invoice'}: "
                       f"{amount_of(f)} billed, waived by {who} on {str(w.known_at)[:10]} ({auth.why})")
        v.cite(Source(w, auth.level, "the waiver"), auth.person and Source(auth.person, 1, "Schedule 1"),
               Source(f, 3, "the billed line"))
        v.learn(auth.feedback_item)
    v.checks.update(term_levels=sorted({a.level for _, _, a in waived.values()}),
                    fx=fx.sources[0] if fx is not None else None)
    return v


def _original_holder(terms: Terms, contract: str, w) -> dict:
    """Who to ask for an original we only saw quoted: its sender if they're on Schedule 1 for waivers, else the
    carrier's Key Account Manager (its Schedule 1 notices contact)."""
    person = terms.auth.schedule1(contract, w.person_email, "waivers")
    if person is not None:
        return {"party": person.party, "person": person.payload.get("name"), "role": person.payload.get("title"),
                "email": person.person_email}
    return terms.holder(contract, "notices")
