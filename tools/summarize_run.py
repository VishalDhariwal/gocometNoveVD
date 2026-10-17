"""The numbers for one test run made by tools/live_run.sh: time per step, per email, per document and per reviewer
case; API calls, latency, tokens and cost; what the harness read and reported; and, for practice data, the grader's
own score card for the batches that were run.

    python tools/summarize_run.py .state/live/<run> --data data/scenarios

Reads <run>/timings.jsonl (when each step started and ended), <run>/calls.jsonl (the proxy's log, one line per API
request), <run>/state/nova.db and <run>/<batch>/report_<mode>.json. Writes <run>/score.json when the data has answers.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# USD per 1M tokens: input, cached input, output (openai.com/api/pricing; checked October 2026). NOVA_PRICE="in,cached,out"
# overrides them for a model not listed here.
PRICES = {"gpt-4o-mini": (0.15, 0.075, 0.60), "gpt-4.1-mini": (0.40, 0.10, 1.60)}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.is_file() else []


def _mmss(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m{s % 60:02d}s"


def _stats(xs: list[float]) -> str:
    if not xs:
        return "-"
    xs = sorted(xs)
    p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
    return f"avg {statistics.mean(xs):.1f} s, median {statistics.median(xs):.1f} s, p95 {p95:.1f} s, max {xs[-1]:.1f} s"


def _price(model: str) -> tuple[float, float, float] | None:
    env = os.environ.get("NOVA_PRICE")
    if env:
        a, b, c = (float(x) for x in env.split(","))
        return a, b, c
    return PRICES.get(model)


def _cost(rows: list[dict]) -> float | None:
    total = 0.0
    for r in rows:
        if r.get("status") != "ok":
            continue
        p = _price(r.get("model", ""))
        if p is None:
            return None
        cached = r.get("cached_input", 0)
        total += ((r.get("input", 0) - cached) * p[0] + cached * p[1] + r.get("output", 0) * p[2]) / 1e6
    return total


class Run:
    def __init__(self, run: Path):
        self.dir = run
        self.steps = _load_jsonl(run / "timings.jsonl")
        self.calls = _load_jsonl(run / "calls.jsonl")
        db = run / "state" / "nova.db"
        self.con = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if db.is_file() else None

    def q(self, sql: str, *args) -> list:
        return self.con.execute(sql, args).fetchall() if self.con else []

    def has_column(self, table: str, column: str) -> bool:
        return any(r[1] == column for r in self.q(f"PRAGMA table_info({table})"))

    def calls_in(self, st: dict) -> list[dict]:
        return [c for c in self.calls if st["start"] <= c["t"] <= st["end"]]   # steps run one after another


def time_section(r: Run) -> list[str]:
    out = ["TIME"]
    if not r.steps:
        return out + ["  no timings"]
    out.append(f"  {'step':34}{'wall':>8}{'API calls':>11}{'input':>11}{'output':>10}")
    for st in r.steps:
        calls = [c for c in r.calls_in(st) if c["status"] == "ok"]
        name = f"{st['batch']} {st['step']}" + (f" {st['mode']}" if st["mode"] != "-" else "")
        out.append(f"  {name:34}{_mmss(st['end'] - st['start']):>8}{len(calls):>11}"
                   f"{sum(c['input'] for c in calls):>11,}{sum(c['output'] for c in calls):>10,}"
                   + ("" if st["rc"] == 0 else f"   exited {st['rc']}"))
    wall = sum(st["end"] - st["start"] for st in r.steps)
    out.append(f"  {'total':34}{_mmss(wall):>8}")
    slow = [st for st in r.steps if st["end"] - st["start"] > 600]
    if slow:
        out.append(f"  ! {len(slow)} step(s) took over 600 s, the grader's limit per step: it would have stopped them")

    ingest = [st for st in r.steps if st["step"] == "ingest"]
    emails = r.q("SELECT COUNT(*) FROM documents WHERE format = 'eml'")
    n_emails = emails[0][0] if emails else 0
    if ingest and n_emails:
        t = sum(st["end"] - st["start"] for st in ingest)
        out.append(f"  per email: {t / n_emails:.1f} s of ingest wall time ({n_emails} emails, read in parallel)")
    if r.has_column("extractions", "seconds"):
        rows = r.q("SELECT e.reader, e.seconds, e.input_tokens, e.output_tokens FROM extractions e "
                   "WHERE e.seconds IS NOT NULL")
        if rows:
            out.append(f"  per document read ({len(rows)} reads; each one's own time, queueing for the API included): "
                       f"{_stats([x[1] for x in rows])}")
            by = collections.defaultdict(list)
            for reader, s, ti, to in rows:
                by[reader].append((s, ti, to))
            out.append(f"    {'reader':16}{'docs':>6}{'avg s':>8}{'p95 s':>8}{'avg in':>9}{'avg out':>9}")
            for reader, xs in sorted(by.items(), key=lambda x: -len(x[1])):
                ss = sorted(x[0] for x in xs)
                p95 = ss[min(len(ss) - 1, int(round(0.95 * (len(ss) - 1))))]
                out.append(f"    {reader:16}{len(xs):6}{statistics.mean(ss):8.1f}{p95:8.1f}"
                           f"{statistics.mean(x[1] for x in xs):9,.0f}{statistics.mean(x[2] for x in xs):9,.0f}")
    if r.has_column("reviews", "seconds"):
        rows = r.q("SELECT reason, outcome, turns, seconds, input_tokens, output_tokens FROM reviews")
        if rows:
            secs = [x[3] for x in rows if x[3] is not None]
            out.append(f"  per reviewer case ({len(rows)} cases): {_stats(secs)}; "
                       f"avg {statistics.mean(x[2] for x in rows):.1f} turns, "
                       f"{statistics.mean(x[4] for x in rows):,.0f} in / {statistics.mean(x[5] for x in rows):,.0f} "
                       f"out tokens")
            reasons = collections.Counter(x[0] for x in rows)
            outcomes = collections.Counter(x[1] for x in rows)
            out.append("    why: " + ", ".join(f"{n} {k}" for k, n in reasons.most_common()))
            out.append("    outcome: " + ", ".join(f"{n} {k}" for k, n in outcomes.most_common()))
        else:
            out.append("  reviewer: no cases")
    return out


def api_section(r: Run) -> list[str]:
    out = ["API (the proxy's log)"]
    if not r.calls:
        return out + ["  no calls logged"]
    status = collections.Counter(c["status"] for c in r.calls)
    ok = [c for c in r.calls if c["status"] == "ok"]
    out.append(f"  requests: {len(r.calls)} (" + ", ".join(f"{n} {s}" for s, n in status.most_common()) + ")"
               f"; retries {sum(c.get('retries', 0) for c in r.calls)}, unusable tool calls asked again "
               f"{sum(c.get('bad_calls', 0) for c in r.calls)}")
    out.append(f"  latency per request: {_stats([c['api_seconds'] for c in ok])}")
    queued = sum(c.get("queued", 0) for c in r.calls)
    out.append(f"  waiting for rate limits: {queued:,.0f} s in all (summed over requests)")
    out.append(f"  {'tool the model had to call':30}{'calls':>6}{'input':>10}{'cached':>9}{'output':>9}"
               f"{'avg s':>7}{'p95 s':>7}{'failed':>7}")
    by = collections.defaultdict(list)
    for c in r.calls:
        by[c.get("tool") or "-"].append(c)
    for tool, cs in sorted(by.items(), key=lambda x: -sum(c.get("input", 0) for c in x[1])):
        good = [c for c in cs if c["status"] == "ok"]
        lat = sorted(c["api_seconds"] for c in good) or [0.0]
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        out.append(f"  {tool:30}{len(cs):6}{sum(c.get('input', 0) for c in good):10,}"
                   f"{sum(c.get('cached_input', 0) for c in good):9,}{sum(c.get('output', 0) for c in good):9,}"
                   f"{statistics.mean(lat):7.1f}{p95:7.1f}{sum(c['status'] not in ('ok', 'cached') for c in cs):7}")
    for c in [c for c in r.calls if c["status"] not in ("ok", "cached")][:8]:
        out.append(f"  ! {c['status']} ({c.get('tool')}): {c.get('error', '')[:160]}")
    return out


def cost_section(r: Run) -> list[str]:
    ok = [c for c in r.calls if c["status"] == "ok"]
    models = sorted({c["model"] for c in ok})
    tin, tcached = sum(c["input"] for c in ok), sum(c.get("cached_input", 0) for c in ok)
    tout = sum(c["output"] for c in ok)
    out = [f"TOKENS AND COST ({', '.join(models) or 'no model'})",
           f"  input {tin:,} tokens ({tcached:,} of them cached), output {tout:,} tokens"]
    cost = _cost(ok)
    if cost is None:
        out.append("  cost: no price for this model; set NOVA_PRICE=\"in,cached,out\" (USD per 1M tokens)")
        return out
    docs = r.q("SELECT COUNT(*) FROM documents")
    emails = r.q("SELECT COUNT(*) FROM documents WHERE format = 'eml'")
    n_docs, n_emails = (docs[0][0] if docs else 0), (emails[0][0] if emails else 0)
    out.append(f"  cost: ${cost:.4f}" + (f"; ${cost / n_emails:.5f} per email, ${cost / n_docs:.5f} per document, "
                                          f"${1000 * cost / n_docs:.2f} per 1,000 documents" if n_docs and n_emails else ""))
    for p in models:
        pr = _price(p)
        out.append(f"  ({p}: ${pr[0]} input, ${pr[1]} cached input, ${pr[2]} output per 1M tokens)")
    return out


def harness_section(r: Run) -> list[str]:
    out = ["HARNESS (the state store)"]
    if r.con is None:
        return out + ["  no state store"]
    out.append("  documents: " + ", ".join(f"{n} {f} {s}" for f, s, n in
                                           r.q("SELECT format, status, COUNT(*) FROM documents GROUP BY 1, 2")))
    out.append("  attachment types: " + ", ".join(f"{t} {n}" for t, n in r.q(
        "SELECT COALESCE(doc_type, '?'), COUNT(*) FROM documents WHERE format != 'eml' GROUP BY 1 ORDER BY 2 DESC")))
    out.append("  reads: " + ", ".join(f"{rd} {s} {n}" for rd, s, n in
                                       r.q("SELECT reader, status, COUNT(*) FROM extractions GROUP BY 1, 2")))
    out.append("  facts: " + ", ".join(f"{k} {n}" for k, n in
                                       r.q("SELECT kind, COUNT(*) FROM facts GROUP BY 1 ORDER BY 2 DESC")))
    for cmd, st, calls, ti, to, err in r.q("SELECT command, status, api_calls, input_tokens, output_tokens, error "
                                            "FROM runs ORDER BY id"):
        out.append(f"  run {cmd}: {st}, {calls} API calls, {ti:,} in / {to:,} out" + (f" ({err[:120]})" if err else ""))
    for ref, reader, err in r.q("SELECT d.doc_ref, e.reader, e.error FROM extractions e JOIN documents d "
                                "ON d.id = e.doc_id WHERE e.status = 'failed' ORDER BY d.doc_ref")[:12]:
        out.append(f"  ! failed {reader} {ref}: {(err or '')[:150]}")
    unread = r.q("SELECT doc_ref FROM documents WHERE id NOT IN (SELECT doc_id FROM extractions) "
                 "AND status NOT IN ('skipped')")
    if unread:
        out.append(f"  ! {len(unread)} document(s) never read: " + ", ".join(x[0] for x in unread[:8]))
    warn = r.q("SELECT level, COUNT(*) FROM run_log GROUP BY 1")
    if warn:
        out.append("  run log: " + ", ".join(f"{n} {lv}" for lv, n in warn))
    return out


def reports(r: Run) -> dict[tuple[str, str], dict]:
    found = {}
    for st in r.steps:
        if st["step"] != "report":
            continue
        f = r.dir / st["batch"] / f"report_{st['mode']}.json"
        try:
            found[(st["batch"], st["mode"])] = json.loads(f.read_text())
        except (OSError, ValueError):
            found[(st["batch"], st["mode"])] = None
    return found


def report_section(reps: dict) -> list[str]:
    from grader import score as S
    out = ["REPORTS"]
    for (batch, mode), rep in reps.items():
        if rep is None:
            out.append(f"  {batch} {mode}: no report (see its log)")
            continue
        items = S.report_items(rep)
        c = collections.Counter((S.key_class(it), it.get("verdict")) for it in items)
        acts = collections.Counter(str(it.get("action")) for it in rep.get("findings") or [])
        people = sum(1 for it in items if it.get("human"))
        out.append(f"  {batch} {mode}: {len(items)} item(s): " + ", ".join(f"{n} {k} {v}" for (k, v), n in sorted(c.items())))
        out.append("    actions: " + (", ".join(f"{n} {a}" for a, n in acts.most_common()) or "-")
                   + (f"; {people} sent to a person" if people else ""))
    return out


def score_section(r: Run, data: Path, reps: dict, limited: bool) -> list[str]:
    from grader import score as S
    answers = data / "answers"
    if not answers.is_dir() or not reps:
        return []
    out = ["SCORE (the grader's own scorer, the batches run only)"]
    if limited:
        out.append("  (LIMIT was set: not every email was read, so the score is not meaningful)")
    batches = list(dict.fromkeys(b for b, _ in reps))
    key = copy.deepcopy(S.load_key(str(answers), data))
    key["worlds"] = {w: [(b, a) for b, a in bs if b in batches] for w, bs in key["worlds"].items()}
    key["worlds"] = {w: bs for w, bs in key["worlds"].items() if bs}
    for s in key["scenarios"]:
        s["by_batch"] = {b: e for b, e in s["by_batch"].items() if b in batches}
    key["scenarios"] = [s for s in key["scenarios"] if s["by_batch"]]
    runs, feedback = {}, {}
    for w, bs in key["worlds"].items():
        for b, as_of in bs:
            for m in sorted({m for bb, m in reps if bb == b}):
                runs.setdefault(m, {}).setdefault(w, []).append(
                    {"batch": b, "as_of": as_of, "report": reps.get((b, m)), "error": None, "steps": []})
            fb = data / "feedback" / f"{w}_{b}.json"
            if fb.is_file() and any(st["step"] == "feedback" and st["batch"] == b for st in r.steps):
                feedback.setdefault(w, {})[b] = json.loads(fb.read_text())
    card = S.score(key, runs, {w: data for w in key["worlds"]}, feedback, None)
    (r.dir / "score.json").write_text(json.dumps(card, indent=1, ensure_ascii=False, default=str) + "\n")
    return out + ["  " + x for x in S.page(card).splitlines()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", help="the run folder, .state/live/<run>")
    ap.add_argument("--data", required=True, help="the data the run used (for the answers and the quote checks)")
    ap.add_argument("--limit", type=int, default=0, help="the LIMIT the run used, if any")
    a = ap.parse_args(argv)
    r = Run(Path(a.run))
    reps = reports(r)
    lines = [f"RUN {r.dir}", ""]
    for section in (time_section(r), api_section(r), cost_section(r), harness_section(r), report_section(reps)):
        lines += section + [""]
    try:
        lines += score_section(r, Path(a.data), reps, a.limit > 0)
    except Exception as e:  # noqa: BLE001 - the summary still prints without the score
        lines.append(f"SCORE: couldn't score: {type(e).__name__}: {e}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
