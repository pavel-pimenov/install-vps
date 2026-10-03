"""Команда `audit-lighttpd`: сверка старого конфига lighttpd с `caddy_site`.

Раньше вся эта логика жила heredoc'ом внутри `scripts/audit-lighttpd.sh`:
114 строк python, которые ruff не видел и тесты не доставали. Именно там
и спряталась поломка, о которой говорит AGENTS.md: «не получилось
прочитать» и «lighttpd уже удалён» печатались одинаково, и сломанный sudo
читался как успешная миграция. Теперь исход разбирается в функциях, которые
покрыты тестами, а скрипт — тонкая обёртка над `python3 -m install_vps.audit`.

Сам разбор правил остаётся в `lighttpd.py`: этот модуль только ходит по SSH
и печатает отчёт.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from . import lighttpd
from .config import Config, load_config
from .installer import _split_site, _static_options

# Метки разбора удалённого вывода. Скрипт различает исходы явными метками,
# потому что «не получилось прочитать» и «lighttpd уже удалён» — это
# противоположные новости.
ABSENT = "### LIGHTTPD_ABSENT"
UNREADABLE = "### LIGHTTPD_UNREADABLE"

# Читаем и основной конфиг, и подключаемые. Оба остаются на хосте после
# переезда, и в conf-enabled лежат правила, которых нет в основном файле
# (alias /javascript, точечный index-file.names, accesslog).
READ_SCRIPT = r"""
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
"""

# Исходы разбора
OK, ABSENT_OUT, UNREADABLE_OUT, SSH_FAILED, EMPTY = (
    "ok", "absent", "unreadable", "ssh-failed", "empty"
)

# Коды возврата — контракт скрипта, на него завязан CI и привычка «1 = потери».
EXIT_OK = 0
EXIT_LOST = 1     # конфиг прочитан, но что-то не перенесено
EXIT_ERROR = 2    # не смогли прочитать конфиг или плохой вызов


def sites_from_config(cfg: Config) -> list[tuple[str, str, dict]]:
    """Сайты из `caddy_sites` в виде, который ждёт `lighttpd.audit`."""
    sites = []
    for raw in cfg.caddy_sites or []:
        domain, upstream, options = _split_site(raw)
        if upstream.startswith(("file:", "static:")):
            sites.append((domain, upstream.split(":", 1)[1], _static_options(options)))
        else:
            sites.append((domain, "", {}))
    return sites


def ssh_argv(cfg: Config, script: str) -> tuple[list[str], str | None]:
    """Команда чтения конфига: argv и, при sudo, текст для stdin.

    sudo оборачивает `bash -c`, а не цикл: «sudo for f in ...» — синтаксическая
    ошибка, и такой вызов молча ничего не читает.
    """
    argv = ["ssh", "-p", str(cfg.port), "-i", cfg.key_path, "-o", "BatchMode=yes"]
    if cfg.accept_new:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if cfg.sudo:
        return argv + [f"{cfg.user}@{cfg.host}", "sudo bash -c " + shlex.quote(script)], None
    return argv + [f"{cfg.user}@{cfg.host}", "bash -s"], script


def classify(out: str, returncode: int, stderr: str, cfg: Config) -> tuple[str, str]:
    """Что означает ответ хоста: (исход, текст для stderr/stdout).

    Порядок важен: сначала сам ssh, потом «файлов нет», потом «не прочитались».
    Сломанный sudo не должен читаться как выполненная миграция.
    """
    if returncode != 0:
        hint = ""
        if cfg.sudo and "sudo" in stderr.lower():
            hint = "\nПохоже, на хосте нет sudo -n без пароля (NOPASSWD)."
        text = f"Не удалось выполнить команду на {cfg.host}: {stderr.strip()}"
        if hint:
            text += "\n" + hint.strip()
        else:
            text += "\nМиграция lighttpd на этом хосте ещё не завершена — разбираться нужно."
        return SSH_FAILED, text
    if ABSENT in out:
        return ABSENT_OUT, (f"на {cfg.host}: lighttpd.conf не найден — "
                           "lighttpd удалён, аудит не нужен.")
    if UNREADABLE in out:
        return UNREADABLE_OUT, (
            f"на {cfg.host}: файлы lighttpd есть, но они не прочитались — "
            f"проверьте sudo -n (NOPASSWD) для пользователя {cfg.user}."
        )
    if not out.strip():
        return EMPTY, (f"на {cfg.host}: команда отработала, но не вывела ничего — "
                       "нечего сверять.")
    return OK, ""


def run_audit(cfg: Config) -> tuple[int, str]:
    """Чтение конфига с хоста и печать отчёта. Возвращает (код, вывод)."""
    if not cfg.host:
        return EXIT_ERROR, "В конфиге нет host — нечего проверять"

    argv, stdin = ssh_argv(cfg, READ_SCRIPT)
    proc = subprocess.run(argv, input=stdin, capture_output=True,
                          text=True, check=False)

    outcome, message = classify(proc.stdout, proc.returncode, proc.stderr, cfg)
    if outcome == OK:
        sites = sites_from_config(cfg)
        static = [s for s in sites if s[1]]
        rules = lighttpd.audit(proc.stdout, sites)
        lines = [f"хост: {cfg.host} ({len(static)} сайтов-статики из {len(sites)} "
                 f"в caddy_sites)", lighttpd.report(rules)]
        lost = any(r.level == lighttpd.LOST for r in rules)
        return (EXIT_LOST if lost else EXIT_OK), "\n".join(lines)
    if outcome == ABSENT_OUT:
        return EXIT_OK, message
    return EXIT_ERROR, message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit-lighttpd",
        description="Аудит миграции lighttpd -> Caddy: что из старого конфига "
                    "не перенесено в caddy_site. Ничего не меняет.",
        epilog="Код возврата 1 означает «есть потерянные правила» — это повод "
               "открыть caddy_site, а не повод бежать на хост.",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"),
                        help="конфиг хоста (config.<хост>.local.toml)")
    args = parser.parse_args(argv)

    if not args.config.is_file():
        return _fail(f"Нет конфига {args.config} (укажите -c config.<хост>.local.toml)")
    return _run(load_config(args.config))


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _run(cfg: Config) -> int:
    code, output = run_audit(cfg)
    if code == EXIT_ERROR:
        return _fail(output)
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
