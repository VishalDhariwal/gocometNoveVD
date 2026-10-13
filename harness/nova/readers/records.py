"""Shipping records in PDF: air waybills (single and master), dangerous-goods declarations, customs notices."""
from __future__ import annotations

from .. import config
from .. import normalize as norm
from ..llm import Gateway
from . import values as v
from .base import (AUTOMATION_FIELD, COMMON_RULES, QUOTE, STR, DocContext, FactOut, ReadOutcome, call, ground,
                   injection_facts, plain, tool)

VERSION = "records-v2"

SYSTEM = COMMON_RULES + """

This is a shipping record: an air waybill, a master air waybill listing house shipments, a shipper's declaration for dangerous goods or dry ice, or a customs notice. Record what it says about the shipment: its waybill number, the master waybill if this is a house, where and when it was executed, origin and destination, pieces, actual and chargeable weight and dimensions as printed, and the goods. For a master waybill list every house with its pieces and chargeable weight. For a declaration list each dangerous-goods item with its UN number and class; mark is_dry_ice for dry ice (solid carbon dioxide, UN1845) and refrigerant_only when it is used only to keep other goods cold. For a customs notice record the hold and release dates, the station, the reason code and the reason as printed, and choose cause from the list below.

""" + v.CAUSE_HELP

TOOL = tool("record_shipping_record", "Record what a waybill, declaration or customs notice says.", {
    "doc_kind": {"enum": ["awb", "master_awb", "dg_declaration", "customs_notice", "other"]},
    "awb": STR, "master_awb": STR, "executed_on": STR, "executed_at": STR, "origin": STR, "destination": STR,
    "pieces": STR, "actual_weight": STR, "chargeable_weight": STR, "dimensions": STR, "goods": STR,
    "houses": {"type": "array", "items": {"type": "object", "properties": {
        "awb": STR, "pieces": STR, "chargeable_weight": STR, "quote": QUOTE}, "required": ["awb", "quote"]}},
    "dangerous_goods": {"type": "array", "items": {"type": "object", "properties": {
        "un_number": STR, "description": STR, "class": STR, "quantity": STR,
        "is_dry_ice": {"type": "boolean"}, "refrigerant_only": {"type": "boolean"}, "quote": QUOTE},
        "required": ["quote"]}},
    "customs": {"type": "object", "properties": {"hold_date": STR, "release_date": STR, "station": STR,
                                                "reason_code": STR, "reason": STR, "cause": {"enum": v.CAUSES},
                                                "quote": QUOTE}},
    "quote": QUOTE,
    "automation_instructions": AUTOMATION_FIELD,
}, ["doc_kind", "automation_instructions"])


class RecordsReader:
    name = "records"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        data = call(gateway, out, purpose="records", models=config.READER_MODELS, system=SYSTEM, tool_def=TOOL,
                    content=ctx.document_block(), max_tokens=4000)
        out.facts, out.warnings = build(ctx, data)
        out.doc_type = data.get("doc_kind")
        out.status = "partial" if out.warnings else "ok"
        return out


def build(ctx: DocContext, data: dict) -> tuple[list[FactOut], list[str]]:
    problems: list[str] = []
    facts = injection_facts(ctx, data.get("automation_instructions"))
    kind = data.get("doc_kind") or "other"
    awb = v.text_or_none(data.get("awb"))
    order = v.date_order([data.get("executed_on"), (data.get("customs") or {}).get("hold_date"),
                          (data.get("customs") or {}).get("release_date")])
    if kind == "other":
        return facts, problems
    if not awb:
        return facts, [f"{kind} without a waybill number"]

    page, quote, ver = ground(ctx, data.get("quote"), [awb])
    if ver == "unverified":
        problems.append(f"waybill number {awb} isn't on the page as quoted")

    if kind in ("awb", "master_awb"):
        qty = lambda raw: v.parse_quantity(raw)[0]  # noqa: E731
        houses = []
        for h in data.get("houses") or []:
            hp, hq, hv = ground(ctx, h.get("quote"), [h.get("awb")])
            houses.append({"awb": norm.awb(h["awb"]), "pieces": qty(h.get("pieces")),
                           "chargeable_kg": qty(h.get("chargeable_weight")), "verification": hv})
        facts.append(FactOut("shipment", {
            "is_master": kind == "master_awb", "master_awb": norm.awb(data["master_awb"]) if data.get("master_awb") else None,
            "executed_on": v.iso(v.parse_date(data.get("executed_on"), order)), "executed_at": data.get("executed_at"),
            "origin": data.get("origin"), "destination": data.get("destination"), "pieces": qty(data.get("pieces")),
            "actual_kg": qty(data.get("actual_weight")), "chargeable_kg": qty(data.get("chargeable_weight")),
            "dimensions": data.get("dimensions"), "goods": data.get("goods"), "houses": houses},
            keys={"awb": awb}, page=page, quote=quote, verification=ver))
    elif kind == "dg_declaration":
        items = []
        for item in data.get("dangerous_goods") or []:
            ip, iq, iv = ground(ctx, item.get("quote"), [item.get("un_number")])
            items.append({"un_number": (item.get("un_number") or "").upper().replace(" ", "") or None,
                          "description": item.get("description"), "class": plain(item.get("class")),
                          "quantity": plain(item.get("quantity")), "refrigerant_only": item.get("refrigerant_only"),
                          "is_dry_ice": bool(item.get("is_dry_ice")),
                          "quote": iq, "verification": iv})
        for i in items:  # UN1845 is dry ice by definition; otherwise Claude's reading decides
            i["is_dry_ice"] = i["is_dry_ice"] or i["un_number"] == "UN1845"
        dry_ice = [i for i in items if i["is_dry_ice"]]
        facts.append(FactOut("dg_declaration", {
            "items": items, "goods": data.get("goods"), "contains_dry_ice": bool(dry_ice),
            "dry_ice_only": bool(items) and len(dry_ice) == len(items) and all(i["refrigerant_only"] for i in dry_ice),
            "has_dg_other_than_dry_ice": any(not i["is_dry_ice"] for i in items)},
            keys={"awb": awb}, page=page, quote=quote, verification=ver))
    elif kind == "customs_notice":
        c = data.get("customs") or {}
        facts.append(FactOut("customs_notice", {
            "hold_date": v.iso(v.parse_date(c.get("hold_date"), order)),
            "release_date": v.iso(v.parse_date(c.get("release_date"), order)), "station": c.get("station"),
            "reason_code": c.get("reason_code"), "reason": c.get("reason"),
            "cause": v.choice(c.get("cause"), v.CAUSES, "not_stated")},
            keys={"awb": awb}, page=page, quote=quote, verification=ver))
    return facts, problems
