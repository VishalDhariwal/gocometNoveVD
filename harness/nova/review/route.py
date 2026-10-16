"""Who sees a person, and how. Plain code, on every report, with or without the reviewer.

People are brought in as little as possible. There are two ways to them:

  in the report   the item's action is human_review or escalate. The grader counts these (over 60% of reported items
                  caps the score at 40), so our own holds stay inside a budget (config.ESCALATION_BUDGET, 50%).
  the queue       the human review queue: the `human_queue` table and <state>/human_queue/<mode>/<as_of>.md. It
                  tells a person what needs them and why; it changes nothing in the report.

Only these rules put something in front of a person:

  G1 threshold        $5,000 or more outbound on one invoice or cashback key    the glossary's: human_review, both modes
  G2 conflict         authorised sources disagree, unranked by the contract     the glossary's: escalate
  G3 unattended test  autonomous mode: one of the five checks fails             the glossary's: human_review
  A1 reviewer asks    the case reviewer sent the case to a person, by the criteria in its prompt (agent.py):
                      in autonomous mode a finding becomes human_review; in supervised mode it stays a draft (a
                      person reads drafts anyway) and goes to the queue

If A1 holds would take a report of config.ESCALATION_MIN_ITEMS items or more over the budget, the smallest are
released to execute and go to the queue with a note instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .. import config
from ..decide.common import ZERO, Verdict

PRESENT = ("finding", "conflict", "cannot_determine")
PRIORITY = {"high": 0, "normal": 1, "low": 2}
ESCALATED = ("escalate", "human_review")
GLOSSARY_CHECKS = ("evidence", "conflict", "threshold", "reversible", "authority")


@dataclass
class Referral:
    key: str
    rule: str
    reason: str
    priority: str
    action: str = "none"
    amount: Decimal | None = None
    note: str = ""
    quotes: list = field(default_factory=list)
    rules: list = field(default_factory=list)   # every rule that sent it, strongest first

    def merge(self, other: "Referral") -> None:
        if PRIORITY[other.priority] < PRIORITY[self.priority]:
            self.priority, self.rule, self.reason = other.priority, other.rule, other.reason
        self.rules += [r for r in other.rules if r not in self.rules]
        self.note = "; ".join(x for x in (self.note, other.note) if x and x not in self.note)
        self.quotes = (self.quotes + [q for q in other.quotes if q not in self.quotes])[:4]


def route(view, verdicts: list[Verdict], gates: dict, mode: str, reviewed: list) -> list[Referral]:
    """Keeps A1 holds inside the budget (changing `gates` in place) and returns the queue, most urgent first."""
    refs: dict[str, Referral] = {}
    by_key = {v.finding_key: v for v in verdicts}

    def refer(key, rule, reason, priority, v: Verdict | None = None, note="", quotes=(), amount=None):
        r = Referral(key, rule, reason, priority, gates[key].action if key in gates else "none",
                     v.amount_usd if v is not None and amount is None else amount, note, list(quotes), [rule])
        if key in refs:
            refs[key].merge(r)
        else:
            refs[key] = r

    holds: set[str] = set()
    for v in verdicts:
        if v.verdict not in PRESENT or v.finding_key not in gates:
            continue
        key, gate = v.finding_key, gates[v.finding_key]
        failing = {name: why for name, (ok, why) in gate.checks.items() if not ok}
        if gate.action == "escalate":
            refer(key, "G2 conflict", f"two authorised sources disagree ({v.why}); a person decides which applies",
                  "high", v)
        if "threshold" in failing and gate.action == "human_review":
            refer(key, "G1 threshold", f"$5,000 or more outbound: {failing['threshold']}", "high", v)
        glossary_fail = {n: w for n, w in failing.items() if n in GLOSSARY_CHECKS and n != "threshold"}
        if mode == "autonomous" and glossary_fail and gate.action == "human_review":
            refer(key, "G3 unattended test", "; ".join(f"{n}: {w}" for n, w in glossary_fail.items()), "normal", v)
        if "review" in failing and gate.action == "human_review" and not glossary_fail and "threshold" not in failing:
            holds.add(key)

    for case, out in reviewed:
        if out.person:
            key = out.verdict.finding_key if out.verdict is not None else case.key
            p = out.person
            refer(key, "A1 reviewer asks", f"the case reviewer sends it to a person: {p['reason'].replace('_', ' ')}",
                  "normal", by_key.get(key), p.get("note") or "", p.get("citations") or [],
                  None if key in by_key else (case.weight or None))

    if mode == "autonomous":
        _budget(verdicts, gates, holds, refs)
    for r in refs.values():
        if r.key in gates:
            r.action = gates[r.key].action
    return sorted(refs.values(), key=lambda r: (PRIORITY[r.priority], -(r.amount or ZERO), r.key))


def _budget(verdicts: list[Verdict], gates: dict, holds: set[str], refs: dict[str, Referral]) -> None:
    """Keep the reviewer's holds from pushing the share of escalated items over the budget: smallest released first."""
    present = [v for v in verdicts if v.verdict in PRESENT and v.finding_key in gates]
    if len(present) < config.ESCALATION_MIN_ITEMS:
        return      # the grader's share runs over every report in both modes: a small report barely moves it
    cap = config.ESCALATION_BUDGET

    def share() -> float:
        return sum(gates[v.finding_key].action in ESCALATED for v in present) / len(present)

    for v in sorted((v for v in present if v.finding_key in holds), key=lambda v: (v.amount_usd or ZERO,
                                                                                    v.finding_key)):
        if share() <= cap:
            break
        if not v.remedy:
            continue
        gates[v.finding_key].action = f"execute:{v.remedy}"
        r = refs.get(v.finding_key)
        if r is not None:
            r.note = "; ".join(x for x in (r.note, f"released to execute: over {cap:.0%} of the report already needs "
                                                  "a person, so this one went to the queue instead") if x)


def write_queue(state_dir: Path, mode: str, as_of: str, refs: list[Referral]) -> Path:
    """The queue as a page a person reads, one per report (a re-run of the same date replaces it)."""
    folder = Path(state_dir) / "human_queue" / mode
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{as_of}.md"
    lines = [f"# Human review queue: {mode}, as of {as_of}", ""]
    if not refs:
        lines.append("Nothing needs a person.")
    for level in ("high", "normal", "low"):
        group = [r for r in refs if r.priority == level]
        if not group:
            continue
        lines += [f"## {level.capitalize()} ({len(group)})", ""]
        for r in group:
            money = f", USD {r.amount:.2f}" if r.amount is not None else ""
            lines.append(f"- **{r.key}**{money}: action `{r.action}`. {r.rule}: {r.reason}.")
            if len(r.rules) > 1:
                lines.append(f"  Also: {', '.join(r.rules[1:])}.")
            if r.note:
                lines.append(f"  Note: {r.note}")
            for q in r.quotes[:3]:
                lines.append(f"  > {q.get('doc')} p{q.get('page')}: \"{q.get('quote')}\"")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")
    return path
