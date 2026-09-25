# HISTORY.md

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/). Обратный
хронологический порядок.

## [0.5.0] — 2026-09-25

### Добавлено

- `CADDY` — обратный прокси с автоматическим HTTPS: `caddy:2-alpine` в
  `/opt/caddy`, `network_mode: host` (сам занимает 80/443 и ходит в
  `127.0.0.1:<порт>`, поэтому чужие compose-проекты не нужно переделывать).
  Сертификаты Let's Encrypt выпускаются и продлеваются сами; HTTP
  перенаправляется на HTTPS. Флаги `--caddy`, `--caddy-email`, `--caddy-domain`
  (Beszel) и `--caddy-site ДОМЕН=UPSTREAM` (любой другой сайт, можно
  повторять), одноимённые ключи в `config.toml`.
- Сайты лежат отдельными файлами в `/opt/caddy/sites/*.caddy`, корневой
  Caddyfile подключает их через `import` — новый сайт добавляется одним файлом.
  Для Beszel используется конфигурация из его документации: `max_size 10MB`
  (импорт/экспорт) и `read_timeout 360s` (долгие опросы и WebSocket агентов).
- `CADDY_VERIFY` — проверка контейнера, занятости 80/443, валидности Caddyfile
  и списка доменов; работает и в `--verify-only`.
- При `--caddy --caddy-domain` у Beszel выставляется публичный
  `APP_URL=https://<домен>` вместо `http://localhost:8090` — иначе в ссылках
  уведомлений и сгенерированном конфиге агента остаётся локальный адрес.
- Домены и upstream валидируются на стороне Python (строгие регулярки), до
  отправки скрипта на хост: `rm -rf /`, `javascript:...` и мусор в домене
  отклоняются с `ValueError`.

### Исправлено

- Caddy не подхватывал обновлённый Caddyfile при повторном прогоне: конфиг
  примонтирован, но процесс держит старый в памяти. Теперь после записи
  выполняется `caddy reload` (при неудаче — перезапуск контейнера), так что
  повторный запуск действительно применяет изменения.
- `import` в Caddyfile указывал на хостовый путь `/opt/caddy/sites`; внутри
  контейнера сайты лежат в `/etc/caddy/sites`, поэтому ни один сайт не
  подключался и 80/443 не открывались.

## [0.4.0] — 2026-09-25

### Добавлено

- `UNATTENDED_UPGRADES` — `/etc/apt/apt.conf.d/52unattended-upgrades-local`
  с `Dpkg::Options` (`--force-confdef`, `--force-confold`). Без них фоновое
  обновление без tty зависает на вопросах debconf/ucf об изменённых конфигах.
  Если установлен `needrestart`, выставляется `NEEDRESTART_MODE=a`; иначе его
  не трогаем. Повторный прогон идемпотентен.

### Исправлено

- **Критично: `zram` не поднимался после перезагрузки.** На VPS с 704 МБ RAM
  `systemd-zram-generator` падал с `ENOMEM` (`Committed_AS` выше `CommitLimit`)
  и не повторял попытку, поэтому в swap оставался только `/swapfile`.
  Теперь вместо генератора ставится `zram-swap.service` +
  `/usr/local/sbin/zram-swap-up`, который стартует позже, когда память уже
  разгружена: уменьшает размер по шагам (`256→128→64→32` МБ) до первого
  успеха, ограничивает каждую запись в `disksize` таймаутом (иначе ядро может
  уйти в своп-цикл), не даёт запросить zram больше физической RAM и корректно
  переживает отсутствие `/proc/meminfo`. Конфиг генератора сохранён.

## [0.3.0] — 2026-09-25

### Добавлено

- `ZRAM` — сжатый своп через пакет `systemd-zram-generator`:
  `/etc/systemd/zram-generator.conf` (`zram-size`, `compression-algorithm = zstd`).
  Активируется сразу (`/dev/zram0`, приоритет 100 — выше обычного swapfile).
  Включается `--zram-size-mb
  256`; по умолчанию выключено (`0`), чтобы не менять поведение молча.
  *См. 0.4.0: загрузочный путь перенесён на собственный юнит.*
- `JOURNALD_LIMIT` — лимит systemd-журнала через drop-in
  `/etc/systemd/journald.conf.d/10-size-limit.conf` (`SystemMaxUse`,
  `SystemKeepFree=200M`, `MaxRetentionSec=2week`) + `journalctl --vacuum-size`.
  На 4 ГБ VPS журнал иначе съедает диск.
- `DOCKER_LOGROTATE` — `log-driver=json-file` с `max-size`/`max-file` в
  `/etc/docker/daemon.json`. JSON мерджится, а не перезаписывается: чужие ключи
  (например `storage-driver`) сохраняются, невалидный файл не трогается ( docker
  продолжает работать с прежней конфигурацией), рестарт `docker` — только при
  фактическом изменении файла. Флаги: `--docker-log-max-size`, `--docker-log-max-file`.
- Флаги `--zram-size-mb`, `--journald-max-use`, `--docker-log-max-size`,
  `--docker-log-max-file` и одноимённые ключи `config.toml`.
