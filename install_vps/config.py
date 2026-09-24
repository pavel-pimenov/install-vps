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
    "docker-compose-v2",
    "fail2ban",   # блокировка bruteforce (sshd)
    "ncdu",       # анализ диска (экономия места)
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


def apply_overrides(cfg: Config, args) -> Config:
    if args.host:
        cfg.host = args.host
    if args.user:
        cfg.user = args.user
    if args.port:
        cfg.port = args.port
    if args.key:
        cfg.key_path = args.key
    if args.packages:
        cfg.packages = args.packages
    if args.sudo:
        cfg.sudo = True
    cfg.key_path = os.path.expanduser(cfg.key_path)
    return cfg
