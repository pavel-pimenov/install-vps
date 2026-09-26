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

from install_vps import installer  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
