# HISTORY.md

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/). Обратный
хронологический порядок.

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
