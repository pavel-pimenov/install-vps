from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .config import apply_overrides, load_config
from .installer import EXTRA_TOOLS, install, verify
from .ssh import RemoteHost


def _add_beszel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--beszel", action="store_true",
                   help="поднять Beszel: хаб+агент (--beszel-mode hub) "
                        "либо только агент для удалённого хаба (--beszel-mode agent)")
    p.add_argument("--beszel-mode", choices=("hub", "agent"),
                   help="что ставить: hub — хаб с локальным агентом (по умолчанию), "
                        "agent — только агент, к которому ходит хаб с другого сервера")
    p.add_argument("--beszel-agent-allow", metavar="CIDR", action="append",
                   help="кому открыть порт агента (45876) в режиме agent, "
                        "например --beszel-agent-allow 185.50.202.219/32 "
                        "(можно указать несколько раз)")
    p.add_argument("--beszel-port", type=int, help="порт Beszel Hub (по умолчанию 8090)")
    p.add_argument("--beszel-version", metavar="X.Y.Z",
                   help="версия образов henrygd/beszel и henrygd/beszel-agent "
                        "(по умолчанию 0.20.0); hub и agent должны совпадать")
    p.add_argument("--beszel-key",
                   help="публичный ключ агента Beszel (из веб-UI Hub: Add system)")
    p.add_argument("--beszel-token",
                   help="токен агента Beszel (из того же диалога Add system)")
    p.add_argument("--beszel-hub-url", metavar="URL",
                   help="агент сам звонит в хаб по этому URL (https://<домен> "
                        "при хабе под Caddy); нужно, когда провайдер узла режет входящие порты")
    p.add_argument("--beszel-user-creation", action="store_true",
                   help="разрешить регистрацию новых пользователей (нужно для OAuth)")
    p.add_argument("--beszel-disable-password-auth", action="store_true",
                   help="вход только через OAuth, пароль отключить "
                        "(включайте лишь после проверки, что OAuth работает — иначе логин потерян)")
    p.add_argument("--beszel-path", metavar="PATH",
                   help="путь плитки Beszel на портале (по умолчанию /monitor)")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install-vps",
        description="Стартовая настройка новой VPS (Ubuntu 24.04/26.04) по SSH-ключу.",
    )
    p.add_argument("host", nargs="?", help="IP или DNS имя VPS (перекроет конфиг)")
    p.add_argument("-c", "--config", type=Path, default=Path("config.toml"),
                   help="путь к конфигу (по умолчанию config.toml)")
    p.add_argument("-u", "--user", help="пользователь SSH (по умолчанию root)")
    p.add_argument("-p", "--port", type=int, help="порт SSH (по умолчанию 22)")
    p.add_argument("-k", "--key", help="путь к закрытому ключу")
    p.add_argument("--sudo", action="store_true", help="принудительно через sudo")
    p.add_argument("--no-accept-new", action="store_true",
                   help="не добавлять новый host в known_hosts автоматически")
    p.add_argument("--packages", nargs="*",
                   help="список пакетов "
                        "(по умолчанию: nload htop btop mc git docker-compose-v2)")
    p.add_argument("--tools", nargs="*", choices=sorted(EXTRA_TOOLS),
                   metavar="TOOL",
                   help="утилиты вне репозиториев Ubuntu, ставятся из релизов "
                        "GitHub с проверкой sha256: "
                        + ", ".join(sorted(EXTRA_TOOLS)))
    p.add_argument("--swap-size-mb", type=int,
                   help="размер файла подкачки в МБ, 0 — не трогать (по умолчанию 512)")
    p.add_argument("--docker-source", choices=("ubuntu", "official"),
                   help="источник docker: ubuntu (docker.io, по умолчанию) "
                        "или official (docker-ce с download.docker.com)")
    p.add_argument("--verify-only", action="store_true",
                   help="только проверить наличие пакетов, ничего не ставить")
    _add_beszel_args(p)
    p.add_argument("--dozzle", action="store_true",
                   help="логи контейнеров в браузере (Dozzle) под путём портала, "
                        "с входом по паролю; требует --caddy-portal")
    p.add_argument("--dozzle-port", type=int,
                   help="локальный порт Dozzle, слушает только 127.0.0.1 "
                        "(по умолчанию 8082)")
    p.add_argument("--dozzle-path", metavar="PATH",
                   help="путь плитки логов на портале (по умолчанию /logs)")
    p.add_argument("--dozzle-user", metavar="NAME",
                   help="логин Dozzle (по умолчанию admin)")
    p.add_argument("--dozzle-password", metavar="PASSWORD",
                   help="пароль Dozzle; не задан — сгенерируется на хосте и "
                        "покажется один раз, users.yml больше не перезаписывается")
    p.add_argument("--zram-size-mb", type=int,
                   help="размер zram-свопа в МБ, 0 — не трогать (по умолчанию 0)")
    p.add_argument("--journald-max-use", metavar="SIZE",
                   help="лимит systemd-журнала, напр. 100M (по умолчанию 100M)")
    p.add_argument("--docker-log-max-size", metavar="SIZE",
                   help="размер лога контейнера, напр. 10m (по умолчанию 10m)")
    p.add_argument("--docker-log-max-file", metavar="N",
                   help="число файлов лога на контейнер (по умолчанию 3)")
    p.add_argument("--caddy", action="store_true",
                   help="поставить Caddy: автоматический HTTPS для сайтов (80/443)")
    p.add_argument("--caddy-email", metavar="EMAIL",
                   help="e-mail для Let's Encrypt (уведомления об истечении сертификата)")
    p.add_argument("--caddy-domain", metavar="DOMAIN",
                   help="домен для Beszel Hub, напр. monitor.example.com")
    p.add_argument("--portainer", action="store_true",
                   help="поставить Portainer CE: сервер с UI (--portainer-mode server) "
                        "либо edge-агент узла (--portainer-mode agent)")
    p.add_argument("--portainer-mode", choices=("server", "agent"),
                   help="что ставить: server — UI и туннель (по умолчанию), "
                        "agent — edge-агент, который сам ходит на сервер")
    p.add_argument("--portainer-domain", metavar="DOMAIN",
                   help="домен для UI Portainer, напр. portainer.example.com "
                        "(для mode=server обязателен: под путём портала Portainer "
                        "не работает)")
    p.add_argument("--portainer-version", metavar="ВЕРСИЯ",
                   help="версия образов portainer-ce и portainer/agent "
                        "(по умолчанию 2.45.1; сервер и агенты должны совпадать)")
    p.add_argument(
        "--portainer-local-env",
        metavar="ИМЯ",
        help="создать локальное окружение Portainer с таким именем: без него "
        "в списке узлов нет самого сервера, хотя docker socket хоста под рукой",
    )
    p.add_argument("--portainer-admin-password", metavar="ПАРОЛЬ",
                   help="пароль первого администратора Portainer: окно создания "
                        "admin в UI длится 5 секунд, потом портал его больше не "
                        "предложит. Пароль пишется на хосте файлом 0600 и не "
                        "светится в compose")
    p.add_argument("--portainer-tunnel-port", metavar="ПОРТ", type=int,
                   help="порт туннеля edge-агентов ВНУТРИ контейнера сервера: "
                        "именно он попадает в EDGE_KEY и по нему агент идёт на "
                        "<домен>:<порт> (по умолчанию 80 — единственный вариант, "
                        "который проксирует Caddy; агент умеет только ws://)")
    p.add_argument("--portainer-agent-edge-id", metavar="UUID",
                   help="EDGE_ID окружения из UI Portainer (Environments -> "
                        "окружение -> Edge agent standard); нужен для mode=agent")
    p.add_argument("--portainer-agent-edge-key", metavar="BASE64",
                   help="EDGE_KEY того же окружения; вместе с EDGE_ID "
                        "достаточен для mode=agent, домен задавать не нужно")
    p.add_argument("--caddy-site", metavar="DOMAIN=UPSTREAM", action="append",
                   help="любой сайт: monitor.example.com=http://127.0.0.1:8080 "
                        "(можно указать несколько раз)")
    p.add_argument("--caddy-portal", metavar="DOMAIN",
                   help="домен страницы с плитками сервисов: HTTPS сам, "
                        "плитки ведут на сервисы этого сервера")
    p.add_argument("--caddy-portal-title", metavar="TITLE",
                   help="заголовок страницы плиток (по умолчанию «Сервисы»)")
    p.add_argument("--portal-user", metavar="USER",
                   help="логин для входа на портал (по умолчанию «admin»)")
    p.add_argument("--portal-password", metavar="PASSWORD",
                   help="пароль входа на портал: спрашивается один раз, "
                        "внутренние сервисы со своими формами не мешают "
                        "(Dozzle перестаёт спрашивать отдельно)")
    p.add_argument("--caddy-tile", metavar="TILE", action="append",
                   help="плитка портала: Мониторинг=/monitor=127.0.0.1:8090 "
                        "(префикс срезается), или Мониторинг=https://домен "
                        "для внешней ссылки (можно указать несколько раз)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config(args.config if args.config.exists() else None)
    cfg = apply_overrides(cfg, args)

    if not cfg.host:
        print("Ошибка: укажите host (аргументом или в config.toml)")
        return 2

    host = RemoteHost(
        host=cfg.host,
        user=cfg.user,
        port=cfg.port,
        key_path=cfg.key_path,
        accept_new=not args.no_accept_new,
    )

    print(f"Подключение к {host.target()} (порт {host.port}, ключ {host.key_path})...")
    try:
        host.check()
    except Exception as exc:  # noqa: BLE001 - единая точка обработки ошибок CLI
        print(f"Ошибка подключения: {exc}")
        return 1

    try:
        if args.verify_only:
            verify(cfg, host)
        else:
            install(cfg, host)
    except Exception as exc:  # noqa: BLE001 - единая точка обработки ошибок CLI
        print(f"Ошибка установки: {exc}")
        return 1

    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
