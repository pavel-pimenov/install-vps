from __future__ import annotations

import subprocess

from .config import Config
from .ssh import RemoteHost

DOCKER_REPO_KEY = "https://download.docker.com/linux/ubuntu/gpg"
DOCKER_KEYRING_DIR = "/etc/apt/keyrings"
DOCKER_KEYRING_FILE = f"{DOCKER_KEYRING_DIR}/docker.gpg"
DOCKER_SOURCES_LIST = "/etc/apt/sources.list.d/docker.list"
_OS_RELEASE_FIELDS = 2


def _bootstrap(sudo: bool) -> str:
    _ = sudo  # судо определяется автоматически на хосте
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
        # битый/устаревший docker.list и keyring ломают любой apt-get update
        f'$SUDO rm -f {DOCKER_SOURCES_LIST} {DOCKER_KEYRING_FILE}',
    ]
    return "\n".join(lines) + "\n"


APT_UPDATE = 'echo "==> apt-get update"\n$SUDO apt-get update\n'

PKG_INSTALL = (
    'echo "==> установка пакетов: {packages}"\n'
    "$SUDO apt-get install -y {packages}\n"
)

DOCKER_REPO = """
echo "==> подключаем официальный docker-репозиторий"
ARCH="$(dpkg --print-architecture)"
$SUDO install -m 0755 -d {keyring_dir}
if [ ! -f {keyring_file} ]; then
  curl -fsSL {key} | $SUDO gpg --dearmor -o {keyring_file}
fi
echo "deb [arch=$ARCH signed-by={keyring_file}] https://download.docker.com/linux/ubuntu {codename} stable" \\
  | $SUDO tee {sources_list}
echo "==> apt-key подпись (деарморинг docker.gpg)"
if [ ! -f {keyring_file} ]; then
  curl -fsSL {key} | $SUDO gpg --dearmor -o {keyring_file}
fi
echo "==> apt-get update (после добавления docker-репо)"
$SUDO apt-get update
"""

VERIFY = """
echo "==> проверка"
for cmd in nload htop btop mc git; do
  if command -v $cmd >/dev/null 2>&1; then
    echo "  OK: $cmd -> $(command -v $cmd)"
  else
    echo "  MISSING: $cmd"
  fi
done
if command -v docker-compose >/dev/null 2>&1; then
  echo "  OK: docker-compose -> $(command -v docker-compose)"
elif ls /usr/libexec/docker/cli-plugins/docker-compose >/dev/null 2>&1; then
  echo "  OK: docker compose plugin -> /usr/libexec/docker/cli-plugins/docker-compose"
elif $SUDO docker compose version >/dev/null 2>&1; then
  echo "  OK: docker compose plugin -> $($SUDO docker compose version)"
else
  echo "  MISSING: docker-compose-v2"
fi
"""


def remote_distro_codename(host: RemoteHost) -> str:
    """Определяет VERSION_CODENAME на удалённом хосте."""
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
        raise SystemExit(
            f"Поддерживается Ubuntu 24.04/26.04, а на хосте — {version_id}"
        )
    return codename


def install(cfg: Config, host: RemoteHost) -> None:
    """Базовая настройка минимального набора софта."""
    codename = _codename(cfg, host)
    script = _bootstrap(cfg.sudo)
    script += APT_UPDATE
    script += PKG_INSTALL.format(packages="curl ca-certificates gnupg")
    script += DOCKER_REPO.format(
        key=DOCKER_REPO_KEY,
        keyring_dir=DOCKER_KEYRING_DIR,
        keyring_file=DOCKER_KEYRING_FILE,
        codename=codename,
        sources_list=DOCKER_SOURCES_LIST,
    )
    script += PKG_INSTALL.format(packages=" ".join(cfg.packages))
    script += VERIFY
    host.run_script(script)


def verify(cfg: Config, host: RemoteHost) -> None:
    script = _bootstrap(cfg.sudo)
    script += VERIFY
    host.run_script(script)


def _codename(cfg: Config, host: RemoteHost) -> str:
    return remote_distro_codename(host)
