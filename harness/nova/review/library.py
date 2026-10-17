"""What the reviewer may read: everything received by the as_of date, loaded once into memory.

Page text, each email's own words and quoted part, the facts, document status and code's pricing are loaded in the
report's own thread; the agents then run in worker threads against these copies (an SQLite connection can't be shared
across threads). Nothing here writes anything. tools.py builds the agent's tools on top of it.

`check` and `level` are code's seat belt on quotes: a citation only counts if its quote is on the page it names, and
only for as much authority as its document has (GLOSSARY "Which document wins").
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


def _topic(subject: str | None) -> str:
    """An email's subject without its Re: / Fwd: prefixes, to find the rest of its thread."""
    return re.sub(r"^\s*((re|fw|fwd|aw|wg|tr)\s*:\s*)+", "", (subject or "").strip(), flags=re.I).lower()


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
        self.status: dict[int, str | None] = {}
        # documents whose current reading finished cleanly (not partial, not failed): one with nothing in it said nothing
        self.clean = {r[0] for r in store.con.execute(
            "SELECT d.id FROM documents d JOIN extractions e ON e.id = d.current_extraction_id WHERE e.status = 'ok'")}
        self.summary: dict[int, str] = {}        # each document's card, written by its reader
        self.children: dict[int, list[int]] = {}
        for doc_id, d in sorted(self.docs.items()):
            row = store.document(doc_id)
            self.status[doc_id] = row["status"] if row is not None else None
            if row is not None and "summary" in row.keys() and (row["summary"] or "").strip():
                self.summary[doc_id] = row["summary"].strip()
            if d.parent_id is not None:
                self.children.setdefault(d.parent_id, []).append(doc_id)
        # each document's text as letters and digits only, to find which documents name an invoice or a waybill
        self.flat = {doc_id: compact(" ".join(p for _, p in sorted(pages.items())))
                     for doc_id, pages in self.pages.items()}
        self.injected = {f.doc_id for f in view.kind("injection_attempt")}
        self._lock = threading.Lock()     # pricing and terms keep caches: one thread at a time
        self._pricing: dict = {}

    # ---- reading ----------------------------------------------------------------------------------------------
    def describe(self, d: DocInfo) -> dict:
        kind = "email" if d.format == "eml" else (d.doc_type or d.format)
        out = {"doc": d.doc_ref, "kind": kind, "received": d.received_at, "from": d.from_addr,
               "from_name": d.from_name, "subject": d.subject}
        if d.is_scan:
            out["note"] = "scanned: the text is a transcription; cite the page"
        n_pages = len(self.pages.get(d.doc_id) or {})
        if n_pages > 1:
            out["pages"] = n_pages
        out["summary"] = self.summary.get(d.doc_id)
        return {k: v for k, v in out.items() if v}

    def document_text(self, d: DocInfo, page: int | None = None, limit: int = ANSWER_CHARS,
                      only: list[int] | None = None, note: str = "") -> str:
        """The document's card, then its text: every page, one `page`, or the pages in `only` ([] = the card alone)."""
        head = json.dumps(self.describe(d), ensure_ascii=False)
        pages = self.pages.get(d.doc_id) or {}
        if only is not None:
            chosen = {n: pages[n] for n in only if n in pages}
        else:
            chosen = {page: pages.get(page, "")} if page else pages
        parts = [f"<document {head}>"]
        em = self.emails.get(d.doc_id)
        for n, text in sorted(chosen.items()):
            of = f" of {len(pages)}" if len(pages) > 1 and len(chosen) < len(pages) else ""
            parts.append(f"--- page {n}{of} ---\n{clip(text, PAGE_CHARS)}")
        if note:
            parts.append(f"({note})")
        if em and em["quoted"].strip() and (not pages or min(pages) in chosen):
            parts.append("--- of which quoted or forwarded (not the sender's own words) ---\n"
                         + clip(em["quoted"], PAGE_CHARS // 2))
        parts.append("</document>")
        return clip("\n".join(parts), limit)

    def centre_text(self, d: DocInfo, fact_ids: list[int], card_only: str = "") -> str:
        """A document at the centre of a case, for the brief: its card and the pages holding the case's facts. A
        one-page document is shown whole; a document with no card and no such page, from its first page. The agent
        reads any other page with read_document."""
        pages = self.pages.get(d.doc_id) or {}
        if card_only:
            return self.document_text(d, only=[], note=card_only)
        wanted = {f.page or 1 for f in self.facts_by_doc.get(d.doc_id, []) if f.id in set(fact_ids)}
        if len(pages) <= 1:
            wanted = set(pages)
        elif not wanted and d.doc_id not in self.summary:
            wanted = {min(pages)}
        rest = len(pages) - len(wanted & set(pages))
        note = f"{rest} more page(s): read_document gives any of them" if rest > 0 else ""
        return self.document_text(d, only=sorted(wanted), note=note)

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

    def pricing(self, invoice_id: str):
        """Code's pricing of one invoice (knowledge/pricing.InvoicePricing), worked out once."""
        with self._lock:
            if invoice_id not in self._pricing:
                h = self.headers.get(invoice_id)
                self._pricing[invoice_id] = price_invoice(self.terms, h) if h is not None else None
            return self._pricing[invoice_id]

    def names_of(self, invoice_id: str) -> set[str]:
        """The invoice number and its waybills, letters and digits only (how other documents mention them)."""
        h = self.headers.get(invoice_id)
        names = {compact(invoice_id)}
        if h is not None:
            names |= {compact(f.awb) for f in lines_of(self.view, h) if f.awb and len(compact(f.awb)) >= 6}
        return {n for n in names if n}

    def related(self, invoice_id: str | None, extra: list[int] = (), limit: int = 25) -> list[DocInfo]:
        """Every document received by as_of that names this invoice or one of its waybills, plus the invoice's own
        email and attachments: the index the reviewer works from."""
        ids = set(extra)
        if invoice_id:
            names = self.names_of(invoice_id)
            ids |= {doc_id for doc_id, flat in self.flat.items() if any(n in flat for n in names)}
            h = self.headers.get(invoice_id)
            if h is not None:
                d = self.docs.get(h.doc_id)
                parent = d.parent_id if d and d.parent_id else h.doc_id
                ids |= {parent, *self.children.get(parent, [])}
        threads = {_topic(self.docs[i].subject) for i in ids if i in self.emails and i in self.docs} - {""}
        if threads:     # replies and forwards in the same email thread
            ids |= {i for i in self.emails if i in self.docs and _topic(self.docs[i].subject) in threads}
        docs = sorted((self.docs[i] for i in ids if i in self.docs), key=lambda d: (d.received_at, d.doc_ref))
        return docs[:limit]

    def feedback_for(self, finding_key: str) -> list[str]:
        with self._lock:
            items = self.terms.feedback.for_key(finding_key)
        return [f"{it.get('item_id')}: {it.get('decision')} {json.dumps(it.get('fact') or {}, default=str)[:300]}"
                for it in items]

    def unread(self, doc_id: int) -> bool:
        """Nothing was ever read out of this document or its email: it failed, or no reader got to it."""
        if self.status.get(doc_id) == "failed":
            return True
        if self.status.get(doc_id) not in ("new", "read"):
            return False
        d = self.docs.get(doc_id)
        family = {doc_id, *self.children.get(doc_id, [])}
        if d is not None and d.parent_id is not None:
            family |= {d.parent_id, *self.children.get(d.parent_id, [])}
        return not any(self.facts_by_doc.get(i) for i in family)

    # ---- tools ------------------------------------------------------------------------------------------------
    def read_document(self, doc: str, page: int | None = None) -> str:
        """One page at a time: page 1 when none is asked for. A one-page document comes whole."""
        d = self.by_ref.get(str(doc).strip())
        if d is None:
            return f"No document {doc!r} received by the as-of date. Use search_documents to find one."
        pages = self.pages.get(d.doc_id) or {}
        if page is not None and pages and page not in pages:
            return f"{d.doc_ref} has no page {page}: its pages are {min(pages)} to {max(pages)}."
        if page is None and len(pages) > 1:
            page = min(pages)
        text = self.document_text(d, page)
        if page is not None and len(pages) > 1:
            later = [n for n in sorted(pages) if n > page]
            text += (f"\nThis was page {page} of {len(pages)}. "
                     + (f"Ask for page {later[0]} or any other with read_document." if later else "It is the last page."))
        return text

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
