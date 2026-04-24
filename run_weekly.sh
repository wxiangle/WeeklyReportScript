#!/usr/bin/env bash
set -euo pipefail

# Always execute from script directory so relative config path works.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${SCRIPT_DIR}/.venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python 可执行文件不存在: ${PYTHON_BIN}" >&2
  echo "可通过环境变量 PYTHON_BIN 指定解释器，例如: PYTHON_BIN=/usr/bin/python3" >&2
  exit 2
fi

cd "${SCRIPT_DIR}"
exec "${PYTHON_BIN}" weekly_report.py "$@"
