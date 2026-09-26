from __future__ import annotations

import html
import re
import subprocess
from dataclasses import dataclass

from .config import Config
from .ssh import RemoteHost

# docker-репозиторий: постоянные пути
DOCKER_REPO_KEY = "https://download.docker.com/linux/ubuntu/gpg"
DOCKER_KEYRING_DIR = "/etc/apt/keyrings"
DOCKER_KEYRING_FILE = f"{DOCKER_KEYRING_DIR}/docker.gpg"
DOCKER_SOURCES_LIST = "/etc/apt/sources.list.d/docker.list"

# docker-пакеты: свой набор для каждого источника
#   * "ubuntu"   — docker.io / docker-compose-v2 из архивов Ubuntu
#   * "official" — docker-ce из официального репозитория (download.docker.com)
_DOCKER_UBUNTU_PACKAGES = {"docker.io", "docker-compose-v2"}
_DOCKER_OFFICIAL_PACKAGES = [
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
]

_OS_RELEASE_FIELDS = 2

# Утилиты, которых нет в репозиториях Ubuntu: ставим из релизов GitHub.
# Версия, имя файла и sha256 закреплены здесь же: релиз нельзя перезалить
# незаметно, а молча ставить непроверенные бинарники нельзя тем более.
#   * sha256   — sha256 архива, сверяется на хосте до распаковки;
#   * bin_sha256 — sha256 самого бинарника: по нему дешевле и надёжнее
#     проверять, что нужная версия уже стоит (не качать архив заново).
# Набор архетипур у каждого проекта свой, поэтому и url, и хеши заданы
# по ключам uname -m.
_GITHUB_TOOLS = {
    "lazydocker": {
        "version": "0.25.2",
        "url": "https://github.com/jesseduffield/lazydocker/releases/download/v{version}",
        "archive": "lazydocker_{version}_Linux_{arch}.tar.gz",
        "binary": "lazydocker",
        "arch": {
            "x86_64": "x86_64",
            "aarch64": "arm64",
            "armv7l": "armv7",
            "armv6l": "armv6",
        },
        "sha256": {
            "x86_64": "0d9dbfc26068b218e7ed84b104748cadc6e3cf733c0afd35465306fb39b9523c",
            "aarch64": "005c38b685aaa557e7d646d83a3dadb5024340eeed8c6a2e1949eee6f530de23",
            "armv7l": "7a12a63fd39fdbb84b41db14824822f2ce38a549b744ddb5647587ec3aa4cf2e",
            "armv6l": "b03e75ca588e788414385770de09952c7a1f4a852c7ad78a9feea7462991e940",
        },
        "bin_sha256": {
            "x86_64": "fa14fba9f56266e6b1e6374b6ee477fcd5d7858915eb603e8e77ac577ce14a2c",
            "aarch64": "4fd56feb3987c4a0b1dd0e8a756a99ab311dcb2ed16371e62250becd4d88916d",
            "armv7l": "4ca5d6330db66a6094bd30ea87d5b353d1d32958d4b90389e0ff754f9c089cb9",
            "armv6l": "0a2ed07ede47c70e621fdac6c626bc2f2470ed8676a898e07722fac9a1435053",
        },
    },
    "bandwhich": {
        "version": "0.23.1",
        "url": "https://github.com/imsnif/bandwhich/releases/download/v{version}",
        "archive": "bandwhich-v{version}-{arch}-unknown-linux-gnu.tar.gz",
        "binary": "bandwhich",
        "arch": {
            "x86_64": "x86_64",
            "aarch64": "aarch64",
        },
        "sha256": {
            "x86_64": "0de12665fcd1ecafbed84c372fb8edc568bb9eaffee95f53710b8c0b4c687637",
            "aarch64": "46311a6e2652fc3fc386c1186baa6b986e925c686237434445b656112358ce4d",
        },
        "bin_sha256": {
            "x86_64": "d18d24df363458669b9a54ff247356e953e55fa78220736b85eee2da2052f0df",
            "aarch64": "8783c00b09627ec1644d223bddf0874a90dfa4cfad496fb25a7003f7d95006f5",
        },
    },
}

EXTRA_TOOLS = tuple(sorted(_GITHUB_TOOLS))


def _yaml_str(value: str) -> str:
    """Строка для YAML в docker-compose: кавычки, если есть спецсимволы."""
    if value == "" or any(ch in value for ch in ":#{}[],&*?|<>=!%@`\"'"):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return value


def _sh_quote(value: str) -> str:
    """Обернуть значение в одинарные кавычки для безопасной подстановки в bash."""
    return "'" + value.replace("'", "'\\''") + "'"


_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_UPSTREAM_RE = re.compile(r"^(https?://)?[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# диапазон непривилегированных портов: всё, что ниже 1024, требует Capabilities
_PORT_MIN = 1024
_PORT_MAX = 65535

_FIRST_PRINTABLE = 32  # ниже — управляющие символы, в bash-схеме они лишние


def _tool_script(name: str) -> str:
    """bash-фрагмент установки утилиты из GitHub-релиза.

    Подстановки делаются здесь, а не через .format(): в готовом bash много
    $ и скобок, которые .format() либо испортил бы, либо пришлось бы
    экранировать. Каждая утилита ставится в своем подшелле, чтобы её
    `trap ... EXIT` убирал временный каталог и не задевал основной скрипт.
    """
    spec = _GITHUB_TOOLS[name]
    version = spec["version"]
    binary = spec["binary"]
    base_url = spec["url"].format(version=version)
    cases = []
    for machine, arch in spec["arch"].items():
        if machine not in spec["sha256"] or machine not in spec["bin_sha256"]:
            continue
        archive = spec["archive"].format(version=version, arch=arch)
        cases.append(
            f"    {machine}) FILE={_sh_quote(archive)}"
            f" URL={_sh_quote(f'{base_url}/{archive}')}"
            f" SHA={_sh_quote(spec['sha256'][machine])}"
            f" BIN_SHA={_sh_quote(spec['bin_sha256'][machine])} ;;"
        )
    return (
        f'echo "==> утилита {name} {version} (GitHub release, sha256) "\n'
        "(\n"
        f"  BIN={_sh_quote(binary)}\n"
        '  ARCH="$(uname -m)"\n'
        '  case "$ARCH" in\n'
        + "\n".join(cases)
        + "\n"
        f'    *) echo "  ПРОПУСК: {name} не собран под $ARCH"; exit 0 ;;\n'
        "  esac\n"
        '  if [ -x "/usr/local/bin/$BIN" ] && '
        '[ "$(sha256sum "/usr/local/bin/$BIN" | cut -d" " -f1)" = "$BIN_SHA" ]; then\n'
        f'    echo "  {binary} {version} уже стоит (sha256 бинарника совпал) — пропускаю"\n'
        "    exit 0\n"
        "  fi\n"
        '  TMP="$(mktemp -d)"\n'
        "  trap 'rm -rf \"$TMP\"' EXIT\n"
        '  echo "  скачиваю $URL"\n'
        '  curl -fsSL --retry 3 -o "$TMP/tool.tar.gz" "$URL"\n'
        '  echo "$SHA  $TMP/tool.tar.gz" | sha256sum -c - >/dev/null || {\n'
        f'    echo "ОШИБКА: sha256 архива не совпал ({name} {version})" >&2; exit 1; }}\n'
        '  tar xzf "$TMP/tool.tar.gz" -C "$TMP"\n'
        '  SRC="$(find "$TMP" -type f -name "$BIN" | head -1)"\n'
        '  if [ -z "$SRC" ]; then\n'
        f'    echo "ОШИБКА: в архиве нет файла {binary}" >&2; exit 1\n'
        "  fi\n"
        '  $SUDO install -m 0755 "$SRC" "/usr/local/bin/$BIN"\n'
        '  echo "  $BIN: $(timeout 5 /usr/local/bin/"$BIN" --version </dev/null 2>&1 | head -1)"\n'
        ")\n"
    )


def _tools_script(tools: list[str]) -> str:
    """Фрагменты установки всех выбранных утилит, по одной на инструмент."""
    return "".join(_tool_script(name) for name in tools)


def _tools_verify_script(tools: list[str]) -> str:
    """Проверка наличия утилит (работает и в --verify-only)."""
    lines = ['echo "==> утилиты из GitHub-релизов"']
    for name in tools:
        binary = _GITHUB_TOOLS[name]["binary"]
        version = _GITHUB_TOOLS[name]["version"]
        lines.append(
            f"if [ -x /usr/local/bin/{binary} ]; then\n"
            f'  echo "  OK: {name} {version}'
            f' -> $(timeout 5 /usr/local/bin/{binary} --version </dev/null 2>&1 | head -1)"\n'
            f"else\n"
            f'  echo "  MISSING: {name} {version}"\n'
            f"fi"
        )
    return "\n".join(lines) + "\n"


def _beszel_auth_env(cfg: Config) -> str:
    """Дополнительные переменные окружения Hub для режима OAuth."""
    lines = []
    if cfg.beszel_user_creation:
        lines.append('      USER_CREATION: "true"')
    if cfg.beszel_disable_password_auth:
        lines.append('      DISABLE_PASSWORD_AUTH: "true"')
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


def _beszel_auth_warning(cfg: Config) -> str:
    """Предупреждение про потерю пароля, если OAuth ещё не настроен."""
    if not cfg.beszel_disable_password_auth:
        return ""
    domain = cfg.caddy_domain or "<домен Beszel>"
    return (
        'echo "  ВНИМАНИЕ: DISABLE_PASSWORD_AUTH=true — парольный вход отключён."\n'
        'echo "  Убедитесь, что OAuth настроен и вход через него проверен:"\n'
        f'echo "    https://{domain}/_/#/settings -> users -> Options -> OAuth2."\n'
    )


def _caddy_site_block(domain: str, upstream: str) -> str:
    """Обычный сайт: домен -> upstream. Отступы табами — канонический caddy fmt."""
    return f"{domain} {{\n\treverse_proxy {upstream}\n}}\n"


def _caddy_beszel_block(domain: str, port: int) -> str:
    """Сайт Beszel Hub с настройками из официальной документации beszel.dev.

    request_body max_size — Beszel шлёт большие тела при импорте/экспорте,
    read_timeout 360s — долгие опросы и WebSocket агентов с universal token.
    """
    return (
        f"{domain} {{\n"
        f"\trequest_body {{\n"
        f"\t\tmax_size 10MB\n"
        f"\t}}\n"
        f"\treverse_proxy 127.0.0.1:{port} {{\n"
        f"\t\ttransport http {{\n"
        f"\t\t\tread_timeout 360s\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"}}\n"
    )


