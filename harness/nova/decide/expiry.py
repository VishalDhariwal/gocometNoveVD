"""Expiry: a contract whose operative date is 0 to 60 days after as_of (GLOSSARY "Expiry").

The operative date is the end date, or, when the contract renews itself, the last day to give notice of
non-renewal (end date minus the notice days). An extension we hold moves the end date: a signed instrument always;
an email only from the Schedule 1 notices person, and only when the contract lets notices extend the term. Outside
the window the contract must be absent from the report, so only findings are returned.
"""
from __future__ import annotations

from datetime import timedelta

from ..knowledge.terms import Decided, Source, Terms
from ..knowledge.view import day
from .common import Verdict, latest


def detect(terms: Terms) -> list[Verdict]:
    view, as_of = terms.view, terms.view.as_of_date
    out = []
    if as_of is None:
        return out
    for contract in view.contracts():
        c = latest(f for f in view.get("contract", contract_id=contract)
                   if terms.level(f).level == 1 and day(f.payload.get("end_date")))
        if c is None:
            continue
        end, sources = day(c.payload["end_date"]), [Source(c, 1, "the term")]
        rule = terms.rule(contract, "term")
        r = rule.value if isinstance(rule, Decided) else {}
        if isinstance(rule, Decided):
            sources.append(rule.sources[0])
        notices_extend = r.get("extension_by") == "notice_from_authorised_person"
        ext = []
        for f in view.get("contract_notice", contract_id=contract):
            new_end = day(f.payload.get("new_end_date"))
            if f.payload.get("notice_type") not in ("extension", "renewal") or new_end is None:
                continue
            if terms.level(f).level == 1:
                ext.append((f, new_end, 1))
            elif notices_extend and terms.auth.of_email_fact(f, contract, "notices").level <= 2:
                ext.append((f, new_end, 2))
        if ext:
            f, end, level = max(ext, key=lambda x: (x[0].known_at, x[0].id))
            sources.append(Source(f, level, "extends the term"))
        auto = r.get("auto_renews") if r.get("auto_renews") is not None else c.payload.get("auto_renews")
        if auto:
            days = r.get("non_renewal_notice_days")
            if not isinstance(days, int):
                continue   # renews itself, but we can't tell the notice deadline
            operative, what = end - timedelta(days=days), f"non-renewal notice deadline ({days} days before {end})"
        else:
            operative, what = end, "end of the term"
        gap = (operative - as_of).days
        if not 0 <= gap <= 60:
            continue
        v = Verdict("expiry", {"contract_id": contract}, "finding", deadline=operative.isoformat(), remedy="alert",
                    contract=contract, why=f"{what} is {gap} days after {as_of}")
        v.steps.append(f"{what}: {operative}")
        v.cite(*sources)
        out.append(v)
    return out
