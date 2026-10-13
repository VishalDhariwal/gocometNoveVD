"""What kind of document is this PDF (or image)? One small, cheap call, so the right reader gets it.

File names and email subjects are passed as hints but the decision is the content's: a new carrier may call an
invoice a "charges advice" or name its files anything at all.
"""
from __future__ import annotations

from .. import config
from ..llm import Gateway
from .base import COMMON_RULES, STR, DocContext, ReadOutcome, call, tool

VERSION = "classify-v1"

TYPES = ["msa", "amendment", "annex_update", "rate_notice", "invoice", "statement", "credit_note", "awb",
         "master_awb", "dg_declaration", "customs_notice", "letter", "other"]

SYSTEM = COMMON_RULES + """

Your job here is only to say what kind of document this is:
- msa: a master services agreement or contract between a shipper and a carrier (terms, rates, annexes, schedules)
- amendment: a signed amendment to such an agreement
- annex_update: a replacement or update of one annex or rate sheet (for example "Annex A rev 2", an exchange-rate update)
- rate_notice: a notice or letter that changes rates
- invoice: a carrier's bill for shipments or services, whatever it is titled (invoice, tax invoice, charges advice, debit note)
- statement: a periodic statement of account billing monthly or recurring fees
- credit_note: a credit note or credit memo
- awb: an air waybill for one shipment or one house shipment
- master_awb: a master air waybill listing house shipments
- dg_declaration: a shipper's declaration for dangerous goods or dry ice
- customs_notice: a customs hold, release or examination notice
- letter: any other letter or notice (waiver, extension, termination, dispute, renewal)
- other: anything else"""

TOOL = tool("record_document_type", "Record what kind of document this is.", {
    "doc_type": {"enum": TYPES},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reason": STR,
}, ["doc_type", "confidence"])


def classify(ctx: DocContext, gateway: Gateway, outcome: ReadOutcome) -> tuple[str, float]:
    hints = []
    if ctx.parent_email:
        hints.append(f"Attached to an email with subject: {ctx.parent_email.get('subject')!r}")
    hints.append(f"File name: {ctx.filename!r}")
    first_page = ctx.pages[0][1] if ctx.pages else ""
    content = "\n".join(hints) + "\n\n" + f'<document name="{ctx.filename}">\n{first_page[:4000]}\n</document>'
    data = call(gateway, outcome, purpose="classify", models=config.CLASSIFY_MODELS, system=SYSTEM, tool_def=TOOL,
                content=content, max_tokens=300)
    return data["doc_type"], float(data.get("confidence") or 0)
