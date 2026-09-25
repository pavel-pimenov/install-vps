from __future__ import annotations

import subprocess

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


def _sh_quote(value: str) -> str:
    """Обернуть значение в одинарные кавычки для безопасной подстановки в bash."""
    return "'" + value.replace("'", "'\\''") + "'"


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

SWAP512 = r"""
echo "==> своп-файл 512M (для слабых VPS)"
if ! $SUDO swapon --show | grep -q '^/swapfile'; then
  if [ ! -f /swapfile ]; then
    $SUDO dd if=/dev/zero of=/swapfile bs=1M count=512 status=none
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

KEYRING_CLEANUP = r"""
echo "==> чистим старые ядра (для малого места на диске)"
CURRENT="$(uname -r)"
# версионированные linux-image-* / linux-headers-* / linux-modules-*, кроме
# пакета текущего ядра; мета-пакеты (linux-image-generic и др.) не трогаем
OLD_KERNELS="$($SUDO dpkg -l 'linux-image-*' 'linux-headers-*' 'linux-modules-*' 2>/dev/null \
  | awk '/^ii/{print $2}' \
  | grep -E 'linux-(image|headers|modules)-[0-9]' \
  | grep -v "linux-image-$CURRENT$" \
  | grep -v "linux-modules-$CURRENT$" \
  | grep -v "linux-headers-$CURRENT$" || true)"
if [ -n "$OLD_KERNELS" ]; then
  echo "$OLD_KERNELS" | xargs -r $SUDO apt-get purge -y --auto-remove
else
  echo "  старых ядер не найдено — нечего чистить"
fi
echo "  осталось ядер: $($SUDO dpkg -l 'linux-image-*' 2>/dev/null | awk '/^ii/{print $2}' | wc -l)"
"""

BESZEL_DIR = "/opt/beszel"
BESZEL_COMPOSE = "/opt/beszel/docker-compose.yml"

BESZEL_STACK = r"""
echo "==> Beszel Hub + Agent (docker compose, порт {port})"
$SUDO install -m 0755 -d {dir}
cat <<'BESZEL_EOF' | $SUDO tee {compose} >/dev/null
services:
  beszel:
    image: henrygd/beszel:latest
    container_name: beszel
    restart: unless-stopped
    environment:
      APP_URL: http://localhost:{port}
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
if [ -z {key_quoted} ]; then
  echo "  ВНИМАНИЕ: не задан beszel_agent_key — агент не сможет подключиться к Hub."
  echo "  Откройте http://<host>:{port} -> создайте пользователя -> Add system,"
  echo "  скопируйте ключ агента в beszel_agent_key (config.toml или --beszel-key)"
  echo "  и запустите install-vps повторно."
fi
if [ -z {token_quoted} ]; then
  echo "  ВНИМАНИЕ: не задан beszel_agent_token — агент не сможет подключиться к Hub."
  echo "  Токен виден в том же диалоге Add system (или в настройках -> tokens)."
fi
$SUDO docker compose -f {compose} up -d --remove-orphans
$SUDO docker compose -f {compose} ps
echo "  Hub: http://<host>:{port}"
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
if command -v vmstat >/dev/null 2>&1; then
  echo "  OK: vmstat -> $(command -v vmstat)"
else
  echo "  MISSING: vmstat"
fi
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


def install(cfg: Config, host: RemoteHost) -> None:
    """Базовая настройка минимального набора софта."""
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
    script += SWAP512
    script += LOGROTATE_COMPRESS
    script += KEYRING_CLEANUP
    if cfg.beszel:
        for label, value in (
            ("beszel_agent_key", cfg.beszel_agent_key),
            ("beszel_agent_token", cfg.beszel_agent_token),
        ):
            if any(ch in value for ch in '"\n\r'):
                raise ValueError(
                    f"{label} содержит недопустимые символы "
                    '(кавычки или перевод строки); скопируйте значение целиком'
                )
        script += BESZEL_STACK.format(
            port=cfg.beszel_port,
            dir=BESZEL_DIR,
            compose=BESZEL_COMPOSE,
            key=cfg.beszel_agent_key,
            key_quoted=_sh_quote(cfg.beszel_agent_key),
            token=cfg.beszel_agent_token,
            token_quoted=_sh_quote(cfg.beszel_agent_token),
        )
        script += BESZEL_VERIFY
    script += VERIFY
    host.run_script(script)


def verify(cfg: Config, host: RemoteHost) -> None:
    script = _bootstrap(cfg.sudo)
    if cfg.beszel:
        script += BESZEL_VERIFY
    script += VERIFY
    host.run_script(script)