- `apply_overrides` переведён на таблицу `_OVERRIDES`: перекрытия конфига
  флагами не размножаются по числу полей (ruff PLR0912).

### Исправлено

- **Критично: `KEYRING_CLEANUP` больше не сносит ядро, на котором работает.**
  `apt-get purge --auto-remove` однажды удалил `linux-modules-*` и
  `linux-image-*` текущего ядра: на хосте пропал `/lib/modules` целиком, в
  `/boot` не осталось `vmlinuz`, то есть хост не перезагрузился бы. Текущее
  ядро защищено точным списком (`grep -Fxv`), `--auto-remove` убран, а если
  пакет образа текущего ядра отсутствует — он ставится заново, и наличие
  `/boot/vmlinuz-$(uname -r)` проверяется с предупреждением.
- `zram` без модуля ядра больше не валит скрипт: при недоступном
  `/sys/block/zram0` выводится предупреждение «заработает после перезагрузки».
- Литералы `{}` в python-фрагменте `DOCKER_LOGROTATE` экранированы
  (`{{}}`) — иначе `.format()` считал их плейсхолдерами.

## [0.2.0] — 2026-09-25

### Добавлено

- Мониторинг Beszel (Hub + Agent на одной машине) одним `docker compose`:
  шаблон `BESZEL_STACK` пишет `/opt/beszel/docker-compose.yml` и поднимает
  `henrygd/beszel` (Hub, порт 8090 наружу) + `henrygd/beszel-agent`
  (Unix-сокет вместо сетевого порта, доступ к `/var/run/docker.sock:ro`).
  Включается `--beszel` / `beszel = true`.
- Агент Beszel требует **и** публичный ключ, **и** токен (`--beszel-key` /
  `--beszel-token`, поля `beszel_agent_key` / `beszel_agent_token`): оба
  выдаёт веб-UI Hub при «Add system». Без них скрипт поднимает Hub и печатает
  инструкцию; повторный запуск идемпотентно добавляет агента.
- `docker_source` в конфиге: `"ubuntu"` (по умолчанию) — `docker.io` +
  `docker-compose-v2` из архивов Ubuntu; `"official"` — `docker-ce` из
  `download.docker.com`. Официальный репозиторий подключается только при
  явном выборе, codename по-прежнему берётся с хоста.
- `logrotate` в пакетах по умолчанию и защита `LOGROTATE_COMPRESS` проверкой
  существования `/etc/logrotate.conf` — иначе `sed` ронял скрипт на чистом
  Ubuntu 26.04, где конфига ещё нет.
- `KEYRING_CLEANUP` — чистка старых ядер (сбор 527 МБ на 4 ГБ VPS).
  Исключает компоненты текущего ядра (`image`/`modules`/`headers`) и
  идемпотентна: при уже вычищенных ядрах выводит «нечего чистить», а не падает.
- `procps` в пакетах по умолчанию; `vmstat` в `VERIFY`.
- Валидация `beszel_agent_key` / `beszel_agent_token`: кавычка или перевод
  строки в значении ломали бы YAML compose-файла (по аналогии с правилом о
  ненадёжных `sources.list.d`).

### Исправлено

- В шаблонах `DOCKER_REPO` / `SWAP512` двойной обратный слэш (`\\` в
  необработанной строке) ловил `syntax error ... unexpected end of file from
  'if'` — заменено на одиночный `\`.
- В `LOGROTATE_COMPRESS` не хватало закрывающего `fi`.
- `_codename` больше не отказывает на `VERSION_CODENAME` из-за опечатки в
  докстринге; `docker_source` не подключает официальный репозиторий по умолчанию.

## [0.1.0] — 2026-09-24

### Добавлено

- Стартовая настройка VPS (Ubuntu 24.04 / 26.04) по SSH-ключу.
- Без внешних Python-зависимостей: связь — системный `ssh`, удалённые
  действия — одиночный потоковый bash-скрипт через stdin (`bash -s`).
- Минимальный набор по умолчанию: `nload`, `htop`, `btop`, `mc`, `git`,
  `docker-compose-v2` (+ `curl`, `ca-certificates`, `gnupg`).
- Подключение официального docker-репозитория: ключ деарморится
  (`gpg --dearmor`) в `/etc/apt/keyrings/docker.gpg`, листинг в
  `sources.list.d/docker.list`; оба файла пересоздаются каждым запуском
  (идемпотентно), костыль `rm -f` при несработавшем прошлом состоянии —
  из-за того, что сломанный листинг ломает весь `apt-get update`.
- Автоопределение `VERSION_CODENAME` с хоста (`/etc/os-release`) —
  плейсхолдер `{codename}` в шаблонах, без захардкоженного «resolute».
- CLI: `install_vps <host> [--verify-only] [-u USER] [-p PORT] [-k KEY]`
  + слияние `config.toml`.
- `verify()` — потоковая проверка наличия всех пакетов после установки.
- Памятка для агентов (`AGENTS.md`) и этот changelog (`HISTORY.md`).
