from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PACKAGES = [
    "nload",
    "htop",
    "btop",
    "mc",
    "git",
    "procps",          # vmstat / free / ps / top (наблюдение за слабым хостом)
    "docker.io",       # docker из репозитория Ubuntu (тот, что «идёт с убунтой»)
    "docker-compose-v2",
    "fail2ban",        # блокировка bruteforce (sshd)
    "ncdu",            # анализ диска (экономия места)
    "logrotate",       # ротация логов — иначе /etc/logrotate.conf может не быть
]


@dataclass
class Config:
    host: str = ""
    user: str = "root"
    port: int = 22
    key_path: str = "~/.ssh/id_ed25519"
    sudo: bool = False
    accept_new: bool = True
    packages: list[str] = field(default_factory=lambda: list(DEFAULT_PACKAGES))
    tools: list[str] = field(default_factory=list)  # утилиты вне репозиториев Ubuntu
    swap_size_mb: int = 512
    docker_source: str = "ubuntu"   # "ubuntu" — docker.io из архивов Ubuntu; "official" — docker-ce
    beszel: bool = False            # поднять Beszel: хаб+агент (hub) либо только агент (agent)
    beszel_mode: str = "hub"        # "hub" — хаб и локальный агент, "agent" — только агент
    # для удалённого хаба: агент сам слушает порт, хаб ходит к нему
    beszel_port: int = 8090         # порт хаба (режим hub)
    beszel_agent_port: int = 45876  # порт агента (режим agent)
    beszel_version: str = "0.20.0"  # тег образа henrygd/beszel(-agent)
    beszel_agent_allow: list[str] = field(default_factory=list)  # CIDR хабов, кому открыт порт агента
    beszel_agent_key: str = ""      # публичный ключ агента (веб-UI Hub: Add system)
    beszel_agent_token: str = ""    # токен агента из того же диалога (обязателен в 0.20+)
    # Агент сам звонит в хаб по этому URL — нужен, когда провайдер узла режет
    # входящие порты: по умолчанию хаб сам ходит к агенту (LISTEN), а с
    # HUB_URL соединение инициирует агент. Для хаба под Caddy — https://<домен>.
    beszel_hub_url: str = ""
    beszel_user_creation: bool = False  # разрешить самостоятельную регистрацию (для OAuth)
    beszel_disable_password_auth: bool = False  # вход только через OAuth (ломает пароль!)
    zram_size_mb: int = 0          # размер zram-свопа в МБ (0 — выключить; докерится пакетом)
    journald_max_use: str = "100M"  # лимит systemd-журнала
    docker_log_max_size: str = "10m"  # размер одного файла лога контейнера
    docker_log_max_file: str = "3"    # сколько файлов лога хранить на контейнер
    caddy: bool = False            # обратный прокси с автоматическим HTTPS (80/443)
    caddy_email: str = ""          # e-mail для Let's Encrypt (уведомления об истечении)
    # access-лог Caddy в stdout контейнера (docker logs caddy): без него
    # не видно, какие запросы и с какими кодами доходят до сервиса, — а это
    # первое, что нужно, когда плитка «не открывается»
    caddy_access_log: bool = True
    caddy_domain: str = ""         # домен для Beszel Hub (нужен caddy + beszel)
    # Portainer CE: на центральном хосте — сервер с UI, на узлах — edge agent
    # (сам ходит на сервер, входящие порты открывать не нужно)
    portainer: bool = False
    portainer_mode: str = "server"  # "server" — UI, "agent" — edge-агент узла
    portainer_version: str = "2.45.1"  # версия образов portainer-ce и agent
    portainer_port: int = 9443      # UI-порт, слушает только 127.0.0.1 (наружу — Caddy)
    # Туннель edge-агентов. Агент умеет только ws:// и стучится на
    # <домен>:<portainer_tunnel_port> — это порт ВНУТРИ контейнера, он же
    # попадает в EdgeKey. Наружу он публикуется на 127.0.0.1:tunnel_local,
    # а в мир его отдаёт Caddy (websocket на :80 того же домена): у провайдера
    # закрыт весь порт >1024, поэтому публиковать туннель напрямую бессмысленно.
    portainer_tunnel_port: int = 80
    portainer_tunnel_local_port: int = 8000
    portainer_domain: str = ""      # домен для UI Portainer (у Portainer свой сертификат)
    # Данные окружения из UI Portainer (Environments -> нужное -> Edge agent
    # standard): EDGE_ID — UUID, EDGE_KEY — base64, внутри адрес сервера,
    # порт туннеля и отпечаток chisel. Оба нужны вместе.
    portainer_agent_edge_id: str = ""
    portainer_agent_edge_key: str = ""
    portainer_admin_password: str = ""  # пароль первого admin (5-секундное окно UI)
    caddy_sites: list[str] = field(default_factory=list)  # "домен=http://upstream"
    caddy_portal: str = ""         # домен страницы с плитками сервисов
    caddy_portal_title: str = "Сервисы"  # заголовок страницы плиток
    # единый вход на портал (HTTP basic): браузер спрашивает логин/пароль один
    # раз, дальше все плитки открываются без форм (свою авторизацию Dozzle
    # при этом выключаем — её заменяет пароль портала)
    portal_user: str = "admin"
    portal_password: str = ""      # "" — портал без пароля (каждый сервис со своим)
    caddy_tiles: list[str] = field(default_factory=list)
    # "Название=/путь=upstream" (префикс срезается) либо "Название=https://домен"
    beszel_path: str = "/monitor"   # путь плитки Beszel на портале
    # Dozzle: логи контейнеров в браузере, живёт под базовым путём портала
    dozzle: bool = False
    dozzle_port: int = 8082        # слушает только 127.0.0.1, наружу отдаёт Caddy
    dozzle_path: str = "/logs"
    dozzle_user: str = "admin"
    dozzle_password: str = ""      # "" — сгенерировать на хосте и показать один раз


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is not None and path.exists():
        raw = tomllib.loads(path.read_text("utf-8"))
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                raise ValueError(f"Неизвестный ключ конфига: {key!r}")
    return cfg


