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

Ключевая тонкость: правила lighttpd действовали на `server.document-root`
(на dc — `/var/www`), а зеркала в Caddy — отдельные домены со своими корнями
(`/var/www/etc`, `/var/www/update`...). Поэтому «листинг выключен для /etc»
значит «не для всего сайта», и проверять это надо на сайте корня, а не на
всех зеркалах сразу: у зеркал листинг наоборот нужен.
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


def _covers_deny(options: dict[str, list[str]], extensions: list[str]) -> bool:
    """Закрывает ли deny все расширения из lighttpd.

    Сравнение по расширению, а не по строке: `*.php` в Caddy и `.php` в
    lighttpd — одно и то же правило, записанное по-разному.
    """
    deny = set(options.get("deny") or ())
    for raw_ext in extensions:
        ext = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
        if not deny & {f"*{ext}", ext, f"*{ext.lstrip('.')}"}:
            return False
    return True


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


def _denied_extensions(lines: list[str]) -> list[str]:
    """Расширения из url.access-deny и static-file.exclude-extensions."""
    found = {_DEFAULT_DENY[0]}  # .inc из url.access-deny проверяем отдельно ниже
    for line in lines:
        m = re.search(r'url\.access-deny\s*=\s*\(?\s*(.+?)\s*\)?$', line)
        if m:
            found |= {f".{ext}" for ext in re.findall(r'"\.([A-Za-z0-9]+)"', m.group(1))}
        m = re.search(r'static-file\.exclude-extensions\s*=\s*\(?\s*(.+?)\s*\)?$', line)
        if m:
            found |= {f".{ext}" for ext in re.findall(r'"\.?([A-Za-z0-9]+)"', m.group(1))}
    return sorted(found | set(_DEFAULT_DENY))


def _under_root(root: str, doc_root: str) -> bool:
    """Сайт лежит внутри дерева, которое обслуживал lighttpd."""
    if not doc_root:
        return True
    return root == doc_root or root.startswith(doc_root + "/")


def _deny_need(extensions: list[str]) -> str:
    return "добавьте |deny=" + ",".join(f"*{ext}" for ext in extensions) + " к caddy_site"


def _rule_deny(lines: list[str], sites: list[tuple[str, str, dict]], doc_root: str) -> Rule:
    """url.access-deny + static-file.exclude-extensions -> deny= в Caddy.

    Правило действовало глобально для document-root, значит закрывать его надо
    на каждом сайте этого дерева, а не хотя бы на одном. Иначе `upload.php`,
    лежащий в подкаталоге зеркала, снова уедет в 200.
    """
    extensions = _denied_extensions(lines)
    tree = [(domain, opts) for domain, root, opts in sites if _under_root(root, doc_root)]
    uncovered = [domain for domain, opts in tree if not _covers_deny(opts, extensions)]
    what = ("lighttpd не отдавал " + ", ".join(extensions)
            + " (url.access-deny / static-file.exclude-extensions)")
    if not sites:
        return Rule("доступ к исполняемым файлам", what, WARN,
                    "в caddy_sites нет сайтов-статики (file:/static:) — нечего сверять")
    if not uncovered:
        return Rule("доступ к исполняемым файлам", what, OK)
    if len(uncovered) == len(tree):
        return Rule("доступ к исполняемым файлам", what, LOST, _deny_need(extensions))
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
    extensions = _denied_extensions(lines)
    names = [n for n in raw_names
             if f".{n.rsplit('.', 1)[-1]}" not in extensions and n != _LIGHTTPD_ARTIFACT]
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


def audit(text: str, sites: list[tuple[str, str, dict[str, list[str]]]]) -> list[Rule]:
    """Сверяет правила lighttpd с настройкой сайтов.

    `sites` — тройки (домен, корень каталога, разобранные опции сайта).
    Корень — путь после `file:`, а для прокси-сайтов пустая строка.
    """
    lines = _uncommented(text)
    doc_root = _document_root(lines)
    static = [(domain, root, opts) for domain, root, opts in sites if root]
    rules = [
        _rule_deny(lines, static, doc_root),
        _rule_listing(lines, static),
        _rule_index(lines, static),
    ]
    scoped = _rule_scoped_listing(lines, static, doc_root)
    if scoped is not None:
        rules.insert(2, scoped)
    return rules


_MARKS = {OK: "OK:      ", WARN: "WARN:    ", LOST: "ПОТЕРЯНО:"}


def report(rules: list[Rule]) -> str:
    """Человекочитаемый отчёт по правилам аудита."""
    lines = ["аудит lighttpd -> Caddy:"]
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
