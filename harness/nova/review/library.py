"""What the reviewer may read, and the read-only tools it reads it with.

Everything is loaded once, in the report's own thread, from documents received by the as_of date: page text, each
email's own words and quoted part, and the facts. The agents then run in worker threads against these in-memory
copies (an SQLite connection can't be shared across threads). Nothing here writes anything.

`check` and `level` are code's side of the bargain: a citation the reviewer gives only counts if its quote is on the
page it names, and it only counts for as much authority as its document has (GLOSSARY "Which document wins").
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass

from .. import normalize as norm
from ..decide.common import amount_of, contract_of, invoice_headers, lines_of
from ..knowledge.pricing import price_invoice
from ..knowledge.terms import Terms
from ..knowledge.view import DocInfo, domain
from ..store import Fact, Store

MIN_QUOTE = 12
PAGE_CHARS = 8000          # one page in a tool answer
ANSWER_CHARS = 14000       # one tool answer in all
RECORD_TYPES = {"invoice", "statement", "credit_note", "awb", "master_awb", "dg_declaration", "customs_notice"}
CONTRACT_TYPES = {"msa", "amendment", "annex_update"}
INJECTED = "text aimed at automated readers"
KEYS = ("contract_id", "invoice_id", "awb", "lane", "party", "person_email", "period")
DROP = {"doc_ref", "received_at", "register_received_at", "label_key", "charge_keys"}


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def compact(s: str | None) -> str:
    """Letters and digits only, upper case: how an invoice number or waybill is matched inside free text."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n[... {len(text) - n} more characters: ask for a page]"


@dataclass(frozen=True)
class Checked:
    """A citation the reviewer gave, after code checked it."""
    doc_id: int
    doc: str
    page: int
    quote: str
    level: int            # 1-5, as in knowledge/authority.py
    why: str              # why it has that level
    proves: str           # what the reviewer says it shows
    verified: bool        # the quote is on the page (False: a scan, where only the page can be checked)


