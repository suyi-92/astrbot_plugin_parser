#!/usr/bin/env bash
# Isolated, disposable dependencies; never modify AstrBot or the system Python.
set -euo pipefail
command -v curl >/dev/null || { echo '需要 curl。' >&2; exit 1; }
command -v python3 >/dev/null || { echo '需要 Python 3.11+ 和 venv。' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo '需要 Python 3.11+。' >&2; exit 1; }
test_dir=$(mktemp -d -t bili-speedtest.XXXXXXXX)
trap 'rm -rf -- "$test_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Keep installer scratch files inside the directory owned by this invocation too.
mkdir "$test_dir/tmp"
export TMPDIR="$test_dir/tmp"
ref=${BILI_SPEEDTEST_REF:-main}
[[ "$ref" == main || "$ref" =~ ^[0-9a-f]{40}$ ]] || { echo '无效的工具版本。' >&2; exit 1; }
echo '准备独立测速环境（安装耗时不计入测速）；不会修改 AstrBot。'
curl --fail --silent --show-error --connect-timeout 15 --max-time 60 \
    "https://raw.githubusercontent.com/suyi-92/astrbot_plugin_parser/$ref/tools/bili_speedtest.py" \
    -o "$test_dir/probe.py"
python3 -m venv "$test_dir/venv" || { echo '缺少 venv；Debian/Ubuntu 可安装 python3-venv 后重试。' >&2; exit 1; }
"$test_dir/venv/bin/python" -m pip --disable-pip-version-check --no-cache-dir install \
    --quiet --timeout 30 --retries 1 'aiohttp>=3.10,<4' 'bilibili-api-python==17.4.2'
"$test_dir/venv/bin/python" -u "$test_dir/probe.py" "$@"
