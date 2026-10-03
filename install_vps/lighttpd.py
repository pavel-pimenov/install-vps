"""Аудит миграции lighttpd -> Caddy: что из старого конфига не перенесено.

При переносе сайта с lighttpd на Caddy настройки переезжают в хвост `|`
конфигурации `caddy_site` (`browse`, `index=`, `hide=`, `deny=`). Правило
`AGENTS.md` требует сверяться с `/etc/lighttpd/lighttpd.conf`, который
остаётся на хосте, — иначе часть правил теряется молча. Именно так на
`dc.fly-server.ru` потерялись `url.access-deny` и
`static-file.exclude-extensions`: без них лежащий в дереве `upload.php`
отдавался как 200, а поломку заметил только пользователь через месяцы.

Модуль чистый: текст конфига lighttpd и сайты из конфига на входе, список
неперенесённых правил на выходе. Ничего не ходит по SSH — это делает
`scripts/audit-lighttpd.sh`.

Проверяемые правила (каждое — отдельный `Rule` в отчёте):

* `url.access-deny` + `static-file.exclude-extensions` -> `deny=`;
* вложенный корень сайта -> `legacy=` (URL-префикс, по которому ходил клиент);
* `mod_setenv` -> `header=` / `header=/путь/*=`;
* `dir-listing.activate` -> `browse`;
* точечное `server.dir-listing = "disable"` -> `browse=/путь/`;
* `index-file.names` -> `index=`.

Ключевая тонкость: правила lighttpd действовали на `server.document-root`
(на dc — `/var/www`), а зеркала в Caddy — отдельные домены со своими корнями
(`/var/www/etc`, `/var/www/update`...). Отсюда две тонкости сразу:

* «листинг выключен для /etc» значит «не для всего сайта», и проверять это
  надо на сайте корня, а не на всех зеркалах: у зеркал листинг наоборот нужен;
* правило, адресованное пути (`/etc/файл`, `/pvs-studio/`), на зеркале может
  быть неприменимо вовсе, поэтому требование распространяется только на те
  сайты, где этот путь реально обслуживается.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Расширения, которые lighttpd отдавал всегда (static-file.exclude-extensions
# и url.access-deny), а Caddy без явного deny отдал бы как обычный файл.
_DEFAULT_DENY = (".php", ".pl", ".fcgi", ".inc")

# Что отвечает за перенос: покрыто / замечание / потеряно
OK, WARN, LOST = "ok", "warn", "lost"

# Собственная страница листинга lighttpd: её генерировал сам lighttpd, файл в
# дереве сайта не лежит и после переезда на Caddy смысла не имеет. В `index=`
# её не переносим (на dc она была удалена как мёртвая), но показываем в отчёте,
# чтобы отличие от lighttpd было видно, а не молчаливым.
_LIGHTTPD_ARTIFACT = "index.lighttpd.html"

# `file_server` без явного `index` всё равно ищет эти файлы сам, поэтому сайт
# без `index=` в этом смысле не «потерял» индекс.
_CADDY_DEFAULT_INDEX = ("index.html", "index.txt")


@dataclass(frozen=True)
class Rule:
    """Одно правило lighttpd в сравнении с настройкой сайтов Caddy."""

    name: str       # короткое имя для отчёта
    what: str       # что правило делало в lighttpd
    level: str      # OK | WARN | LOST
    hint: str = ""  # чем закрыть, если не покрыто


def _covers_deny(options: dict[str, list[str]], patterns: list[str]) -> bool:
    """Закрывает ли deny все шаблоны из lighttpd.

    Сравнение по шаблону, а не по строке: `*.php` в Caddy и `.php` в
    lighttpd — одно и то же правило, записанное по-разному.
    """
    deny = set(options.get("deny") or ())
    return all(pattern in deny for pattern in patterns)


def _uncommented(text: str) -> list[str]:
    """Строки без комментариев: правило в комментарии не действует.

    Иначе закомментированный `url.access-deny` в старом конфиге выглядел бы
    как потерянное правило, хотя его никогда и не было.
    """
    return [line for line in (raw.strip() for raw in text.splitlines())
            if line and not line.startswith("#")]


def _document_root(lines: list[str]) -> str:
    """`server.document-root`, к которому сводятся правила lighttpd."""
    for line in lines:
        m = re.search(r'server\.document-root\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1).rstrip("/")
    return ""


def _listing_disable_paths(lines: list[str]) -> list[str]:
    """Пути, для которых lighttpd гасил dir-listing.

    `server.dir-listing = "disable"` само по себе значит «список файлов не
    показываем»; вместе с `dir-listing.activate = enable` это способ сказать
    «листинг есть, но не везде».
    """
    paths = []
    for line in lines:
        m = re.search(r'\$HTTP\["url"\]\s*=~\s*"\^?([^"$]*(?:\(\$|\|/)[^"]*)"', line)
        if m and re.search(r"dir-listing\s*=\s*\"?disable", line):
            paths.append(m.group(1).strip())
    return paths


def _index_names(lines: list[str]) -> list[str]:
    """Имена индексных файлов из index-file.names (последнее присваивание)."""
    names: list[str] = []
    for line in lines:
        m = re.search(r"index-file\.names\s*:?=\s*\(?\s*(.+?)\s*\)?$", line)
        if m:
            names = re.findall(r'"([^"]+)"', m.group(1))
    return names


def _deny_patterns(lines: list[str]) -> list[str]:
    """Шаблоны `deny=`, которые lighttpd требовал, в терминах Caddy.

    Два источника, оба действовали глобально на `document-root`:

    * `url.access-deny = ( "~", ".inc" )` — кроме расширений там лежат
      литеральные шаблоны вроде `"~"` (бэкапы вида `config.php~`). Это не
      расширение, и прежний разбор их отбрасывал, из-за чего `*~` не
      проверялся вовсе: зеркала годами держали `deny=` без него;
    * `static-file.exclude-extensions = ( ".php", ".pl", ".fcgi" )`.
    """
    patterns = {f"*{ext}" for ext in _DEFAULT_DENY}
    for line in lines:
        m = re.search(r'url\.access-deny\s*=\s*\(?\s*(.+?)\s*\)?$', line)
        if m:
            for token in re.findall(r'"([^"]*)"', m.group(1)):
                if not token:
                    continue
                patterns.add(token if token.startswith("*") else f"*{token}")
        m = re.search(r'static-file\.exclude-extensions\s*=\s*\(?\s*(.+?)\s*\)?$', line)
        if m:
            patterns |= {f"*.{ext}" for ext in re.findall(r'"\.?([A-Za-z0-9]+)"', m.group(1))}
    return sorted(patterns)


def _under_root(root: str, doc_root: str) -> bool:
    """Сайт лежит внутри дерева, которое обслуживал lighttpd."""
    if not doc_root:
        return True
    return root == doc_root or root.startswith(doc_root + "/")


def _deny_need(patterns: list[str]) -> str:
    return "добавьте |deny=" + ",".join(patterns) + " к caddy_site"


def _rule_deny(lines: list[str], sites: list[tuple[str, str, dict]], doc_root: str) -> Rule:
    """url.access-deny + static-file.exclude-extensions -> deny= в Caddy.

    Правило действовало глобально для document-root, значит закрывать его надо
    на каждом сайте этого дерева, а не хотя бы на одном. Иначе `upload.php`,
    лежащий в подкаталоге зеркала, снова уедет в 200.
    """
    patterns = _deny_patterns(lines)
    tree = [(domain, opts) for domain, root, opts in sites if _under_root(root, doc_root)]
    uncovered = [domain for domain, opts in tree if not _covers_deny(opts, patterns)]
    what = ("lighttpd не отдавал " + ", ".join(patterns)
            + " (url.access-deny / static-file.exclude-extensions)")
    if not sites:
        return Rule("доступ к исполняемым файлам", what, WARN,
                    "в caddy_sites нет сайтов-статики (file:/static:) — нечего сверять")
    if not uncovered:
        return Rule("доступ к исполняемым файлам", what, OK)
    if len(uncovered) == len(tree):
        return Rule("доступ к исполняемым файлам", what, LOST, _deny_need(patterns))
    return Rule("доступ к исполняемым файлам", what, WARN,
                "на этих сайтах deny= закрывает меньше, чем lighttpd: " + ", ".join(uncovered)
                + " (исполняемые файлы из дерева отдаются как обычные)")


def _rule_listing(lines: list[str], sites: list[tuple[str, str, dict]]) -> Rule:
    """dir-listing.activate -> browse хотя бы где-то в дереве."""
    wants = any("dir-listing.activate" in line and "enable" in line for line in lines)
    what = "lighttpd: dir-listing = enable (каталоги видны)" if wants \
        else "lighttpd: листинг выключен"
    if not wants or any("browse" in opts for _, _, opts in sites):
        return Rule("листинг каталогов", what, OK)
    if not sites:
        return Rule("листинг каталогов", what, WARN,
                    "в caddy_sites нет сайтов-статики — нечего сверять")
    return Rule("листинг каталогов", what, LOST,
                "добавьте |browse (или |browse=/путь/) к caddy_site")


def _rule_scoped_listing(lines: list[str], sites: list[tuple[str, str, dict]],
                         doc_root: str) -> Rule | None:
    """Точечное гашение листинга -> browse=/путь/ на сайте корня.

    Гасили пути вида `^/etc($|/)` на одном document-root. После миграции /etc и
    /update стали отдельными доменами, где листинг наоборот нужен, — поэтому
    проверять надо сайт, отдающий сам document-root, и требовать там точечный
    browse, а не листинг на весь сайт.
    """
    paths = _listing_disable_paths(lines)
    if not paths or not any("dir-listing.activate" in ln and "enable" in ln for ln in lines):
        return None
    what = ("lighttpd гасил dir-listing для " + ", ".join(paths)
            + " (листинг был не на весь сайт)")
    root_sites = [(d, o) for d, root, o in sites if root == doc_root] if doc_root else []
    if not root_sites:
        return Rule("точечное гашение листинга", what, WARN,
                    "в caddy_sites нет сайта с корнем " + (doc_root or "?")
                    + " — сверьте листинг вручную")
    whole = [d for d, o in root_sites if "browse" in o and not o.get("browse")]
    scoped = [d for d, o in root_sites if o.get("browse")]
    if scoped and not whole:
        return Rule("точечное гашение листинга", what, OK)
    if whole and not scoped:
        return Rule("точечное гашение листинга", what, LOST,
                    "на " + ", ".join(whole) + " включён листинг всего сайта, а lighttpd "
                    "гасил его для " + ", ".join(paths)
                    + " — используйте |browse=/путь/ для нужных каталогов")
    if whole:
        return Rule("точечное гашение листинга", what, WARN,
                    "на " + ", ".join(whole) + " листинг всего сайта, хотя lighttpd гасил "
                    "его для " + ", ".join(paths))
    return Rule("точечное гашение листинга", what, LOST,
                "на сайте document-root нет листинга вообще")


def _rule_index(lines: list[str], sites: list[tuple[str, str, dict]]) -> Rule | None:
    """index-file.names -> index= там, где каталог должен отдавать индекс.

    Индексы на запрещённое расшире (index.php) отдельно переносить не нужно:
    такой файл lighttpd тоже не отдавал, а Caddy его и подавно закроет
    deny=*.php. Страницу самого lighttpd (index.lighttpd.html) — тоже: её
    генерировал lighttpd, в дереве сайта её нет.
    """
    raw_names = _index_names(lines)
    if not raw_names:
        return None
    # интересуют только запрещённые расширения: литеральные шаблоны вроде
    # «*~» индексным файлам не соответствуют
    denied_exts = {p[1:] for p in _deny_patterns(lines) if p.startswith("*.")}
    names = [n for n in raw_names
             if f".{n.rsplit('.', 1)[-1]}" not in denied_exts and n != _LIGHTTPD_ARTIFACT]
    skipped = [n for n in raw_names if n not in names]
    what = ("lighttpd: index-file.names = " + ", ".join(raw_names)
            + (f" (в Caddy не переносятся: {', '.join(skipped)})" if skipped else ""))
    if not sites:
        return Rule("индексные файлы", what, WARN,
                    "в caddy_sites нет сайтов-статики — нечего сверять")
    # file_server без явного index= всё равно ищет index.html/index.txt сам
    without = [d for d, _, o in sites if not set(o.get("index") or _CADDY_DEFAULT_INDEX) >= set(names)]
    if not names or not without:
        return Rule("индексные файлы", what, OK)
    return Rule("индексные файлы", what, WARN,
                "на этих сайтах index= не покрывает lighttpd-индексы (" + ", ".join(names)
                + "): каталог покажет листинг вместо индекса — " + ", ".join(without))


def _legacy_prefix(root: str, doc_root: str) -> str:
    """URL-префикс, по которому lighttpd отдавал поддерево сайта.

    При `document-root = /var/www` клиент ходил по `/etc/файл`, а сайт в Caddy
    с корнем `/var/www/etc` ждёт `/файл`: без `legacy=/etc` запрос уезжал в
    404. Именно так отвалился поиск FlyLinkDC — его собственный конфиг внутри
    зеркала ссылается на `http://etc.fly-server.ru/etc/xxx-block.txt`.
    """
    if not doc_root or not root or root == doc_root:
        return ""
    if not root.startswith(doc_root + "/"):
        return ""
    return "/" + root.removeprefix(doc_root + "/").partition("/")[0]


def _opts_by_domain(sites: list[tuple[str, str, dict]]) -> dict[str, dict]:
    return {domain: opts for domain, _root, opts in sites}


def _rule_legacy_prefix(sites: list[tuple[str, str, dict]], doc_root: str) -> Rule | None:
    """Вложенные корни требуют legacy= с префиксом пути."""
    needed = [(domain, _legacy_prefix(root, doc_root))
              for domain, root, _opts in sites]
    needed = [(domain, prefix) for domain, prefix in needed if prefix]
    if not needed:
        return None
    what = f"lighttpd отдавал {doc_root} целиком, клиенты ходят по /<подкаталог>/файл"
    lookup = _opts_by_domain(sites)
    uncovered = [(domain, prefix) for domain, prefix in needed
                 if prefix not in (lookup.get(domain, {}).get("legacy") or ())]
    if not uncovered:
        return Rule("legacy-префиксы URL", what, OK)
    return Rule(
        "legacy-префиксы URL", what, LOST,
        "добавьте к caddy_site: "
        + ", ".join(f"{d} |legacy={p}" for d, p in uncovered),
    )


_SETENV_BLOCK_RE = re.compile(
    r'\$HTTP\["url"\]\s*=~\s*"\^(/[^"]*?)/?"\s*\{(.*?)\}', re.DOTALL
)
_SETENV_PAIR_RE = re.compile(r'"([^"]+)"\s*(?:=>|=)\s*"([^"]*)"')


def _setenv_headers(text: str) -> list[tuple[str, str]]:
    """Заголовки, которые lighttpd проставлял через mod_setenv.

    Блок бывает многострочным и с разными формами записи:

        $HTTP["url"] =~ "^/pvs-studio/" {
            setenv.add-response-header = (
                "Cache-Control" => "no-store, no-cache, must-revalidate",
                "Pragma" => "no-cache",
            )
        }

    Возвращает пары (путь-префикс, "Name: value"). Разбираем блок целиком:
    пары лежат на своих строках, и построчное сопоставление их не видело —
    проверка молчала бы, а заголовки на хосте не появились бы.
    """
    out: list[tuple[str, str]] = []
    for match in _SETENV_BLOCK_RE.finditer(text):
        prefix = "/" + match.group(1).strip("/")
        if not match.group(2).strip():
            continue  # блок без setenv — не заголовки
        for field, value in _SETENV_PAIR_RE.findall(match.group(2)):
            out.append((prefix, f"{field}: {value}"))
    return out


def _rule_headers(lines: list[str], text: str, sites: list[tuple[str, str, dict]],
                  doc_root: str) -> Rule | None:
    """mod_setenv lighttpd -> header= в caddy_site."""
    wanted = _setenv_headers(text)
    if not wanted:
        return None
    what = "lighttpd проставлял: " + ", ".join(sorted({h for _, h in wanted}))
    lookup = _opts_by_domain(sites)
    uncovered = []
    for domain, root, _opts in sites:
        if not _under_root(root, doc_root):
            continue
        # условие lighttpd касалось пути, а не домена: на зеркале /var/www/etc
        # запроса /pvs-studio/ не бывает, и требовать там заголовок — шум.
        # Правило действует там, где этот путь реально обслуживается: на сайте
        # самого document-root (всё дерево) и на сайте с таким же legacy-корнем.
        legacy = _legacy_prefix(root, doc_root)
        relevant = [(prefix, header) for prefix, header in wanted
                    if root == doc_root or prefix == legacy]
        if not relevant:
            continue
        # опции хранят тройки (путь, поле, значение); сайт покрывает условие
        # либо заголовком на весь сайт, либо path-совместимым
        covered = any(
            (not scope or scope == prefix)
            and f"{field}: {value}" == header
            for scope, field, value in (lookup.get(domain, {}).get("header") or ())
            for prefix, header in relevant
        )
        if not covered:
            uncovered.append(domain)
    if not uncovered:
        return Rule("заголовки mod_setenv", what, OK)
    return Rule("заголовки mod_setenv", what, LOST,
                "добавьте |header=<Name: value> (или |header=/путь/*=<Name: value>) "
                "к caddy_site: " + ", ".join(uncovered))


def audit(text: str, sites: list[tuple[str, str, dict[str, list[str]]]]) -> list[Rule]:
    """Сверяет правила lighttpd с настройкой сайтов.

    `sites` — тройки (домен, корень каталога, разобранные опции сайта).
    Корень — путь после `file:`, а для прокси-сайтов пустая строка.

    Правила, которых в старом конфиге нет (нет `index-file.names` — нечего
    переносить), просто не попадают в список: проверка без `dir-listing`
    не должна порождать запись «потерян листинг».
    """
    lines = _uncommented(text)
    doc_root = _document_root(lines)
    static = [(domain, root, opts) for domain, root, opts in sites if root]
    checks = (
        _rule_deny(lines, static, doc_root),
        _rule_legacy_prefix(static, doc_root),
        _rule_headers(lines, text, static, doc_root),
        _rule_listing(lines, static),
        _rule_scoped_listing(lines, static, doc_root),
        _rule_index(lines, static),
    )
    return [rule for rule in checks if rule is not None]


_MARKS = {OK: "OK:      ", WARN: "WARN:    ", LOST: "ПОТЕРЯНО:"}


def report(rules: list[Rule]) -> str:
    """Человекочитаемый отчёт по правилам аудита."""
    lines = ["аудит lighttpd -> Caddy:"]
    if not rules:
        # в lighttpd.conf нет ни одного переносимого правила: молчать нельзя,
        # иначе пустой отчёт читается как «всё хорошо проверено»
        lines.append("  WARN:     переносимых правил не найдено — сверьте вручную")
        lines.append("  ИТОГО: нечего сверять")
        return "\n".join(lines) + "\n"
    for rule in rules:
        lines.append(f"  {_MARKS[rule.level]} {rule.name} — {rule.what}")
        if rule.level != OK and rule.hint:
            lines.append(f"            {rule.hint}")
    lost = [r for r in rules if r.level == LOST]
    warned = [r for r in rules if r.level == WARN]
    if lost:
        lines.append(f"  ИТОГО: потеряно правил — {len(lost)}")
    elif warned:
        lines.append(f"  ИТОГО: ничего не потеряно, замечания — {len(warned)}")
    else:
        lines.append("  ИТОГО: все найденные правила перенесены")
    return "\n".join(lines) + "\n"