class Library:
    def __init__(self, store: Store, terms: Terms):
        self.terms = terms
        self.view = view = terms.view
        self.docs: dict[int, DocInfo] = dict(view.docs)
        self.by_ref = {d.doc_ref: d for d in self.docs.values()}
        self.pages: dict[int, dict[int, str]] = {}
        self.emails: dict[int, dict] = {}
        for doc_id in sorted(self.docs):
            self.pages[doc_id] = {p["page_no"]: p["text"] or "" for p in store.pages(doc_id)}
            row = store.email(doc_id)
            if row is not None:
                self.emails[doc_id] = {"own": row["authored_text"] or "", "quoted": row["quoted_text"] or ""}
        self.facts_by_doc: dict[int, list[Fact]] = {}
        for f in view.facts:
            self.facts_by_doc.setdefault(f.doc_id, []).append(f)
        self.facts_by_id = {f.id: f for f in view.facts}
        self.headers = invoice_headers(view)
        self.contract = {inv: contract_of(terms, h) for inv, h in self.headers.items()}
        self._lock = threading.Lock()     # pricing and terms keep caches: one thread at a time
        self._priced: dict[str, list[dict]] = {}

    # ---- reading ----------------------------------------------------------------------------------------------
    def describe(self, d: DocInfo) -> dict:
        kind = "email" if d.format == "eml" else (d.doc_type or d.format)
        out = {"doc": d.doc_ref, "kind": kind, "received": d.received_at, "from": d.from_addr,
               "from_name": d.from_name, "subject": d.subject}
        if d.is_scan:
            out["note"] = "scanned: the text is a transcription; cite the page"
        return {k: v for k, v in out.items() if v}

    def document_text(self, d: DocInfo, page: int | None = None, limit: int = ANSWER_CHARS) -> str:
        head = json.dumps(self.describe(d), ensure_ascii=False)
        pages = self.pages.get(d.doc_id) or {}
        chosen = {page: pages.get(page, "")} if page else pages
        parts = [f"<document {head}>"]
        em = self.emails.get(d.doc_id)
        for n, text in sorted(chosen.items()):
            parts.append(f"--- page {n} ---\n{clip(text, PAGE_CHARS)}")
        if em and em["quoted"].strip():
            parts.append("--- of which quoted or forwarded (not the sender's own words) ---\n"
                         + clip(em["quoted"], PAGE_CHARS // 2))
        parts.append("</document>")
        return clip("\n".join(parts), limit)

    def fact_row(self, f: Fact) -> dict:
        data = {k: v for k, v in f.payload.items() if k not in DROP and v not in (None, "", [], {})}
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text) > 700:
            data = {"(shortened)": text[:700]}
        row = {"fact_id": f.id, "kind": f.kind, "doc": f.doc_ref, "page": f.page or 1,
               **{k: getattr(f, k) for k in KEYS if getattr(f, k)}, "data": data}
        if f.quote:
            row["quote"] = f.quote[:300]
        return row

    def billed_lines(self, invoice_id: str, claimed: dict[int, str]) -> list[dict]:
        h = self.headers.get(invoice_id)
        if h is None:
            return []
        contract = self.contract.get(invoice_id)
        rows = []
        for f in lines_of(self.view, h):
            p = f.payload
            with self._lock:
                code = self.terms.charge_code(contract, p.get("label"))
            rows.append({k: v for k, v in {
                "fact_id": f.id, "line_no": p.get("line_no"), "label": p.get("label"), "awb": f.awb,
                "pickup": p.get("pickup_date"), "period": p.get("period"), "quantity": p.get("quantity"),
                "unit": p.get("unit"), "rate": p.get("rate"), "amount": str(amount_of(f)),
                "currency": p.get("currency"), "charge_code": code or "unmapped",
                "already_claimed_by": claimed.get(f.id)}.items() if v not in (None, "")})
        return rows

    # ---- tools ------------------------------------------------------------------------------------------------
    def read_document(self, doc: str, page: int | None = None) -> str:
        d = self.by_ref.get(str(doc).strip())
        if d is None:
            return f"No document {doc!r} received by the as-of date. Use search_documents to find one."
        return self.document_text(d, page)

    def search_documents(self, text: str) -> str:
        want, want_c = squash(text).lower(), compact(text)
        if len(want) < 3:
            return "Search for at least 3 characters."
        hits = []
        for doc_id in sorted(self.pages):
            for n, page_text in sorted(self.pages[doc_id].items()):
                flat = squash(page_text)
                at = flat.lower().find(want)
                if at < 0 and len(want_c) >= 6 and want_c in compact(flat):
                    at = 0
                if at >= 0:
                    hits.append({"doc": self.docs[doc_id].doc_ref, "page": n,
                                 "snippet": flat[max(0, at - 160): at + len(want) + 160]})
        if not hits:
            return f"Nothing received by the as-of date contains {text!r}."
        more = f" (first 15 of {len(hits)})" if len(hits) > 15 else ""
        return clip(json.dumps({"hits" + more: hits[:15]}, ensure_ascii=False), ANSWER_CHARS)

    def find_facts(self, kind: str | None = None, invoice_id: str | None = None, awb: str | None = None,
                   contract_id: str | None = None) -> str:
        keys = {k: v for k, v in (("invoice_id", invoice_id), ("awb", awb), ("contract_id", contract_id)) if v}
        if kind:
            facts = self.view.get(kind, **keys)
        else:
            facts = [f for f in self.view.facts
                     if all(getattr(f, k) == {"invoice_id": norm.invoice_id, "awb": norm.awb,
                                              "contract_id": norm.code}[k](v) for k, v in keys.items())]
        facts = [f for f in facts if f.kind != "injection_attempt"]
        if not facts:
            return "No facts match."
        rows = [self.fact_row(f) for f in sorted(facts, key=lambda f: (f.known_at, f.id))[:40]]
        more = f"; first 40 of {len(facts)}, narrow the search" if len(facts) > 40 else ""
        return clip(json.dumps({"facts" + more: rows}, ensure_ascii=False, default=str), ANSWER_CHARS)

    def payment_register(self, invoice_id: str) -> str:
        rows = [self.fact_row(f) for f in self.view.get("ledger_entry", invoice_id=invoice_id)]
        return json.dumps({"register_rows": rows}, ensure_ascii=False, default=str) if rows else \
            f"The payment register has no row for {invoice_id} as of this date."

    def price_invoice(self, invoice_id: str) -> str:
        inv = norm.invoice_id(invoice_id)
        h = self.headers.get(inv)
        if h is None:
            return f"No invoice {invoice_id} received by the as-of date."
        with self._lock:
            if inv not in self._priced:
                ip = price_invoice(self.terms, h)
                self._priced[inv] = [{k: v for k, v in {
                    "fact_id": lp.line.id, "label": lp.line.payload.get("label"), "awb": lp.line.awb,
                    "charge_code": lp.code, "billed": str(lp.billed), "status": lp.status,
                    "expected": None if lp.expected is None else str(lp.expected),
                    "options": {k: str(x) for k, x in lp.options.items()} or None, "why": lp.why,
                    "working": lp.steps[:4] or None}.items() if v is not None} for lp in ip.lines()]
        return clip(json.dumps({"invoice": inv, "contract": self.contract.get(inv), "lines": self._priced[inv]},
                               ensure_ascii=False), ANSWER_CHARS)

    # ---- code's checks on what the reviewer cites ------------------------------------------------------------
    def check(self, c: dict, contract: str | None, category: str | None) -> Checked | None:
        d = self.by_ref.get(str(c.get("doc") or "").strip())
        quote = str(c.get("quote") or "").strip()
        if d is None or len(re.sub(r"\s", "", quote)) < MIN_QUOTE:
            return None
        pages = self.pages.get(d.doc_id) or {}
        try:
            page = int(c.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        if d.is_scan:
            if page not in pages:
                return None
            verified = False
        else:
            q = squash(quote)
            if q not in squash(pages.get(page, "")):
                page = next((n for n in sorted(pages) if q in squash(pages[n])), 0)
                if not page:
                    return None
            verified = True
        level, why = self.level(d, quote, contract, category)
        return Checked(d.doc_id, d.doc_ref, page, quote, level, why, str(c.get("proves") or "")[:200], verified)

    def level(self, d: DocInfo, quote: str, contract: str | None, category: str | None) -> tuple[int, str]:
        q = squash(quote).lower()
        for f in self.facts_by_doc.get(d.doc_id, []):
            if f.kind == "injection_attempt":
                t = squash(f.quote or f.payload.get("text")).lower()
                if t and (t in q or q in t):
                    return 5, INJECTED
        auth = self.terms.auth
        if d.format == "xlsx" or d.doc_type in RECORD_TYPES:
            return 3, f"a record ({d.doc_type or d.format})"
        if d.doc_type in CONTRACT_TYPES:
            for f in self.facts_by_doc.get(d.doc_id, []):
                if f.kind == "instrument":
                    with self._lock:
                        a = auth.of_contract_doc(f)
                    return a.level, a.why
            return 4, "a contract document without a signed instrument"
        em = self.emails.get(d.doc_id)
        if em is not None:
            own, quoted = squash(em["own"]).lower(), squash(em["quoted"]).lower()
            if q not in own and q in quoted:
                return 5, "quoted or forwarded text, not the sender's own words"
        sender = d.from_addr
        if not sender:
            return 4, "no sender address"
        if auth.known_domains and domain(sender) not in auth.known_domains:
            return 5, f"{domain(sender)} is outside both companies"
        if any(f.kind == "status_update" for f in self.facts_by_doc.get(d.doc_id, [])):
            return 3, "a carrier status email"
        if contract:
            cats = [category] if category else ["waivers", "payment_terms", "rates", "notices", "billing_disputes",
                                                "treasury"]
            for cat in cats:
                person = auth.schedule1(contract, sender, cat)
                if person is not None:
                    return 2, f"{person.payload.get('name')} is on Schedule 1 for {cat}, in their own words"
            if category and d.from_name:
                with self._lock:
                    grant = self.terms.feedback.grant(contract=contract, person=d.from_name, category=category,
                                                      on=self.view.as_of_date)
                if grant is not None:
                    return 2, f"feedback {grant['item_id']} confirms {d.from_name} for {category}"
        return 4, "an email from someone not on Schedule 1 for this"
