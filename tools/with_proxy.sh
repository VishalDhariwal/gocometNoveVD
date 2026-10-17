#!/usr/bin/env bash
# Run any command with the harness talking to OpenAI through the local proxy (tools/llm_proxy.py), the way
# tools/live_run.sh does. For the submission files and the evals when testing with an OpenAI key:
#
#   export OPENAI_API_KEY=sk-...                     # in this shell only; never in a file in this repo
#   bash tools/with_proxy.sh make eval               # evals/results.json
#   bash tools/with_proxy.sh make grade              # report_card.txt / report_card.json
#   bash tools/with_proxy.sh make sheet              # answer_sheet.json and out/exam_report.json
#
# CACHE=on reuses the answer to an identical request from an earlier call (.state/live/cache): the eval cases share
# their contracts, so this saves most of their reading. The proxy's call log goes to .state/live/proxy-<time>/.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=python3
: "${OPENAI_API_KEY:?Set your key in this shell first: export OPENAI_API_KEY=sk-...}"
[ $# -gt 0 ] || { echo "usage: bash tools/with_proxy.sh <command ...>   e.g. make eval" >&2; exit 2; }
PORT=${PORT:-8765}
LOG=.state/live/proxy-$(date +%Y%m%d-%H%M%S)
mkdir -p "$LOG"
CACHE_OPT=""
[ "${CACHE:-off}" = on ] && CACHE_OPT="--cache-dir .state/live/cache"

"$PY" tools/llm_proxy.py --check
"$PY" tools/llm_proxy.py --port "$PORT" $CACHE_OPT --call-log "$LOG/calls.jsonl" 2>> "$LOG/proxy.log" &
PROXY=$!
trap 'kill $PROXY 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
    curl -s "http://127.0.0.1:$PORT/" > /dev/null 2>&1 && break
    sleep 0.2
done
export ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" ANTHROPIC_API_KEY=local-proxy
"$@"
