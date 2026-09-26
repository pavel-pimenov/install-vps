# install-vps

Автоматизация стартовой настройки новой VPS (Ubuntu 24.04/26.04) по SSH-ключу,
без Python-зависимостей — используется системный `ssh`, удалённые действия
выполняются одним потоковым bash-скриптом через `bash -s`.

## Что ставит

По умолчанию (`config.toml` → `packages`): `nload`, `htop`, `btop`, `mc`, `git`,
`procps`, `docker.io`, `docker-compose-v2`, `fail2ban`, `ncdu`, `logrotate`
(+ `curl`, `ca-certificates`, `gnupg` для репозитория docker).

Дальше — по флагам:

| Что | Флаг / ключ | Про что |
| --- | --- | --- |
| Мониторинг | `--beszel` | Beszel Hub + Agent одним `docker compose` (порт 8090), ключ/токен агента — `--beszel-key` / `--beszel-token` |
| Вход без пароля | `--beszel-user-creation` + `--beszel-disable-password-auth` | OAuth2 вместо пароля (провайдер настраивается в веб-UI Hub) |
| HTTPS | `--caddy`, `--caddy-email` | Caddy в `network_mode: host`, сертификаты Let's Encrypt сами, HTTP → HTTPS |
| Портал | `--caddy-portal ДОМЕН`, `--caddy-tile` | Страница с плитками сервисов; сервисы — под путями `/мониторинг`, `/thinpro` |
| Логи контейнеров | `--dozzle` | Dozzle (`127.0.0.1:8082`) под путём портала `--dozzle-path` (по умолчанию `/logs`), вход по паролю |
| Сайт | `--caddy-site ДОМЕН=UPSTREAM` | Любой домен целиком на локальный сервис |
| Утилиты не из apt | `--tools lazydocker bandwhich` | Ставятся из релизов GitHub с закреплённой версией и проверкой sha256 |
| Сжатый своп | `--zram-size-mb 256` | zram (zstd) поверх swapfile |
| Место на диске | `--journald-max-use`, `--docker-log-max-size/-file`, `--swap-size-mb` | Лимиты журнала и логов контейнеров, размер подкачки |
| Обновления | всегда | `unattended-upgrades` без интерактивных вопросов, чистка старых ядер |
| Защита | всегда | `fail2ban` (sshd), сжатие логов, ротация |

## Требования

- Python 3.11+
- системный `ssh` / `ssh-keygen` на **локальной** машине
- на VPS: доступ по SSH-ключу, `root` либо пользователь с sudo (NOPASSWD)
- для `--caddy`: открытые 80/443 и A-запись домена на IP хоста

## Установка

```bash
pip install -e .
```

## Использование

```bash
install-vps <host>            # host: IP или FQDN
python3 -m install_vps <host> # так же, без установки entry point
```

Параметры подключения (host/user/port/key_path) читаются из `config.toml`
и перекрываются флагами CLI:

```bash
install-vps dev.fly-server.ru
install-vps -u ubuntu -k ~/.ssh/id_ed25519 203.0.113.5
install-vps --verify-only 203.0.113.5   # только проверить, ничего не ставить
```

Всё, что повторяется между хостами, удобнее держать в `config.toml` (см.
[config.toml](config.toml) с комментариями) — тогда хватает
`install-vps <host>`. Повторный прогон идемпотентен: можно дописывать
возможности и запускать снова.

### Пример: HTTPS + портал с плитками

```bash
install-vps dev.fly-server.ru \
  --beszel --beszel-key "ssh-ed25519 AAAA..." --beszel-token "xxxx-xxxx" \
  --caddy --caddy-email you@example.com \
  --caddy-portal dev.fly-server.ru \
  --caddy-portal-title "Сервисы" \
  --caddy-tile "Мониторинг=/monitor=127.0.0.1:8090" \
  --caddy-tile "ThinPro=/thinpro=127.0.0.1:8080"
```

Плитка `Мониторинг` в этом примере лишняя — с `--beszel` и порталом она
добавляется сама, `/monitor` задаётся ключом `beszel_path`. Итог: Caddy
отдаёт `https://<домен>/` (страница плиток), `/monitor/` (Beszel) и
`/thinpro/` (прокси на 127.0.0.1:8080). Префикс у прокси срезается, поэтому
приложение видит обычный `/`; если приложение умеет базовый путь само —
добавьте четвёртое поле `=keep`. Новый сервис = одна строка `caddy_tiles`,
файлы `/opt/caddy/sites/*.caddy` руками править не нужно.

Проксирование под путём подходит не всем приложениям: Beszel и ThinPro
отдают `/assets/...` и `/api/...` от корня и под префиксом ломаются
(проверено на боевом хосте), поэтому для них плитка ведёт на собственный
домен — `caddy_tiles = ["Мониторинг=https://monitor.example.com"]`.

### Логи контейнеров: Dozzle

```bash
install-vps dev.fly-server.ru --dozzle --caddy --caddy-portal dev.fly-server.ru
```

Dozzle не требует отдельного домена: он живёт под базовым путём портала
(`DOZZLE_BASE=/logs`), поэтому прокси сохраняет префикс, а поток логов идёт
без буферизации (`flush_interval -1`, `read_timeout 3600s`) — иначе SSE
приходит пачками. Плитка `Логи` на портале появляется сама.

Вход обязателен: контейнер читает `/var/run/docker.sock`, что равносильно
root на хосте. Пароль — `--dozzle-password`, иначе он генерируется на хосте и
показывается один раз; `users.yml` (bcrypt) дальше не перезаписывается, так
что повторный прогон пароль не сбрасывает. Внутри выключены shell и actions
(`docker.sock` через веб-интерфейс — это root), аватары и аналитика. Наружу
контейнер не смотрит: порт 8082 слушает только `127.0.0.1`, наружу его
отдаёт Caddy — иначе логи были бы доступны в обход авторизации.

## Структура

```
install_vps/
  cli.py        # разбор флагов CLI
  config.py     # Config + загрузка config.toml и слияние с флагами
  ssh.py        # RemoteHost: сборка команды ssh, потоковый запуск скрипта
  installer.py  # bash-шаблоны и генераторы (Caddy, Beszel, портал, утилиты)
tests/          # unittest: конфиг, валидация, bash -n по собранному скрипту
scripts/check.sh   # локальная проверка: ruff + compileall + тесты
```