def _caddy_caddyfile(email: str) -> str:
    """Корневой Caddyfile: глобальные опции + подключение всех сайтов.

    Без завершающего перевода строки: строка-заглушка в шаблоне и heredoc уже
    добавляют перевод, а лишняя пустая строка в конце заставляет Caddy
    ругаться «Caddyfile input is not formatted» при каждом старте.
    """
    lines = ["{"]
    if email:
        lines.append(f"\temail {email}")
    lines.append("}")
    lines.append(f"import {CADDY_SITES_DIR_IN_CONTAINER}/*.caddy")
    return "\n".join(lines)


def _write_site_script(name: str, content: str) -> str:
    """Один сайт = один файл в sites/, подключается через import в Caddyfile.

    Содержимое не проходит через .format(), поэтому фигурные скобки
    Caddyfile экранировать не нужно.
    """
    return (
        f"cat <<'CADDY_SITE_EOF' | $SUDO tee {CADDY_SITES_DIR}/{name}.caddy >/dev/null\n"
        f"{content}"
        "CADDY_SITE_EOF\n"
    )


def _caddy_sites_script(cfg: Config, portal: bool = False) -> str:
    """Готовые bash-фрагменты, создающие по файлу на каждый сайт."""
    sites: list[tuple[str, str]] = []
    if cfg.caddy_domain:
        sites.append(("beszel", _caddy_beszel_block(cfg.caddy_domain, cfg.beszel_port)))
    for raw in cfg.caddy_sites:
        domain, sep, upstream = raw.partition("=")
        if not sep:
            raise ValueError(
                f"caddy_site должен быть вида ДОМЕН=UPSTREAM, а получено {raw!r}"
            )
        domain, upstream = domain.strip(), upstream.strip()
        if not _HOSTNAME_RE.match(domain):
            raise ValueError(f"Некорректный домен в caddy_site: {domain!r}")
        if not _UPSTREAM_RE.match(upstream):
            raise ValueError(f"Некорректный upstream в caddy_site: {upstream!r}")
        sites.append((domain, _caddy_site_block(domain, upstream)))

    script = "".join(_write_site_script(name, content) for name, content in sites)
    if not sites and not portal:
        script += (
            'echo "  ВНИМАНИЕ: не задан ни одного домена — Caddy поднимется,'
            ' но сайтов не будет."\n'
            'echo "  Добавьте --caddy-domain (Beszel) и/или'
            ' --caddy-site ДОМЕН=UPSTREAM."\n'
        )
    return script


# --------------------------------------------------------------------------
# Портал: страница с плитками сервисов на своём домене.
#
# Одна страница https://<домен>/ — список плиток; каждая плитка ведёт либо на
# путь этого же домена (Caddy проксирует его на локальный сервис), либо на
# внешний адрес. Новый сервис = новая строка в caddy_tiles, Caddyfile руками
# править не нужно.
# --------------------------------------------------------------------------
_PORTAL_PATH_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_EXTERNAL_RE = re.compile(r"^https?://", re.IGNORECASE)

# сколько полей в caddy_tile: "Название=адрес", "Название=/путь=upstream",
# "Название=/путь=upstream=strip|keep"
_TILE_FIELDS_EXTERNAL = 2
_TILE_FIELDS_PROXY = 3
_TILE_FIELDS_WITH_MODE = 4

_PORTAL_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 2rem 1rem;
  background: #0f1216; color: #e6e9ef;
  font: 16px/1.5 -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif;
}
main { width: 100%; max-width: 60rem; }
h1 { font-size: 1.35rem; font-weight: 600; margin: 0 0 1.5rem; letter-spacing: .01em; }
ul { list-style: none; margin: 0; padding: 0; display: grid; gap: .9rem;
     grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); }
