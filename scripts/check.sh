#!/bin/sh
# Локальная проверка install-vps перед сдачей: линтер + компиляция + тесты.
set -eu
# shellcheck disable=SC2164
cd "$(dirname "$0")/.."
python3 -m ruff check install_vps tests
python3 -m compileall -q install_vps
python3 -m unittest discover -s tests
echo "OK: lint + compile + tests прошли чисто"
