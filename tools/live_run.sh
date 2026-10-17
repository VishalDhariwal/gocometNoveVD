#!/usr/bin/env bash
# A timed test run on OpenAI. The harness runs unchanged behind the local proxy (tools/llm_proxy.py), batch by batch
# as the grader replays them: ingest, report, then that batch's feedback. Every API call is logged, and
# tools/summarize_run.py prints time, tokens, cost, per-email / per-document / per-case averages and the grader's own
# score for the batches run.
#
#   export OPENAI_API_KEY=sk-...                  # in this shell only; never in a file in this repo
#   bash tools/live_run.sh                         # practice batch_01, supervised
#   BATCHES="batch_01 batch_02" MODE=both bash tools/live_run.sh      # the whole practice replay
#
# Settings (environment):
#   DATA=data/scenarios | data/exam     BATCHES="batch_01 ..."     MODE=supervised | autonomous | both
#   OPENAI_MODEL=gpt-4o-mini            OPENAI_STRONG_MODEL=...    (contract and scan reads; default: the same)
#   LIMIT=N         only the first N emails of each batch (a quick smoke test; the score then means nothing)
#   CACHE=on        reuse answers from earlier runs (cheap re-runs; the times then mean nothing)
#   RESUME=.state/live/<run>   carry on in that run's state (after a quota stop, or with the next batch)
# The harness keeps its own default settings (workers, time budgets), as under the grader. One difference: with
# MODE=both the two reports share one ingest, where the grader ingests again for each mode.
# Everything lands in .state/live/<run>/ (git-ignored); the summary is <run>/summary.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=python3
: "${OPENAI_API_KEY:?Set your key in this shell first: export OPENAI_API_KEY=sk-...}"
DATA=${DATA:-data/scenarios}
BATCHES=${BATCHES:-batch_01}
MODE=${MODE:-supervised}
PORT=${PORT:-8765}
LIMIT=${LIMIT:-0}
RUN=${RESUME:-.state/live/$(basename "$DATA")-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$RUN/mailbox"
CACHE_OPT=""
[ "${CACHE:-off}" = on ] && CACHE_OPT="--cache-dir .state/live/cache"
modes=$MODE
[ "$MODE" = both ] && modes="supervised autonomous"
now() { "$PY" -c 'import time; print(round(time.time(), 3))'; }

echo "== checking the key and the model"
"$PY" tools/llm_proxy.py --check

"$PY" tools/llm_proxy.py --port "$PORT" $CACHE_OPT --call-log "$RUN/calls.jsonl" 2>> "$RUN/proxy.log" &
PROXY=$!
trap 'kill $PROXY 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
    curl -s "http://127.0.0.1:$PORT/" > /dev/null 2>&1 && break
    sleep 0.2
done
export ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ANTHROPIC_API_KEY=local-proxy

# step <name> <batch> <mode> <stdout file> <stderr log> <command...>: run it and record when it started and ended
step() {
    local name=$1 batch=$2 mode=$3 out=$4 err=$5 t0 t1 rc=0
    shift 5
    t0=$(now)
    "$@" > "$out" 2>> "$err" || rc=$?
    t1=$(now)
    printf '{"step": "%s", "batch": "%s", "mode": "%s", "start": %s, "end": %s, "rc": %s}\n' \
        "$name" "$batch" "$mode" "$t0" "$t1" "$rc" >> "$RUN/timings.jsonl"
    [ "$rc" = 0 ] || echo "   $name exited with $rc; see $err"
}

for BATCH in $BATCHES; do
    [ -d "$DATA/$BATCH" ] || { echo "no batch folder $DATA/$BATCH"; exit 2; }
    AS_OF=$("$PY" -c 'import json,sys; print(next(b["as_of"] for b in json.load(open(sys.argv[1]))["batches"] if b["batch"] == sys.argv[2]))' "$DATA/batches.json" "$BATCH")
    OUT="$RUN/$BATCH"
    mkdir -p "$OUT"
    if [ ! -d "$RUN/mailbox/$BATCH" ]; then
        if [ "$LIMIT" -gt 0 ]; then
            mkdir -p "$RUN/mailbox/$BATCH"
            for f in "$DATA/$BATCH"/*; do
                case "$f" in *.eml) ;; *) cp "$f" "$RUN/mailbox/$BATCH/" ;; esac
            done
            ls "$DATA/$BATCH"/*.eml | sort | head -n "$LIMIT" | while read -r f; do cp "$f" "$RUN/mailbox/$BATCH/"; done
        else
            cp -R "$DATA/$BATCH" "$RUN/mailbox/"
        fi
    fi
    echo "== $BATCH: ingest   (follow it: tail -f $OUT/ingest.log)"
    step ingest "$BATCH" - /dev/null "$OUT/ingest.log" \
        "$PY" harness/harness.py ingest "$RUN/mailbox/$BATCH" --state-dir "$RUN/state"
    for m in $modes; do
        echo "== $BATCH: report $m as of $AS_OF"
        step report "$BATCH" "$m" "$OUT/report_$m.json" "$OUT/report_$m.log" \
            "$PY" harness/harness.py report --as-of "$AS_OF" --mode "$m" --state-dir "$RUN/state"
    done
    # As the grader does: a batch's feedback goes in after its report, ready for the next batch.
    for fb in "$DATA"/feedback/*_"$BATCH".json; do
        [ -f "$fb" ] || continue
        mkdir -p "$RUN/mailbox/feedback" && cp "$fb" "$RUN/mailbox/feedback/"
        step feedback "$BATCH" - /dev/null "$OUT/feedback.log" \
            "$PY" harness/harness.py feedback "$RUN/mailbox/feedback/$(basename "$fb")" --state-dir "$RUN/state"
    done
done

kill $PROXY 2>/dev/null || true
echo "== summary"
"$PY" tools/summarize_run.py "$RUN" --data "$DATA" ${LIMIT:+--limit "$LIMIT"} | tee "$RUN/summary.txt"
echo
echo "Run folder: $RUN"
