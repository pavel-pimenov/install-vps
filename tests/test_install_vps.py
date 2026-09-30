"""Тесты install-vps: конфиг, валидация и генерация bash-скриптов.

Запуск:
    python3 -m unittest discover -s tests -v

Собранный скрипт прогоняется через `bash -n`: так ловятся незакрытые
if/кавычки/heredoc в шаблонах — на боевом хосте это выглядело бы как
падение установки, а не как ошибка в коде.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from install_vps import (
    installer,  # noqa: E402
    lighttpd,  # noqa: E402
)
from install_vps.cli import _parser  # noqa: E402
from install_vps.config import Config, apply_overrides, load_config  # noqa: E402
from install_vps.installer import (  # noqa: E402
    EXTRA_TOOLS,
    _beszel_public_url,
    _parse_tile,
    _portal_caddy_block,
    _portal_html,
    _tiles,
    _tools_script,
    install,
    verify,
)

BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"


class FakeHost:
    """Заглушка RemoteHost: собирает скрипт вместо отправки на хост."""

    def __init__(self) -> None:
        self.script = ""

    def run_script(self, script: str) -> None:
        self.script += script


def full_config(**overrides) -> Config:
    """Конфиг со всеми включёнными фичами разом."""
    cfg = Config(
        host="203.0.113.5",
        packages=["htop", "docker.io"],
        tools=list(EXTRA_TOOLS),
        beszel=True,
        beszel_agent_key="ssh-ed25519 AAAA",
        beszel_agent_token="xxxx-xxxx",
        beszel_user_creation=True,
        caddy=True,
        caddy_email="you@example.com",
        caddy_portal="dev.fly-server.ru",
        caddy_tiles=["ThinPro=/thinpro=127.0.0.1:8080"],
        caddy_sites=["shop.example.com=http://127.0.0.1:3000"],
        zram_size_mb=256,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def bash_check(script: str) -> None:
    proc = subprocess.run(
        [BASH, "-n"], input=script, text=True, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(f"bash -n не прошёл:\n{proc.stderr}\n---\n{script}")


class ConfigTests(unittest.TestCase):
    def test_load_config_rejects_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("unknown_key = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_load_config_reads_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'caddy = true\ncaddy_portal = "dev.fly-server.ru"\n'
                'caddy_tiles = ["ThinPro=/thinpro=127.0.0.1:8080"]\n',
                encoding="utf-8",
            )
            cfg = load_config(path)
        self.assertTrue(cfg.caddy)
        self.assertEqual(cfg.caddy_portal, "dev.fly-server.ru")
        self.assertEqual(len(cfg.caddy_tiles), 1)

    def test_load_config_missing_file_is_default(self) -> None:
        cfg = load_config(Path("/nonexistent/config.toml"))
        self.assertEqual(cfg.port, 22)
        self.assertEqual(cfg.caddy_portal, "")
        self.assertEqual(cfg.beszel_path, "/monitor")

    def test_flags_override_config(self) -> None:
        args = _parser().parse_args(
            [
                "example.com",
                "--caddy-portal", "dev.fly-server.ru",
                "--caddy-tile", "Мониторинг=/monitor=127.0.0.1:8090",
                "--beszel-path", "/mon",
                "--swap-size-mb", "1024",
                "--tools", "lazydocker",
            ]
        )
        cfg = apply_overrides(Config(), args)
        self.assertEqual(cfg.host, "example.com")
        self.assertEqual(cfg.caddy_portal, "dev.fly-server.ru")
        self.assertEqual(cfg.caddy_tiles, ["Мониторинг=/monitor=127.0.0.1:8090"])
        self.assertEqual(cfg.beszel_path, "/mon")
        self.assertEqual(cfg.swap_size_mb, 1024)
        self.assertEqual(cfg.tools, ["lazydocker"])

    def test_store_true_does_not_override(self) -> None:
        args = _parser().parse_args(["example.com"])
        cfg = apply_overrides(Config(beszel=True, caddy=True), args)
        self.assertTrue(cfg.beszel)
        self.assertTrue(cfg.caddy)

    def test_zero_swap_size_is_applied(self) -> None:
        args = _parser().parse_args(["example.com", "--swap-size-mb", "0"])
        cfg = apply_overrides(Config(swap_size_mb=512), args)
        self.assertEqual(cfg.swap_size_mb, 0)


class TileTests(unittest.TestCase):
    portal = "dev.fly-server.ru"

    def test_proxy_tile_strips_prefix_by_default(self) -> None:
        tile = _parse_tile("Мониторинг=/monitor=127.0.0.1:8090", self.portal)
        self.assertEqual(tile.path, "/monitor")
        self.assertEqual(tile.upstream, "127.0.0.1:8090")
        self.assertFalse(tile.keep_prefix)
        self.assertEqual(tile.href, "https://dev.fly-server.ru/monitor/")

    def test_keep_mode(self) -> None:
        tile = _parse_tile("A=/a=127.0.0.1:1=keep", self.portal)
        self.assertTrue(tile.keep_prefix)
        tile = _parse_tile("A=/a=127.0.0.1:1=strip", self.portal)
        self.assertFalse(tile.keep_prefix)

    def test_external_tile(self) -> None:
        tile = _parse_tile("Wiki=https://wiki.example.com/x", self.portal)
        self.assertEqual(tile.path, "")
        self.assertEqual(tile.href, "https://wiki.example.com/x")
        self.assertEqual(tile.hint, "wiki.example.com/x")

    def test_bad_tiles_rejected(self) -> None:
        cases = [
            "Мониторинг",                                # без '='
            "=/monitor=127.0.0.1:8090",                  # пустое название
            "X=javascript:alert(1)",                     # не http(s)
            "X=//evil.example.com",                      # protocol-relative
            "X=https://",                                # пустой хост
            "X=monitor=127.0.0.1:8090",                  # путь без слэша
            "X==127.0.0.1:8090",                         # пустой путь
            "X=/monitor=rm -rf /",                       # upstream с пробелом
            "X=/monitor=127.0.0.1:8090=maybe",           # неизвестный режим
            "X=/a=b=c=d=e",                              # лишние поля
            "X\nY=/a=127.0.0.1:1",                       # перевод строки в названии
            "X<script>=/a=127.0.0.1:1",                 # теги в названии
            # название попадает в строки проверочного bash: кавычки, доллар
            # и обратный слэш сломали бы скрипт или выполнили подстановку
            'X"y=/a=127.0.0.1:1',
            "X'y=/a=127.0.0.1:1",
            "X$y=/a=127.0.0.1:1",
            "X`id`=/a=127.0.0.1:1",
            "X\\y=/a=127.0.0.1:1",
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _parse_tile(raw, self.portal)

    def test_nested_paths_rejected(self) -> None:
        cfg = full_config(
            caddy_tiles=["A=/app=127.0.0.1:1", "B=/app/sub=127.0.0.1:2"]
        )
        with self.assertRaises(ValueError):
            _tiles(cfg)

    def test_duplicate_paths_rejected(self) -> None:
        cfg = full_config(
            caddy_tiles=["A=/app=127.0.0.1:1", "B=/app=127.0.0.1:2"]
        )
        with self.assertRaises(ValueError):
            _tiles(cfg)

    def test_beszel_tile_autogenerated(self) -> None:
        tiles = _tiles(full_config())
        self.assertEqual([t.name for t in tiles], ["Мониторинг", "ThinPro"])
        self.assertTrue(tiles[0].beszel)
        self.assertEqual(tiles[0].upstream, "127.0.0.1:8090")

    def test_no_beszel_tile_on_own_domain(self) -> None:
        cfg = full_config(caddy_domain="monitor.example.com")
        self.assertNotIn("Мониторинг", [t.name for t in _tiles(cfg)])

    def test_beszel_path_configurable(self) -> None:
        cfg = full_config(beszel_path="mon")
        tiles = _tiles(cfg)
        self.assertEqual(tiles[0].path, "/mon")
        self.assertEqual(tiles[0].href, "https://dev.fly-server.ru/mon/")

    def test_tiles_require_portal(self) -> None:
        cfg = full_config(caddy_portal="")
        with self.assertRaises(ValueError):
            installer._validate(cfg)


class DozzleTests(unittest.TestCase):
    def _cfg(self, **overrides) -> Config:
        return full_config(dozzle=True, **overrides)

    def test_tile_autogenerated(self) -> None:
        tiles = _tiles(self._cfg())
        self.assertEqual([t.name for t in tiles], ["Мониторинг", "Логи", "ThinPro"])
        dozzle = next(t for t in tiles if t.name == "Логи")
        self.assertEqual(dozzle.path, "/logs")
        self.assertEqual(dozzle.upstream, "127.0.0.1:8082")
        # DOZZLE_BASE: префикс обязателен, иначе ассеты и API не найдутся
        self.assertTrue(dozzle.keep_prefix)
        self.assertTrue(dozzle.streaming)

    def test_path_configurable(self) -> None:
        tiles = _tiles(self._cfg(dozzle_path="docker-logs"))
        self.assertEqual(next(t for t in tiles if t.name == "Логи").path, "/docker-logs")

    def test_requires_portal(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(caddy_portal=""))

    def test_bad_port_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._dozzle_script(self._cfg(dozzle_port=80))

    def test_caddy_block_disables_buffering(self) -> None:
        cfg = self._cfg()
        block = _portal_caddy_block(cfg, _tiles(cfg))
        # keep, а не handle_path: Dozzle сам монтируется под базовым путём
        self.assertIn("\thandle /logs/* {", block)
        self.assertNotIn("handle_path /logs", block)
        # без flush_interval -1 SSE-логи приходят пачками или не приходят вовсе
        self.assertIn("\t\t\tflush_interval -1", block)
        self.assertIn("read_timeout 3600s", block)

    def test_listens_on_localhost_only(self) -> None:
        # наружу отдаёт Caddy: иначе логи доступны в обход авторизации
        self.assertIn('"127.0.0.1:8082:8080"', installer._dozzle_script(self._cfg()))

    def test_users_yml_not_overwritten(self) -> None:
        # пароль не задан — файл создаётся один раз, иначе повторный прогон
        # тихо сбрасывал бы уже запомненный пароль
        script = installer._dozzle_script(self._cfg())
        self.assertIn(f"if [ -s {installer.DOZZLE_USERS} ]; then", script)
        self.assertIn("пароль прежний, не трогаю", script)

    def test_password_from_config_regenerates(self) -> None:
        script = installer._dozzle_script(self._cfg(dozzle_password="s3cret-pass"))
        self.assertIn("DOZZLE_PASS='s3cret-pass'", script)
        self.assertNotIn("пароль прежний, не трогаю", script)

    def test_password_not_in_argv(self) -> None:
        # пароль уходит в контейнер через stdin, иначе светится в ps хоста
        script = installer._dozzle_script(self._cfg(dozzle_password="s3cret-pass"))
        self.assertIn('echo "$DOZZLE_PASS" | $SUDO docker run', script)
        self.assertNotIn("--password", script)

    def test_version_pinned_and_hardened(self) -> None:
        script = installer._dozzle_script(self._cfg())
        self.assertIn(f"amir20/dozzle:{installer.DOZZLE_VERSION}", script)
        self.assertIn("DOZZLE_AUTH_PROVIDER: simple", script)
        # docker.sock = root на хосте, поэтому actions/shell выключены явно
        self.assertIn('DOZZLE_ENABLE_ACTIONS: "false"', script)
        self.assertIn('DOZZLE_ENABLE_SHELL: "false"', script)

    def test_password_change_recreates_container(self) -> None:
        # регрессия (найдено на боевом хосте): Dozzle держит users.yml в памяти,
        # поэтому после смены пароля вход оставался 401, пока контейнер
        # не пересоздали. Пересоздание — только когда users.yml перезаписан.
        script = installer._dozzle_script(self._cfg(dozzle_password="s3cret-pass"))
        self.assertIn("DOZZLE_USERS_NEW=1", script)
        self.assertIn("--force-recreate", script)
        # ...и при обычном прогоне контейнер не трогаем
        keep = installer._dozzle_script(self._cfg())
        self.assertIn("DOZZLE_USERS_NEW=''", keep)
        self.assertIn("пароль прежний, не трогаю", keep)

    def test_verify_script(self) -> None:
        script = installer._dozzle_verify_script(self._cfg())
        self.assertIn("http://127.0.0.1:8082/logs/", script)
        self.assertIn("grep -qx dozzle", script)
        # регрессия: .format() съедал одну пару скобок, docker получал шаблон
        # '{.Names}', отдавал пустоту, и живой контейнер выглядел MISSING
        self.assertIn("docker ps --format '{{.Names}}'", script)
        self.assertNotIn("'{.Names}'", script)

    def test_tile_probe_reports_http_code(self) -> None:
        # регрессия: в одинарных кавычках $CODE оставался литералом, и
        # строка печатала «($CODE)» вместо настоящего кода
        script = installer._portal_verify_script(self._cfg(), _tiles(self._cfg()))
        line = next(ln for ln in script.splitlines() if "OK: плитка Логи" in ln)
        out = subprocess.run(
            ["bash", "-c", f"CODE=307\ncase $CODE in\n{line.strip()}\nesac"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("307", out.stdout)
        self.assertNotIn("$CODE", out.stdout)


class PortalOutputTests(unittest.TestCase):
    def test_caddy_block_shape(self) -> None:
        cfg = full_config()
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertTrue(block.startswith("dev.fly-server.ru {"))
        # `*` обязателен: у redir первый позиционный аргумент — матчер, и
        # без него редиректом стал бы сам путь (проверено caddy validate +
        # живым прогоном: `redir /monitor/ 308` молча ничего не делает)
        self.assertIn("\t\tredir * /monitor/ 308", block)
        self.assertNotIn("\t\tredir /monitor/ 308", block)
        self.assertIn("\thandle_path /monitor/* {", block)

        self.assertIn("\t\treverse_proxy 127.0.0.1:8090 {", block)
        self.assertIn("\t\t\t\tread_timeout 360s", block)
        self.assertIn("\thandle_path /thinpro/* {", block)
        self.assertIn("\t\treverse_proxy 127.0.0.1:8080\n", block)
        # статика должна быть последней, иначе перехватит прокси
        self.assertLess(block.index("reverse_proxy"), block.index("file_server"))
        self.assertEqual(block.count("file_server"), 1)

    def test_keep_prefix_uses_handle(self) -> None:
        cfg = full_config(caddy_tiles=["A=/a=127.0.0.1:1=keep"])
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertIn("\thandle /a/* {", block)
        self.assertNotIn("handle_path /a", block)

    def test_portal_probe_waits_for_certificate(self) -> None:
        # регрессия (найдено на первом прогоне): Caddy после reload ещё
        # выпускает сертификат, TLS не проходит и портал ложно MISSING
        cfg = full_config()
        script = installer._portal_verify_script(cfg, _tiles(cfg))
        self.assertIn("for _ in $(seq 1 45); do", script)
        self.assertIn('sleep 2', script)
        # ожидание должно быть до проверки, иначе оно бессмысленно
        self.assertLess(
            script.index("seq 1 45"), script.index("OK: страница плиток отдаётся")
        )

    def test_restarting_agent_is_not_ok(self) -> None:
        # регрессия: docker ps показывает и перезапускающиеся контейнеры,
        # агент без ключа падал в цикл, а проверка рапортовала OK
        cfg = full_config()
        host = FakeHost()
        verify(cfg, host)  # type: ignore[arg-type]
        script = host.script
        self.assertIn("Restarting*)", script)
        self.assertIn("ПРОБЛЕМА: beszel-agent", script)
        self.assertIn("--beszel-key", script)

    def test_html_escapes_names(self) -> None:
        # кавычки в названии теперь отвергаются _parse_tile (они ломали бы
        # проверочный bash), но & остаётся допустимым — HTML его экранирует
        cfg = full_config(caddy_tiles=["A&B=/x=127.0.0.1:1"])
        tile = _parse_tile("A&B=/x=127.0.0.1:1", cfg.caddy_portal)
        html = _portal_html(cfg, [tile])
        self.assertIn("A&amp;B", html)
        self.assertNotIn("A&B", html)

    def test_html_has_no_external_assets(self) -> None:
        cfg = full_config()
        html = _portal_html(cfg, _tiles(cfg))
        for needle in ("http://", "cdn", "<script"):
            self.assertNotIn(needle, html, f"в странице портала есть {needle!r}")


class PortalAuthTests(unittest.TestCase):
    """Единый вход на портал: пароль спрашивается один раз."""

    password = "test-portal-pass-42"

    def _cfg(self, **overrides) -> Config:
        values = {"portal_user": "admin", "portal_password": self.password}
        values.update(overrides)
        return full_config(dozzle=True, **values)

    def test_basic_auth_with_hash_placeholder(self) -> None:
        cfg = self._cfg()
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertIn("\tbasic_auth {", block)
        self.assertIn("\t\tadmin __PORTAL_HASH__", block)
        # пароль в Caddyfile оставаться не должен: на диск уходит только хеш
        self.assertNotIn(self.password, block)

    def test_hash_counted_on_host_without_argv(self) -> None:
        cfg = self._cfg()
        script = installer._portal_script(cfg, _tiles(cfg))
        self.assertIn("caddy hash-password", script)
        # stdin, а не --plaintext: открытый пароль не должен светиться в ps
        self.assertNotIn("--plaintext", script)
        self.assertIn("printf '%s\\n' 'test-portal-pass-42'", script)
        self.assertIn('s|__PORTAL_HASH__|$PORTAL_HASH|', script)
        self.assertIn("/opt/caddy/sites/portal.caddy", script)

    def test_no_auth_without_password(self) -> None:
        cfg = full_config(dozzle=True)
        self.assertNotIn("basic_auth", _portal_caddy_block(cfg, _tiles(cfg)))
        self.assertNotIn("hash-password", installer._portal_script(cfg, _tiles(cfg)))

    def test_dozzle_own_auth_disabled(self) -> None:
        script = installer._dozzle_script(self._cfg())
        self.assertNotIn("DOZZLE_AUTH_PROVIDER", script)
        self.assertNotIn("users.yml:/data/users.yml", script)
        self.assertIn("свой вход Dozzle выключен", script)

    def test_dozzle_own_auth_kept_without_portal_password(self) -> None:
        script = installer._dozzle_script(full_config(dozzle=True))
        self.assertIn("DOZZLE_AUTH_PROVIDER: simple", script)
        self.assertIn("- ./users.yml:/data/users.yml:ro", script)

    def test_verify_probes_with_credentials(self) -> None:
        cfg = self._cfg()
        verify_script = installer._portal_verify_script(cfg, _tiles(cfg))
        # иначе каждая плитка отвечала бы 401 и проверка врала бы
        self.assertIn("-u 'admin:test-portal-pass-42'", verify_script)
        self.assertIn("401) echo '  OK: без пароля портал не отдаётся (401)'", verify_script)
        # анонимная проба обязана идти без credentials, иначе 401 не проверить
        anon = verify_script.split("ANON=")[1].split("\n")[0]
        self.assertNotIn(" -u ", anon)

    def test_verify_without_password_has_no_credentials(self) -> None:
        cfg = full_config()
        self.assertNotIn("curl -u", installer._portal_verify_script(cfg, _tiles(cfg)))

    def test_dozzle_verify_reports_who_asks_password(self) -> None:
        self.assertIn(
            "вход: пароль портала", installer._dozzle_verify_script(self._cfg())
        )
        self.assertIn(
            "вход: собственная форма",
            installer._dozzle_verify_script(full_config(dozzle=True)),
        )

    def test_password_requires_portal(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(
                full_config(portal_password="x", caddy_portal="", caddy_tiles=[])
            )

    def test_password_rejects_shell_and_curl_metacharacters(self) -> None:
        for bad in ('a"b', "a'b", "a:b", "a\nb", "a b\rc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                installer._validate(self._cfg(portal_password=bad))

    def test_empty_user_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(portal_user="  "))

    def test_dozzle_on_separate_site_rejected(self) -> None:
        # пароль портала не действует на домене из caddy_sites, а свой вход
        # Dozzle при portal_password выключен — логи были бы без входа
        with self.assertRaises(ValueError):
            installer._validate(
                self._cfg(caddy_sites=["logs.example.com=http://127.0.0.1:8082"])
            )

    def test_cli_flags(self) -> None:
        args = _parser().parse_args(
            ["--portal-user", "operator", "--portal-password", "test-portal-pass-42"]
        )
        cfg = apply_overrides(Config(), args)
        self.assertEqual(cfg.portal_user, "operator")
        self.assertEqual(cfg.portal_password, "test-portal-pass-42")


class PortalAssetTests(unittest.TestCase):
    """Крупные бандлы SPA отдаются без буферизации."""

    def test_beszel_tile_flushes_big_bundle(self) -> None:
        cfg = full_config()
        block = _portal_caddy_block(cfg, _tiles(cfg))
        # без flush_interval бандл Hub (490 КБ) уходит одним куском, и на
        # канале с буксующим TCP-окном браузер рвёт загрузку: страница белая
        self.assertIn("\thandle_path /monitor/* {\n\t\treverse_proxy 127.0.0.1:8090 {\n"
                      "\t\t\tflush_interval -1", block)
        # свой Cache-Control не добавляем: приложения ставят его сами, иначе
        # в ответе будет два заголовка
        self.assertNotIn("Cache-Control", block)

    def test_dozzle_tile_keeps_streaming(self) -> None:
        cfg = full_config(dozzle=True)
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertIn("\thandle /logs/* {", block)
        self.assertIn("\t\t\tflush_interval -1", block)

    def test_plain_proxy_tile_untouched(self) -> None:
        cfg = full_config(
            dozzle=False,
            beszel=False,
            caddy_domain="",
            caddy_tiles=["Shop=/shop=127.0.0.1:3000"],
        )
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertIn("\thandle_path /shop/* {\n\t\treverse_proxy 127.0.0.1:3000", block)
        self.assertNotIn("flush_interval", block)

    def test_access_log_on_by_default_and_disableable(self) -> None:
        cfg = full_config()
        block = _portal_caddy_block(cfg, _tiles(cfg))
        self.assertIn("\tlog {\n\t\toutput stdout\n\t\tformat json\n\t}\n", block)
        site = installer._caddy_beszel_block(cfg, "monitor.example.com", 8090)
        self.assertIn("format json", site)
        self.assertIn("flush_interval -1", site)
        # диагностика не должна стоить лишних логов, если они не нужны
        quiet = full_config(caddy_access_log=False)
        self.assertNotIn("log {", _portal_caddy_block(quiet, _tiles(quiet)))
        self.assertNotIn("log {", installer._caddy_beszel_block(quiet, "m.example.com", 8090))


class BeszelModeTests(unittest.TestCase):
    """Режим agent: на узле только агент, хаб живёт на другом сервере."""

    def _cfg(self, **overrides) -> Config:
        kwargs = {
            "beszel_mode": "agent",
            "caddy_portal": "",
            "caddy_tiles": [],
            "caddy_sites": [],
            "caddy_domain": "",
        }
        kwargs.update(overrides)
        return full_config(**kwargs)

    def test_agent_compose_has_no_hub(self) -> None:
        script = installer._beszel_script(self._cfg())
        bash_check(script)
        self.assertIn("LISTEN: 45876", script)
        self.assertNotIn("image: henrygd/beszel:", script)
        # хаб и агент делят один compose: перезапись убирает контейнер хаба
        self.assertIn("--remove-orphans", script)
        self.assertEqual(script.count("container_name: beszel-agent"), 1)

    def test_agent_port_checked_not_hub(self) -> None:
        verify_script = installer._beszel_verify_script(self._cfg())
        bash_check(verify_script)
        self.assertIn("sport = :45876", verify_script)
        self.assertNotIn("beszel (hub)", verify_script)

    def test_agent_state_template_survives_format(self) -> None:
        # docker ps -format '{{.Status}}': после str.format одинарные скобки
        # съедаются, и в сообщение попадает литерал '{.Status}' вместо статуса
        verify_script = installer._beszel_verify_script(self._cfg())
        bash_check(verify_script)
        self.assertIn("'{{.Status}}'", verify_script)
        self.assertNotIn("'{.Status}'", verify_script)
        self.assertIn("Restarting*", verify_script)

    def test_hub_mode_keeps_hub(self) -> None:
        cfg = self._cfg(beszel_mode="hub", caddy_portal="dc.example.com")
        script = installer._beszel_script(cfg)
        bash_check(script)
        self.assertIn("image: henrygd/beszel:0.20.0", script)
        self.assertIn("image: henrygd/beszel-agent:0.20.0", script)
        self.assertNotIn(":latest", script)
        self.assertIn("beszel (hub)", installer._beszel_verify_script(cfg))

    def test_beszel_version_configurable(self) -> None:
        script = installer._beszel_script(self._cfg(beszel_version="0.21.3"))
        self.assertIn("image: henrygd/beszel-agent:0.21.3", script)

    def test_bad_beszel_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(beszel_version="latest"))

    def test_allow_list_opens_port_for_hub_only(self) -> None:
        script = installer._beszel_script(
            self._cfg(beszel_agent_allow=["185.50.202.219/32"])
        )
        bash_check(script)
        self.assertIn(
            "ufw allow from 185.50.202.219/32 to any port 45876 proto tcp", script
        )
        # без активного ufw правило молча не сработает — об этом честно пишем
        self.assertIn("ufw не активен", script)

    def test_bad_mode_and_cidr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(beszel_mode="both"))
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(beszel_agent_allow=["не адрес"]))

    def test_agent_without_hub_url_only_listens(self) -> None:
        script = installer._beszel_script(self._cfg())
        bash_check(script)
        self.assertNotIn("HUB_URL", script)
        self.assertIn("хаб ходит к нему по этому порту", script)

    def test_hub_url_makes_agent_dial_out(self) -> None:
        # Провайдер узла режет входящие порты: агент сам звонит в хаб
        cfg = self._cfg(
            beszel_hub_url="monitor.example.com",
            beszel_agent_allow=["185.50.202.219/32"],
        )
        script = installer._beszel_script(cfg)
        bash_check(script)
        self.assertIn('HUB_URL: "https://monitor.example.com"', script)
        self.assertIn("агент сам подключается к хабу", script)
        # входящий firewall не нужен: хаб к агенту не ходит
        self.assertNotIn("ufw allow from", script)
        self.assertIn("соединение инициирует агент", installer._beszel_verify_script(cfg))

    def test_hub_url_keeps_explicit_scheme(self) -> None:
        script = installer._beszel_script(
            self._cfg(beszel_hub_url="http://10.0.0.1:8090/")
        )
        self.assertIn('HUB_URL: "http://10.0.0.1:8090"', script)

    def test_bad_hub_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(beszel_hub_url="https://bad url/"))

    def test_no_monitoring_tile_on_agent_node(self) -> None:
        cfg = self._cfg(caddy_portal="vpn.fly-server.ru", dozzle=True)
        names = [tile.name for tile in installer._tiles(cfg)]
        self.assertIn("Логи", names)
        self.assertNotIn("Мониторинг", names)


EDGE_ID = "3c0b1d6e-9a2f-4c1b-8f7d-5e6a7b8c9d0e"
EDGE_KEY = (
    "aHR0cHM6Ly9wb3J0YWluZXIuZXhhbXBsZS5jb218cG9ydGFpbmVyLmV4YW1wbGUuY29tOjgw"
    "fEZJTkdFUlBSSU5UfDM="
)


class PortainerTests(unittest.TestCase):
    """Edge-агент: EDGE_ID + EDGE_KEY из UI Portainer вместо join token."""

    def test_server_compose_and_caddy_site(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_mode="server",
            portainer_domain="portainer.example.com",
        )
        script = installer._portainer_script(cfg)
        bash_check(script)
        self.assertIn("image: portainer/portainer-ce:2.45.1", script)
        # UI только на localhost; туннель — на локальный порт хоста, наружу его
        # выводит Caddy (провайдер режет всё, кроме 80/443)
        self.assertIn('"127.0.0.1:9443:9443"', script)
        self.assertIn('"127.0.0.1:8000:80"', script)
        self.assertNotIn('"8000:8000"', script)
        # chisel внутри контейнера слушает 0.0.0.0:80 — иначе publish до туннеля
        # не доходит и агенты не подключатся
        self.assertIn("--tunnel-addr=0.0.0.0 --tunnel-port=80", script)
        site = installer._caddy_portainer_block(cfg, "portainer.example.com", 9443)
        self.assertIn("tls_insecure_skip_verify", site)
        self.assertIn("flush_interval -1", site)
        sites = installer._caddy_sites_script(cfg)
        self.assertIn("portainer.example.com {", sites)
        # отдельный сайт на :80 — только websocket, остальное редирект на https
        self.assertIn("portainer-tunnel.caddy", sites)
        self.assertIn("http://portainer.example.com {", sites)
        self.assertIn("@ws header Connection *Upgrade*", sites)
        self.assertIn("reverse_proxy @ws 127.0.0.1:8000", sites)
        self.assertIn("redir @notws https://{host}{uri} 308", sites)

    def test_agent_compose_uses_edge_key(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_mode="agent",
            portainer_agent_edge_id=EDGE_ID,
            portainer_agent_edge_key=EDGE_KEY,
        )
        script = installer._portainer_script(cfg)
        bash_check(script)
        self.assertIn("image: portainer/agent:2.45.1", script)
        self.assertIn(f'EDGE_ID: "{EDGE_ID}"', script)
        self.assertIn(f'EDGE_KEY: "{EDGE_KEY}"', script)
        self.assertIn('EDGE: "true"', script)
        self.assertIn('EDGE_INSECURE_POLL: "true"', script)
        # Docker-сокет и volumes видят Docker-операции Portainer через туннель
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", script)
        self.assertIn("/var/lib/docker/volumes:/var/lib/docker/volumes", script)
        # наружу ничего не публикуем: агент сам ходит на сервер
        self.assertNotIn("ports:", script)
        # --join-token в Portainer 2.45 нет, ключ несут переменные окружения
        self.assertNotIn("--join-token", script)
        self.assertNotIn("AGENT_CLUSTER_ADDR:", script)

    def test_edge_key_survives_second_run(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_mode="agent",
            portainer_agent_edge_id=EDGE_ID,
        )
        script = installer._portainer_script(cfg)
        bash_check(script)
        # пустой ключ на новом узле не затирает старый, но честно ругается
        self.assertIn("EDGE_KEY: \"\"", script)
        self.assertIn("EDGE_KEY пуст", script)

    def test_agent_reports_tunnel_from_key(self) -> None:
        script = installer._portainer_script(
            full_config(
                portainer=True,
                portainer_mode="agent",
                portainer_agent_edge_id=EDGE_ID,
                portainer_agent_edge_key=EDGE_KEY,
            )
        )
        self.assertIn("ws://portainer.example.com:80", script)

    def test_edge_key_kept_when_not_in_config(self) -> None:
        script = installer._portainer_script(
            full_config(portainer=True, portainer_mode="agent")
        )
        bash_check(script)
        self.assertIn('EDGE_KEY: "__PORTAINER_EDGE_KEY_KEEP__"', script)
        self.assertIn('EDGE_ID: "__PORTAINER_EDGE_ID_KEEP__"', script)
        self.assertIn("PORTAINER_OLD_EDGE_KEY=", script)

    def test_agent_edge_id_and_key_go_together(self) -> None:
        # без ключа агент не зарегистрируется, без id — тоже; молча ставить
        # половину хуже, чем отказать до похода в SSH
        with self.assertRaises(ValueError):
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_mode="agent",
                    portainer_agent_edge_id=EDGE_ID,
                )
            )
        with self.assertRaises(ValueError):
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_mode="agent",
                    portainer_agent_edge_key=EDGE_KEY,
                )
            )
        with self.assertRaises(ValueError):  # не UUID
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_mode="agent",
                    portainer_agent_edge_id="не-uuid",
                    portainer_agent_edge_key=EDGE_KEY,
                )
            )
        with self.assertRaises(ValueError):  # не base64-ключ Portainer
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_mode="agent",
                    portainer_agent_edge_id=EDGE_ID,
                    portainer_agent_edge_key="portainer_edp_abc",
                )
            )
        # домен задавать не обязательно: адрес сервера лежит в ключе
        installer._validate(
            full_config(
                portainer=True,
                portainer_mode="agent",
                portainer_agent_edge_id=EDGE_ID,
                portainer_agent_edge_key=EDGE_KEY,
            )
        )
        # но если задан — должен совпадать с ключом, иначе агент уйдёт в чужой
        # Portainer и будет молча молчать в логах
        with self.assertRaises(ValueError):
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_mode="agent",
                    portainer_agent_edge_id=EDGE_ID,
                    portainer_agent_edge_key=EDGE_KEY,
                    portainer_domain="other.example.com",
                )
            )
        installer._validate(
            full_config(
                portainer=True,
                portainer_mode="agent",
                portainer_agent_edge_id=EDGE_ID,
                portainer_agent_edge_key=EDGE_KEY,
                portainer_domain="portainer.example.com",
            )
        )

    def test_server_requires_domain_and_caddy(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(full_config(portainer=True, portainer_domain=""))
        with self.assertRaises(ValueError):
            installer._validate(
                full_config(
                    portainer=True,
                    portainer_domain="portainer.example.com",
                    caddy=False,
                )
            )

    def test_tile_is_external_link(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_mode="server",
            portainer_domain="portainer.example.com",
        )
        tile = next(t for t in installer._tiles(cfg) if t.name == "Контейнеры")
        self.assertEqual(tile.href, "https://portainer.example.com/")
        # под базовым путём портала Portainer не работает — только внешняя ссылка
        self.assertEqual(tile.path, "")

    def test_bad_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(full_config(portainer=True, portainer_mode="agent2"))

    def test_admin_password_written_as_secret_file(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_domain="portainer.example.com",
            portainer_admin_password="Секрет-Пароль-42",
        )
        script = installer._portainer_script(cfg)
        bash_check(script)
        # пароль не в compose: только путь к файлу 0600
        self.assertNotIn("Секрет-Пароль-42", script.split("PORTAINER_EOF")[1])
        self.assertIn(
            "install -m 0600 -o root -g root", script
        )
        self.assertIn(
            "command: --admin-password-file /run/portainer-admin-password"
            " --tunnel-addr=0.0.0.0 --tunnel-port=80",
            script,
        )
        # образ запускает сам portainer: command = только флаги, иначе
        # контейнер уходит в restart с "unexpected portainer"
        self.assertNotIn("command: portainer", script)
        # проверка смотрит на путь на хосте, а не внутри контейнера
        self.assertIn("if [ -f /opt/portainer/admin-password ]", script)
        self.assertIn('stat -c %a /opt/portainer/admin-password', script)

    def test_without_admin_password_warns_about_window(self) -> None:
        script = installer._portainer_script(
            full_config(portainer=True, portainer_domain="portainer.example.com")
        )
        bash_check(script)
        self.assertNotIn("--admin-password-file", script)
        self.assertIn("5 секунд", script)

    def test_verify_checks_ui_tunnel_and_admin_file(self) -> None:
        # --verify-only не должен быть слабее установки: UI, туннель и файл
        # пароля проверяются одинаково
        cfg = full_config(
            portainer=True,
            portainer_domain="portainer.example.com",
            portainer_admin_password="Секрет-Пароль-42",
        )
        script = installer.PORTAINER_VERIFY.format(
            name="portainer",
            extra=installer._portainer_extra_verify(cfg),
            image_check=installer._portainer_image_check(cfg),
        )
        bash_check(script)
        self.assertIn("https://127.0.0.1:9443/api/status", script)
        self.assertIn("sport = :8000", script)
        # туннель проверяем через Caddy на :80 — ровно тем путём, каким идёт агент
        self.assertIn("--resolve portainer.example.com:80:127.0.0.1", script)
        self.assertIn("ws://portainer.example.com:80", script)
        # присваивание без кавычек вокруг имени: под set -u '"$VAR"=...'
        # падает с "unbound variable", и bash -n такой опечатки не видит
        self.assertIn('PORT_TUNNEL_CODE="$(', script)
        self.assertNotIn('"$PORT_TUNNEL_CODE"=', script)
        # весь curl одной строкой: внутри $(...) перевод строки разделяет команды
        self.assertIn(
            "Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw=='"
            " http://portainer.example.com/ 2>/dev/null",
            script,
        )
        self.assertIn("if [ -f /opt/portainer/admin-password ]", script)

    def test_agent_verify_keeps_edge_key_check(self) -> None:
        cfg = full_config(
            portainer=True,
            portainer_mode="agent",
            portainer_agent_edge_id=EDGE_ID,
            portainer_agent_edge_key=EDGE_KEY,
        )
        script = installer._portainer_script(cfg)
        self.assertIn("portainer-agent", script)
        self.assertIn("EDGE_ID/EDGE_KEY заданы в compose", script)


class PortainerLocalEnvTests(unittest.TestCase):
    """--portainer-local-env: сам сервер в списке узлов Portainer."""

    def _cfg(self, **overrides) -> Config:
        params = {
            "portainer": True,
            "portainer_mode": "server",
            "portainer_domain": "portainer.example.com",
            "portainer_admin_password": "Секрет-Пароль-42",
            "portainer_local_env": "dc",
        }
        params.update(overrides)
        return full_config(**params)

    def test_env_created_via_api(self) -> None:
        script = installer._portainer_script(self._cfg())
        bash_check(script)
        # Type=1 — «Docker Standalone»: docker socket хоста, агент не нужен
        self.assertIn('\\"Name\\":\\"dc\\"', script)
        self.assertIn('\\"Type\\":1', script)
        self.assertIn("https://127.0.0.1:9443/api/auth", script)
        # пароль берётся с файла 0600 на хосте, а не из конфига
        self.assertIn("$SUDO cat /opt/portainer/admin-password", script)
        self.assertIn("Authorization: Bearer $JWT_LOCAL", script)
        # в JSON запроса идёт переменная, а не сам секрет
        self.assertIn('\\"password\\":\\"$PW_LOCAL\\"', script)
        # в compose секрета нет: он пишется на хост файлом 0600
        self.assertNotIn("Секрет-Пароль-42", script.split("PORTAINER_EOF")[1])

    def test_env_creation_in_install_and_verify(self) -> None:
        cfg = self._cfg()
        installer._validate(cfg)
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            install(cfg, host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn("/api/endpoints", host.script)
        # фрагмент идёт после compose: контейнер к этому моменту поднят
        self.assertLess(
            host.script.index("PORTAINER_EOF"),
            host.script.index("/api/endpoints"),
        )
        vhost = FakeHost()
        verify(cfg, vhost)  # type: ignore[arg-type]
        bash_check(vhost.script)
        self.assertIn("/api/endpoints", vhost.script)
        self.assertNotIn("apt-get install", vhost.script)

    def test_no_env_fragment_by_default(self) -> None:
        script = installer._portainer_script(
            self._cfg(portainer_local_env="")
        )
        bash_check(script)
        self.assertNotIn("/api/endpoints", script)

    def test_env_absent_means_honest_warning(self) -> None:
        # файла пароля нет — узел не создастся, и сказать об этом лучше
        # прямо, чем рапортовать об успехе
        script = installer._portainer_local_env_script(self._cfg())
        bash_check(script)
        self.assertIn("ВНИМАНИЕ: нет /opt/portainer/admin-password", script)
        self.assertIn("не удалось войти в Portainer API", script)

    def test_failed_auth_does_not_kill_install(self) -> None:
        # под set -euo pipefail неудачный curl в $(...) роняет весь скрипт:
        # нужен || true, иначе вместо предупреждения — молчаливый обрыв
        # установки на середине
        script = installer._portainer_local_env_script(self._cfg())
        bash_check(script)
        self.assertIn("""| sed -n 's/.*"jwt":"\\([^"]*\\)".*/\\1/p' || true)\"""", script)

    def test_agent_node_rejected(self) -> None:
        # у edge-агента своего API нет: опция молча ничего бы не сделала
        with self.assertRaises(ValueError):
            installer._validate(
                self._cfg(
                    portainer_mode="agent",
                    portainer_agent_edge_id=EDGE_ID,
                    portainer_agent_edge_key=EDGE_KEY,
                )
            )
        with self.assertRaises(ValueError):
            installer._validate(self._cfg(portainer=False))

    def test_bad_env_name_rejected(self) -> None:
        # имя подставляется в JSON и в grep: кавычка или пробел ломают
        # и запрос, и проверку «уже есть»
        for name in ('dc "x"', "моё окружение", "dc/x", ".hidden", "a" * 32):
            with self.assertRaises(ValueError):
                installer._validate(self._cfg(portainer_local_env=name))
        # пустое имя — это «не просим», а не ошибка
        installer._validate(self._cfg(portainer_local_env=""))


class ForeignCaddyTests(unittest.TestCase):
    """Чужой контейнер caddy: установщик предупреждает, а не переименовывает."""

    def _caddy_script(self) -> str:
        return installer.CADDY.format(
            dir=installer.CADDY_DIR,
            sites_dir=installer.CADDY_SITES_DIR,
            portal_dir=installer.PORTAL_DIR,
            compose=installer.CADDY_COMPOSE,
            email="you@example.com",
            caddyfile=installer._caddy_caddyfile("you@example.com"),
            portal_cleanup=installer._portal_cleanup_script(),
            sites="",
            static_volumes="",
            static_check="",
        )

    def test_preflight_present_and_safe(self) -> None:
        script = self._caddy_script()
        bash_check(script)
        self.assertIn("project.working_dir", script)
        self.assertIn("docker rename caddy caddy-prev", script)
        # установщик не имеет права молча трогать чужой контейнер
        self.assertNotIn("docker rm -f caddy\n", script.replace("      $SUDO docker rm -f caddy", ""))
        self.assertIn("exit 1", script)

    def test_preflight_runs_before_compose_write(self) -> None:
        script = self._caddy_script()
        self.assertLess(script.index("FOREIGN_CADDY=\"\""), script.index("CADDY_COMPOSE_EOF"))
        self.assertNotIn("{$caddy_id}", script)

    def test_container_starts_before_sites(self) -> None:
        # пароль портала хешируется через `docker exec caddy`: на первом
        # запуске контейнера ещё нет, и сайты до `compose up` падали бы
        cfg = full_config(portal_password="test-portal-pass-42")
        tiles = installer._tiles(cfg)
        script = installer.CADDY.format(
            dir=installer.CADDY_DIR,
            sites_dir=installer.CADDY_SITES_DIR,
            portal_dir=installer.PORTAL_DIR,
            compose=installer.CADDY_COMPOSE,
            email=cfg.caddy_email,
            caddyfile=installer._caddy_caddyfile(cfg.caddy_email),
            portal_cleanup=installer._portal_cleanup_script(),
            sites=installer._caddy_sites_script(cfg, portal=True)
            + installer._portal_script(cfg, tiles),
            static_volumes="",
            static_check="",
        )
        bash_check(script)
        up = script.index(f"docker compose -f {installer.CADDY_COMPOSE} up -d")
        first_site = script.index("CADDY_SITE_EOF")
        hash_call = script.index("PORTAL_HASH=$(printf")
        self.assertLess(up, first_site)
        self.assertLess(up, hash_call)
        # хеш обязан быть посчитан ДО записи portal.caddy: заглушка
        # __PORTAL_HASH__ невалидна для Caddy и роняет контейнер
        self.assertLess(hash_call, script.index("tee /opt/caddy/sites/portal.caddy"))
        self.assertLess(
            script.index("tee /opt/caddy/sites/portal.caddy"),
            script.index("__PORTAL_HASH__|"),
        )

    def test_broken_portal_site_removed_before_start(self) -> None:
        # остаток неудачного прогона: portal.caddy с заглушкой вместо хеша
        # не даёт контейнеру подняться, а следующий прогон падает на exec
        script = self._caddy_script()
        cleanup = script.index("битый portal.caddy")
        self.assertLess(cleanup, script.index("compose -f /opt/caddy/docker-compose.yml up -d"))
        self.assertIn("2[aby]", script)

    def test_hash_pattern_is_ere_not_bre(self) -> None:
        # в ERE `\|` — литеральный пайп: с ним валидный хеш $2a$ никогда не
        # совпал бы, и битый portal.caddy удалялся бы на каждом прогоне
        cleanup = installer._portal_cleanup_script()
        self.assertIn(r"'\$(2[aby]|argon2id)\$'", cleanup)
        self.assertNotIn(r"\|", cleanup)
        # настоящий bcrypt-хеш должен считаться валидным файлом
        valid = "www.example.com {\n\tbasic_auth {\n\t\tadmin $2a$14$abcdefghij\n\t}\n}\n"
        for pattern in (r"\$(2[aby]|argon2id)\$",):
            proc = subprocess.run(
                ["grep", "-qE", pattern],
                input=valid,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{pattern} не нашёл валидный хеш")
        for content in ("__PORTAL_HASH__", ""):
            proc = subprocess.run(
                ["grep", "-qE", r"\$(2[aby]|argon2id)\$"],
                input=f"www.example.com {{\n\tbasic_auth {{\n\t\tadmin {content}\n\t}}\n}}\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0, f"заглушка {content!r} принята за хеш")

    def test_broken_portal_forces_recreate(self) -> None:
        # контейнер читает конфиг только при старте, а после серии падений
        # docker держит паузу (backoff) — без пересоздания он продолжит
        # падать на старой версии конфига
        script = self._caddy_script()
        cleanup = script.index("битый portal.caddy")
        recreate = script.index("--force-recreate")
        self.assertLess(cleanup, recreate)
        self.assertIn("--remove-orphans", script)


class StaticSiteTests(unittest.TestCase):
    """Сайт-статика: caddy_site вида "домен=file:/каталог"."""

    def test_static_site_serves_directory(self) -> None:
        cfg = full_config(caddy_sites=["www.example.com=file:/var/www"])
        block = installer._caddy_static_block(cfg, "www.example.com", "/var/www")
        self.assertIn("root * /var/www", block)
        self.assertIn("file_server", block)
        self.assertNotIn("reverse_proxy", block)
        script = installer._caddy_sites_script(cfg)
        self.assertIn("www.example.com {", script)
        self.assertIn("root * /var/www", script)
        self.assertIn("/opt/caddy/sites/www.example.com.caddy", script)

    def test_site_script_keeps_proxy_mode(self) -> None:
        cfg = full_config(caddy_sites=["shop.example.com=127.0.0.1:3000"])
        self.assertIn("reverse_proxy 127.0.0.1:3000", installer._caddy_sites_script(cfg))

    def test_bad_root_rejected(self) -> None:
        for root in ("file:relative/dir", "file:/var/www/../etc", "file:/var/w ww"):
            with self.assertRaises(ValueError):
                installer._caddy_sites_script(full_config(caddy_sites=[f"x.example.com={root}"]))

    def test_reserved_root_rejected(self) -> None:
        # каталог статики нельзя навести на пути контейнера caddy: mount
        # перекроет конфиг, сертификаты или сам каталог портала
        for root in ("/etc/caddy", "/etc/caddy/sites", "/srv/portal", "/data", "/config/cfg"):
            with self.assertRaises(ValueError):
                installer._caddy_sites_script(full_config(caddy_sites=[f"x.example.com=file:{root}"]))

    def test_static_root_mounted_into_container(self) -> None:
        # сеть host не даёт контейнеру видеть файловую систему хоста: без
        # bind-mount статики caddy отдаёт 404 на каждый файл
        cfg = full_config(caddy_sites=["www.example.com=file:/var/www"])
        roots = installer._static_roots(cfg)
        self.assertEqual(roots, ["/var/www"])
        script = installer.CADDY.format(
            dir=installer.CADDY_DIR,
            sites_dir=installer.CADDY_SITES_DIR,
            portal_dir=installer.PORTAL_DIR,
            compose=installer.CADDY_COMPOSE,
            email=cfg.caddy_email,
            caddyfile=installer._caddy_caddyfile(cfg.caddy_email),
            portal_cleanup="",
            sites="",
            static_volumes=installer._static_volumes(roots),
            static_check=installer._static_roots_check(roots),
        )
        bash_check(script)
        self.assertIn("      - /var/www:/var/www:ro\n", script)
        # каталога нет — docker создал бы пустой, нужен явный предупреждение
        self.assertIn('if [ ! -d "/var/www" ]', script)

    def test_static_roots_deduplicated(self) -> None:
        cfg = full_config(
            caddy_sites=[
                "www.example.com=file:/var/www",
                "old.example.com=static:/var/www",
                "shop.example.com=127.0.0.1:3000",
            ]
        )
        self.assertEqual(installer._static_roots(cfg), ["/var/www"])
        self.assertEqual(installer._static_volumes(["/var/www"]), "      - /var/www:/var/www:ro\n")

    def test_browse_index_hide_reproduce_lighttpd(self) -> None:
        # те же опции, что задавал lighttpd на мигрируемых сайтах
        cfg = full_config(
            caddy_sites=[
                "etc.example.com=file:/var/www/etc|browse"
                "|index=index.html,index.lighttpd.html|hide=.svn"
            ]
        )
        script = installer._caddy_sites_script(cfg)
        bash_check(script)
        self.assertIn("\tfile_server browse {\n", script)
        self.assertIn("\t\tindex index.html index.lighttpd.html\n", script)
        self.assertIn("\t\thide .svn\n", script)
        self.assertIn("\t}\n", script)

    def test_site_file_backed_up_before_write(self) -> None:
        # файлы правили руками, откат после неудачного прогона иначе означал бы
        # дописывание конфига заново
        script = installer._caddy_sites_script(full_config(
            caddy_sites=["www.example.com=file:/var/www"]))
        bash_check(script)
        self.assertIn("mkdir -p \"$backup\"", script)
        self.assertIn("cp -a /opt/caddy/sites/www.example.com.caddy", script)
        self.assertIn("/opt/caddy/sites.bak/$(date +%Y%m%d-%H%M%S)", script)

    def test_site_write_skipped_when_unchanged(self) -> None:
        # идемпотентный повторный прогон не должен плодить одинаковые копии
        script = installer._caddy_sites_script(full_config(
            caddy_sites=["www.example.com=file:/var/www"]))
        self.assertIn("cmp -s", script)
        self.assertIn("rm -f /opt/caddy/.sites-new/www.example.com.caddy", script)

    def test_old_site_backups_pruned(self) -> None:
        script = installer._caddy_sites_script(full_config(
            caddy_sites=["www.example.com=file:/var/www"]))
        self.assertIn(f"tail -n +{installer.SITE_BACKUP_KEEP + 1}", script)

    def test_site_draft_outside_served_dir(self) -> None:
        # sites/ смонтирован в контейнер и подключён import *.caddy — черновик
        # туда класть нельзя
        script = installer._caddy_sites_script(full_config(
            caddy_sites=["www.example.com=file:/var/www"]))
        self.assertIn("/opt/caddy/.sites-new/www.example.com.caddy", script)
        self.assertNotIn("> /opt/caddy/sites/www.example.com.caddy.new", script)

    def test_lua_never_goes_to_hide(self) -> None:
        # hide у caddy не «скрывает из списка», а отдаёт 404 и на прямой
        # запрос: *.lua в hide отрезал flylinkdc-search-engine.lua, который
        # клиент FlyLinkDC качает с зеркала для поиска по RSS
        cfg = full_config(
            caddy_sites=["etc.example.com=file:/var/www/etc|browse|hide=.svn"]
        )
        self.assertNotIn("lua", installer._caddy_sites_script(cfg))

    def test_browse_only_when_listed(self) -> None:
        # пустой список значений для browse — флаг, а не «листинг выключен»
        opts = installer._static_options("browse|index=index.html")
        self.assertIn("browse", opts)
        self.assertIn("file_server browse", installer._caddy_static_block(
            full_config(), "x.example.com", "/var/www", opts))

    def test_deny_becomes_403_matcher(self) -> None:
        # у caddy нет директивы deny: путь ловит matcher, ответ — respond
        cfg = full_config(caddy_sites=["etc.example.com=file:/var/www|deny=*.inc,*.php,*~"])
        script = installer._caddy_sites_script(cfg)
        bash_check(script)
        self.assertIn("\t@forbidden path *.inc *.php *~\n", script)
        self.assertIn("\trespond @forbidden 403\n", script)
        # respond в порядке директив caddy идёт раньше file_server
        self.assertLess(script.index("respond @forbidden"), script.index("file_server"))

    def test_browse_path_lists_only_that_path(self) -> None:
        # lighttpd гасил dir-listing для /update и /etc, а /install/ оставлял:
        # листинг включается точечно, весь сайт открывать нельзя
        cfg = full_config(
            caddy_sites=["www.example.com=file:/var/www|browse=/install/|hide=.svn"]
        )
        script = installer._caddy_sites_script(cfg)
        bash_check(script)
        self.assertIn("\t@listing path /install /install/ /install/*\n", script)
        # блоки handle идут по порядку: сперва листинг, потом всё остальное
        self.assertIn("\thandle @listing {\n\t\tfile_server browse {\n", script)
        self.assertIn("\thandle {\n\t\tfile_server {\n", script)
        self.assertNotIn("\tfile_server browse\n", script)
        # hide нужен в обоих file_server, иначе .svn отдаётся мимо листинга
        self.assertEqual(script.count("hide .svn"), 2)

    def test_browse_path_keeps_deny_working(self) -> None:
        # respond упорядочен после handle: снаружи он не сработал бы вовсе
        cfg = full_config(
            caddy_sites=["www.example.com=file:/var/www|browse=/install/|deny=*.inc"]
        )
        script = installer._caddy_sites_script(cfg)
        bash_check(script)
        self.assertIn("\thandle @forbidden {\n\t\trespond 403\n\t}\n", script)
        self.assertLess(script.index("handle @forbidden"), script.index("handle @listing"))

    def test_browse_path_bad_value_rejected(self) -> None:
        for options in (
            "browse=install/",   # без ведущего слэша
            "browse=/a/*",       # маска: matcher собирает путь сам
            "browse=/etc,c",     # без ведущего слэша
        ):
            with self.assertRaises(ValueError):
                installer._caddy_sites_script(
                    full_config(caddy_sites=[f"www.example.com=file:/var/www|{options}"])
                )

    def test_options_rejected_on_proxy_site(self) -> None:
        with self.assertRaises(ValueError):
            installer._caddy_sites_script(
                full_config(caddy_sites=["shop.example.com=127.0.0.1:3000|browse"])
            )

    def test_bad_options_rejected(self) -> None:
        for options in (
            "brouse",            # опечатка
            "index=",            # пустой список
            "hide=a b",          # пробел ломает разбор
            "index=../etc",      # путь, а не имя файла
            "x=1",               # неизвестная опция
            "browse||hide=.svn",  # пустая опция между разделителями
        ):
            with self.assertRaises(ValueError):
                installer._caddy_sites_script(
                    full_config(caddy_sites=[f"etc.example.com=file:/var/www|{options}"])
                )

    def test_trailing_separator_tolerated(self) -> None:
        # хвостовой | — обычная описка при склейке строк, а не ошибка
        cfg = full_config(caddy_sites=["etc.example.com=file:/var/www|browse|"])
        self.assertIn("\tfile_server browse\n", installer._caddy_sites_script(cfg))

    def test_root_parsed_without_options(self) -> None:
        # хвост опций не должен попадать в bind-mount: каталога "…|browse" нет
        cfg = full_config(caddy_sites=["etc.example.com=file:/var/www/etc|browse"])
        self.assertEqual(installer._static_roots(cfg), ["/var/www/etc"])


class BeszelUrlTests(unittest.TestCase):
    def test_portal_url(self) -> None:
        self.assertEqual(
            _beszel_public_url(full_config()), "https://dev.fly-server.ru/monitor"
        )

    def test_own_domain(self) -> None:
        cfg = full_config(caddy_domain="monitor.example.com")
        self.assertEqual(_beszel_public_url(cfg), "https://monitor.example.com")

    def test_localhost(self) -> None:
        cfg = full_config(caddy=False)
        self.assertEqual(_beszel_public_url(cfg), "http://localhost:8090")


class ToolsTests(unittest.TestCase):
    def test_script_has_pinned_checksums(self) -> None:
        script = _tools_script(list(EXTRA_TOOLS))
        for name in EXTRA_TOOLS:
            spec = installer._GITHUB_TOOLS[name]
            for machine, sha in spec["sha256"].items():
                self.assertIn(sha, script, f"{name}/{machine}: нет sha256 архива")
            for machine, sha in spec["bin_sha256"].items():
                self.assertIn(sha, script, f"{name}/{machine}: нет sha256 бинарника")

    def test_script_covers_arches(self) -> None:
        script = _tools_script(["lazydocker"])
        for machine in installer._GITHUB_TOOLS["lazydocker"]["arch"]:
            self.assertIn(f"    {machine})", script)

    def test_unknown_tool_rejected(self) -> None:
        with self.assertRaises(ValueError):
            installer._validate(full_config(tools=["нет-такой"]))


class GeneratedScriptTests(unittest.TestCase):
    def _install_script(self, cfg: Config) -> str:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            install(cfg, host)  # type: ignore[arg-type]
        return host.script

    def test_full_install_script_is_valid_bash(self) -> None:
        bash_check(self._install_script(full_config()))

    def test_dozzle_in_script(self) -> None:
        script = self._install_script(full_config(dozzle=True))
        bash_check(script)
        self.assertIn("/opt/dozzle/docker-compose.yml", script)
        self.assertIn("DOZZLE_BASE: /logs", script)
        # порядок: контейнер поднимается до прокси, иначе проверка плитки
        # сразу после reload поймает 502 и запутает
        self.assertLess(
            script.index("amir20/dozzle:"), script.index("==> Caddy:")
        )

    def test_dozzle_in_verify_only(self) -> None:
        host = FakeHost()
        verify(full_config(dozzle=True), host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn("grep -qx dozzle", host.script)
        self.assertNotIn("apt-get install", host.script)

    def test_minimal_script_is_valid_bash(self) -> None:
        bash_check(self._install_script(Config(host="203.0.113.5")))

    def test_portal_auth_script_is_valid_bash(self) -> None:
        cfg = full_config(dozzle=True, portal_password="test-portal-pass-42")
        script = self._install_script(cfg)
        bash_check(script)
        # заглушка обязана подменяться уже после записи файла сайта
        self.assertLess(script.index("CADDY_SITE_EOF"), script.index("__PORTAL_HASH__|$"))
        host = FakeHost()
        verify(cfg, host)  # type: ignore[arg-type]
        bash_check(host.script)

    def test_official_docker_source(self) -> None:
        cfg = full_config(docker_source="official")
        script = self._install_script(cfg)
        bash_check(script)
        self.assertIn("download.docker.com", script)
        self.assertIn("docker-ce", script)

    def test_zram_and_tools_in_script(self) -> None:
        script = self._install_script(full_config())
        self.assertIn("zram-swap.service", script)
        self.assertIn("lazydocker", script)

    def test_zero_swap_skips_block(self) -> None:
        script = self._install_script(full_config(swap_size_mb=0))
        self.assertNotIn("своп-файл", script)

    def test_swap_size_used(self) -> None:
        script = self._install_script(full_config(swap_size_mb=1024))
        self.assertIn("count=1024", script)
        self.assertNotIn("count=512", script)

    def test_portal_html_lands_in_script(self) -> None:
        script = self._install_script(full_config())
        self.assertIn(installer.PORTAL_INDEX, script)
        self.assertIn("class=\"tile\"", script)
        self.assertIn("PORTAL_EOF", script)
        # страница не должна содержать строк, ломающих heredoc
        self.assertNotIn("PORTAL_EOF\n", script.split("PORTAL_EOF\n", 1)[0])

    def test_verify_only_script(self) -> None:
        host = FakeHost()
        verify(full_config(), host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn("портал плиток", host.script)
        self.assertNotIn("apt-get install", host.script)

    def test_beszel_oauth_guard(self) -> None:
        cfg = full_config(
            beszel_user_creation=False, beszel_disable_password_auth=True
        )
        with self.assertRaises(ValueError):
            self._install_script(cfg)

    def test_beszel_keeps_agent_key_if_not_in_config(self) -> None:
        # повторный прогон без ключей не должен затереть уже рабочий агент
        script = self._install_script(
            full_config(beszel_agent_key="", beszel_agent_token="")
        )
        self.assertIn("__BESZEL_KEY_KEEP__", script)
        self.assertIn("__BESZEL_TOKEN_KEEP__", script)
        self.assertIn('sed -i "s|__BESZEL_KEY_KEEP__|${BESZEL_OLD_KEY}|"', script)
        # прежнее значение читается ДО перезаписи compose, иначе сохранять нечего
        self.assertLess(
            script.index("BESZEL_OLD_KEY="),
            script.index(f"tee {installer.BESZEL_COMPOSE}"),
        )
        bash_check(script)

    def test_key_warning_follows_compose_not_config(self) -> None:
        # ключ мог быть сохранён с хоста: предупреждение обязано смотреть в
        # записанный compose, иначе агент ругается «не задан ключ» при
        # полностью рабочей связке с хабом
        script = self._install_script(
            full_config(beszel_agent_key="", beszel_agent_token="")
        )
        bash_check(script)
        self.assertIn("""grep -qE '^      KEY: ""$'""", script)
        self.assertIn("""grep -qE '^      TOKEN: ""$'""", script)
        # в блоке beszel больше нет проверки «пусто ли значение в конфиге»
        beszel = script[script.index("BESZEL_EOF") : script.index("docker compose -f")]
        self.assertNotIn("if [ -z ", beszel)
        # с заданным ключом в конфиге предупреждение не появится: в compose
        # будет непустое значение
        with_key = self._install_script(
            full_config(beszel_agent_key="ssh-ed25519 AAAA", beszel_agent_token="t0ken")
        )
        bash_check(with_key)
        self.assertIn('KEY: "ssh-ed25519 AAAA"', with_key)
        self.assertIn('TOKEN: "t0ken"', with_key)

    def test_beszel_key_read_survives_missing_compose(self) -> None:
        # регрессия (найдено на чистом Ubuntu 26.04): compose на пустом хосте
        # ещё нет, sed по нему падает с кодом 2, а pipefail + set -e роняют
        # всю установку. Чтение обязано быть под [ -f ]
        script = self._install_script(
            full_config(beszel_agent_key="", beszel_agent_token="")
        )
        self.assertIn(f"if [ -f {installer.BESZEL_COMPOSE} ]; then", script)
        # ...и это чтение обязано реально переживать отсутствие файла
        read = next(
            block
            for block in script.split("if [ -f ")
            if block.startswith(installer.BESZEL_COMPOSE)
        )
        out = subprocess.run(
            ["bash", "-c", f'set -euo pipefail\nif [ -f {read}'],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("No such file", out.stderr)

    def test_beszel_key_in_config_written_as_is(self) -> None:
        script = self._install_script(full_config())
        self.assertNotIn("__BESZEL_", script)
        self.assertIn('KEY: "ssh-ed25519 AAAA"', script)

    def test_bad_email_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._install_script(full_config(caddy_email="не-почта"))

    def test_bad_site_rejected_before_script(self) -> None:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            with self.assertRaises(ValueError):
                install(full_config(caddy_sites=["rm -rf /=x"]), host)  # type: ignore[arg-type]
        self.assertEqual(host.script, "", "скрипт ушёл на хост до проверки конфига")


class HostBindTests(unittest.TestCase):
    """Публикация портов: наружу торчит только то, что не проксирует Caddy."""

    def test_hub_bound_to_localhost_with_caddy(self) -> None:
        script = installer._beszel_script(
            full_config(beszel_mode="hub", beszel_port=8090)
        )
        bash_check(script)
        self.assertIn('- "127.0.0.1:8090:8090"', script)
        self.assertNotIn('- "8090:8090"', script)
        self.assertIn("наружу его отдаёт Caddy", script)

    def test_hub_bound_everywhere_without_caddy(self) -> None:
        script = installer._beszel_script(
            full_config(beszel_mode="hub", caddy=False, beszel_port=8090)
        )
        bash_check(script)
        self.assertIn('- "0.0.0.0:8090:8090"', script)
        self.assertIn("на всех интерфейсах", script)

    def test_agent_stack_has_no_hub_port(self) -> None:
        script = installer._beszel_script(full_config(beszel_mode="agent"))
        bash_check(script)
        self.assertNotIn(":8090:8090", script)


class ImageVersionTests(unittest.TestCase):
    """Дрейф версий: работающий образ сверяется с version из конфига."""

    def test_beszel_checks_both_images(self) -> None:
        script = installer._beszel_script(
            full_config(beszel_mode="hub", beszel_version="0.20.0")
        )
        bash_check(script)
        self.assertIn("image: henrygd/beszel:0.20.0", script)
        self.assertIn("в конфиге henrygd/beszel:0.20.0", script)
        self.assertIn("в конфиге henrygd/beszel-agent:0.20.0", script)
        self.assertIn("docker inspect -f", script)

    def test_agent_checks_only_agent_image(self) -> None:
        script = installer._beszel_script(full_config(beszel_mode="agent"))
        bash_check(script)
        self.assertIn("в конфиге henrygd/beszel-agent:0.20.0", script)
        self.assertNotIn("в конфиге henrygd/beszel:0.20.0", script)

    def test_portainer_checks_server_and_agent(self) -> None:
        script = installer._portainer_script(
            full_config(portainer=True, portainer_version="2.45.1")
        )
        bash_check(script)
        self.assertIn("в конфиге portainer/portainer-ce:2.45.1", script)
        self.assertIn("в конфиге portainer/agent:2.45.1", script)

    def test_portainer_agent_checks_only_agent_image(self) -> None:
        script = installer._portainer_script(
            full_config(portainer=True, portainer_mode="agent")
        )
        bash_check(script)
        self.assertIn("в конфиге portainer/agent:2.45.1", script)
        self.assertNotIn("portainer-ce", script)

    def test_missing_container_does_not_abort(self) -> None:
        # у сервера Portainer контейнера portainer-agent нет: docker inspect
        # вернёт непустой код, и на `set -e` весь скрипт упал бы на ровном месте
        snippet = installer._image_check("no-such-container-xyz", "img:1.2.3")
        script = "set -euo pipefail\nSUDO=\n" + snippet + 'echo "СКРИПТ ДОШЁЛ ДО КОНЦА"\n'
        proc = subprocess.run([BASH, "-c", script], text=True, capture_output=True, check=False)
        self.assertIn("СКРИПТ ДОШЁЛ ДО КОНЦА", proc.stdout, msg=proc.stderr)
        self.assertNotIn("ПРОБЛЕМА", proc.stdout)

    def test_verify_only_keeps_image_check(self) -> None:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            verify(
                full_config(portainer=True, portainer_domain="portainer.example.com"),
                host,  # type: ignore[arg-type]
            )
        bash_check(host.script)
        self.assertIn("в конфиге henrygd/beszel:0.20.0", host.script)
        self.assertIn("в конфиге portainer/portainer-ce:2.45.1", host.script)


class SiteProbeTests(unittest.TestCase):
    """caddy validate ловит синтаксис, но не «каталог пуст» и не сертификат."""

    def _cfg(self, **overrides) -> Config:
        kwargs = {
            "caddy_sites": [
                "etc.example.com=file:/var/www/etc|browse",
                "shop.example.com=http://127.0.0.1:3000",
            ]
        }
        kwargs.update(overrides)
        return full_config(**kwargs)

    def test_probe_every_site_over_resolve(self) -> None:
        script = installer._caddy_site_check_script(self._cfg())
        bash_check("check_site() { :; }\n" + script)
        self.assertIn('--resolve "$domain:443:127.0.0.1"', script)
        self.assertIn('check_site "etc.example.com" static', script)
        self.assertIn('check_site "shop.example.com" proxy', script)
        # хвост опций не должен попасть в пробу
        self.assertNotIn("browse", script.split('check_site "etc.example.com"')[1])

    def test_static_404_mentions_index(self) -> None:
        script = installer._caddy_site_check_script(self._cfg())
        self.assertIn("каталог пуст, нет index-файла и не задан листинг", script)
        self.assertIn("проверьте upstream", script)
        # нулевой код = TLS/caddy, а не 404
        self.assertIn('000|"")', script)

    def test_static_404_is_problem_not_warn(self) -> None:
        # 404 у статики значит, что пользователь не видит сайт вообще:
        # раньше такая поломка проходила как WARN в конце лога
        script = installer._caddy_site_check_script(self._cfg())
        static_branch = script.split('404) if [ "$kind" = static ]; then')[1].split("else")[0]
        self.assertIn("ПРОБЛЕМА", static_branch)
        self.assertNotIn("WARN", static_branch)
        # у прокси 404 остаётся WARN: там дело не в файлах
        proxy_branch = script.split("else")[1].split("fi ;;")[0]
        self.assertIn("WARN", proxy_branch)

    def test_browse_path_probed_separately(self) -> None:
        # корень может быть без index-файла, а листинг настроен на
        # подкаталог: без отдельной пробы 404 на /install/ не заметить
        cfg = self._cfg(caddy_sites=["www.example.com=file:/var/www|browse=/install/"])
        script = installer._caddy_site_check_script(cfg)
        bash_check("check_site() { :; }\ncheck_browse() { :; }\n" + script)
        self.assertIn('check_browse "www.example.com" "/install/"', script)
        self.assertIn("ПРОБЛЕМА", script)

    def test_browse_probe_ignores_plain_browse(self) -> None:
        # у сайта с обычным browse путей нет — вызов пробы не нужен
        script = installer._caddy_site_check_script(self._cfg())
        self.assertNotIn('check_browse "', script)

    def test_browse_probe_skipped_for_proxy(self) -> None:
        # у прокси путей из browse не бывает: опции запрещены ещё в _validate
        cfg = self._cfg(caddy_sites=["api.example.com=https://backend:8443"])
        self.assertNotIn('check_browse "', installer._caddy_site_check_script(cfg))

    def test_vcs_junk_warned_in_served_tree(self) -> None:
        # hide .svn скрывает мусор от клиента, но он лежит на диске
        script = installer._static_junk_check(self._cfg())
        bash_check(script)
        self.assertIn("-name .svn -o -name .git", script)
        self.assertIn('for root in "/var/www/etc"', script)
        # проверка ничего не удаляет — молчаливое rm -r в verify недопустимо
        self.assertNotIn("rm ", script)
        self.assertNotIn("rmdir", script)

    def test_must_serve_probed_by_code(self) -> None:
        # проба корня ничего не говорит о содержимом: сломанный lua держался
        # месяцами, пока на него не пожаловался клиент
        cfg = full_config(caddy_must_serve=["etc.example.com=/flylinkdc-search-engine.lua"])
        script = installer._must_serve_check_script(cfg)
        bash_check("check_must_serve() { :; }\n" + script)
        self.assertIn('check_must_serve "etc.example.com" "/flylinkdc-search-engine.lua"', script)
        self.assertIn('ПРОБЛЕМА', script)
        self.assertIn('--resolve "$domain:443:127.0.0.1"', script)

    def test_must_serve_empty_by_default(self) -> None:
        self.assertEqual(installer._must_serve_check_script(full_config()), "")
        self.assertEqual(
            installer._must_serve_check_script(full_config(caddy=False, caddy_must_serve=["a.com=/x"])),
            "",
        )

    def test_must_serve_bad_value_rejected(self) -> None:
        for raw in (
            "etc.example.com",             # без пути
            "etc.example.com=",            # пустой путь
            "=/x",                         # без домена
            "etc.example.com=x",           # путь без ведущего слэша
            "не домен=/x",                 # пробел в домене
        ):
            with self.assertRaises(ValueError):
                installer._must_serve_check_script(full_config(caddy_must_serve=[raw]))

    def test_must_serve_rejected_before_ssh(self) -> None:
        # ошибка конфига не должна стоить подключения к хосту
        host = FakeHost()
        with self.assertRaises(ValueError):
            verify(full_config(caddy_must_serve=["etc.example.com=x"]), host)  # type: ignore[arg-type]
        self.assertEqual(host.script, "")

    def test_must_serve_in_verify_script(self) -> None:
        host = FakeHost()
        cfg = full_config(caddy_must_serve=["etc.example.com=/a.lua"])
        with mock.patch.object(installer, "_codename", return_value="noble"):
            verify(cfg, host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn('check_must_serve "etc.example.com" "/a.lua"', host.script)

    def test_no_junk_check_without_static_sites(self) -> None:
        cfg = self._cfg(caddy_sites=["shop.example.com=http://127.0.0.1:3000"])
        self.assertEqual(installer._static_junk_check(cfg), "")

    def test_no_probe_without_caddy(self) -> None:
        self.assertEqual(installer._caddy_site_check_script(self._cfg(caddy=False)), "")
        self.assertEqual(installer._static_junk_check(self._cfg(caddy=False)), "")

    def test_probe_in_verify_script(self) -> None:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            verify(self._cfg(), host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn('check_site "etc.example.com" static', host.script)
        self.assertIn("служебные каталоги VCS", host.script)

    def test_no_probe_for_tls_based_proxy(self) -> None:
        cfg = self._cfg(caddy_sites=["api.example.com=https://backend:8443"])
        self.assertIn('check_site "api.example.com" proxy', installer._caddy_site_check_script(cfg))


class AptHoldTests(unittest.TestCase):
    """apt-mark hold: версии docker не должны уезжать major-обновлением."""

    def test_hold_script_lists_versions(self) -> None:
        script = installer.APT_HOLD.format(packages="docker-ce containerd.io")
        bash_check(script)
        self.assertIn('apt-mark hold "$pkg"', script)
        self.assertIn("docker-ce containerd.io", script)
        # версия печатается: hold без версии в журнале бесполезен
        self.assertIn("dpkg-query -W", script)
        self.assertIn("hold: $pkg ->", script)

    def test_absent_package_is_warn_not_failure(self) -> None:
        # set -e: hold несуществующего пакета не должен валить прогон
        script = installer.APT_HOLD.format(packages="docker-ce")
        self.assertIn("WARN:", script)
        self.assertIn("dpkg -s", script)

    def test_no_hold_script_by_default(self) -> None:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            install(full_config(), host)  # type: ignore[arg-type]
        self.assertNotIn("apt-mark hold", host.script)

    def test_hold_in_install_script(self) -> None:
        host = FakeHost()
        cfg = full_config(apt_hold_packages=["docker-ce", "containerd.io"])
        with mock.patch.object(installer, "_codename", return_value="noble"):
            install(cfg, host)  # type: ignore[arg-type]
        bash_check(host.script)
        self.assertIn("apt-mark hold", host.script)
        self.assertIn("docker-ce containerd.io", host.script)

    def test_hold_parsed_from_cli(self) -> None:
        args = _parser().parse_args(["--apt-hold", "docker-ce", "--apt-hold", "containerd.io"])
        cfg = apply_overrides(Config(), args)
        self.assertEqual(cfg.apt_hold_packages, ["docker-ce", "containerd.io"])

    def test_hold_survives_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('apt_hold_packages = ["docker-ce"]\n', encoding="utf-8")
            self.assertEqual(load_config(path).apt_hold_packages, ["docker-ce"])


class DockerVersionTests(unittest.TestCase):
    """Версии docker/compose должны быть видны в verify, а не прятаться."""

    def _verify_script(self, cfg) -> str:
        host = FakeHost()
        with mock.patch.object(installer, "_codename", return_value="noble"):
            verify(cfg, host)  # type: ignore[arg-type]
        bash_check(host.script)
        return host.script

    def test_versions_reported(self) -> None:
        script = self._verify_script(full_config())
        self.assertIn("версии: docker", script)
        self.assertIn("docker compose version", script)
        # hold-статус рядом: без него непонятно, что удерживается от апгрейда
        self.assertIn("apt-mark showhold", script)

    def test_verify_survives_hosts_without_docker_hold(self) -> None:
        # хост без apt-mark hold для docker: grep не находит ничего и отдаёт 1,
        # а под set -euo pipefail это уронило бы весь verify (поймано на боевых
        # хостах: ai/dc/vpn падали с кодом 1 без единой строки ПРОБЛЕМА)
        script = self._verify_script(full_config())
        self.assertIn("|| true)", script.split("apt-mark showhold")[1][:200])

    def test_verify_script_runs_on_host_without_hold(self) -> None:
        # настоящая проверка: фрагмент должен отработать на хосте без hold
        proc = subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + installer._docker_versions_check(full_config())],
            text=True, capture_output=True, check=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/tmp"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_versions_reported_for_official_source(self) -> None:
        # пакетов docker в cfg нет, но source=official его ставит — версии
        # всё равно нужно показывать
        script = self._verify_script(full_config(packages=["htop"], docker_source="official"))
        self.assertIn("версии: docker", script)

    def test_no_versions_without_docker(self) -> None:
        self.assertEqual(
            installer._docker_versions_check(full_config(packages=["htop", "git"])), ""
        )

    def test_hold_listed_in_report(self) -> None:
        host = FakeHost()
        cfg = full_config(apt_hold_packages=["docker-ce"])
        with mock.patch.object(installer, "_codename", return_value="noble"):
            install(cfg, host)  # type: ignore[arg-type]
        self.assertIn("apt-mark showhold", host.script)


class LighttpdAuditTests(unittest.TestCase):
    """Аудит миграции lighttpd -> Caddy: потерянные правила должны быть видны.

    На dc.fly-server.ru из lighttpd не переехали `url.access-deny` и
    `static-file.exclude-extensions`: лежащий в дереве `upload.php` отдавался
    как 200, пока это не заметил пользователь. Регрессия такого рода больше
    не должна проходить молча — это и проверяет аудит.
    """

    # Реальный /etc/lighttpd/lighttpd.conf с dc, сокращённый до значимого
    LTPD = """
    server.document-root        = "/var/www"
    dir-listing.activate = "enable"
    $HTTP["url"] =~ "^/update($|/)" { server.dir-listing = "disable" }
    $HTTP["url"] =~ "^/etc($|/)" { server.dir-listing = "disable" }
    index-file.names := ( "index.php", "index.html", "index.lighttpd.html" )
    url.access-deny             = ( "~", ".inc" )
    static-file.exclude-extensions = ( ".php", ".pl", ".fcgi" )
    #url.access-deny = ( "" )
    """

    DENY = ["*.inc", "*.php", "*.pl", "*.fcgi"]

    def _sites(self, *specs: tuple[str, str, list[str]]) -> list:
        return [(domain, root, opts) for domain, root, opts in specs]

    def _rule(self, rules, name):
        found = [r for r in rules if r.name == name]
        self.assertEqual(len(found), 1, f"правило {name!r} ожидалось один раз")
        return found[0]

    def test_deny_lost_on_mirrors(self) -> None:
        # deny есть только на сайте корня, зеркала его не унаследовали
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
            ("etc.example.com", "/var/www/etc", {"browse": [], "index": ["index.html"]}),
        ))
        rule = self._rule(rules, "доступ к исполняемым файлам")
        self.assertEqual(rule.level, lighttpd.WARN)
        self.assertIn("etc.example.com", rule.hint)

    def test_deny_lost_everywhere(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"]}),
        ))
        self.assertEqual(self._rule(rules, "доступ к исполняемым файлам").level, lighttpd.LOST)

    def test_deny_covered(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
            ("etc.example.com", "/var/www/etc", {"browse": [], "deny": self.DENY}),
        ))
        self.assertEqual(self._rule(rules, "доступ к исполняемым файлам").level, lighttpd.OK)

    def test_deny_partial_extensions_flagged(self) -> None:
        # закрыт только .php — остальное lighttpd тоже не отдавал
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"deny": ["*.php"]}),
        ))
        self.assertEqual(self._rule(rules, "доступ к исполняемым файлам").level, lighttpd.LOST)

    def test_deny_dot_php_form_accepted(self) -> None:
        # '.php' в lighttpd и '*.php' в Caddy — одно правило, записанное иначе
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"deny": [".php", ".pl", ".fcgi", ".inc"]}),
        ))
        self.assertEqual(self._rule(rules, "доступ к исполняемым файлам").level, lighttpd.OK)

    def test_whole_site_browse_is_lost_rule(self) -> None:
        # lighttpd гасил листинг для /etc и /update, а не для сайта целиком
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": []}),
            ("etc.example.com", "/var/www/etc", {"browse": []}),
        ))
        rule = self._rule(rules, "точечное гашение листинга")
        self.assertEqual(rule.level, lighttpd.LOST)
        self.assertIn("browse=/", rule.hint)

    def test_scoped_browse_covers_disabled_paths(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"]}),
            ("etc.example.com", "/var/www/etc", {"browse": []}),
        ))
        # зеркала со своим листингом — не потеря: пути стали отдельными доменами
        self.assertEqual(self._rule(rules, "точечное гашение листинга").level, lighttpd.OK)

    def test_index_default_needs_no_index_option(self) -> None:
        # file_server сам ищет index.html, поэтому index= не обязателен
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
        ))
        rule = self._rule(rules, "индексные файлы")
        self.assertEqual(rule.level, lighttpd.OK)
        self.assertIn("index.php", rule.what)

    def test_index_missing_is_warn(self) -> None:
        rules = lighttpd.audit(self.LTPD.replace('"index.html"', '"home.html"'), self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
        ))
        self.assertEqual(self._rule(rules, "индексные файлы").level, lighttpd.WARN)

    def test_commented_rule_not_counted(self) -> None:
        # закомментированное правило никогда не действовало: ругаться не на что
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
        ))
        self.assertNotIn("spam", " ".join(r.what for r in rules))

    def test_no_static_sites_is_warn_not_crash(self) -> None:
        rules = lighttpd.audit(self.LTPD, [("api.example.com", "", {})])
        self.assertEqual(self._rule(rules, "доступ к исполняемым файлам").level, lighttpd.WARN)
        self.assertEqual(self._rule(rules, "листинг каталогов").level, lighttpd.WARN)

    def test_listing_lost(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"deny": self.DENY}),
        ))
        self.assertEqual(self._rule(rules, "листинг каталогов").level, lighttpd.LOST)

    def test_report_marks_and_summary(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": []}),
            ("etc.example.com", "/var/www/etc", {"browse": []}),
        ))
        text = lighttpd.report(rules)
        self.assertIn("ПОТЕРЯНО:", text)
        self.assertIn("OK:", text)
        self.assertIn("ИТОГО: потеряно правил", text)
        self.assertIn("аудит lighttpd -> Caddy:", text)

    def test_report_warn_summary(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
            ("etc.example.com", "/var/www/etc", {"browse": []}),
        ))
        text = lighttpd.report(rules)
        self.assertIn("WARN:", text)
        self.assertNotIn("ПОТЕРЯНО:", text)
        self.assertIn("ничего не потеряно", text)

    def test_report_clean_summary(self) -> None:
        rules = lighttpd.audit(self.LTPD, self._sites(
            ("www.example.com", "/var/www", {"browse": ["/install/"], "deny": self.DENY}),
            ("etc.example.com", "/var/www/etc", {"browse": [], "deny": self.DENY}),
        ))
        text = lighttpd.report(rules)
        self.assertIn("все найденные правила перенесены", text)


if __name__ == "__main__":
    unittest.main()
