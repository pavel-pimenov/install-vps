from __future__ import annotations

import re
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

# Утилиты, которых нет в репозиториях Ubuntu: ставим из релизов GitHub с
# закреплённой версией и обязательной проверкой sha256 (checksums.txt).
# {version} и {goarch} подставляются при сборке скрипта.
_GITHUB_TOOLS = {
    "lazydocker": {
        "repo": "jesseduffield/lazydocker",
        "version": "0.25.2",
        "archive": "lazydocker_{version}_Linux_{goarch}.tar.gz",
        "binary": "lazydocker",
    },
    "bandwhich": {
        "repo": "imsodin/bandwhich",
        "version": "0.23.2",
        "archive": "bandwhich-{version}-linux-{goarch}.tar.gz",
        "binary": "bandwhich",
    },
}

# Утилиты, распространяемые готовым .deb: ставим через dpkg.
_DEB_TOOLS = {
    "systemd-manager-tui": {
        "repo": "matheus-git/systemd-manager-tui",
        "version": "1.2.5",
        "package": "systemd-manager-tui_{version}_{debarch}.deb",
        "binary": "systemd-manager-tui",
        "dpkg_name": "systemd-manager-tui",
    },
}

EXTRA_TOOLS = tuple(sorted(set(_GITHUB_TOOLS) | set(_DEB_TOOLS)))


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
    """Корневой Caddyfile: глобальные опции + подключение всех сайтов."""
    lines = ["{"]
    if email:
        lines.append(f"\temail {email}")
    lines.append("}")
    lines.append(f"import {CADDY_SITES_DIR_IN_CONTAINER}/*.caddy")
    return "\n".join(lines) + "\n"


def _caddy_sites_script(cfg: Config) -> str:
    """Готовые bash-фрагменты, создающие по файлу на каждый сайт.

    Содержимое файлов не проходит через .format(), поэтому фигурные скобки
    Caddyfile не нужно экранировать.
    """
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

    script = ""
    for name, content in sites:
        script += (
            f"cat <<'CADDY_SITE_EOF' | $SUDO tee {CADDY_SITES_DIR}/{name}.caddy >/dev/null\n"
            f"{content}"
            "CADDY_SITE_EOF\n"
        )
    if not sites:
        script += (
            'echo "  ВНИМАНИЕ: не задан ни одного домена — Caddy поднимется,'
            ' но сайтов не будет."\n'
            'echo "  Добавьте --caddy-domain (Beszel) и/или'
            ' --caddy-site ДОМЕН=UPSTREAM."\n'
        )
    return script


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

# Caddy: постоянные пути. Сайты лежат отдельными файлами в sites/ и
# подключаются через import — новый сайт добавляется одним файлом.
CADDY_DIR = "/opt/caddy"
CADDY_COMPOSE = "/opt/caddy/docker-compose.yml"
CADDY_SITES_DIR = "/opt/caddy/sites"
# внутри контейнера sites/ примонтирован в /etc/caddy/sites — Caddyfile
# пишется и читается уже по контейнерному пути
CADDY_SITES_DIR_IN_CONTAINER = "/etc/caddy/sites"

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
cat <<'BESZEL_EOF' | $SUDO tee {compose} >/dev/null
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
{auth_warning}if [ -z {key_quoted} ]; then
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

CADDY = r"""
echo "==> Caddy: обратный прокси с автоматическим HTTPS (порты 80/443)"
$SUDO install -m 0755 -d {dir} {sites_dir}
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
        script += BESZEL_STACK.format(
            port=cfg.beszel_port,
            dir=BESZEL_DIR,
            compose=BESZEL_COMPOSE,
            app_url=_yaml_str(
                f"https://{cfg.caddy_domain}"
                if cfg.caddy and cfg.caddy_domain
                else f"http://localhost:{cfg.beszel_port}"
            ),
            auth_env=_beszel_auth_env(cfg),
            auth_warning=_beszel_auth_warning(cfg),
            key=cfg.beszel_agent_key,
            key_quoted=_sh_quote(cfg.beszel_agent_key),
            token=cfg.beszel_agent_token,
            token_quoted=_sh_quote(cfg.beszel_agent_token),
        )
        script += BESZEL_VERIFY
    if cfg.caddy:
        if cfg.caddy_email and not _EMAIL_RE.match(cfg.caddy_email):
            raise ValueError(f"Некорректный caddy_email: {cfg.caddy_email!r}")
        script += CADDY.format(
            dir=CADDY_DIR,
            compose=CADDY_COMPOSE,
            sites_dir=CADDY_SITES_DIR,
            email=cfg.caddy_email,
            caddyfile=_caddy_caddyfile(cfg.caddy_email),
            sites=_caddy_sites_script(cfg),
        )
        script += CADDY_VERIFY.format(sites_dir=CADDY_SITES_DIR)
    script += VERIFY
    host.run_script(script)


def verify(cfg: Config, host: RemoteHost) -> None:
    script = _bootstrap(cfg.sudo)
    if cfg.beszel:
        script += BESZEL_VERIFY
    if cfg.caddy:
        script += CADDY_VERIFY.format(sites_dir=CADDY_SITES_DIR)
    script += VERIFY
    host.run_script(script)