.tile {
  display: flex; flex-direction: column; gap: .3rem; padding: 1.1rem 1.2rem;
  border: 1px solid #232a33; border-radius: .7rem; background: #161b22;
  color: inherit; text-decoration: none; transition: border-color .15s, transform .15s;
}
.tile:hover, .tile:focus-visible { border-color: #3d82f6; transform: translateY(-2px); }
.name { font-size: 1.05rem; font-weight: 600; }
.hint { font-size: .82rem; color: #8b949e; word-break: break-all; }
footer { margin-top: 1.6rem; font-size: .8rem; color: #6e7681; }
"""


@dataclass
class Tile:
    """Плитка портала: подпись + адрес, который открывает браузер."""

    name: str
    href: str
    hint: str
    path: str = ""        # путь на портале; "" — внешняя ссылка, прокси не нужен
    upstream: str = ""     # что проксировать на этот путь
    keep_prefix: bool = False  # True — не срезать префикс (приложение знает базу)
    beszel: bool = False
    streaming: bool = False    # True — поток (SSE): без flush_interval -1 логи
    #                            приходят пачками или не приходят вовсе


def _external_url(raw: str) -> str:
    """Внешний адрес плитки: только http(s) с валидным хостом.

    Всё остальное (mailto:, javascript:, //host) отбрасываем: адрес попадает
    в href генерируемой страницы.
    """
    if not _EXTERNAL_RE.match(raw):
        raise ValueError(
            f"Внешний адрес плитки должен быть http(s)-ссылкой, а получено {raw!r}"
        )
    if any(ch in raw for ch in " \t\n\r"):
        raise ValueError(f"Пробелы в адресе плитки недопустимы: {raw!r}")
    authority = raw.split("://", 1)[1].split("/", 1)[0]
    host = authority.rsplit(":", 1)[0] if authority.count(":") == 1 else authority
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"Некорректный хост в адресе плитки: {host!r}")
    return raw


def _parse_tile(raw: str, portal: str) -> Tile:
    """Плитка из строки конфига.

    Формы:
      НАЗВАНИЕ=https://домен           — внешняя ссылка, только клик по плитке
      НАЗВАНИЕ=/путь=upstream          — прокси на локальный сервис, префикс
                                         срезается (приложение видит "/")
      НАЗВАНИЕ=/путь=upstream=keep     — то же, но префикс остаётся
    """
    parts = [part.strip() for part in raw.split("=")]
    if len(parts) < _TILE_FIELDS_EXTERNAL or not parts[0]:
        raise ValueError(
            "caddy_tile должен быть вида НАЗВАНИЕ=/путь=upstream "
            "(например Мониторинг=/monitor=127.0.0.1:8090) "
            "или НАЗВАНИЕ=https://домен для внешней ссылки, "
            f"а получено {raw!r}"
        )
    name = parts[0]
    # название попадает и в HTML, и в строки проверочного bash — кавычки,
    # доллары и обратные слэши там ломали бы скрипт или выполнили подстановку
    if any(ch in name for ch in "\n\r<>'\"$`\\") or any(ord(ch) < _FIRST_PRINTABLE for ch in name):
        raise ValueError(f"Недопустимое название плитки: {name!r}")
    if len(parts) == _TILE_FIELDS_EXTERNAL:
        href = _external_url(parts[1])
        return Tile(name=name, href=href, hint=href.split("://", 1)[1])

    if len(parts) == _TILE_FIELDS_PROXY:
        path, upstream, mode = parts[1], parts[2], "strip"
    elif len(parts) == _TILE_FIELDS_WITH_MODE:
        path, upstream, mode = parts[1], parts[2], parts[3].lower()
        if mode not in ("strip", "keep"):
            raise ValueError(
                f"Четвёртое поле плитки — strip или keep, а получено {mode!r}"
            )
    else:
        raise ValueError(
            f"Слишком много полей в caddy_tile ({len(parts)}): {raw!r}; "
            "ожидается НАЗВАНИЕ=/путь=upstream[=strip|keep]"
        )
    if not _PORTAL_PATH_RE.match(path):
        raise ValueError(
            f"Путь плитки должен быть вида /имя (без пробелов, не «/»): {path!r}"
        )
    if not _UPSTREAM_RE.match(upstream):
        raise ValueError(f"Некорректный upstream плитки: {upstream!r}")
    return Tile(
        name=name,
        href=f"https://{portal}{path}/",
        hint=f"{portal}{path}",
        path=path,
        upstream=upstream,
        keep_prefix=mode == "keep",
    )


def _beszel_path(cfg: Config) -> str:
    return "/" + cfg.beszel_path.strip("/")


def _dozzle_path(cfg: Config) -> str:
    return "/" + cfg.dozzle_path.strip("/")


def _beszel_public_url(cfg: Config) -> str:
    """Публичный адрес Beszel Hub: APP_URL и подсказки в выводе."""
    if cfg.caddy and cfg.caddy_domain:
        return f"https://{cfg.caddy_domain}"
    if cfg.caddy and cfg.caddy_portal:
        return f"https://{cfg.caddy_portal}{_beszel_path(cfg)}"
    return f"http://localhost:{cfg.beszel_port}"


def _tiles(cfg: Config) -> list[Tile]:
    """Плитки портала: из конфига + автоплитки мониторинга и логов.

    Плитка Beszel появляется сама, когда Caddy включён, домен портала задан, а
    для Beszel не выделен отдельный домен (caddy_domain) — иначе он жил бы
    вне портала, и ссылаться на него нужно было бы отдельной строкой.
    Плитка Dozzle добавляется по той же причине: путь и upstream известны из
    конфига, а прокси для неё особенный (keep + flush_interval).
    """
    tiles = [_parse_tile(raw, cfg.caddy_portal) for raw in cfg.caddy_tiles]
    if cfg.beszel and cfg.caddy and cfg.caddy_portal and not cfg.caddy_domain:
        path = _beszel_path(cfg)
        tiles.insert(
            0,
            Tile(
                name="Мониторинг",
                href=f"https://{cfg.caddy_portal}{path}/",
                hint=f"{cfg.caddy_portal}{path}",
                path=path,
                upstream=f"127.0.0.1:{cfg.beszel_port}",
                beszel=True,
            ),
        )
    if cfg.dozzle and cfg.caddy and cfg.caddy_portal:
        path = _dozzle_path(cfg)
        tiles.insert(
            0 if not tiles else 1,
            Tile(
                name="Логи",
                href=f"https://{cfg.caddy_portal}{path}/",
                hint=f"{cfg.caddy_portal}{path}",
                path=path,
                upstream=f"127.0.0.1:{cfg.dozzle_port}",
                # dozzle живёт под базовым путём (DOZZLE_BASE): префикс
                # обязателен, иначе вёрстка и ассеты не найдутся
                keep_prefix=True,
                streaming=True,
            ),
        )
    paths = [tile.path for tile in tiles if tile.path]
    for path in paths:
        for other in paths:
            if path != other and (other.startswith(f"{path}/") or path.startswith(f"{other}/")):
                raise ValueError(
                    f"Пути плиток вложены друг в друга ({path!r} и {other!r}): "
                    "handle_path в Caddy выбрал бы внешний путь первым"
                )
    if len(set(paths)) != len(paths):
        raise ValueError("Пути плиток повторяются: плитка на такой путь уже есть")
    return tiles


def _portal_caddy_block(cfg: Config, tiles: list[Tile]) -> str:
    """Сайт портала: раздача index.html + прокси плиток по путям.

    Всё внутри handle-блоков: иначе Caddy сортирует директивы по своему
    внутреннему порядку, и catch-all с index.html может отрезать прокси.
    """
    lines = [
        f"{cfg.caddy_portal} {{",
        "\tencode gzip zstd",
        "\trequest_body {",
        "\t\tmax_size 10MB",
        "\t}",
    ]
    for tile in tiles:
        if not tile.path:
            continue
        # без завершающего слэша Caddy отдал бы 404, а не приложение.
        # `redir * путь 308`, а не `redir путь 308`: у redir первый позиционный
        # аргумент — матчер, поэтому без `*` редиректом стал бы сам путь.
        lines += [
            f"\thandle {tile.path} {{",
            f"\t\tredir * {tile.path}/ 308",
            "\t}",
        ]
        keyword = "handle" if tile.keep_prefix else "handle_path"
        lines.append(f"\t{keyword} {tile.path}/* {{")
        if tile.beszel:
            # настройки из документации beszel.dev: импорт/экспорт систем
            lines += [
                f"\t\treverse_proxy {tile.upstream} {{",
                "\t\t\ttransport http {",
                "\t\t\t\tread_timeout 360s",
                "\t\t\t}",
                "\t\t}",
            ]
        elif tile.streaming:
            # dozzle.dev/guide/changing-base: SSE требует отключённой
            # буферизации и больших таймаутов, иначе логи замирают
            lines += [
                f"\t\treverse_proxy {tile.upstream} {{",
                "\t\t\tflush_interval -1",
                "\t\t\ttransport http {",
                "\t\t\t\tread_timeout 3600s",
                "\t\t\t}",
                "\t\t}",
            ]
        else:
            lines.append(f"\t\treverse_proxy {tile.upstream}")
        lines.append("\t}")
    lines += [
        "\thandle {",
        f"\t\troot * {PORTAL_ROOT_IN_CONTAINER}",
        "\t\tfile_server",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _portal_html(cfg: Config, tiles: list[Tile]) -> str:
    """Самодостаточная страница плиток: без внешних шрифтов и скриптов."""
    title = (cfg.caddy_portal_title or "Сервисы").strip() or "Сервисы"
    if any(ch in title for ch in "\n\r<"):
        raise ValueError(f"Недопустимый заголовок портала: {cfg.caddy_portal_title!r}")
    items = "".join(
        '    <li><a class="tile" href="{href}">'
        '<span class="name">{name}</span>'
        '<span class="hint">{hint}</span></a></li>\n'.format(
            href=html.escape(tile.href, quote=True),
            name=html.escape(tile.name),
            hint=html.escape(tile.hint),
        )
        for tile in tiles
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_PORTAL_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"  <h1>{html.escape(title)}</h1>\n"
        f"  <ul>\n{items}  </ul>\n"
        f"  <footer>{html.escape(cfg.caddy_portal)}</footer>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _portal_script(cfg: Config, tiles: list[Tile]) -> str:
    """Портал на хосте: каталог, index.html и файл сайта для Caddy."""
    if not tiles:
        return (
            'echo "  ВНИМАНИЕ: портал включён, но плиток нет — будет пустая страница."\n'
            'echo "  Добавьте --caddy-tile НАЗВАНИЕ=/путь=upstream."\n'
        )
    return (
        _write_site_script("portal", _portal_caddy_block(cfg, tiles))
        + f"echo \"==> портал {cfg.caddy_portal}: страница плиток\"\n"
        + f"$SUDO install -m 0755 -d {PORTAL_DIR}\n"
        + f"cat <<'PORTAL_EOF' | $SUDO tee {PORTAL_INDEX} >/dev/null\n"
        + _portal_html(cfg, tiles)
        + "PORTAL_EOF\n"
        + f'echo "  плиток: {len(tiles)}"\n'
    )


def _portal_verify_script(cfg: Config, tiles: list[Tile]) -> str:
    """Проверка портала: страница отдаётся и каждая плитка отвечает.

    Caddy работает в network_mode: host, поэтому 127.0.0.1:443 — это его же
    порт: --resolve подставляет имя для SNI и Host, а -k отключает проверку
    сертификата (Let's Encrypt на первом запросе мог ещё не выпустить его).
    """
    domain = cfg.caddy_portal
    lines = [f'echo "==> портал плиток: {domain}"']
    if not tiles:
        lines.append('  echo "  плиток нет"')
        return "\n".join(lines) + "\n"

    def probe(url: str) -> str:
        return (
            f"$($SUDO curl -sk -o /dev/null -w '%{{http_code}}' --resolve "
            f"{domain}:443:127.0.0.1 {url} 2>/dev/null || true)"
        )

    lines += [
        f'CODE="{probe(f"https://{domain}/")}"',
        '[ -n "$CODE" ] || CODE=000',
        'case "$CODE" in',
        "  200) echo '  OK: страница плиток отдаётся' ;;",
        "  *) echo \"  MISSING: страница плиток не отдаётся (код $CODE)\" ;;",
        "esac",
    ]
    for tile in tiles:
        if not tile.path:
            lines.append(
                f'  echo "  плитка {tile.name}: внешняя ссылка {tile.href}"'
            )
            continue
        target = f"https://{domain}{tile.path}/"
        lines += [
            f'CODE="{probe(target)}"',
            '[ -n "$CODE" ] || CODE=000',
            'case "$CODE" in',
            # кавычки двойные, иначе $CODE не подставится (в одинарных он
            # остаётся литералом) — название плитки уже проверено в _parse_tile
            "  2??|3??) echo \"  OK: плитка " + tile.name + " -> " + tile.path
            + "/ (код $CODE)\" ;;",
            "  000|502|503|504) echo \"  WARN: " + tile.name + " не отвечает ("
            + tile.upstream + ", код $CODE)\" ;;",
            "  *) echo \"  WARN: " + tile.name + " ответил кодом $CODE\" ;;",
            "esac",
        ]
    return "\n".join(lines) + "\n"


def _bootstrap(sudo: bool) -> str:
    _ = sudo  # sudo определяется автоматически на хосте
    lines = [
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        'export APT_LISTCHANGES_FRONTEND=none',
        'export LC_ALL=C',
        'if [ "$(id -u)" -eq 0 ]; then SUDO="";',
        'else',
        '  SUDO="sudo -n"',
        '  sudo -n true 2>/dev/null || { '  # noqa: E501
        'echo "ERROR: нужен root или sudo без пароля (NOPASSWD)" >&2; exit 1; }',
        'fi',
        # битый/устаревший docker.list и keyring ломают весь apt-get update
        f'$SUDO rm -f {DOCKER_SOURCES_LIST} {DOCKER_KEYRING_FILE}',
    ]
    return "\n".join(lines) + "\n"


APT_UPDATE = 'echo "==> apt-get update"\n$SUDO apt-get update\n'

PKG_INSTALL = (
    'echo "==> установка пакетов: {packages}"\n'
    "$SUDO apt-get install -y {packages}\n"
)

DOCKER_REPO = r"""
echo "==> подключаем официальный docker-репозиторий"
ARCH="$(dpkg --print-architecture)"
$SUDO install -m 0755 -d {keyring_dir}
if [ ! -f {keyring_file} ]; then
  curl -fsSL {key} | $SUDO gpg --dearmor -o {keyring_file}
fi
echo "deb [arch=$ARCH signed-by={keyring_file}] https://download.docker.com/linux/ubuntu {codename} stable" \
  | $SUDO tee {sources_list}
echo "==> apt-get update (после добавления docker-репо)"
$SUDO apt-get update
"""

SWAP = r"""
echo "==> своп-файл {size_mb}M (для слабых VPS)"
if ! $SUDO swapon --show | grep -q '^/swapfile'; then
  if [ ! -f /swapfile ]; then
    $SUDO dd if=/dev/zero of=/swapfile bs=1M count={size_mb} status=none
    $SUDO chmod 600 /swapfile
    $SUDO mkswap /swapfile
  fi
  $SUDO swapon /swapfile
  grep -q '^/swapfile' /etc/fstab \
    || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
fi
$SUDO swapon --show
"""

LOGROTATE_COMPRESS = r"""
echo "==> logrotate: сжимаем старые логи"
if [ -f /etc/logrotate.conf ]; then
# включаем глобальное сжатие (оставлять несжатые *.1, *.2 не нужно)
$SUDO sed -i 's/^#compress/compress/' /etc/logrotate.conf
grep -q '^compress' /etc/logrotate.conf || {
  echo 'compress' | $SUDO tee -a /etc/logrotate.conf >/dev/null
}
# rsyslog-ротация тоже должна сжиматься
$SUDO sed -i 's/^#compress/compress/' /etc/logrotate.d/rsyslog 2>/dev/null || true
echo "  compress: $(grep -h '^compress' /etc/logrotate.conf)"
fi
"""

UNATTENDED_UPGRADES = r"""
echo "==> unattended-upgrades: без интерактивных вопросов"
$SUDO apt-get install -y unattended-upgrades
$SUDO install -m 0755 -d /etc/apt/apt.conf.d
cat <<'UU_EOF' | $SUDO tee /etc/apt/apt.conf.d/52unattended-upgrades-local >/dev/null
// Локальные настройки install-vps.
// unattended-upgrades запускается без tty: на вопросы debconf/ucf про
// изменённые конфиги отвечать некому — процесс просто зависнет.
// --force-confdef: оставить версию из пакета, --force-confold: оставить свою.
Dpkg::Options {
  "--force-confdef";
  "--force-confold";
};
UU_EOF
if [ -f /etc/needrestart/needrestart.conf ]; then
  $SUDO sed -i 's/^\(NEEDRESTART_MODE=\).*/\1a/' /etc/needrestart/needrestart.conf
  grep -q '^NEEDRESTART_MODE=a' /etc/needrestart/needrestart.conf \
    || echo 'NEEDRESTART_MODE=a' \
      | $SUDO tee -a /etc/needrestart/needrestart.conf >/dev/null
  echo "  needrestart: NEEDRESTART_MODE=a (авто, без вопросов)"
fi
$SUDO systemctl enable unattended-upgrades.service >/dev/null 2>&1 || true
echo "  Dpkg::Options: --force-confdef --force-confold"
echo "  unattended-upgrades: $(systemctl is-enabled unattended-upgrades.service 2>/dev/null)"
echo "  apt-daily-upgrade.timer: $(systemctl is-enabled apt-daily-upgrade.timer 2>/dev/null)"
"""

KEYRING_CLEANUP = r"""
echo "==> чистим старые ядра (для малого места на диске)"
CURRENT="$(uname -r)"
# версионированные linux-image-* / linux-headers-* / linux-modules-*; пакеты
# текущего ядра неприкосновенны (без них хост не перезагрузится)
PROTECTED="$(printf 'linux-image-%s\nlinux-modules-%s\nlinux-headers-%s\nlinux-extra-%s' \
  "$CURRENT" "$CURRENT" "$CURRENT" "$CURRENT")"
OLD_KERNELS="$($SUDO dpkg -l 'linux-image-*' 'linux-headers-*' 'linux-modules-*' 2>/dev/null \
  | awk '/^ii/{print $2}' \
  | grep -E 'linux-(image|headers|modules)-[0-9]' \
  | grep -Fxv -f <(printf '%s\n' "$PROTECTED") || true)"
if [ -n "$OLD_KERNELS" ]; then
  # без --auto-remove: autoremove однажды снёс модули и образ текущего ядра
  echo "$OLD_KERNELS" | xargs -r $SUDO apt-get purge -y
else
  echo "  старых ядер не найдено — нечего чистить"
fi
if ! $SUDO dpkg -s "linux-image-$CURRENT" >/dev/null 2>&1; then
  echo "  ВОССТАНОВЛЕНИЕ: пакет ядра linux-image-$CURRENT отсутствует — ставим заново"
  $SUDO apt-get install -y "linux-image-$CURRENT" "linux-modules-$CURRENT"
fi
if [ -e "/boot/vmlinuz-$CURRENT" ]; then
  echo "  OK: /boot/vmlinuz-$CURRENT на месте (хост перезагрузится)"
else
  echo "  ВНИМАНИЕ: нет /boot/vmlinuz-$CURRENT — перезагрузка может не удаться!"
fi
echo "  осталось ядер: $($SUDO dpkg -l 'linux-image-*' 2>/dev/null | awk '/^ii/{print $2}' | wc -l)"
"""

BESZEL_DIR = "/opt/beszel"
BESZEL_COMPOSE = "/opt/beszel/docker-compose.yml"

# Dozzle: веб-морда над логами контейнеров. Живёт под базовым путём портала
# (DOZZLE_BASE) и слушает только 127.0.0.1: наружу его отдаёт Caddy.
# Версия закреплена: `latest` у dozzle означает majors, а логи сервера —
# чувствительное место, где молчаливый апгрейд особенно нежелателен.
DOZZLE_VERSION = "v11.1.1"
DOZZLE_DIR = "/opt/dozzle"
DOZZLE_COMPOSE = "/opt/dozzle/docker-compose.yml"
DOZZLE_USERS = "/opt/dozzle/users.yml"
DOZZLE_DATA = "/opt/dozzle/data"

# Caddy: постоянные пути. Сайты лежат отдельными файлами в sites/ и
# подключаются через import — новый сайт добавляется одним файлом.
CADDY_DIR = "/opt/caddy"
CADDY_COMPOSE = "/opt/caddy/docker-compose.yml"
CADDY_SITES_DIR = "/opt/caddy/sites"
# внутри контейнера sites/ примонтирован в /etc/caddy/sites — Caddyfile
# пишется и читается уже по контейнерному пути
CADDY_SITES_DIR_IN_CONTAINER = "/etc/caddy/sites"
# портал: генерируемая страница плиток, отдаётся как статика
PORTAL_DIR = "/opt/caddy/portal"
PORTAL_INDEX = f"{PORTAL_DIR}/index.html"
PORTAL_ROOT_IN_CONTAINER = "/srv/portal"

JOURNALD_LIMIT = r"""
echo "==> journald: ограничиваем размер журнала ({max_use})"
$SUDO install -m 0755 -d /etc/systemd/journald.conf.d
cat <<'JOURNALD_EOF' | $SUDO tee /etc/systemd/journald.conf.d/10-size-limit.conf >/dev/null
[Journal]
SystemMaxUse={max_use}
SystemKeepFree={keep_free}
MaxRetentionSec=2week
JOURNALD_EOF
$SUDO systemctl restart systemd-journald
$SUDO journalctl --vacuum-size={max_use} >/dev/null 2>&1 || true
echo "  $(grep -h '^SystemMaxUse' /etc/systemd/journald.conf.d/10-size-limit.conf)"
du -sh /var/log/journal 2>/dev/null || true
"""

DOCKER_DAEMON_JSON = "/etc/docker/daemon.json"

DOCKER_LOGROTATE = r"""
echo "==> docker: ротация логов контейнеров ({max_size} x {max_file})"
if ! command -v python3 >/dev/null 2>&1; then
  echo "  ВНИМАНИЕ: python3 не найден — настройка логов docker пропущена."
else
  $SUDO install -m 0755 -d /etc/docker
  CHANGED="$($SUDO python3 - {daemon_json} "{max_size}" "{max_file}" <<'DOCKER_EOF_PY'
import json
import os
import sys

path, max_size, max_file = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    cfg = {{}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        if text:
            cfg = json.loads(text)
    if not isinstance(cfg, dict):
        raise ValueError("not an object")
    opts = dict(cfg.get("log-opts") or {{}})
    opts["max-size"] = max_size
    opts["max-file"] = max_file
    updated = dict(cfg)
    updated["log-driver"] = "json-file"
    updated["log-opts"] = opts
    if updated == cfg:
        print("same")
    else:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        print("changed")
except Exception as exc:
    print("ERROR:" + str(exc))
DOCKER_EOF_PY
)"
  case "$CHANGED" in
    changed)
      echo "  обновлён {daemon_json}: log-driver=json-file"
      $SUDO systemctl restart docker
      for _ in $(seq 1 30); do
        if $SUDO docker info >/dev/null 2>&1; then break; fi
        sleep 1
      done
      ;;
    same)
      echo "  {daemon_json} уже настроен — рестарт docker не требуется"
      ;;
    *)
      echo "  ВНИМАНИЕ: {daemon_json} не перезаписан ($CHANGED)."
      echo "  Проверьте файл вручную; действующая конфигурация docker не изменена."
      ;;
  esac
fi
"""

ZRAM = r"""
echo "==> zram: сжатый своп {size_mb}M (алгоритм zstd)"
$SUDO apt-get install -y systemd-zram-generator
$SUDO install -m 0755 -d /etc/systemd
cat <<'ZRAM_CONF_EOF' | $SUDO tee /etc/systemd/zram-generator.conf >/dev/null
[zram0]
zram-size = {size_mb}M
compression-algorithm = zstd
ZRAM_CONF_EOF
# systemd-zram-generator на слабой памяти падает с ENOMEM (Committed_AS выше
# CommitLimit) и не повторяет попытку — zram не появляется после перезагрузки.
# Свой юнит запускается позже, когда память уже разгружена, и уменьшает
# размер по шагам, пока не хватит.
cat <<'ZRAM_UNIT_EOF' | $SUDO tee /etc/systemd/system/zram-swap.service >/dev/null
[Unit]
Description=Compressed swap in RAM (zram)
After=systemd-zram-generator.service swap.target
Before=multi-user.target
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=ZRAM_TARGET_MB={size_mb}
ExecStart=/usr/local/sbin/zram-swap-up

[Install]
WantedBy=multi-user.target
ZRAM_UNIT_EOF
cat <<'ZRAM_SCRIPT_EOF' | $SUDO tee /usr/local/sbin/zram-swap-up >/dev/null
#!/bin/bash
# Ставит сжатый своп zram, уменьшая размер, пока хватает памяти.
set -uo pipefail

TARGET_MB="${{ZRAM_TARGET_MB:-{size_mb}}}"
MIN_MB=32
DISKSIZE=/sys/block/zram0/disksize

if swapon --show=NAME --noheadings | grep -q '^/dev/zram'; then
  echo "zram уже активен"
  exit 0
fi

modprobe zram 2>/dev/null || true
for _ in $(seq 1 10); do
  [ -e "$DISKSIZE" ] && break
  sleep 1
done
if [ ! -e "$DISKSIZE" ]; then
  echo "zram: /sys/block/zram0 недоступен, модуль zram не загрузился" >&2
  exit 1
fi

case "$TARGET_MB" in
  ''|*[!0-9]*) echo "zram: некорректный размер '$TARGET_MB'" >&2; exit 1 ;;
esac
if [ "$TARGET_MB" -lt "$MIN_MB" ]; then
  TARGET_MB="$MIN_MB"
fi
# zram больше всей RAM бесполезен и опасен: memlimit вытеснит в своп всё
# остальное. Ограничиваем размером физической памяти.
RAM_KB="$(awk '/^MemTotal:/{{print $2}}' /proc/meminfo)"
RAM_MB=$((RAM_KB / 1024))
if [ "$RAM_MB" -gt 0 ] && [ "$TARGET_MB" -gt "$RAM_MB" ]; then
  echo "zram: запрошено ${{TARGET_MB}}M > RAM ${{RAM_MB}}M, ограничиваю"
  TARGET_MB="$RAM_MB"
fi

MB="$TARGET_MB"
ATTEMPT=0
while [ "$MB" -ge "$MIN_MB" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  # сброс обязателен: повторная запись disksize без него вернёт EINVAL
  timeout 10 sh -c "printf '%s\n' 1 > /sys/block/zram0/reset" 2>/dev/null || true
  # запись в disksize может висеть неограниченно долго, если ядро уходит в
  # своп-цикл при выделении memlimit — ограничиваем и падаем на таймауте
  if timeout 10 sh -c "printf '%s\n' $((MB * 1024 * 1024)) > $DISKSIZE" \
      2>/dev/null; then
    printf '%s\n' zstd > /sys/block/zram0/comp_algorithm 2>/dev/null || true
    if mkswap /dev/zram0 >/dev/null 2>&1 \
      && swapon -p 100 /dev/zram0 2>/dev/null; then
      echo "zram: активирован ${{MB}}M (попытка $ATTEMPT)"
      exit 0
    fi
  fi
  echo "zram: ${{MB}}M не удалось выделить, уменьшаю" >&2
  NEXT=$((MB / 2))
  [ "$NEXT" -lt "$MIN_MB" ] && NEXT="$MIN_MB"
  MB="$NEXT"
  sleep 2
done

echo "zram: не удалось выделить даже ${{MIN_MB}}M" >&2
exit 1
ZRAM_SCRIPT_EOF
$SUDO chmod 0755 /usr/local/sbin/zram-swap-up
$SUDO systemctl daemon-reload
$SUDO systemctl enable zram-swap.service >/dev/null 2>&1 || true
if swapon --show=NAME --noheadings | grep -q '^/dev/zram'; then
  echo "  zram уже активен, конфигурацию не трогаем"
else
  $SUDO /usr/local/sbin/zram-swap-up
fi
swapon --show
echo "  после перезагрузки zram поднимет zram-swap.service"
"""

BESZEL_STACK = r"""
echo "==> Beszel Hub + Agent (docker compose, порт {port})"
$SUDO install -m 0755 -d {dir}
{key_read}cat <<'BESZEL_EOF' | $SUDO tee {compose} >/dev/null
services:
  beszel:
    image: henrygd/beszel:latest
    container_name: beszel
    restart: unless-stopped
    environment:
      APP_URL: {app_url}{auth_env}
    ports:
      - "{port}:{port}"
    volumes:
      - ./beszel_data:/beszel_data
      - ./beszel_socket:/beszel_socket

  beszel-agent:
    image: henrygd/beszel-agent:latest
    container_name: beszel-agent
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./beszel_agent_data:/var/lib/beszel-agent
      - ./beszel_socket:/beszel_socket
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      LISTEN: /beszel_socket/beszel.sock
      HUB_URL: http://localhost:{port}
      KEY: "{key}"
      TOKEN: "{token}"
BESZEL_EOF
{key_fix}{auth_warning}if [ -z {key_quoted} ]; then
  echo "  ВНИМАНИЕ: не задан beszel_agent_key — новый агент не сможет подключиться."
  echo "  Откройте http://<host>:{port} -> создайте пользователя -> Add system,"
  echo "  скопируйте ключ агента в beszel_agent_key (config.toml или --beszel-key)"
  echo "  и запустите install-vps повторно."
fi
if [ -z {token_quoted} ]; then
  echo "  ВНИМАНИЕ: не задан beszel_agent_token — новый агент не сможет подключиться."
  echo "  Токен виден в том же диалоге Add system (или в настройках -> tokens)."
fi
$SUDO docker compose -f {compose} up -d --remove-orphans
$SUDO docker compose -f {compose} ps
echo "  Hub (внутренний): http://localhost:{port}"
echo "  Hub (публичный): {public_url}"
"""

CADDY = r"""
echo "==> Caddy: обратный прокси с автоматическим HTTPS (порты 80/443)"
$SUDO install -m 0755 -d {dir} {sites_dir} {portal_dir}
cat <<'CADDY_COMPOSE_EOF' | $SUDO tee {compose} >/dev/null
services:
  caddy:
    image: caddy:2-alpine
    container_name: caddy
    restart: unless-stopped
    network_mode: host
    environment:
      ACME_EMAIL: "{email}"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./sites:/etc/caddy/sites:ro
      - ./portal:/srv/portal:ro
      - caddy_data:/data
      - caddy_config:/config
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  caddy_data:
  caddy_config:
CADDY_COMPOSE_EOF
cat <<'CADDYFILE_EOF' | $SUDO tee {dir}/Caddyfile >/dev/null
{caddyfile}
CADDYFILE_EOF
{sites}
$SUDO docker compose -f {compose} up -d --remove-orphans
$SUDO docker compose -f {compose} ps
for _ in $(seq 1 30); do
  if $SUDO docker ps --format '{{{{.Names}}}}' | grep -qx caddy; then break; fi
  sleep 2
done
if $SUDO docker ps --format '{{{{.Names}}}}' | grep -qx caddy; then
  # конфиг примонтирован, но Caddy держит старый в памяти — нужен reload,
  # иначе повторный прогон ничего не применит
  if $SUDO docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "  конфиг перечитан (caddy reload)"
  else
    echo "  caddy reload не удался, перезапускаю контейнер"
    $SUDO docker restart caddy >/dev/null
  fi
else
  echo "  ОШИБКА: контейнер caddy не запустился:"
  $SUDO docker compose -f {compose} logs 2>&1 | tail -20 | sed 's/^/    /'
  exit 1
fi
if ! $SUDO docker exec caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  echo "  ОШИБКА: Caddyfile не прошёл проверку:"
  $SUDO docker exec caddy caddy validate --config /etc/caddy/Caddyfile 2>&1 | sed 's/^/    /'
  exit 1
fi
echo "  Caddyfile корректен (caddy validate)"
echo "  сайты: {sites_dir}/*.caddy — новый сайт добавляется одним файлом"
echo "  перечитать: $SUDO docker exec caddy caddy reload --config /etc/caddy/Caddyfile"
echo "  логи:      $SUDO docker logs -f caddy"
"""

CADDY_VERIFY = r"""
if $SUDO docker ps --format '{{{{.Names}}}}' | grep -qx caddy; then
  echo "  OK: caddy -> $($SUDO docker ps --filter name=^/caddy$ --format '{{{{.Status}}}}')"
  if $SUDO ss -tln 2>/dev/null | grep -qE ':(80|443)[[:space:]]'; then
    echo "  OK: caddy слушает 80/443"
  else
    echo "  WARN: порты 80/443 не слушаются"
  fi
  if $SUDO docker exec caddy caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "  OK: Caddyfile валиден"
  else
    echo "  MISSING: Caddyfile невалиден"
  fi
else
  echo "  MISSING: caddy"
fi
for f in $($SUDO ls {sites_dir}/*.caddy 2>/dev/null); do
  [ -e "$f" ] || continue
  echo "  сайт: $(basename "$f" .caddy)"
done
"""

VERIFY = r"""
echo "==> проверка"
for cmd in nload htop btop mc git vmstat; do
  if command -v $cmd >/dev/null 2>&1; then
    echo "  OK: $cmd -> $(command -v $cmd)"
  else
    echo "  MISSING: $cmd"
  fi
done
if command -v fail2ban-client >/dev/null 2>&1; then
  echo "  OK: fail2ban -> $(command -v fail2ban-client)"
elif $SUDO systemctl is-active fail2ban >/dev/null 2>&1; then
  echo "  OK: fail2ban (systemd) -> $($SUDO systemctl is-active fail2ban)"
else
  echo "  MISSING: fail2ban"
fi
if command -v ncdu >/dev/null 2>&1; then
  echo "  OK: ncdu -> $(command -v ncdu)"
else
  echo "  MISSING: ncdu"
fi
if command -v docker-compose >/dev/null 2>&1; then
  echo "  OK: docker-compose -> $(command -v docker-compose)"
elif ls /usr/libexec/docker/cli-plugins/docker-compose >/dev/null 2>&1; then
  echo "  OK: docker compose plugin -> /usr/libexec/docker/cli-plugins/docker-compose"
elif $SUDO docker compose version >/dev/null 2>&1; then
  echo "  OK: docker compose plugin -> $($SUDO docker compose version)"
else
  echo "  MISSING: docker-compose-v2"
fi
if $SUDO swapon --show | grep -q '^/swapfile'; then
  echo "  OK: swap -> $($SUDO swapon --show)"
else
  echo "  MISSING: swap"
fi
"""

BESZEL_VERIFY = r"""
if $SUDO docker ps --format '{{.Names}}' | grep -qx beszel; then
  echo "  OK: beszel (hub) -> $($SUDO docker ps --filter name=^/beszel$ --format '{{.Status}}')"
else
  echo "  MISSING: beszel (hub)"
fi
if $SUDO docker ps --format '{{.Names}}' | grep -qx beszel-agent; then
  echo "  OK: beszel-agent -> $($SUDO docker ps --filter name=^/beszel-agent$ --format '{{.Status}}')"
else
  echo "  MISSING: beszel-agent"
fi
"""

DOZZLE_STACK = r"""
echo "==> Dozzle {version}: логи контейнеров (127.0.0.1:{port}, путь {base})"
$SUDO install -m 0755 -d {dir} {data_dir}
{users_block}cat <<'DOZZLE_EOF' | $SUDO tee {compose} >/dev/null
services:
  dozzle:
    image: {image}
    container_name: dozzle
    restart: unless-stopped
    # только localhost: наружу отдаёт Caddy, иначе логи были бы доступны
    # всем, кто угадает порт, в обход авторизации
    ports:
      - "127.0.0.1:{port}:8080"
    environment:
      DOZZLE_BASE: {base}
      DOZZLE_AUTH_PROVIDER: simple
      DOZZLE_ENABLE_ACTIONS: "false"
      DOZZLE_ENABLE_SHELL: "false"
      DOZZLE_DISABLE_AVATARS: "true"
      DOZZLE_NO_ANALYTICS: "true"
      DOZZLE_LEVEL: info
      DOZZLE_TIMEOUT: 30s
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./users.yml:/data/users.yml:ro
      - ./data:/data
DOZZLE_EOF
$SUDO docker compose -f {compose} up -d --remove-orphans
$SUDO docker compose -f {compose} ps
for _ in $(seq 1 30); do
  if $SUDO docker ps --format '{{{{.Names}}}}' | grep -qx dozzle; then break; fi
  sleep 2
done
"""

DOZZLE_VERIFY = r"""
# docker ps --format принимает шаблон Go: после format() quadruple-скобки
# становятся {{.Names}}, иначе docker получает битый шаблон и молчит
if $SUDO docker ps --format '{{{{.Names}}}}' | grep -qx dozzle; then
  echo "  OK: dozzle -> $($SUDO docker ps --filter name=^/dozzle$ --format '{{{{.Status}}}}')"
else
  echo "  MISSING: dozzle"
fi
CODE="$($SUDO curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}{base}/ 2>/dev/null || true)"
[ -n "$CODE" ] || CODE=000
case "$CODE" in
  200|302|303|307) echo "  OK: dozzle отвечает на 127.0.0.1:{port}{base}/ (код $CODE — редирект" \
      "на форму входа, авторизация включена)" ;;
  401) echo "  OK: dozzle отвечает и требует входа (401)" ;;
  000) echo "  MISSING: dozzle не отвечает на 127.0.0.1:{port}{base}/" ;;
  *) echo "  WARN: dozzle ответил кодом $CODE" ;;
esac
"""


def _codename(cfg: Config, host: RemoteHost) -> str:
    """Определяет VERSION_CODENAME на удалённом хосте через /etc/os-release."""
    cmd = [
        *host.base_cmd(),
        host.target(),
        ". /etc/os-release && printf '%s %s\\n' \"$VERSION_ID\" \"$VERSION_CODENAME\"",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Не удалось определить дистрибутив: {proc.stderr.strip()}")
    fields = proc.stdout.strip().split()
    if len(fields) != _OS_RELEASE_FIELDS:
        raise RuntimeError(f"Неожиданный ответ os-release: {proc.stdout!r}")
    version_id, codename = fields
    if not version_id.startswith("24.04") and not version_id.startswith("26.04"):
        raise SystemExit(f"Поддерживается Ubuntu 24.04/26.04, а на хосте — {version_id}")
    return codename


def _validate(cfg: Config) -> list[Tile]:
    """Проверки, которые обязаны случиться до отправки скрипта на хост.

    Ошибка в конфиге (домен, upstream, путь плитки) не должна оставлять хост
    наполовину настроенным, поэтому всё это проверяется на стороне Python.
    """
    if cfg.beszel and cfg.caddy and cfg.caddy_portal:
        path = _beszel_path(cfg)
        if not _PORTAL_PATH_RE.match(path):
            raise ValueError(f"Некорректный beszel_path: {cfg.beszel_path!r}")
    if cfg.dozzle and not cfg.caddy_portal:
        raise ValueError(
            "dozzle требует caddy_portal: Dozzle живёт под базовым путём "
            "портала (DOZZLE_BASE), отдельный домен ему не нужен. Задайте "
            "портал (--caddy-portal ДОМЕН) вместе с --dozzle"
        )
    if cfg.dozzle and cfg.caddy_portal:
        path = _dozzle_path(cfg)
        if not _PORTAL_PATH_RE.match(path):
            raise ValueError(f"Некорректный dozzle_path: {cfg.dozzle_path!r}")
    if cfg.caddy_portal and not _HOSTNAME_RE.match(cfg.caddy_portal):
        raise ValueError(f"Некорректный домен портала: {cfg.caddy_portal!r}")
    for name in cfg.tools:
        if name not in _GITHUB_TOOLS:
            raise ValueError(
                f"Неизвестная утилита в tools: {name!r}; доступны: "
                + ", ".join(EXTRA_TOOLS)
            )
    if not cfg.caddy:
        return []
    if cfg.caddy_tiles and not cfg.caddy_portal:
        raise ValueError(
            "caddy_tiles требует caddy_portal: плитка-прокси живёт на домене "
            "портала. Либо задайте портал (--caddy-portal ДОМЕН), либо опишите "
            "сервис отдельным сайтом (--caddy-site ДОМЕН=UPSTREAM)"
        )
    tiles = _tiles(cfg)
    # Генераторы и есть валидация того, что реально уйдёт на хост: домены,
    # upstream, заголовок портала. Прогоняем их до похода в SSH, чтобы
    # ошибка в конфиге не стоила ещё одного подключения.
    if cfg.caddy_email and not _EMAIL_RE.match(cfg.caddy_email):
        raise ValueError(f"Некорректный caddy_email: {cfg.caddy_email!r}")
    _caddy_sites_script(cfg, portal=bool(cfg.caddy_portal))
    if cfg.caddy_portal:
        _portal_script(cfg, tiles)
    return tiles


def _beszel_key_keep(compose: str, key: str, token: str) -> tuple[str, str]:
    """(чтение, подстановка) — чтобы не затереть ключ агента на хосте.

    Повторный прогон install-vps без beszel_agent_key/token переписал бы
    compose пустыми KEY/TOKEN и тихо отвалил бы уже работающий мониторинг.
    Поэтому в compose попадает маркер, который bash заменяет на прежнее
    значение из существующего файла (если оно там есть).
    """
    if key and token:
        return "", ""
    reads: list[str] = []
    fixes: list[str] = []
    for value, name in ((key, "KEY"), (token, "TOKEN")):
        if value:
            continue
        marker = f"__BESZEL_{name}_KEEP__"
        var = f"BESZEL_OLD_{name}"
        reads.append(
            f'{var}="$(sed -n \'s/^ *{name}: "\\(.*\\)"$/\\1/p\' {compose} | head -1)"\n'
        )
        fixes.append(
            f'if [ -n "${var}" ]; then\n'
            f'  $SUDO sed -i "s|{marker}|${{{var}}}|" {compose}\n'
            f'  echo "  {name.lower()} агента: оставлен прежний (в конфиге не задан)"\n'
            f"else\n"
            f'  $SUDO sed -i "s|{marker}||" {compose}\n'
            f"fi\n"
        )
    return "".join(reads), "".join(fixes)


def _dozzle_users_block(cfg: Config, image: str) -> str:
    """Создание users.yml: пароль из конфига либо сгенерированный на хосте.

    users.yml хранит bcrypt-хеш, поэтому восстановить из него пароль нельзя.
    Если пароль не задан в конфиге, файл создаётся один раз и дальше не
    трогается: иначе повторный прогон тихо сбрасывал бы пароль, который
    пользователь уже запомнил. Задать свой — dozzle_password в конфиге.
    Пароль передаётся в контейнер через stdin, а не --password, чтобы не
    светился в ps хоста.
    """
    create = (
        "  if ! echo \"$DOZZLE_PASS\" | $SUDO docker run --rm -i {image} generate "
        '{user} > "$TMP_USERS"; then\n'
        '    echo "  ОШИБКА: не удалось создать users.yml" >&2\n'
        '    rm -f "$TMP_USERS"; exit 1\n'
        "  fi\n"
        '  if [ ! -s "$TMP_USERS" ]; then\n'
        '    echo "  ОШИБКА: users.yml пустой" >&2\n'
        '    rm -f "$TMP_USERS"; exit 1\n'
        "  fi\n"
        '  $SUDO install -m 0600 "$TMP_USERS" {users}\n'
        '  rm -f "$TMP_USERS"\n'
    ).format(image=image, user=_sh_quote(cfg.dozzle_user), users=DOZZLE_USERS)
    if cfg.dozzle_password:
        return (
            'TMP_USERS="$(mktemp)"\n'
            f"DOZZLE_PASS={_sh_quote(cfg.dozzle_password)}\n"
            + create
            + f'  echo "  логин: {cfg.dozzle_user} (пароль из конфига)"\n'
        )
    return (
        f"if [ -s {DOZZLE_USERS} ]; then\n"
        '  echo "  users.yml уже есть — пароль прежний, не трогаю"\n'
        "else\n"
        '  TMP_USERS="$(mktemp)"\n'
        "  DOZZLE_PASS=\"$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-16)\"\n"
        '  if [ -z "$DOZZLE_PASS" ]; then\n'
        '    echo "  ОШИБКА: не удалось сгенерировать пароль" >&2; exit 1\n'
        "  fi\n"
        + create
        + f'  echo "  логин: {cfg.dozzle_user}"\n'
        '  echo "  пароль (сохраните, показывается один раз): $DOZZLE_PASS"\n'
        "fi\n"
    )


def _dozzle_script(cfg: Config) -> str:
    """Фрагмент установки Dozzle: users.yml, compose, запуск."""
    if not _PORT_MIN <= cfg.dozzle_port <= _PORT_MAX:
        raise ValueError(f"Некорректный dozzle_port: {cfg.dozzle_port!r}")
    if any(ch in cfg.dozzle_user for ch in "/\\\n\r\"' $"):
        raise ValueError(
            f"Недопустимое имя пользователя dozzle: {cfg.dozzle_user!r}"
        )
    if any(ch in cfg.dozzle_password for ch in "\n\r\"'"):
        raise ValueError(
            "dozzle_password не должен содержать кавычки или перевод строки"
        )
    image = f"amir20/dozzle:{DOZZLE_VERSION}"
    base = _dozzle_path(cfg)
    return DOZZLE_STACK.format(
        version=DOZZLE_VERSION,
        dir=DOZZLE_DIR,
        data_dir=DOZZLE_DATA,
        compose=DOZZLE_COMPOSE,
        users=DOZZLE_USERS,
        image=image,
        port=cfg.dozzle_port,
        base=_yaml_str(base),
        users_block=_dozzle_users_block(cfg, image),
    )


def _dozzle_verify_script(cfg: Config) -> str:
    """Проверка Dozzle (работает и в --verify-only)."""
    return DOZZLE_VERIFY.format(port=cfg.dozzle_port, base=_dozzle_path(cfg))


def _beszel_script(cfg: Config) -> str:
    """Фрагмент установки Beszel Hub+Agent: compose, проверка, валидация ключей."""
    for label, value in (
        ("beszel_agent_key", cfg.beszel_agent_key),
        ("beszel_agent_token", cfg.beszel_agent_token),
    ):
        if any(ch in value for ch in '"\n\r'):
            raise ValueError(
                f"{label} содержит недопустимые символы "
                '(кавычки или перевод строки); скопируйте значение целиком'
            )
    if cfg.beszel_disable_password_auth and not cfg.beszel_user_creation:
        raise ValueError(
            "beszel_disable_password_auth требует beszel_user_creation: "
            "иначе новые пользователи не смогут войти через OAuth и вы "
            "потеряете доступ к Hub. Сначала настройте OAuth в веб-UI, "
            "затем включайте оба флага вместе"
        )
    public_url = _beszel_public_url(cfg)
    key_read, key_fix = _beszel_key_keep(
        BESZEL_COMPOSE, cfg.beszel_agent_key, cfg.beszel_agent_token
    )
    return BESZEL_STACK.format(
        port=cfg.beszel_port,
        dir=BESZEL_DIR,
        compose=BESZEL_COMPOSE,
        key_read=key_read,
        key_fix=key_fix,
        app_url=_yaml_str(public_url),
        public_url=public_url,
        auth_env=_beszel_auth_env(cfg),
        auth_warning=_beszel_auth_warning(cfg),
        key=cfg.beszel_agent_key or "__BESZEL_KEY_KEEP__",
        key_quoted=_sh_quote(cfg.beszel_agent_key),
        token=cfg.beszel_agent_token or "__BESZEL_TOKEN_KEEP__",
        token_quoted=_sh_quote(cfg.beszel_agent_token),
    ) + BESZEL_VERIFY


def install(cfg: Config, host: RemoteHost) -> None:
    """Базовая настройка минимального набора софта."""
    tiles = _validate(cfg)
    codename = _codename(cfg, host)
    script = _bootstrap(cfg.sudo)
    script += APT_UPDATE
    script += PKG_INSTALL.format(packages="curl ca-certificates gnupg")
    packages = list(cfg.packages)
    if cfg.docker_source == "official":
        # официальный docker-ce: свой репозиторий + свои имена пакетов
        script += DOCKER_REPO.format(
            key=DOCKER_REPO_KEY,
            keyring_dir=DOCKER_KEYRING_DIR,
            keyring_file=DOCKER_KEYRING_FILE,
            codename=codename,
            sources_list=DOCKER_SOURCES_LIST,
        )
        packages = [
            p for p in packages
            if p not in _DOCKER_UBUNTU_PACKAGES
        ] + _DOCKER_OFFICIAL_PACKAGES
    # иначе ("ubuntu"): docker.io/docker-compose-v2 уже в cfg.packages
    script += PKG_INSTALL.format(packages=" ".join(packages))
    if cfg.swap_size_mb > 0:
        script += SWAP.format(size_mb=cfg.swap_size_mb)
    if cfg.tools:
        script += _tools_script(list(cfg.tools))
    script += JOURNALD_LIMIT.format(
        max_use=cfg.journald_max_use,
        keep_free="200M",
    )
    script += LOGROTATE_COMPRESS
    script += UNATTENDED_UPGRADES
    script += KEYRING_CLEANUP
    if cfg.zram_size_mb > 0:
        script += ZRAM.format(size_mb=cfg.zram_size_mb)
    if any(p.startswith("docker") for p in packages) or cfg.beszel:
        script += DOCKER_LOGROTATE.format(
            daemon_json=DOCKER_DAEMON_JSON,
            max_size=cfg.docker_log_max_size,
            max_file=cfg.docker_log_max_file,
        )
    if cfg.beszel:
        script += _beszel_script(cfg)
    if cfg.dozzle:
        script += _dozzle_script(cfg)
    if cfg.caddy:
        sites = _caddy_sites_script(cfg, portal=bool(cfg.caddy_portal))
        if cfg.caddy_portal:
            sites += _portal_script(cfg, tiles)
        script += CADDY.format(
            dir=CADDY_DIR,
            compose=CADDY_COMPOSE,
            sites_dir=CADDY_SITES_DIR,
            portal_dir=PORTAL_DIR,
            email=cfg.caddy_email,
            caddyfile=_caddy_caddyfile(cfg.caddy_email),
            sites=sites,
        )
        script += CADDY_VERIFY.format(sites_dir=CADDY_SITES_DIR)
        if cfg.caddy_portal:
            script += _portal_verify_script(cfg, tiles)
    if cfg.tools:
        script += _tools_verify_script(list(cfg.tools))
    script += VERIFY
    host.run_script(script)


def verify(cfg: Config, host: RemoteHost) -> None:
    tiles = _validate(cfg)
    script = _bootstrap(cfg.sudo)
    if cfg.beszel:
        script += BESZEL_VERIFY
    if cfg.dozzle:
        script += _dozzle_verify_script(cfg)
    if cfg.caddy:
        script += CADDY_VERIFY.format(sites_dir=CADDY_SITES_DIR)
        if cfg.caddy_portal:
            script += _portal_verify_script(cfg, tiles)
    if cfg.tools:
        script += _tools_verify_script(list(cfg.tools))
    script += VERIFY
    host.run_script(script)
