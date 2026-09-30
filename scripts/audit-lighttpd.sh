#!/bin/sh
# Аудит миграции lighttpd -> Caddy: какие правила старого конфига НЕ перенеслись
# в caddy_site. Правило AGENTS.md требует сверки с /etc/lighttpd/lighttpd.conf,
# который остаётся на хосте: без этого на dc.fly-server.ru потерялись
# url.access-deny и static-file.exclude-extensions, и лежащий в дереве
# upload.php отдавался как 200.
#
# Использование (на ноутбуке, ключ и хост — из конфига):
#   sh scripts/audit-lighttpd.sh -c config.dc.local.toml
#
# Ничего не меняет: только читает lighttpd.conf и печатает отчёт. Код возврата
# 1 означает «есть потерянные правила» — это повод открыть caddy_site, а не
# повод бежать на хост.
set -eu

# shellcheck disable=SC2164
cd "$(dirname "$0")/.."

CFG="config.toml"
while [ "$#" -gt 0 ]; do
    case "$1" in
        -c|--config) CFG="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

if [ ! -f "$CFG" ]; then
    echo "Нет конфига $CFG (укажите -c config.<хост>.local.toml)" >&2
    exit 2
fi

# Читаем параметры подключения и список сайтов из TOML, затем тянем конфиг
# lighttpd с хоста одним каналом и печатаем отчёт. Всё через python3: в проекте
# нет внешних зависимостей, tomllib — стандартный модуль.
python3 - "$CFG" <<'PY'
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from install_vps import lighttpd
from install_vps.installer import _split_site, _static_options

cfg_path = Path(sys.argv[1])
cfg = tomllib.loads(cfg_path.read_text("utf-8"))

host = cfg.get("host", "")
user = cfg.get("user", "root")
port = str(cfg.get("port", 22))
key = cfg.get("key_path", "~").replace("~", str(Path.home()), 1)

sites = []
for raw in cfg.get("caddy_sites", []):
    domain, upstream, options = _split_site(raw)
    if upstream.startswith(("file:", "static:")):
        root = upstream.split(":", 1)[1]
        sites.append((domain, root, _static_options(options)))
    else:
        sites.append((domain, "", {}))

if not host:
    print("В конфиге нет host — нечего проверять", file=sys.stderr)
    raise SystemExit(2)

# конфиг lighttpd мог остаться в conf-enabled/*.conf, поэтому читаем все
read_script = r'''
for f in /etc/lighttpd/lighttpd.conf /etc/lighttpd/conf-enabled/*.conf; do
  [ -r "$f" ] && { echo "### $f"; cat "$f"; }
done
'''
cmd = ["ssh", "-p", port, "-i", key, "-o", "BatchMode=yes"]
if cfg.get("accept_new", True):
    cmd += ["-o", "StrictHostKeyChecking=accept-new"]
if cfg.get("sudo"):
    # sudo оборачивает bash -c, а не цикл: «sudo for f in ...» — синтаксическая
    # ошибка, и такой вызов молча ничего не читает
    read_cmd = f"sudo bash -c {shlex.quote(read_script)}"
else:
    read_cmd = "bash -s"
cmd += [f"{user}@{host}", read_cmd]

proc = subprocess.run(
    cmd,
    input=None if read_cmd != "bash -s" else read_script,
    capture_output=True,
    text=True,
    check=False,
)
if proc.returncode != 0 or not proc.stdout.strip():
    print(f"Не удалось прочитать lighttpd.conf на {host}: {proc.stderr.strip()}", file=sys.stderr)
    print("Если lighttpd уже удалён — аудит не нужен, миграция закончена.", file=sys.stderr)
    raise SystemExit(2)

rules = lighttpd.audit(proc.stdout, sites)
static = [s for s in sites if s[1]]
print(f"хост: {host} ({len(static)} сайтов-статики из {len(sites)} в caddy_sites)")
print(lighttpd.report(rules))
raise SystemExit(1 if any(r.level == lighttpd.LOST for r in rules) else 0)
PY
