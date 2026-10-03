#!/bin/sh
# Аудит миграции lighttpd -> Caddy: что из старого конфига хоста не перенесено
# в caddy_site. Ничего на хосте не меняет — только читает и печатает отчёт.
#
# Логика живёт в install_vps/audit.py, а не здесь: её покрывают тесты, и ruff
# её видит. Правила разбирает install_vps/lighttpd.py.
set -e
cd "$(dirname "$0")/.."
exec python3 -m install_vps.audit "$@"
