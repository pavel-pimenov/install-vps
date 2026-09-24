# install-vps

Автоматизация стартовой настройки новой VPS (Ubuntu 24.04/26.04) по SSH-ключу,
без Python-зависимостей — используется системный `ssh`.

## Что ставит

Минимальный набор по умолчанию (`config.toml` → `packages`):
`nload htop btop mc git docker-compose-v2` (+ `curl ca-certificates gnupg` для
подключения официального docker-репозитория).

## Требования

- Python 3.11+
- системный `ssh-keygen` / `gpg` / `curl` на **локальной** машине
- на VPS: доступ по SSH-ключу, `root` либо пользователь с sudo (NOPASSWD)

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
и перекрываются флагами CLI. Пример:

```bash
install-vps dev.fly-server.ru
install-vps -u ubuntu -k ~/.ssh/id_ed25519 203.0.113.5
install-vps --verify-only 203.0.113.5   # только проверить установку
```

Понятие FQDN поддерживается — host принимает и IP, и DNS-имя.

## Структура

```
install_vps/
  cli.py        # разбор аргументов CLI
  config.py     # загрузка config.toml + слияние с флагами
  install_vps/  # (сам пакет проекта, не путать с каталогом)
```