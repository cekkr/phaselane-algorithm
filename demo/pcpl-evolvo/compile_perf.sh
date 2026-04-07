#!/usr/bin/env bash
set -euo pipefail

# Compile pcpl-evolvo Python sources to optimized bytecode for faster startup.
# Supports macOS and Linux.
#
# Usage:
#   ./compile_perf.sh
#   ./compile_perf.sh --clean
#   ./compile_perf.sh --python /path/to/python3 --skip-warmup

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN=""
CLEAN=0
WARMUP=1
INCLUDE_EVOLVO=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --clean)
      CLEAN=1
      shift
      ;;
    --skip-warmup)
      WARMUP=0
      shift
      ;;
    --skip-evolvo)
      INCLUDE_EVOLVO=0
      shift
      ;;
    -h|--help)
      sed -n '1,20p' "$0"
      exit 0
      ;;
    *)
      echo "[pcpl-evolvo] unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${SCRIPT_DIR}/venv/bin/python" ]]; then
    PYTHON_BIN="${SCRIPT_DIR}/venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[pcpl-evolvo] python not found: ${PYTHON_BIN}" >&2
  exit 1
fi

cpu_count_detect() {
  if command -v getconf >/dev/null 2>&1; then
    getconf _NPROCESSORS_ONLN 2>/dev/null && return 0
  fi
  if command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.ncpu 2>/dev/null && return 0
  fi
  echo 1
}

CORES="$(cpu_count_detect | head -n1)"
if [[ -z "${CORES}" || "${CORES}" -lt 1 ]]; then
  CORES=1
fi

if [[ "${CLEAN}" -eq 1 ]]; then
  echo "[pcpl-evolvo] cleaning previous __pycache__ directories"
  find "${SCRIPT_DIR}" -type d -name "__pycache__" -prune -exec rm -rf {} +
fi

TARGETS=(
  "${SCRIPT_DIR}/src"
  "${SCRIPT_DIR}/run_experiments.py"
  "${SCRIPT_DIR}/config.py"
)
if [[ "${INCLUDE_EVOLVO}" -eq 1 ]]; then
  TARGETS+=("${SCRIPT_DIR}/evolvo/src")
fi

echo "[pcpl-evolvo] python=${PYTHON_BIN}"
echo "[pcpl-evolvo] cores=${CORES}"
echo "[pcpl-evolvo] compiling optimized bytecode (-O1 and -O2)"

"${PYTHON_BIN}" -m compileall \
  -f \
  -q \
  -j "${CORES}" \
  -o 1 \
  -o 2 \
  "${TARGETS[@]}"

if [[ "${WARMUP}" -eq 1 ]]; then
  echo "[pcpl-evolvo] warming runtime import/cache path"
  PYTHONOPTIMIZE=2 "${PYTHON_BIN}" "${SCRIPT_DIR}/run_experiments.py" \
    --mode dynamic \
    --profile fast \
    --print-effective-config >/dev/null
fi

echo "[pcpl-evolvo] compile completed"
