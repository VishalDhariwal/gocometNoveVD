"""Drafts and the outbox. Templates only, no LLM, so nothing a document says can change what gets written or to whom.

- Supervised mode: every outbound item (draft:* and human_review) becomes a draft, written to
  <state>/drafts/<mode>/<item id>.txt and kept in the outbox as 'drafted'.
- Autonomous mode: execute:* items go into the outbox as 'queued' (this harness never sends mail itself);
  human_review items are drafted for a person.
- The idempotency key is item id (which carries its version) + action, so running the same report again never
  queues anything twice. When an item is superseded, its earlier drafts are marked 'superseded'.
- Recipients come only from Schedule 1 (see decide/policy.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..decide.common import OUTBOUND
from ..store import Store

WHAT = {"overcharge": "charges above the contract rates", "duplicate": "charges billed more than once",
        "waived_but_billed": "charges you waived in writing", "late_fee_not_owed": "late fees that were not owed"}


def _cites(item: dict, limit: int = 6) -> str:
    return "\n".join(f'- {e["doc"]}, page {e["page"]}: "{e["quote"]}"' for e in item["evidence"][:limit])


def render(v, item: dict, recipient: dict | None) -> tuple[str, str]:
    name = (recipient or {}).get("person") or "colleagues"
    contract = v.contract or "our agreement"
    if v.remedy == "dispute":
        inv, amount = item["invoice_id"], item["amount_usd"]
        subject = f"Billing dispute: invoice {inv}, USD {amount}"
        body = (f"Dear {name},\n\nUnder {contract} we dispute USD {amount} on invoice {inv}: {WHAT.get(v.cls, v.cls)}."
                f"\n\n{v.why}.\n\nHow we worked it out:\n" + "\n".join(f"- {s}" for s in v.steps[:8]) +
                f"\n\nSupporting documents:\n{_cites(item)}\n\nPlease issue a credit note for USD {amount} against "
                f"invoice {inv}. The undisputed balance will be paid by its due date.\n\nAccounts Payable")
    elif v.remedy == "claim_credit":
        period, kind, amount = item.get("period"), item.get("credit_type"), item["amount_usd"]
        subject = f"Credit due under {contract}: {kind} {period}, USD {amount}"
        body = (f"Dear {name},\n\nUnder {contract} a {kind.replace('_', ' ')} credit of USD {amount} is due for "
                f"{period}, and we have no credit note for it.\n\nHow we worked it out:\n" +
                "\n".join(f"- {s}" for s in v.steps[:8]) + f"\n\nSupporting documents:\n{_cites(item)}\n\n"
                f"Please issue the credit note.\n\nAccounts Payable")
    else:
        doc = (item.get("missing_doc") or {}).get("name") or "the document"
        about = item.get("invoice_id") or item.get("period") or contract
        subject = f"Request for {doc} ({contract})"
        body = (f"Dear {name},\n\n{doc} is referred to in documents we hold under {contract}, but we don't have a "
                f"copy. We need it to complete our check of {about}.\n\nWhere it is cited:\n{_cites(item, 3)}\n\n"
                f"Could you send it to us, please?\n\nAccounts Payable")
    return subject, body


def file_actions(store: Store, report_id: int, mode: str, emitted: list) -> dict:
    counts = {"drafted": 0, "queued": 0, "already": 0}
    folder = Path(store.path).parent / "drafts" / mode
    for v, gate, item in emitted:
        action = item["action"]
        if v.remedy not in OUTBOUND or not (action.startswith(("draft:", "execute:")) or action == "human_review"):
            continue
        subject, body = render(v, item, gate.recipient)
        status = "queued" if action.startswith("execute:") else "drafted"
        r = gate.recipient or {}
        new = store.queue_outbound(idempotency_key=f"{item['id']}|{action}", report_id=report_id, mode=mode,
                                   action=action, finding_key=v.finding_key, item_id=item["id"], subject=subject,
                                   body=body, status=status, recipient_email=r.get("email"),
                                   recipient_name=r.get("person"))
        if not new:
            counts["already"] += 1
            continue
        counts[status] += 1
        with store.transaction():
            store.con.execute("UPDATE outbox SET status = 'superseded' WHERE finding_key = ? AND mode = ? AND "
                              "item_id != ? AND status IN ('drafted', 'queued')", (v.finding_key, mode, item["id"]))
        folder.mkdir(parents=True, exist_ok=True)
        to = f"{r.get('person')} <{r.get('email')}>" if r.get("email") else "(no Schedule 1 recipient)"
        (folder / (re.sub(r"[^A-Za-z0-9_.~-]+", "_", item["id"]) + ".txt")).write_text(
            f"To: {to}\nAction: {action}\nSubject: {subject}\n\n{body}\n")
    return counts
