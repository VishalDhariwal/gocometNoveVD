"""Tests for phase 3: the as-of view, authority, the terms engine and pricing.

Each test builds a small world straight into the store (documents plus the facts a correct reading would give), in the
shapes the readers write, then prices it. The numbers come from the worked examples in data/examples/ and the rules in
GLOSSARY.md.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.knowledge.pricing import price_all, price_invoice  # noqa: E402
from nova.knowledge.reconcile import reconcile_labels  # noqa: E402
from nova.knowledge.terms import Conflict, Decided, Missing, Terms, Undefined, cited_name  # noqa: E402
from nova.knowledge.view import View  # noqa: E402
from nova.llm import Gateway  # noqa: E402
from nova.readers.values import parse_band  # noqa: E402
from nova.store import Store  # noqa: E402
from tests.test_readers import Scripted  # noqa: E402

C = "TBX-US-EXP-E60"
T0 = "2031-11-25T10:00:00Z"
P1, P2 = ("2031-12-01", "2032-05-31"), ("2032-06-01", None)
FINTAN = ("fintan.corrigan@tarrowby.example", "Fintan Corrigan")       # rates
JOAQUIN = ("joaquin.rathbone@tarrowby.example", "Joaquin Rathbone")    # notices, billing_disputes: not rates
PEOPLE = [("Joaquin Rathbone", "Key Account Manager", "TBX-US", ["notices", "billing_disputes"], JOAQUIN[0]),
          ("Fintan Corrigan", "Pricing Manager", "TBX-US", ["rates"], FINTAN[0]),
          ("Signe Underhill", "Customer Finance Lead", "TBX-US", ["waivers", "payment_terms"],
           "signe.underhill@tarrowby.example"),
          ("Odette Sandoval", "Treasury Analyst", "TBX-US", ["treasury"], "odette.sandoval@tarrowby.example"),
          ("Isolde Pembroke", "AP Manager", "QMP-US", ["notices", "payment_terms", "billing_disputes"],
           "isolde.pembroke@quillmere.example")]
RATES = [  # lane, band, P1, P2
    ("USE2-INE2", "minimum charge", "50.00", "50.00"), ("USE2-INE2", "0-44.5 kg", "7.73", "7.93"),
    ("USE2-INE2", "45-99.5 kg", "6.81", "6.99"), ("USE2-INE2", "100-299.5 kg", "6.01", "6.16"),
    ("USE2-INE2", "300+ kg", "5.50", "5.60"),
    ("USW1-USC3", "100-299.5 kg", "3.50", "3.60"),
    ("USC3-INS1", "45-99.5 kg", "4.71", "4.69"), ("USC3-INS1", "100-299.5 kg", "4.49", "4.47"),
    ("USS2-INN1", "100-299.5 kg", "7.82", "7.79"),
    ("USE2-INW2", "100-299.5 kg", "5.57", "5.55"),
]
FUEL = {"2031-12": "20.16", "2032-01": "21.00", **{f"2032-{m:02d}": "23.14" for m in range(2, 13)}}
FEES = [("SEC", "Security surcharge", "0.15", "per_kg", None), ("DG", "Dangerous goods fee", "124.02", "per_shipment", None),
        ("DRYICE", "Dry ice handling fee", "45.40", "per_shipment", None),
        ("STOR", "Storage", "28.88", "per_day", None), ("DLV", "Delivery fee", "121.81", "per_shipment", None),
        ("CUSTOMS", "Customs hold fee", "227.14", "per_shipment", None),
        ("ACCT", "Monthly account fee", "350.28", "per_month", None),
        ("PEAK", "Peak season surcharge", "0.29", "per_kg", [9, 10, 11])]
ALIASES = {"FRT": ["Air freight", "Express freight"], "FUEL": ["Fuel surcharge", "FSC"],
           "SEC": ["Security surcharge"], "DG": ["Dangerous goods fee", "DG handling"],
           "DRYICE": ["Dry ice handling fee"], "STOR": ["Storage"], "DLV": ["Delivery fee", "Door delivery"],
           "CUSTOMS": ["Customs hold fee"], "ACCT": ["Monthly account fee"], "PEAK": ["Peak season surcharge", "PSS"],
           "TAX": ["Sales tax", "GST"]}
UNRANKED = ("13.3 Unranked instruments. Where a rate notice under clause 3.5 and an amendment state different rates "
            "for the same lane and period, the parties shall resolve the difference jointly; neither prevails "
            "automatically.")


def F(kind, payload, **kw):
    return kind, payload, kw


class World:
    """A mailbox written straight into the store."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)
        self.n = 0

    def close(self):
        self.store.close()
        self.tmp.cleanup()

    def email(self, received, sender, name=None, subject="", attachment=None, fmt="pdf"):
        self.n += 1
        ref = f"batch_01/{self.n:04d}.eml"
        rid, _ = self.store.add_receipt(path=ref, batch="batch_01", data=ref.encode(), message_id=None,
                                        received_at=received)
        eid, _ = self.store.add_document(doc_ref=ref, receipt_id=rid, fmt="eml", data=ref.encode() + b"e",
                                         received_at=received)
        self.store.set_email(eid, from_addr=sender, from_name=name, authored_text="(test)", subject=subject)
        if attachment is None:
            return eid
        aid, _ = self.store.add_document(doc_ref=f"{ref}#{attachment}", receipt_id=rid, fmt=fmt,
                                         data=(ref + attachment).encode(), received_at=received,
                                         filename=attachment, parent_id=eid)
        return aid

    def put(self, doc_id, *facts):
        ext = self.store.begin_extraction(doc_id, "test", "t1")
        for kind, payload, kw in facts:
            kw = {"quote": f"{kind} as printed on the page", "page": 1, **kw}
            self.store.add_fact(ext, kind, payload, verification="verified", **kw)
        self.store.finish_extraction(ext, "ok")
        return doc_id

    def terms(self, as_of=None) -> Terms:
        return Terms(View.load(self.store, as_of))

    def priced(self, number, as_of=None):
        terms = self.terms(as_of)
        inv = next(f for f in terms.view.kind("invoice") if f.invoice_id == number)
        return price_invoice(terms, inv)

    # ---- documents ----
    def msa(self, precedence=("unranked", "13.3", UNRANKED), received=T0, gst=None):
        doc = self.email(received, "nadia.iyengar@quillmere.example", "Nadia Iyengar", f"Executed agreement {C}",
                         f"MSA-{C}.pdf")
        ref = {"instrument": "msa", "instrument_ref": C}
        facts = [F("instrument", {"type": "msa", "reference": C, "title": "Master Services Agreement",
                                  "effective_from": P1[0], "signed_by_both": True}, contract_id=C, valid_from=P1[0]),
                 F("contract", {"parties": [{"role": "shipper", "name": "Quillmere", "code": "QMP-US"},
                                            {"role": "carrier", "name": "Tarrowby", "code": "TBX-US"}],
                                "currency": "USD"}, contract_id=C, valid_from=P1[0]),
                 F("contract_term", {"term": "pricing_date", "clause": "3.2", "value": {}, **ref}, contract_id=C,
                   valid_from=P1[0], quote="3.2 Pricing date. Each shipment is priced at the rates, surcharges and "
                                           "fees in force on its Pickup Date.", page=3),
                 F("contract_term", {"term": "rule:storage", "clause": "6.1", "value": {
                     "free_days": 5, "arrival_event": "ARRIVED_FACILITY",
                     "release_events": ["DELIVERED", "COLLECTED_BY_CONSIGNEE"]}, **ref},
                   contract_id=C, valid_from=P1[0], quote="6.1 Storage. Storage days = release date - arrival date "
                                                          "(ARRIVED_FACILITY) - 5 free days, minimum 0."),
                 F("contract_term", {"term": "dry_ice", "clause": "5.2", "value": {}, **ref}, contract_id=C,
                   valid_from=P1[0], quote="5.2 Dry ice. Dry ice (UN1845) used solely as a refrigerant"),
                 F("contract_term", {"term": "rule:customs_holds", "clause": "6.3",
                                     "value": {"carrier_error_exempt": True}, **ref}, contract_id=C, valid_from=P1[0],
                   quote="6.3 Customs holds. Customs hold and examination fees are for the Shipper's account, except "
                         "where the hold results from an error or omission in documents prepared or filed by the "
                         "Carrier.")]
        if precedence:
            facts.append(F("contract_term", {"term": "rule:notice_vs_amendment", "clause": precedence[1],
                                             "value": {"ranking": precedence[0]}, **ref},
                           contract_id=C, valid_from=P1[0], quote=precedence[2], page=5))
        if gst:
            facts.append(F("contract_term", {"term": "rule:gst", "clause": "12", "value": gst, **ref}, contract_id=C,
                           valid_from=P1[0], quote="GST applies as stated in clause 12 of this agreement."))
        for name, title, party, cats, mail in PEOPLE:
            facts.append(F("schedule1_person", {"name": name, "title": title, "categories": cats, "instrument_ref": C},
                           contract_id=C, party=party, person_email=mail, valid_from=P1[0]))
        for lane, band, r1, r2 in RATES:
            kind, lo, hi = parse_band(band)
            for (start, end), rate, label in ((P1, r1, "P1"), (P2, r2, "P2")):
                facts.append(F("rate", {"kind": kind, "band_min": lo, "band_max": hi, "band_text": band, "rate": rate,
                                        "currency": "USD", "period_label": label, **ref},
                               contract_id=C, lane=lane, valid_from=start, valid_to=end,
                               quote=f"{lane} {band} {r1} {r2}", page=8))
        for month, pct in FUEL.items():
            facts.append(F("fuel_rate", {"pct": pct, "instrument_ref": C}, contract_id=C, period=month,
                           quote=f"{month} {pct}%"))
        for code, name, fee, basis, months in FEES:
            facts.append(F("accessorial", {"code": code, "name": name, "fee": fee, "currency": "USD", "basis": basis,
                                           "months": months, "instrument_ref": C}, contract_id=C, valid_from=P1[0],
                           quote=f"{code} {name} {fee} USD"))
        for code, labels in ALIASES.items():
            facts.append(F("charge_alias", {"code": code, "name": labels[0], "labels": labels,
                                            "label_keys": sorted({"".join(ch for ch in x.lower() if ch.isalnum())
                                                                  for x in labels}), "instrument_ref": C},
                           contract_id=C, valid_from=P1[0], quote=f"Code {code}: {labels[0]}"))
        return self.put(doc, *facts)

    def amendment(self, ref, received, effective, rates=(), refs=(), signed=True):
        doc = self.email(received, "farid.underhill@quillmere.example", "Farid Underhill", f"Signed {ref}",
                         f"{ref.replace('/', '-')}.pdf")
        facts = [F("instrument", {"type": "amendment", "reference": ref, "effective_from": effective,
                                  "signed_by_both": signed}, contract_id=C, valid_from=effective)]
        for lane, band, rate in rates:
            kind, lo, hi = parse_band(band)
            facts.append(F("rate", {"kind": kind, "band_min": lo, "band_max": hi, "band_text": band, "rate": rate,
                                    "currency": "USD", "period_label": None, "instrument": "amendment",
                                    "instrument_ref": ref},
                           contract_id=C, lane=lane, valid_from=effective, quote=f"{lane} {band} {rate} USD"))
        for name, lanes, eff in refs:
            facts.append(F("doc_reference", {"name_as_cited": name, "governs": "express rates", "lanes": lanes,
                                             "effective_from": eff, "issued_by": "the Carrier", "category": "rates",
                                             "said_to_be_attached": True, "cited_in": ref},
                           contract_id=C, quote=f"{name} applies from {eff} to lane(s) {', '.join(lanes)}."))
        return self.put(doc, *facts)

    def notice(self, received, who, rows, start, end=None, quoted=False):
        sender, name = who
        doc = self.email(received, sender, name, "Rate notice")
        base = {"sender_email": sender, "from_quoted_text": quoted, "original_sender": name if quoted else None,
                "received_at": received}
        facts = [F("instrument", {**base, "type": "rate_notice", "reference": None, "effective_from": start,
                                  "effective_to": end, "signed_by_both": False},
                   contract_id=C, person_email=sender, valid_from=start, valid_to=end)]
        for lane, band, rate in rows:
            kind, lo, hi = parse_band(band)
            facts.append(F("rate", {**base, "kind": kind, "band_min": lo, "band_max": hi, "band_text": band,
                                    "rate": rate, "currency": "USD", "period_label": None, "instrument": "rate_notice",
                                    "instrument_ref": None},
                           contract_id=C, lane=lane, person_email=sender, valid_from=start, valid_to=end,
                           quote=f"{lane} {band}: {rate} USD"))
        return self.put(doc, *facts)

    def invoice(self, number, received, lines, doc_date=None, contract=C, taxes=()):
        doc = self.email(received, "billing@tarrowby.example", "Tarrowby Billing", f"Invoice {number}",
                         f"{number.replace('/', '-')}.pdf")
        facts = [F("invoice", {"number_as_printed": number, "doc_kind": "invoice", "currency": "USD",
                               "document_date": doc_date or received[:10], "taxes": list(taxes)},
                   invoice_id=number, contract_id=contract)]
        for i, (label, awb, lane, pickup, qty, unit, rate, amount) in enumerate(lines, start=1):
            pct = isinstance(rate, str) and rate.endswith("%")
            facts.append(F("invoice_line", {"line_no": i, "label": label, "label_key": "".join(
                ch for ch in label.lower() if ch.isalnum()), "awb": awb, "lane": lane, "pickup_date": pickup,
                "quantity": qty, "unit": unit, "rate": rate.rstrip("%") if rate else None, "rate_is_pct": pct,
                "amount": amount, "currency": "USD", "period": None},
                invoice_id=number, awb=awb, lane=lane, contract_id=contract,
                quote=f"{i} {awb} {pickup} {label} {lane} {qty} {unit} {rate} {amount}"))
        return self.put(doc, *facts)

    def tracking(self, received, rows):
        doc = self.email(received, "maren.bhandari@quillmere.example", "Maren Bhandari", "Tracking export",
                         "tracking.xlsx", fmt="xlsx")
        return self.put(doc, *[F("tracking_event", {"event": r[1], "date": r[2], "station": "USE2", "reason_code": r[3],
                                                    "cause": r[4] if len(r) > 4 else None},
                                 awb=r[0], quote=f"{r[0]} | {r[1]} | {r[2]} | USE2 | {r[3]}") for r in rows])

    def feedback(self, items, decided_at="2032-06-02T10:15:00Z"):
        doc = {"schema_version": "1.0", "world": "T", "after_batch": "batch_01", "decided_at": decided_at,
               "decided_by": {"name": "Isolde Pembroke"}, "items": items}
        self.store.add_feedback_file("feedback.json", json.dumps(doc).encode())


