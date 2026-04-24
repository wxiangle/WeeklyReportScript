#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${SCRIPT_DIR}/.venv/bin/python}"
CRON_SCHEDULE_DEFAULT="0 18 * * 5"
INSTALL_CRON="false"
FORCE="false"

usage() {
  cat <<'EOF'
Usage: ./setup.sh [options]

Options:
  --python /path/to/python    指定 Python 解释器（默认 .venv/bin/python）
  --install-cron              安装每周五定时任务（默认不安装）
  --cron-schedule "expr"      指定 crontab 表达式，默认 "0 18 * * 5"
  --force                     已存在 config.yaml 时也覆盖
  -h, --help                  显示帮助
EOF
}

CRON_SCHEDULE="${CRON_SCHEDULE_DEFAULT}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --install-cron)
      INSTALL_CRON="true"
      shift
      ;;
    --cron-schedule)
      CRON_SCHEDULE="$2"
      shift 2
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python 可执行文件不存在: ${PYTHON_BIN}" >&2
  echo "可通过 --python 或 PYTHON_BIN 指定解释器，例如: --python /usr/bin/python3" >&2
  exit 2
fi

cd "${SCRIPT_DIR}"

echo "[1/4] 安装依赖"
"${PYTHON_BIN}" -m pip install -r requirements.txt

echo "[2/4] 生成配置文件"
if [[ -f config.yaml && "${FORCE}" != "true" ]]; then
  echo "config.yaml 已存在，跳过（使用 --force 可覆盖）"
else
  cp config.example.yaml config.yaml
  echo "已生成 config.yaml，请编辑 webhook 和 projects"
fi

echo "[3/4] 检查脚本可运行性"
"${PYTHON_BIN}" -m py_compile weekly_report.py
./run_weekly.sh --help >/dev/null

echo "[4/4] 完成初始化"

if [[ "${INSTALL_CRON}" == "true" ]]; then
  CRON_CMD="${CRON_SCHEDULE} ${SCRIPT_DIR}/run_weekly.sh --yes"
  TMP_CRON="$(mktemp)"
  crontab -l 2>/dev/null | grep -vF "${SCRIPT_DIR}/run_weekly.sh --yes" > "${TMP_CRON}" || true
  echo "${CRON_CMD}" >> "${TMP_CRON}"
  crontab "${TMP_CRON}"
  rm -f "${TMP_CRON}"
  echo "已写入 crontab: ${CRON_CMD}"
else
  echo "未安装 crontab。需要时可执行："
  echo "  crontab -e"
  echo "  ${CRON_SCHEDULE} ${SCRIPT_DIR}/run_weekly.sh --yes"
fi

echo "下一步：编辑 config.yaml 后执行 ./run_weekly.sh"
