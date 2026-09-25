# HISTORY.md

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/). Обратный
хронологический порядок.

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