def line_of(ip, code):
    return next(lp for lp in ip.lines() if lp.code == code)


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.msa()

    def tearDown(self):
        self.w.close()


# ---- the worked examples ---------------------------------------------------------------------------------------
class Overcharge(Base):
    def setUp(self):
        super().setUp()
        self.w.invoice("TX-US-2032-5394", "2032-01-03T09:00:00Z", [
            ("Air freight", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", "6.69", "1612.29"),
            ("Fuel surcharge", "TE56915687", "USE2-INE2", "2031-12-25", "1612.29", None, "20.16%", "325.04"),
            ("Security surcharge", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", "0.15", "36.15"),
            ("Delivery fee", "TE56915687", "USE2-INE2", "2031-12-25", "1", "shipment", "121.81", "121.81")])
        self.w.invoice("TX-US-2032-5397", "2032-01-27T09:00:00Z", [
            ("Air freight", "TE68271803", "USE2-INE2", "2032-01-18", "44", "kg", "7.73", "340.12")])
        self.w.tracking("2032-02-29T09:00:00Z", [("TE56915687", "PICKED_UP", "2031-12-25", "-"),
                                                  ("TE56915687", "DELIVERED", "2031-12-29", "-"),
                                                  ("TE68271803", "PICKED_UP", "2032-01-18", "-")])

    def test_reprices_freight_and_the_fuel_on_it(self):
        ip = self.w.priced("TX-US-2032-5394", "2032-08-31")
        frt, fuel = line_of(ip, "FRT"), line_of(ip, "FUEL")
        self.assertEqual((frt.status, frt.expected), ("priced", D("1448.41")))
        self.assertEqual((fuel.status, fuel.expected), ("priced", D("292.00")))
        self.assertEqual(line_of(ip, "SEC").expected, D("36.15"))
        self.assertEqual(line_of(ip, "DLV").expected, D("121.81"))
        self.assertEqual(ip.overcharge_range(), (D("196.92"), D("196.92")))
        self.assertEqual(ip.shipments[0].pickup.source, "tracking")
        quotes = [s.fact.quote for s in frt.sources]
        self.assertIn("USE2-INE2 100-299.5 kg 6.01 6.16", quotes)
        self.assertIn("TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -", quotes)
        self.assertTrue(any(q.startswith("3.2 Pricing date") for q in quotes))
        self.assertEqual(ip.missing(), [])

    def test_decoy_in_the_lower_band_is_fine(self):
        ip = self.w.priced("TX-US-2032-5397", "2032-08-31")
        self.assertEqual(line_of(ip, "FRT").expected, D("340.12"))
        self.assertEqual(ip.overcharge_range(), (D("0"), D("0")))

    def test_nothing_after_as_of(self):
        terms = self.w.terms("2032-01-10")
        self.assertEqual([f.invoice_id for f in terms.view.kind("invoice")], ["TX-US-2032-5394"])
        ip = price_invoice(terms, terms.view.kind("invoice")[0])
        self.assertEqual(ip.shipments[0].pickup.source, "invoice")   # the tracking export hadn't arrived


class Conflicts(Base):
    LINES = [("Air freight", "TE92942629", "USW1-USC3", "2032-06-17", "113", "kg", "3.03", "342.39"),
             ("Fuel surcharge", "TE92942629", "USW1-USC3", "2032-06-17", "342.39", None, "23.14%", "79.23")]

    def setUp(self):
        super().setUp()
        self.amendment()

    def amendment(self):
        self.w.amendment(f"{C}/AMD-1", "2032-03-16T09:00:00Z", "2032-03-16", rates=[("USW1-USC3", "100-299.5 kg", "2.88")])

    def build(self, who=FINTAN, quoted=False):
        self.w.notice("2032-06-02T09:00:00Z", who, [("USW1-USC3", "100-299.5 kg", "3.03")], "2032-06-01", quoted=quoted)
        self.w.invoice("TX-US-2032-5166", "2032-06-26T09:00:00Z", self.LINES)
        self.w.tracking("2032-06-27T09:00:00Z", [("TE92942629", "PICKED_UP", "2032-06-17", "-")])

    def test_amendment_against_authorised_notice_is_a_conflict(self):
        self.build()
        ip = self.w.priced("TX-US-2032-5166", "2032-11-30")
        frt, fuel = line_of(ip, "FRT"), line_of(ip, "FUEL")
        self.assertEqual(frt.status, "conflict")
        self.assertEqual(sorted(frt.options.values()), [D("325.44"), D("342.39")])
        self.assertEqual(sorted(fuel.options.values()), [D("75.31"), D("79.23")])
        self.assertEqual(ip.overcharge_range(), (D("0"), D("20.87")))
        self.assertTrue(any(s.fact.quote == UNRANKED for s in frt.sources))

    def test_unauthorised_notice_leaves_the_amendment_alone(self):
        self.build(who=JOAQUIN)
        ip = self.w.priced("TX-US-2032-5166", "2032-11-30")
        self.assertEqual(line_of(ip, "FRT").expected, D("325.44"))
        self.assertEqual(ip.overcharge_range(), (D("20.87"), D("20.87")))

    def test_quoted_notice_has_no_authority(self):
        self.build(quoted=True)
        self.assertEqual(self.w.priced("TX-US-2032-5166", "2032-11-30").overcharge_range()[0], D("20.87"))

    def test_outsider_notice_has_no_authority(self):
        self.build(who=("fintan.corrigan@tarrowby-billing.example", "Fintan Corrigan"))
        self.assertEqual(self.w.priced("TX-US-2032-5166", "2032-11-30").overcharge_range()[0], D("20.87"))

    def test_contract_that_ranks_amendments_first(self):
        self.w.close()
        self.w = World()
        self.w.msa(precedence=("amendment_prevails", "14.2",
                               "14.2 A signed amendment prevails over any rate notice for the same lane."))
        self.amendment()
        self.build()
        self.assertEqual(line_of(self.w.priced("TX-US-2032-5166"), "FRT").expected, D("325.44"))

    def test_feedback_resolves_the_conflict_until_a_later_amendment(self):
        self.build()
        self.w.feedback([{"item_id": "fb-02-06", "decision": "resolve_conflict",
                          "finding_key": {"invoice_id": "TX-US-2032-5166", "class": "overcharge"},
                          "fact": {"type": "precedence", "subject": {"contract_id": C, "lane": "USW1-USC3"},
                                   "value": {"use": "rate notice"}},
                          "scope": {"kind": "standing", "applies_to": {"contract_id": C, "lane": "USW1-USC3"},
                                    "valid_from": "2032-06-01", "valid_to": None}}], decided_at="2032-07-01T10:00:00Z")
        ip = self.w.priced("TX-US-2032-5166", "2032-11-30")
        frt = line_of(ip, "FRT")
        self.assertEqual((frt.status, frt.expected, frt.feedback), ("priced", D("342.39"), ["fb-02-06"]))
        self.assertEqual(self.w.priced("TX-US-2032-5166", "2032-06-30").overcharge_range(), (D("0"), D("20.87")))
        self.w.amendment(f"{C}/AMD-2", "2032-08-01T09:00:00Z", "2032-06-01", rates=[("USW1-USC3", "100-299.5 kg", "2.95")])
        self.assertEqual(line_of(self.w.priced("TX-US-2032-5166", "2032-11-30"), "FRT").status, "conflict")


class Amendments(Base):
    def test_later_amendment_wins_from_its_date(self):
        self.w.amendment(f"{C}/AMD-1", "2032-01-05T09:00:00Z", "2032-01-10", rates=[("USE2-INE2", "100-299.5 kg", "5.90")])
        self.w.amendment(f"{C}/AMD-2", "2032-02-05T09:00:00Z", "2032-02-10", rates=[("USE2-INE2", "100-299.5 kg", "5.80")])
        terms = self.w.terms()
        rate = lambda d: terms.rate(C, "USE2-INE2", d, weight=D("150"))  # noqa: E731
        self.assertEqual(rate(date(2032, 1, 5)).value, D("6.01"))
        self.assertEqual(rate(date(2032, 1, 20)).value, D("5.90"))
        self.assertEqual(rate(date(2032, 3, 1)).value, D("5.80"))
        self.assertEqual(terms.rate(C, "USE2-INE2", date(2032, 3, 1), weight=D("40")).value, D("7.73"))  # other band

    def test_unsigned_amendment_is_ignored(self):
        self.w.amendment(f"{C}/AMD-9", "2032-01-05T09:00:00Z", "2032-01-10",
                         rates=[("USE2-INE2", "100-299.5 kg", "5.00")], signed=False)
        r = self.w.terms().rate(C, "USE2-INE2", date(2032, 2, 1), weight=D("150"))
        self.assertEqual(r.value, D("6.01"))
        self.assertTrue(any("isn't signed" in n for n in r.notes))

    def test_missing_annex_names_its_holder(self):
        self.w.amendment(f"{C}/AMD-1", "2032-05-20T09:00:00Z", "2032-05-20",
                         refs=[("Annex A rev 2 (attached)", ["USS2-INN1"], "2032-06-04")])
        terms = self.w.terms()
        r = terms.rate(C, "USS2-INN1", date(2032, 6, 10), weight=D("150"))
        self.assertIsInstance(r, Missing)
        self.assertEqual(r.name, "Annex A rev 2")
        self.assertEqual((r.holder["party"], r.holder["person"], r.holder["role"]),
                         ("TBX-US", "Fintan Corrigan", "Pricing Manager"))
        self.assertIsInstance(terms.rate(C, "USS2-INN1", date(2032, 6, 1), weight=D("150")), Decided)
        self.assertIsInstance(terms.rate(C, "USE2-INE2", date(2032, 6, 10), weight=D("150")), Decided)

    def test_missing_annex_once_it_arrives(self):
        self.w.amendment(f"{C}/AMD-1", "2032-05-20T09:00:00Z", "2032-05-20",
                         refs=[("Annex A rev 2 (attached)", ["USS2-INN1"], "2032-06-04")])
        self.w.amendment(f"{C}/Annex A rev 2", "2032-07-01T09:00:00Z", "2032-06-04",
                         rates=[("USS2-INN1", "100-299.5 kg", "7.10")])
        self.assertEqual(self.w.terms().rate(C, "USS2-INN1", date(2032, 6, 10), weight=D("150")).value, D("7.10"))

    def test_cited_name(self):
        self.assertEqual(cited_name("Annex A rev 2 (attached)"), "Annex A rev 2")
        self.assertEqual(cited_name("Annex F"), "Annex F")


class FeedbackRates(Base):
    def setUp(self):
        super().setUp()
        self.w.notice("2032-01-28T09:00:00Z", JOAQUIN, [("USC3-INS1", "45-99.5 kg", "5.03")], "2032-02-01", "2032-09-25")
        self.w.notice("2032-01-29T09:00:00Z", JOAQUIN, [("USE2-INW2", "100-299.5 kg", "5.86")], "2032-02-01")
        self.w.invoice("TX-US-2032-0210", "2032-04-20T09:00:00Z", [
            ("Air freight", "TX13648356", "USC3-INS1", "2032-04-10", "91", "kg", "5.03", "457.73")])
        self.w.invoice("TX-US-2032-0259", "2032-08-25T09:00:00Z", [
            ("Air freight", "TX43529111", "USE2-INW2", "2032-08-22", "172", "kg", "5.86", "1007.92")])
        self.accept = {"item_id": "fb-01-p1", "decision": "close",
                       "finding_key": {"invoice_id": "TX-US-2032-0210", "class": "overcharge"},
                       "fact": {"type": "rate_accepted", "subject": {"contract_id": C, "lane": "USC3-INS1"},
                                "value": {"source": "rate notice", "sent_by": "Joaquin Rathbone",
                                          "effective_from": "2032-02-01", "effective_to": "2032-09-25"}},
                       "scope": {"kind": "standing", "applies_to": {"contract_id": C, "lane": "USC3-INS1"},
                                 "valid_from": "2032-02-01", "valid_to": "2032-09-25"}}

    def test_before_feedback_the_unauthorised_notice_is_ignored(self):
        self.w.feedback([self.accept])
        frt = line_of(self.w.priced("TX-US-2032-0210", "2032-05-31"), "FRT")
        self.assertEqual((frt.expected, frt.feedback), (D("428.61"), []))

    def test_rate_accepted_makes_the_notice_the_rate_for_that_lane_only(self):
        self.w.feedback([self.accept])
        frt = line_of(self.w.priced("TX-US-2032-0210", "2032-09-30"), "FRT")
        self.assertEqual((frt.expected, frt.feedback), (D("457.73"), ["fb-01-p1"]))
        other = line_of(self.w.priced("TX-US-2032-0259", "2032-09-30"), "FRT")
        self.assertEqual((other.expected, other.feedback), (D("954.60"), []))

    def test_grant_of_authority_counts_for_its_category_only(self):
        self.w.feedback([{"item_id": "fb-03-01", "decision": "confirm",
                          "finding_key": {"invoice_id": "X", "class": "overcharge"},
                          "fact": {"type": "sender_authority", "subject": {"person": "Joaquin Rathbone"}},
                          "scope": {"kind": "standing", "applies_to": {"contract_id": C, "person": "Joaquin Rathbone",
                                                                       "category": "waivers"},
                                    "valid_from": "2032-01-01", "valid_to": None}}])
        self.assertEqual(line_of(self.w.priced("TX-US-2032-0259", "2032-09-30"), "FRT").expected, D("954.60"))


# ---- fees ------------------------------------------------------------------------------------------------------
class Fees(Base):
    def bill(self, lines, rows=(), decl=None, notice=None):
        awb = "TX64694457"
        self.w.invoice("TX-US-2032-0198", "2032-03-03T09:00:00Z",
                       [(lb, awb, "USE2-INE2", "2032-02-24", q, u, r, a) for lb, q, u, r, a in lines])
        self.w.tracking("2032-03-04T09:00:00Z", [(awb, "PICKED_UP", "2032-02-24", "-"), *[(awb, *x) for x in rows]])
        if decl is not None:
            doc = self.w.email("2032-02-24T09:00:00Z", "shipping@quillmere.example", "Shipping", "DG declaration",
                               "dgd.pdf")
            self.w.put(doc, F("dg_declaration", decl, awb=awb))
        if notice is not None:
            doc = self.w.email("2032-02-28T09:00:00Z", "broker@ashgrove.example", "Broker", "Customs hold")
            self.w.put(doc, F("customs_notice", notice, awb=awb))
        return self.w.priced("TX-US-2032-0198")

    def test_dry_ice_only_does_not_attract_the_dg_fee(self):
        ip = self.bill([("Dry ice handling fee", "1", "shipment", "45.40", "45.40"),
                        ("DG handling", "1", "shipment", "124.02", "124.02")],
                       decl={"items": [{"un_number": "UN1845", "is_dry_ice": True, "refrigerant_only": True}],
                             "contains_dry_ice": True, "dry_ice_only": True, "has_dg_other_than_dry_ice": False})
        self.assertEqual(line_of(ip, "DRYICE").expected, D("45.40"))
        self.assertEqual(line_of(ip, "DG").expected, D("0.00"))
        self.assertEqual(ip.overcharge_range()[0], D("124.02"))

    def test_mixed_shipment_pays_only_the_dg_fee(self):
        ip = self.bill([("Dry ice handling fee", "1", "shipment", "45.40", "45.40"),
                        ("DG handling", "1", "shipment", "124.02", "124.02")],
                       decl={"items": [{"un_number": "UN1845", "is_dry_ice": True},
                                       {"un_number": "UN3373", "is_dry_ice": False}],
                             "contains_dry_ice": True, "dry_ice_only": False, "has_dg_other_than_dry_ice": True})
        self.assertEqual((line_of(ip, "DRYICE").expected, line_of(ip, "DG").expected), (D("0.00"), D("124.02")))

    def test_no_declaration_means_not_judged(self):
        ip = self.bill([("DG handling", "1", "shipment", "124.02", "124.02")])
        self.assertEqual(line_of(ip, "DG").status, "unpriceable")
        self.assertEqual(ip.overcharge_range(), (D("0"), D("0")))

    def test_storage_days_after_free_days(self):
        ip = self.bill([("Storage", "4", "day", "28.88", "115.52")],
                       rows=[("ARRIVED_FACILITY", "2032-02-26", "-"), ("DELIVERED", "2032-03-04", "-")])
        st = line_of(ip, "STOR")
        self.assertEqual(st.expected, D("57.76"))   # 7 - 5 = 2 days
        self.assertEqual(ip.overcharge_range()[0], D("57.76"))

    def test_collected_by_consignee_owes_no_delivery(self):
        ip = self.bill([("Door delivery", "1", "shipment", "121.81", "121.81")],
                       rows=[("COLLECTED_BY_CONSIGNEE", "2032-02-27", "-")])
        self.assertEqual(line_of(ip, "DLV").expected, D("0.00"))

    def test_delivery_without_a_record_is_not_judged(self):
        self.assertEqual(line_of(self.bill([("Delivery fee", "1", "shipment", "121.81", "121.81")]), "DLV").status,
                         "unpriceable")

    def test_customs_hold_by_carrier_error_is_on_the_carrier(self):
        ip = self.bill([("Customs hold fee", "1", "shipment", "227.14", "227.14")],
                       rows=[("CUSTOMS_HOLD", "2032-02-25", "CARRIER_DOCS_INCOMPLETE", "carrier_documents")])
        self.assertEqual(line_of(ip, "CUSTOMS").expected, D("0.00"))

    def test_customs_reason_code_alone_decides_nothing(self):
        ip = self.bill([("Customs hold fee", "1", "shipment", "227.14", "227.14")],
                       rows=[("CUSTOMS_HOLD", "2032-02-25", "CARRIER_DOCS_INCOMPLETE")])   # no cause read
        self.assertEqual(line_of(ip, "CUSTOMS").expected, D("227.14"))   # contract default: shipper's account

    def test_customs_hold_for_the_shippers_permit_is_owed(self):
        ip = self.bill([("Customs hold fee", "1", "shipment", "227.14", "227.14")],
                       notice={"hold_date": "2032-02-25", "reason_code": "SHIPPER_PERMIT_MISSING",
                               "reason": "the shipper's import permit was not provided",
                               "cause": "shipper_or_consignee"})
        self.assertEqual(line_of(ip, "CUSTOMS").expected, D("227.14"))

    def test_peak_surcharge_outside_its_months(self):
        ip = self.bill([("Air freight", "150", "kg", "6.01", "901.50"), ("PSS", "150", "kg", "0.29", "43.50")])
        self.assertEqual(line_of(ip, "PEAK").expected, D("0.00"))
        self.assertEqual(line_of(ip, "FRT").expected, D("901.50"))

    def test_minimum_charge(self):
        ip = self.bill([("Air freight", "5", "kg", "7.73", "38.65")])
        self.assertEqual(line_of(ip, "FRT").expected, D("50.00"))
        self.assertEqual(ip.overcharge_range(), (D("0"), D("0")))     # an undercharge is never a finding

    def test_dg_fee_charged_twice_on_one_shipment(self):
        ip = self.bill([("DG handling", "1", "shipment", "124.02", "124.02"),
                        ("Dangerous goods fee", "1", "shipment", "124.02", "124.02")],
                       decl={"items": [{"un_number": "UN3373", "is_dry_ice": False}], "contains_dry_ice": False,
                             "dry_ice_only": False, "has_dg_other_than_dry_ice": True})
        self.assertEqual([lp.expected for lp in ip.lines()], [D("124.02"), D("0.00")])


# ---- labels and contracts --------------------------------------------------------------------------------------
class Labels(Base):
    def setUp(self):
        super().setUp()
        self.w.invoice("TX-US-2032-0300", "2032-03-03T09:00:00Z", [
            ("Linehaul air charge", "TX1", "USE2-INE2", "2032-02-24", "150", "kg", "6.20", "930.00")])

    def test_unknown_label_is_not_judged(self):
        ip = self.w.priced("TX-US-2032-0300")
        self.assertEqual((ip.lines()[0].code, ip.lines()[0].status), (None, "not_priced"))

    def test_reconciler_maps_it_once(self):
        client = Scripted(record_charge_codes=[{"mappings": [
            {"label": "Linehaul air charge", "code": "FRT", "confidence": 0.9}]}])
        gw = Gateway(client=client)
        self.assertEqual(reconcile_labels(self.w.store, gw)["mapped"], 1)
        self.assertEqual(reconcile_labels(self.w.store, gw), {"labels": 0, "mapped": 0, "no code": 0})
        self.assertEqual(client.calls("record_charge_codes"), 1)
        lp = self.w.priced("TX-US-2032-0300").lines()[0]
        self.assertEqual((lp.code, lp.expected), ("FRT", D("901.50")))

    def test_reconciler_refuses_codes_outside_annex_e(self):
        gw = Gateway(client=Scripted(record_charge_codes=[{"mappings": [
            {"label": "Linehaul air charge", "code": "XYZ", "confidence": 0.95}]}]))
        self.assertEqual(reconcile_labels(self.w.store, gw)["no code"], 1)
        self.assertIsNone(self.w.priced("TX-US-2032-0300").lines()[0].code)

    def test_invoice_without_agreement_number(self):
        self.w.invoice("TX-US-2032-0301", "2032-03-03T09:00:00Z", [
            ("Air freight", "TX2", "USE2-INE2", "2032-02-24", "150", "kg", "6.01", "901.50")], contract=None)
        ip = self.w.priced("TX-US-2032-0301")
        self.assertEqual(ip.contract, C)
        self.assertEqual(ip.lines()[0].status, "priced")


class Money(Base):
    def test_fx_missing_for_the_month_names_treasury(self):
        terms = self.w.terms()
        r = terms.fx(C, "2032-02")
        self.assertIsInstance(r, Missing)
        self.assertEqual((r.name, r.holder["person"]), ("Annex F", "Odette Sandoval"))

    def test_fuel_month_outside_annex_c_is_undefined(self):
        self.assertIsInstance(self.w.terms().fuel(C, "2033-01"), Undefined)

    def test_precedence_from_the_contract_readers_answer(self):
        p = self.w.terms().precedence(C)
        self.assertEqual((p.rule, p.from_contract, p.source.fact.quote), ("unranked", True, UNRANKED))

    def test_precedence_not_stated_defaults_to_unranked(self):
        w = World()
        try:
            w.msa(precedence=None)
            p = w.terms().precedence(C)
            self.assertEqual((p.rule, p.from_contract, p.source), ("unranked", False, None))
        finally:
            w.close()

    def test_price_all_skips_nothing(self):
        self.w.invoice("TX-US-2032-0400", "2032-03-03T09:00:00Z", [
            ("Monthly account fee", None, None, None, "1", "month", "350.28", "350.28")])
        ip = price_all(self.w.terms())[0]
        self.assertEqual((ip.other[0].code, ip.other[0].expected), ("ACCT", D("350.28")))



class Gst(unittest.TestCase):
    def priced(self, gst, lane, tax):
        w = World()
        self.addCleanup(w.close)
        w.msa(gst=gst)
        w.invoice("TX-IN-2032-0001", "2032-03-03T09:00:00Z",
                  [("Air freight", "TX9", lane, "2032-02-24", "150", "kg", "6.01", "901.50")],
                  taxes=[{"label": "GST", "amount": tax}])
        return w.priced("TX-IN-2032-0001").tax

    def test_domestic_only_charges_domestic_lanes(self):
        RATES.append(("INN1-INS2", "100-299.5 kg", "6.01", "6.16"))
        try:
            tax = self.priced({"applies": "domestic_only", "rate_pct": "18"}, "INN1-INS2", "162.27")
        finally:
            RATES.pop()
        self.assertEqual((tax.status, tax.expected, tax.over()), ("priced", D("162.27"), D("0")))

    def test_domestic_only_on_an_export_is_zero(self):
        tax = self.priced({"applies": "domestic_only", "rate_pct": "18"}, "USE2-INE2", "162.27")
        self.assertEqual((tax.expected, tax.over()), (D("0.00"), D("162.27")))

    def test_no_rule_means_not_judged(self):
        tax = self.priced(None, "USE2-INE2", "162.27")
        self.assertEqual((tax.status, tax.over()), ("not_priced", D("0")))


if __name__ == "__main__":
    unittest.main()
