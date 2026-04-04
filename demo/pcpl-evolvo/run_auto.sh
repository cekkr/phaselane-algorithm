#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${SCRIPT_DIR}/runs/${STAMP}-auto"

mkdir -p "${OUT_DIR}"

echo "[pcpl-evolvo] auto run output: ${OUT_DIR}"
python3 "${SCRIPT_DIR}/run_experiments.py" --out-dir "${OUT_DIR}" "$@" | tee "${OUT_DIR}/console.log"
