#!/bin/sh
# Локальная проверка install-vps перед сдачей: линтер + компиляция.
set -eu
# shellcheck disable=SC2164
cd "$(dirname "$0")/.."
python3 -m ruff check install_vps
python3 -m compileall -q install_vps
echo "OK: lint + compile прошли чисто"
