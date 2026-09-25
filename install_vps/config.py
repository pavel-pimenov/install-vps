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
    swap_size_mb: int = 512
    docker_source: str = "ubuntu"   # "ubuntu" — docker.io из архивов Ubuntu; "official" — docker-ce
    beszel: bool = False            # поднять Beszel Hub+Agent одним docker-compose (порт 8090)
    beszel_port: int = 8090
    beszel_agent_key: str = ""      # публичный ключ агента (веб-UI Hub: Add system)
    beszel_agent_token: str = ""    # токен агента из того же диалога (обязателен в 0.20+)
    zram_size_mb: int = 0          # размер zram-свопа в МБ (0 — выключить; докерится пакетом)
    journald_max_use: str = "100M"  # лимит systemd-журнала
    docker_log_max_size: str = "10m"  # размер одного файла лога контейнера
    docker_log_max_file: str = "3"    # сколько файлов лога хранить на контейнер


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
    ("beszel", "beszel"),
    ("beszel_port", "beszel_port"),
    ("beszel_agent_key", "beszel_key"),
    ("beszel_agent_token", "beszel_token"),
    ("zram_size_mb", "zram_size_mb"),
    ("journald_max_use", "journald_max_use"),
    ("docker_log_max_size", "docker_log_max_size"),
    ("docker_log_max_file", "docker_log_max_file"),
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
