from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .config import apply_overrides, load_config
from .installer import install, verify
from .ssh import RemoteHost


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
    p.add_argument("--verify-only", action="store_true",
                   help="только проверить наличие пакетов, ничего не ставить")
    p.add_argument("--beszel", action="store_true",
                   help="поднять Beszel Hub+Agent одним docker compose (порт 8090)")
    p.add_argument("--beszel-port", type=int, help="порт Beszel Hub (по умолчанию 8090)")
    p.add_argument("--beszel-key",
                   help="публичный ключ агента Beszel (из веб-UI Hub: Add system)")
    p.add_argument("--beszel-token",
                   help="токен агента Beszel (из того же диалога Add system)")
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
    p.add_argument("--caddy-site", metavar="DOMAIN=UPSTREAM", action="append",
                   help="любой сайт: monitor.example.com=http://127.0.0.1:8080 "
                        "(можно указать несколько раз)")
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