_OVERRIDES = (
    ("host", "host"),
    ("user", "user"),
    ("port", "port"),
    ("key_path", "key"),
    ("packages", "packages"),
    ("tools", "tools"),
    ("swap_size_mb", "swap_size_mb"),
    ("docker_source", "docker_source"),
    ("beszel", "beszel"),
    ("beszel_mode", "beszel_mode"),
    ("beszel_agent_allow", "beszel_agent_allow"),
    ("beszel_port", "beszel_port"),
    ("beszel_version", "beszel_version"),
    ("beszel_agent_key", "beszel_key"),
    ("beszel_agent_token", "beszel_token"),
    ("beszel_hub_url", "beszel_hub_url"),
    ("beszel_user_creation", "beszel_user_creation"),
    ("beszel_disable_password_auth", "beszel_disable_password_auth"),
    ("beszel_path", "beszel_path"),
    ("dozzle", "dozzle"),
    ("dozzle_port", "dozzle_port"),
    ("dozzle_path", "dozzle_path"),
    ("dozzle_user", "dozzle_user"),
    ("dozzle_password", "dozzle_password"),
    ("zram_size_mb", "zram_size_mb"),
    ("journald_max_use", "journald_max_use"),
    ("docker_log_max_size", "docker_log_max_size"),
    ("docker_log_max_file", "docker_log_max_file"),
    ("caddy", "caddy"),
    ("caddy_email", "caddy_email"),
    ("caddy_domain", "caddy_domain"),
    ("portainer", "portainer"),
    ("portainer_mode", "portainer_mode"),
    ("portainer_version", "portainer_version"),
    ("portainer_domain", "portainer_domain"),
    ("portainer_tunnel_port", "portainer_tunnel_port"),
    ("portainer_agent_edge_id", "portainer_agent_edge_id"),
    ("portainer_agent_edge_key", "portainer_agent_edge_key"),
    ("portainer_admin_password", "portainer_admin_password"),
    ("caddy_sites", "caddy_site"),
    ("caddy_portal", "caddy_portal"),
    ("caddy_portal_title", "caddy_portal_title"),
    ("portal_user", "portal_user"),
    ("portal_password", "portal_password"),
    ("caddy_tiles", "caddy_tile"),
)


def apply_overrides(cfg: Config, args) -> Config:
    for field_name, arg_name in _OVERRIDES:
        value = getattr(args, arg_name)
        # store_true даёт False, nargs — [], опции — None: пропускаем «пустое»,
        # но 0 для zram_size_mb пропускать нельзя (0 = явно отключить zram)
        if value is None or value is False or value in ([], ""):
            continue
        setattr(cfg, field_name, value)
    if args.sudo:
        cfg.sudo = True
    cfg.key_path = os.path.expanduser(cfg.key_path)
    return cfg
