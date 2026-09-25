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
    beszel: bool = False            # поднять Beszel Hub+Agent одним docker-compose (порт 8090)
    beszel_port: int = 8090
    beszel_agent_key: str = ""      # публичный ключ агента (веб-UI Hub: Add system)
    beszel_agent_token: str = ""    # токен агента из того же диалога (обязателен в 0.20+)
    beszel_user_creation: bool = False  # разрешить самостоятельную регистрацию (для OAuth)
    beszel_disable_password_auth: bool = False  # вход только через OAuth (ломает пароль!)
    zram_size_mb: int = 0          # размер zram-свопа в МБ (0 — выключить; докерится пакетом)
    journald_max_use: str = "100M"  # лимит systemd-журнала
    docker_log_max_size: str = "10m"  # размер одного файла лога контейнера
    docker_log_max_file: str = "3"    # сколько файлов лога хранить на контейнер
    caddy: bool = False            # обратный прокси с автоматическим HTTPS (80/443)
    caddy_email: str = ""          # e-mail для Let's Encrypt (уведомления об истечении)
    caddy_domain: str = ""         # домен для Beszel Hub (нужен caddy + beszel)
    caddy_sites: list[str] = field(default_factory=list)  # "домен=http://upstream"


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
    ("beszel", "beszel"),
    ("beszel_port", "beszel_port"),
    ("beszel_agent_key", "beszel_key"),
    ("beszel_agent_token", "beszel_token"),
    ("beszel_user_creation", "beszel_user_creation"),
    ("beszel_disable_password_auth", "beszel_disable_password_auth"),
    ("zram_size_mb", "zram_size_mb"),
    ("journald_max_use", "journald_max_use"),
    ("docker_log_max_size", "docker_log_max_size"),
    ("docker_log_max_file", "docker_log_max_file"),
    ("caddy", "caddy"),
    ("caddy_email", "caddy_email"),
    ("caddy_domain", "caddy_domain"),
    ("caddy_sites", "caddy_site"),
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
