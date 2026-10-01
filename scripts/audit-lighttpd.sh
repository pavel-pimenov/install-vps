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
# нет внешних зависимостей; конфиг читается тем же load_config, что и в cli.
python3 - "$CFG" <<'PY'
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from install_vps import lighttpd
from install_vps.config import load_config
from install_vps.installer import _split_site, _static_options

# Через load_config, а не tomllib: он же раскрывает `~` в key_path. Раньше тут
# стояло tomllib + ручной .replace("~", home), и дефолт "~" превращался в
# путь "/Users/admin" — ssh падал с «Load key: bad permissions», и аудит молча
# выглядел как «lighttpd уже удалён».
cfg = load_config(Path(sys.argv[1]))

host = cfg.host
user = cfg.user
port = str(cfg.port)
key = cfg.key_path

sites = []
for raw in cfg.caddy_sites:
    domain, upstream, options = _split_site(raw)
    if upstream.startswith(("file:", "static:")):
        root = upstream.split(":", 1)[1]
        sites.append((domain, root, _static_options(options)))
    else:
        sites.append((domain, "", {}))

if not host:
    print("В конфиге нет host — нечего проверять", file=sys.stderr)
    raise SystemExit(2)

# конфиг lighttpd мог остаться в conf-enabled/*.conf, поэтому читаем все.
# Скрипт различает три исхода явными метками, потому что «не получилось
# прочитать» и «lighttpd уже удалён» — это противоположные новости, а раньше
# обе печатались одинаково, и сломанный sudo читался как успешная миграция.
read_script = r'''
found=0
unreadable=0
for f in /etc/lighttpd/lighttpd.conf /etc/lighttpd/conf-enabled/*.conf; do
  [ -e "$f" ] || continue
  found=1
  if [ -r "$f" ]; then
    echo "### FILE $f"
    cat "$f"
  else
    unreadable=1
  fi
done
[ "$found" = 0 ] && echo "### LIGHTTPD_ABSENT"
[ "$unreadable" = 1 ] && echo "### LIGHTTPD_UNREADABLE"
exit 0
'''
cmd = ["ssh", "-p", port, "-i", key, "-o", "BatchMode=yes"]
if cfg.accept_new:
    cmd += ["-o", "StrictHostKeyChecking=accept-new"]
use_stdin = not cfg.sudo
if cfg.sudo:
    # sudo оборачивает bash -c, а не цикл: «sudo for f in ...» — синтаксическая
    # ошибка, и такой вызов молча ничего не читает
    read_cmd = f"sudo bash -c {shlex.quote(read_script)}"
else:
    read_cmd = "bash -s"
cmd += [f"{user}@{host}", read_cmd]

proc = subprocess.run(
    cmd,
    input=read_script if use_stdin else None,
    capture_output=True,
    text=True,
    check=False,
)
out = proc.stdout

# 1. сам ssh или sudo не сработал — это поломка доступа, а не итог миграции
if proc.returncode != 0:
    hint = ""
    if cfg.sudo and "sudo" in proc.stderr.lower():
        hint = "\nПохоже, на хосте нет sudo -n без пароля (NOPASSWD)."
    print(f"Не удалось выполнить команду на {host}: {proc.stderr.strip()}", file=sys.stderr)
    if hint:
        print(hint.strip(), file=sys.stderr)
    else:
        print("Миграция lighttpd на этом хосте ещё не завершена — разбираться нужно.", file=sys.stderr)
    raise SystemExit(2)

# 2. файлов нет вообще: lighttpd снят, сверять нечего — это успех, а не ошибка
if "### LIGHTTPD_ABSENT" in out:
    print(f"на {host}: lighttpd.conf не найден — lighttpd удалён, аудит не нужен.")
    raise SystemExit(0)

# 3. файлы есть, но не прочитались: почти всегда виноват sudo, а не миграция
if "### LIGHTTPD_UNREADABLE" in out:
    print(
        f"на {host}: файлы lighttpd есть, но они не прочитались — "
        f"проверьте sudo -n (NOPASSWD) для пользователя {user}.",
        file=sys.stderr,
    )
    raise SystemExit(2)

# 4. остался пустой вывод при нулевом коде — тоже тишина, а не «всё хорошо»
if not out.strip():
    print(f"на {host}: команда отработала, но не вывела ничего — нечего сверять.", file=sys.stderr)
    raise SystemExit(2)

rules = lighttpd.audit(out, sites)
static = [s for s in sites if s[1]]
print(f"хост: {host} ({len(static)} сайтов-статики из {len(sites)} в caddy_sites)")
print(lighttpd.report(rules))
raise SystemExit(1 if any(r.level == lighttpd.LOST for r in rules) else 0)
PY
