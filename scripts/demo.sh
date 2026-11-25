#!/usr/bin/env bash
# One command, live or replay.
#
#   ./scripts/demo.sh                                   # replay the bundled trace: no engine, no GPU
#   SIDEBAND_ENDPOINT=http://127.0.0.1:8000/v1 \
#   SIDEBAND_MAX_NUM_SEQS=8 ./scripts/demo.sh           # measure your own engine
#
# Writes channel_budget.json and channel_budget.md in the repo root.
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v sideband >/dev/null 2>&1; then
    sb() { sideband "$@"; }
else
    sb() { "${PYTHON:-python3}" -m sideband.cli "$@"; }
fi

if [ -n "${SIDEBAND_ENDPOINT:-}" ]; then
    model="${SIDEBAND_MODEL:-Qwen/Qwen2.5-0.5B-Instruct}"
    seqs="${SIDEBAND_MAX_NUM_SEQS:-8}"
    echo "# measuring ${SIDEBAND_ENDPOINT}"
    args=(measure
        --endpoint "$SIDEBAND_ENDPOINT"
        --model "$model"
        --seconds "${SIDEBAND_SECONDS:-60}"
        --declare-max-num-seqs "$seqs"
        --launch-cmd "${SIDEBAND_LAUNCH_CMD:-vllm serve $model --max-num-seqs $seqs}"
        --out .)
    if [ -n "${SIDEBAND_TRACE_OUT:-}" ]; then
        args+=(--trace-out "$SIDEBAND_TRACE_OUT")
    fi
    sb "${args[@]}"
else
    echo "# no SIDEBAND_ENDPOINT set: replaying a recorded trace, no engine and no GPU"
    sb replay examples/trace-vllm-qwen05b.jsonl --out .
fi

echo
echo "# channel_budget.md"
cat channel_budget.md
