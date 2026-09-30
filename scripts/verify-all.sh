#!/bin/sh
# Проверка всех хостов парка одним прогоном: install-vps --verify-only по
# каждому config.<хост>.local.toml. Раньше проверка шла по одному хосту, и
# тихие поломки (404 у статики, чужой образ, спящий агент) накапливались годами.
#
# Хосты берутся из шаблонов config-templates/*.toml — это список машин, за
# которыми следим. Готовые конфиги с секретами (config.*.local.toml) в git не
# лежат, поэтому отсутствие файла — не ошибка, а пропуск с предупреждением.
#
# Использование:
#   sh scripts/verify-all.sh              # все хосты из config-templates/
#   sh scripts/verify-all.sh dc fi2       # только указанные
#
# Код возврата: 0 — все прошли, 1 — есть ПРОБЛЕМА/MISSING, 2 — хост не
# проверен (нет конфига или хост недоступен). Ничего не чинит и не меняет:
# проверка только читает.
set -u

# shellcheck disable=SC2164
cd "$(dirname "$0")/.."

if [ "$#" -gt 0 ]; then
    HOSTS="$*"
else
    HOSTS=""
    for tpl in config-templates/*.toml; do
        [ -e "$tpl" ] || continue
        # имя шаблона и есть короткое имя хоста: config-templates/dc.toml
        # соответствует config.dc.local.toml, суффикс .toml убираем
        tpl="${tpl##*/}"
        HOSTS="$HOSTS ${tpl%.toml}"
    done
fi

if [ -z "$HOSTS" ]; then
    echo "Нет шаблонов в config-templates/ — нечего проверять." >&2
    exit 2
fi

RC=0
PROBLEMS=""
SKIPPED=""

# Печатает строки отчёта, отбрасывая заведомо ложные MISSING: утилиту, которой
# нет в packages конфига, и swap при swap_size_mb = 0. Самим install-vps это
# неизвестно, поэтому фильтр живёт здесь.
_drop_expected() {
    python3 -c '
import re, sys, tomllib

cfg = tomllib.load(open(sys.argv[1], "rb"))
want = set(cfg.get("packages") or ())
no_swap = int(cfg.get("swap_size_mb") or 0) == 0

for line in sys.stdin:
    # перевод строки теряется при strip(), а без него склеиваются соседние
    # строки отчёта — поэтому восстанавливаем его всегда
    text = line.rstrip("\n")
    m = re.search(r"MISSING: ([A-Za-z0-9_.-]+)", text)
    if m and m.group(1) not in want:
        text = f"  (ожидаемо) {text.strip()}"
    elif "MISSING: swap" in text and no_swap:
        text = "  (ожидаемо) MISSING: swap -> swap_size_mb = 0"
    sys.stdout.write(text + "\n")
' "$1"
}

for name in $HOSTS; do
    cfg="config.${name}.local.toml"
    if [ ! -f "$cfg" ]; then
        echo ""
        echo "=== $name: пропуск — нет $cfg"
        echo "    cp config-templates/${name}.toml $cfg и вставьте секреты"
        SKIPPED="$SKIPPED $name"
        continue
    fi

    echo ""
    echo "=== $name ($(grep -m1 '^host' "$cfg" | sed 's/.*= *//'))"
    out=$(python3 -m install_vps -c "$cfg" --verify-only 2>&1)
    status=$?
    # ПРОБЛЕМА/MISSING — не то же самое, что ненулевой код: установщик
    # специально не падает на них, но молчать о таком здесь нельзя.
    # Часть MISSING заведомо ложная: packages = [] и swap_size_mb = 0 означают
    # «ничего не ставить» (узлы заняты AmneziaWG, полный прогон их обрушил бы),
    # а общая проверка об этом не знает и ругается на fail2ban/ncdu/swap.
    # Такие строки гасим, остальные — настоящие.
    real=$(printf '%s\n' "$out" | _drop_expected "$cfg")
    printf '%s\n' "$real"

    if [ "$status" -ne 0 ]; then
        PROBLEMS="$PROBLEMS $name(выход $status)"
        RC=1
        continue
    fi
    # строки, помеченные нами как ожидаемые, в подсчёт проблем не идут
    if printf '%s\n' "$real" | grep -v 'ожидаемо' | grep -qE 'ПРОБЛЕМА|MISSING'; then
        PROBLEMS="$PROBLEMS $name"
        RC=1
    fi
done

echo ""
echo "============================================================"
if [ -n "$SKIPPED" ]; then
    echo "Не проверены (нет конфига):$SKIPPED"
    # отмеченные хосты не считаются чистыми: их состояние неизвестно
    [ "$RC" -eq 0 ] && RC=2
fi
if [ "$RC" -eq 1 ]; then
    echo "С ПРОБЛЕМАМИ или MISSING:$PROBLEMS"
    echo "Подробности выше: ищите строки 'ПРОБЛЕМА' и 'MISSING'."
elif [ "$RC" -eq 2 ]; then
    echo "Проверенные хосты прошли, но список неполный (код 2)."
else
    echo "Все проверенные хосты прошли без замечаний."
fi
exit "$RC"
