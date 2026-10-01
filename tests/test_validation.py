from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install_vps import cli, installer, lighttpd  # noqa: E402
from install_vps import ssh as installer_ssh  # noqa: E402
from install_vps.cli import _parser  # noqa: E402
from install_vps.config import Config, apply_overrides  # noqa: E402
from install_vps.installer import (  # noqa: E402
    _GITHUB_TOOLS,
    _beszel_auth_env,
    _beszel_auth_warning,
    _beszel_key_keep,
    _beszel_script,
    _caddy_beszel_block,
    _caddy_sites_script,
    _caddy_token,
    _dozzle_script,
    _external_url,
    _parse_tile,
    _portainer_edge_keep,
    _portainer_script,
    _portal_html,
    _portal_script,
    _portal_verify_script,
    _split_site,
    _tiles,
    _validate,
    _validate_modes,
)
from install_vps.ssh import RemoteHost, SSHCommandError  # noqa: E402

BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"


def bash_check(script: str) -> None:
    proc = subprocess.run([BASH, "-n"], input=script, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"bash -n не прошёл:\n{proc.stderr}\n---\n{script}")


class FakeHost:
    """Заглушка RemoteHost: base_cmd/target для теста _codename."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def base_cmd(self) -> list[str]:
        return ["ssh", "-p", "22", "-i", "/tmp/key", "-o", "BatchMode=yes"]

    def target(self) -> str:
        return "root@example.com"

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "24.04 noble\n", "")


class CodenameTests(unittest.TestCase):
    """VERSION_CODENAME берётся с хоста: неверный дистрибутив должен падать."""

    def _codename(self, stdout: str, returncode: int = 0, stderr: str = ""):
        host = FakeHost()
        proc = subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)
        with mock.patch.object(installer.subprocess, "run", return_value=proc):
            return installer._codename(Config(), host)

    def test_returns_codename(self) -> None:
        self.assertEqual(self._codename("24.04 noble\n"), "noble")
        self.assertEqual(self._codename("26.04 resolute\n"), "resolute")
        self.assertEqual(self._codename("24.04.3 noble\n"), "noble")

    def test_ssh_command_sources_os_release(self) -> None:
        host = FakeHost()
        proc = subprocess.CompletedProcess(["ssh"], 0, "24.04 noble\n", "")
        with mock.patch.object(installer.subprocess, "run", return_value=proc) as run:
            installer._codename(Config(), host)
        cmd = run.call_args[0][0]
        self.assertIn("root@example.com", cmd)
        self.assertIn(". /etc/os-release", cmd[-1])
        self.assertIn("VERSION_CODENAME", cmd[-1])

    def test_ssh_failure_raises_runtime_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Не удалось определить дистрибутив"):
            self._codename("", returncode=255, stderr="Permission denied")

    def test_unexpected_output_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Неожиданный ответ os-release"):
            self._codename("noble\n")

    def test_unsupported_ubuntu_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._codename("22.04 jammy\n")
        self.assertIn("22.04", str(ctx.exception))

    def test_debian_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self._codename("12 bookworm\n")


class QuotingTests(unittest.TestCase):
    """Значения попадают в Caddyfile/YAML/bash — экранирование обязательно."""

    def test_plain_token_unquoted(self) -> None:
        self.assertEqual(_caddy_token("/install/"), "/install/")
        self.assertEqual(_caddy_token("index.html"), "index.html")

    def test_token_with_specials_is_quoted(self) -> None:
        self.assertEqual(_caddy_token("a b"), '"a b"')
        self.assertEqual(_caddy_token('a"b'), '"a\\"b"')
        self.assertEqual(_caddy_token("a\\b"), '"a\\\\b"')
        self.assertEqual(_caddy_token("{root}"), '"{root}"')
        self.assertEqual(_caddy_token(""), '""')

    def test_split_site_requires_equals(self) -> None:
        with self.assertRaisesRegex(ValueError, "ДОМЕН=UPSTREAM"):
            _split_site("shop.example.com")

    def test_split_site_options_tail(self) -> None:
        self.assertEqual(
            _split_site("d.example.com=file:/var/www|browse=/install/|deny=*.inc"),
            ("d.example.com", "file:/var/www", "browse=/install/|deny=*.inc"),
        )

    def test_external_url_rejects_spaces(self) -> None:
        with self.assertRaisesRegex(ValueError, "Пробелы в адресе плитки"):
            _external_url("https://ex ample.com/x")

    def test_external_url_rejects_non_http(self) -> None:
        for raw in ("mailto:a@b.c", "javascript:alert(1)", "//host/x", "ftp://h/x"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _external_url(raw)

    def test_external_url_rejects_bad_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "Некорректный хост"):
            _external_url("https://-bad-.example.com/x")

    def test_external_url_accepts_port(self) -> None:
        self.assertEqual(_external_url("https://h.example.com:8443/x"),
                         "https://h.example.com:8443/x")


class PortalGuardTests(unittest.TestCase):
    """Заголовок портала попадает в HTML — опасные символы не пропускаем."""

    def test_title_is_escaped_ok(self) -> None:
        cfg = Config(caddy_portal_title="  Мои сервисы  ")
        html = _portal_html(cfg, [])
        self.assertIn("Мои сервисы", html)

    def test_title_default_when_blank(self) -> None:
        cfg = Config(caddy_portal_title="   ")
        self.assertIn("Сервисы", _portal_html(cfg, []))

    def test_title_rejects_markup_and_newline(self) -> None:
        for bad in ("<script>", "a\nb", "a\rb"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "заголовок"):
                _portal_html(Config(caddy_portal_title=bad), [])

    def test_verify_script_without_tiles(self) -> None:
        cfg = Config(caddy_portal="portal.example.com")
        script = _portal_verify_script(cfg, [])
        self.assertIn("плиток нет", script)
        self.assertNotIn("curl", script)


class BeszelAuthEnvTests(unittest.TestCase):
    """OAuth-флаги попадают в compose Hub и ломают вход, если неверны."""

    def test_empty_without_flags(self) -> None:
        self.assertEqual(_beszel_auth_env(Config()), "")

    def test_user_creation_only(self) -> None:
        env = _beszel_auth_env(Config(beszel_user_creation=True))
        self.assertIn('USER_CREATION: "true"', env)
        self.assertNotIn("DISABLE_PASSWORD_AUTH", env)

    def test_disable_password_auth_only(self) -> None:
        env = _beszel_auth_env(Config(beszel_disable_password_auth=True))
        self.assertIn('DISABLE_PASSWORD_AUTH: "true"', env)
        self.assertNotIn("USER_CREATION", env)

    def test_both_flags(self) -> None:
        env = _beszel_auth_env(
            Config(beszel_user_creation=True, beszel_disable_password_auth=True)
        )
        self.assertIn("USER_CREATION", env)
        self.assertIn("DISABLE_PASSWORD_AUTH", env)
        self.assertTrue(env.startswith("\n"))

    def test_warning_only_when_password_auth_disabled(self) -> None:
        self.assertEqual(_beszel_auth_warning(Config()), "")

    def test_warning_mentions_domain(self) -> None:
        warn = _beszel_auth_warning(
            Config(beszel_disable_password_auth=True, caddy_domain="mon.example.com")
        )
        self.assertIn("mon.example.com", warn)
        self.assertIn("OAuth", warn)

    def test_warning_without_domain_placeholder(self) -> None:
        warn = _beszel_auth_warning(Config(beszel_disable_password_auth=True))
        self.assertIn("<домен Beszel>", warn)

    def test_beszel_block_proxies_to_port(self) -> None:
        cfg = Config(caddy_domain="mon.example.com")
        block = _caddy_beszel_block(cfg, "mon.example.com", 8090)
        self.assertIn("reverse_proxy 127.0.0.1:8090", block)
        self.assertTrue(block.startswith("mon.example.com {"))

    def test_beszel_path_lands_in_portal_url(self) -> None:
        cfg = Config(beszel=True, caddy=True, caddy_portal="p.example.com",
                     beszel_path="/mon")
        tiles = _tiles(cfg)
        mon = [t for t in tiles if t.beszel]
        self.assertEqual(len(mon), 1, [t.name for t in tiles])
        self.assertEqual(mon[0].path, "/mon")
        self.assertEqual(mon[0].href, "https://p.example.com/mon/")

    def test_beszel_tile_absent_when_domain_is_separate(self) -> None:
        """Свой домен у Beszel — плитка в портале не нужна."""
        cfg = Config(beszel=True, caddy=True, caddy_portal="p.example.com",
                     caddy_domain="mon.example.com")
        self.assertEqual([t for t in _tiles(cfg) if t.beszel], [])


class ValidateDomainTests(unittest.TestCase):
    """Домены/пути проверяются до SSH: опечатка не должна оставить хост наполовину."""

    def test_bad_portainer_domain(self) -> None:
        cfg = Config(portainer=True, portainer_mode="server",
                     portainer_domain="not a domain", caddy=True)
        with self.assertRaisesRegex(ValueError, "portainer_domain"):
            _validate_modes(cfg)

    def test_bad_beszel_agent_allow(self) -> None:
        for bad in ("185.50.202.219/33", "999.999.999.999", "12:34", "не адрес"):
            with self.subTest(cidr=bad), self.assertRaisesRegex(ValueError, "beszel_agent_allow"):
                _validate_modes(Config(beszel_agent_allow=[bad]))

    def test_good_beszel_agent_allow(self) -> None:
        for good in ("185.50.202.219", "185.50.202.219/32", "::1", "2001:db8::/32"):
            with self.subTest(cidr=good):
                _validate_modes(Config(beszel_agent_allow=[good]))

    def test_bad_beszel_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "beszel_version"):
            _validate_modes(Config(beszel_version="latest"))

    def test_unknown_portainer_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "portainer_mode"):
            _validate_modes(Config(portainer=True, portainer_mode="nope"))

    def test_unknown_beszel_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "beszel_mode"):
            _validate_modes(Config(beszel=True, beszel_mode="nope"))

    def test_bad_beszel_path(self) -> None:
        cfg = Config(beszel=True, caddy=True, caddy_portal="p.example.com",
                     beszel_path="mon itor")
        with self.assertRaisesRegex(ValueError, "beszel_path"):
            _validate(cfg)

    def test_bad_dozzle_path(self) -> None:
        cfg = Config(dozzle=True, caddy=True, caddy_portal="p.example.com",
                     dozzle_path="lo gs")
        with self.assertRaisesRegex(ValueError, "dozzle_path"):
            _validate(cfg)

    def test_beszel_path_without_leading_slash_is_ok(self) -> None:
        """Путь нормализуется сам — «monitor» не ошибка, а то же /monitor."""
        cfg = Config(beszel=True, caddy=True, caddy_portal="p.example.com",
                     beszel_path="monitor")
        _validate(cfg)

    def test_bad_portal_domain(self) -> None:
        cfg = Config(caddy=True, caddy_portal="portal example.com")
        with self.assertRaisesRegex(ValueError, "домен портала"):
            _validate(cfg)

    def test_dozzle_requires_portal(self) -> None:
        with self.assertRaisesRegex(ValueError, "caddy_portal"):
            _validate(Config(dozzle=True))

    def test_unknown_tool(self) -> None:
        with self.assertRaisesRegex(ValueError, "Неизвестная утилита"):
            _validate(Config(caddy=True, tools=["nope"]))

    def test_static_options_only_for_static_site(self) -> None:
        cfg = Config(caddy=True, caddy_sites=["d.example.com=http://127.0.0.1:80|browse"])
        with self.assertRaisesRegex(ValueError, "только для сайта-статики"):
            _validate(cfg)

    def test_bad_upstream(self) -> None:
        cfg = Config(caddy=True, caddy_sites=["d.example.com=не апстрим"])
        with self.assertRaisesRegex(ValueError, "upstream"):
            _validate(cfg)


class SecretInjectionTests(unittest.TestCase):
    """Значения из UI копируются целиком: кавычка ломает и compose, и файл."""

    def _dozzle(self, **overrides) -> Config:
        cfg = Config(dozzle=True, caddy=True, caddy_portal="p.example.com")
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_dozzle_bad_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "dozzle_port"):
            _dozzle_script(self._dozzle(dozzle_port=99999))

    def test_dozzle_bad_user(self) -> None:
        for bad in ("a/b", 'a"b', "a b", "a$b"):
            with self.subTest(user=bad), self.assertRaisesRegex(ValueError, "пользователя"):
                _dozzle_script(self._dozzle(dozzle_user=bad))

    def test_dozzle_password_with_quote(self) -> None:
        with self.assertRaisesRegex(ValueError, "dozzle_password"):
            _dozzle_script(self._dozzle(dozzle_password="pa'ss"))

    def test_beszel_key_with_quote(self) -> None:
        for key in ("beszel_agent_key", "beszel_agent_token"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                _beszel_script(Config(beszel=True, **{key: 'va"lue'}))

    def test_portainer_edge_key_with_quote(self) -> None:
        for key in ("portainer_agent_edge_id", "portainer_agent_edge_key"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                _portainer_script(Config(portainer=True, **{key: 'va"lue'}))

    def test_portainer_admin_password_with_quote(self) -> None:
        cfg = Config(portainer=True, portainer_admin_password="pa\"ss")
        with self.assertRaisesRegex(ValueError, "portainer_admin_password"):
            _portainer_script(cfg)


class SudoOverrideTests(unittest.TestCase):
    """--sudo обязан поднимать флаг: иначе не-root хост падает на sudo -n."""

    def test_sudo_flag_sets_config(self) -> None:
        args = _parser().parse_args(["example.com", "--sudo"])
        self.assertTrue(apply_overrides(Config(), args).sudo)

    def test_sudo_stays_true_without_flag(self) -> None:
        """--sudo только поднимает флаг; из конфига он не сбрасывается."""
        args = _parser().parse_args(["example.com"])
        self.assertTrue(apply_overrides(Config(sudo=True), args).sudo)

    def test_key_path_is_expanded(self) -> None:
        args = _parser().parse_args(["example.com"])
        cfg = apply_overrides(Config(), args)
        self.assertFalse(cfg.key_path.startswith("~"))


class UnderRootTests(unittest.TestCase):
    """Сайт внутри дерева lighttpd наследует его правила."""

    def test_empty_doc_root_matches_everything(self) -> None:
        self.assertTrue(lighttpd._under_root("/var/www", ""))

    def test_exact_root(self) -> None:
        self.assertTrue(lighttpd._under_root("/var/www", "/var/www"))

    def test_nested_root(self) -> None:
        self.assertTrue(lighttpd._under_root("/var/www/ftp", "/var/www"))

    def test_sibling_prefix_is_not_under_root(self) -> None:
        self.assertFalse(lighttpd._under_root("/var/wwwsite", "/var/www"))

    def test_parent_root_is_not_under_root(self) -> None:
        self.assertFalse(lighttpd._under_root("/var", "/var/www"))


class SitesScriptTests(unittest.TestCase):
    """Сборка файлов sites/*.caddy: автосайты и предупреждения."""

    def test_beszel_domain_becomes_site(self) -> None:
        cfg = Config(beszel=True, caddy=True, caddy_domain="mon.example.com")
        script = _caddy_sites_script(cfg)
        self.assertIn("mon.example.com", script)
        self.assertIn("reverse_proxy 127.0.0.1:8090", script)

    def test_no_domains_warns(self) -> None:
        script = _caddy_sites_script(Config(caddy=True))
        self.assertIn("не задан ни одного домена", script)

    def test_no_domains_silent_when_portal_exists(self) -> None:
        cfg = Config(caddy=True, caddy_portal="p.example.com",
                     caddy_tiles=["X=/x=127.0.0.1:1"])
        script = _caddy_sites_script(cfg, portal=True)
        self.assertNotIn("не задан ни одного домена", script)

    def test_portainer_domain_becomes_site(self) -> None:
        cfg = Config(portainer=True, caddy=True, portainer_mode="server",
                     portainer_domain="pt.example.com")
        script = _caddy_sites_script(cfg)
        self.assertIn("pt.example.com", script)

    def test_script_is_bash_clean(self) -> None:
        cfg = Config(beszel=True, caddy=True, caddy_domain="mon.example.com",
                     caddy_sites=["d.example.com=http://127.0.0.1:8080"])
        bash_check(_caddy_sites_script(cfg))


class PortalScriptTests(unittest.TestCase):
    def test_portal_without_tiles_warns(self) -> None:
        script = _portal_script(Config(caddy_portal="p.example.com"), [])
        self.assertIn("плиток нет", script)

    def test_portaler_with_tile_writes_files(self) -> None:
        cfg = Config(caddy=True, caddy_portal="p.example.com")
        tiles = _tiles(cfg)
        tiles.insert(0, _parse_tile("X=/x=127.0.0.1:1", cfg.caddy_portal))
        script = _portal_script(cfg, tiles)
        self.assertIn("portal.caddy", script)
        bash_check(script)


class AutoTileTests(unittest.TestCase):
    """Автоплитки появляются только при нужной комбинации флагов."""

    def test_portainer_tile_is_external_link(self) -> None:
        cfg = Config(portainer=True, caddy=True, portainer_mode="server",
                     caddy_portal="p.example.com", portainer_domain="pt.example.com")
        tiles = _tiles(cfg)
        pt = [t for t in tiles if "pt.example.com" in t.href]
        self.assertEqual(len(pt), 1, [(t.name, t.href) for t in tiles])
        self.assertEqual(pt[0].href, "https://pt.example.com/")

    def test_no_portainer_tile_without_domain(self) -> None:
        cfg = Config(portainer=True, caddy=True, portainer_mode="server",
                     caddy_portal="p.example.com", portainer_domain="")
        self.assertEqual([t for t in _tiles(cfg) if "portainer" in t.name.lower()], [])

    def test_dozzle_tile_has_streaming(self) -> None:
        cfg = Config(dozzle=True, caddy=True, caddy_portal="p.example.com")
        tiles = _tiles(cfg)
        log = [t for t in tiles if t.streaming]
        self.assertEqual(len(log), 1, [(t.name, t.href) for t in tiles])
        self.assertEqual(log[0].path, "/logs")

    def test_dozzle_path_respected(self) -> None:
        cfg = Config(dozzle=True, caddy=True, caddy_portal="p.example.com",
                     dozzle_path="/journal")
        log = [t for t in _tiles(cfg) if t.streaming]
        self.assertEqual(log[0].href, "https://p.example.com/journal/")


class KeepFromComposeTests(unittest.TestCase):
    """Пустые ключи в конфиге не должны затирать уже работающий агент."""

    def test_beszel_both_given(self) -> None:
        reads, fixes = _beszel_key_keep("compose", "ssh-ed25519 AAAA", "tok")
        self.assertEqual((reads, fixes), ("", ""))

    def test_beszel_key_only(self) -> None:
        """Ключ задан, токен нет — прежний токен читаем и подставляем обратно."""
        reads, fixes = _beszel_key_keep("compose", "ssh-ed25519 AAAA", "")
        self.assertIn("TOKEN", reads)
        self.assertNotIn("BESZEL_OLD_KEY", reads)
        self.assertIn("__BESZEL_TOKEN_KEEP__", fixes)
        self.assertNotIn("__BESZEL_KEY_KEEP__", fixes)

    def test_beszel_token_only(self) -> None:
        reads, fixes = _beszel_key_keep("compose", "", "tok")
        self.assertIn("__BESZEL_KEY_KEEP__", fixes)
        self.assertNotIn("__BESZEL_TOKEN_KEEP__", fixes)

    def test_portainer_edge_both_given(self) -> None:
        reads, fixes = _portainer_edge_keep("compose", "id", "key")
        self.assertEqual((reads, fixes), ("", ""))


class GithubToolSpecTests(unittest.TestCase):
    """Закреплённые утилиты: версия и хеши обязаны быть полными."""

    def test_every_tool_has_hashes_for_every_arch(self) -> None:
        for name, spec in _GITHUB_TOOLS.items():
            with self.subTest(tool=name):
                self.assertEqual(set(spec["arch"]), set(spec["sha256"]), name)
                self.assertEqual(set(spec["arch"]), set(spec["bin_sha256"]), name)
                self.assertTrue(spec["version"], name)
                self.assertTrue(spec["binary"], name)

    def test_missing_binary_hash_skips_arch(self) -> None:
        """Неполная запись не должна молча ехать на хост без проверки хеша."""
        broken = {
            "version": "1.0.0",
            "binary": "tool",
            "url": "https://example.com/{version}",
            "archive": "tool-{version}-{arch}.tar.gz",
            "arch": {"x86_64": "linux-amd64"},
            "sha256": {"x86_64": "aa"},
            "bin_sha256": {},
        }
        with mock.patch.dict(installer._GITHUB_TOOLS, {"broken": broken}, clear=False):
            script = installer._tool_script("broken")
        # вместо ветки установки — явный пропуск: хеша нет, ставить нечего
        self.assertNotIn("x86_64)", script)
        self.assertIn("ПРОПУСК", script)
        bash_check(script)

    def test_complete_spec_emits_case(self) -> None:
        spec = {
            "version": "1.0.0",
            "binary": "tool",
            "url": "https://example.com/{version}",
            "archive": "tool-{version}-{arch}.tar.gz",
            "arch": {"x86_64": "linux-amd64"},
            "sha256": {"x86_64": "aa"},
            "bin_sha256": {"x86_64": "bb"},
        }
        with mock.patch.dict(installer._GITHUB_TOOLS, {"ok": spec}, clear=False):
            script = installer._tool_script("ok")
        self.assertIn("x86_64)", script)
        self.assertIn("tool-1.0.0-linux-amd64.tar.gz", script)
        bash_check(script)


class RemoteHostTests(unittest.TestCase):
    """Команда ssh собирается на Python: ключи и BatchMode важны для скрипта."""

    def _host(self, **overrides) -> RemoteHost:
        params = {"host": "203.0.113.5", "user": "ppa", "port": 2222,
                  "key_path": "/tmp/id_ed25519"}
        params.update(overrides)
        return RemoteHost(**params)

    def test_target(self) -> None:
        self.assertEqual(self._host().target(), "ppa@203.0.113.5")

    def test_base_cmd_has_port_key_batchmode(self) -> None:
        cmd = self._host().base_cmd()
        self.assertEqual(cmd[:5], ["ssh", "-p", "2222", "-i", "/tmp/id_ed25519"])
        self.assertIn("BatchMode=yes", cmd)

    def test_accept_new_default(self) -> None:
        self.assertIn("StrictHostKeyChecking=accept-new", self._host().base_cmd())

    def test_accept_new_disabled(self) -> None:
        cmd = self._host(accept_new=False).base_cmd()
        self.assertNotIn("StrictHostKeyChecking=accept-new", cmd)
        self.assertIn("BatchMode=yes", cmd)

    def test_check_ok(self) -> None:
        proc = subprocess.CompletedProcess(["ssh"], 0, "", "")
        with mock.patch.object(installer_ssh.subprocess, "run", return_value=proc) as run:
            self._host().check()
        self.assertEqual(run.call_args[0][0][-2:], ["ppa@203.0.113.5", "true"])

    def test_check_failure_raises_with_reason(self) -> None:
        proc = subprocess.CompletedProcess(["ssh"], 255, "", "Permission denied")
        with mock.patch.object(installer_ssh.subprocess, "run", return_value=proc):
            with self.assertRaises(SSHCommandError) as ctx:
                self._host().check()
        self.assertIn("Permission denied", str(ctx.exception))
        self.assertIn("ppa@203.0.113.5", str(ctx.exception))

    def test_run_script_streams_and_raises_on_rc(self) -> None:
        class FakeProc:
            def __init__(self, rc: int) -> None:
                self.rc = rc
                self.stdin = mock.Mock()
                self.stdout = iter(["строка 1\n", "строка 2\n"])

            def wait(self) -> int:
                return self.rc

        proc = FakeProc(0)
        out = io.StringIO()
        with mock.patch.object(installer_ssh.subprocess, "Popen", return_value=proc) as popen:
            with contextlib.redirect_stdout(out):
                self._host().run_script("echo привет\n")
        self.assertEqual(out.getvalue(), "строка 1\nстрока 2\n")
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[-3:], ["ppa@203.0.113.5", "bash", "-s"])
        # скрипт уходит через stdin, а не аргументом: кавычки не ломаются
        proc.stdin.write.assert_called_once_with("echo привет\n")
        proc.stdin.close.assert_called_once()

        proc = FakeProc(100)
        with mock.patch.object(installer_ssh.subprocess, "Popen", return_value=proc):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SSHCommandError, "кодом 100"):
                    self._host().run_script("boom")


class MainTests(unittest.TestCase):
    """main() переводит ошибки в код возврата: 0 успех, 1 сбой, 2 нет host.

    Каждому вызову подсовывается пустой конфиг: реальный `config.toml` в
    корне репозитория содержит боевой host, и без этого тест молча ушёл бы
    в SSH по сети.
    """

    def _run_argv(self, *extra: str) -> tuple[list[str], tempfile.TemporaryDirectory]:
        tmp = tempfile.TemporaryDirectory()
        empty = Path(tmp.name) / "config.toml"
        empty.write_text("", encoding="utf-8")
        return ["--config", str(empty), *extra], tmp

    def test_missing_host_returns_2(self) -> None:
        argv, tmp = self._run_argv()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(argv)
        finally:
            tmp.cleanup()
        self.assertEqual(code, 2)
        self.assertIn("укажите host", out.getvalue())

    def test_verify_only_calls_verify(self) -> None:
        argv, tmp = self._run_argv("example.com", "--verify-only")
        calls: list[str] = []
        try:
            with mock.patch.object(cli.RemoteHost, "check"), \
                 mock.patch.object(cli, "verify", side_effect=lambda c, h: calls.append("verify")), \
                 mock.patch.object(cli, "install", side_effect=lambda c, h: calls.append("install")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(argv)
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["verify"])

    def test_default_calls_install(self) -> None:
        argv, tmp = self._run_argv("example.com")
        calls: list[str] = []
        try:
            with mock.patch.object(cli.RemoteHost, "check"), \
                 mock.patch.object(cli, "verify", side_effect=lambda c, h: calls.append("verify")), \
                 mock.patch.object(cli, "install", side_effect=lambda c, h: calls.append("install")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(argv)
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["install"])

    def test_connect_failure_returns_1(self) -> None:
        argv, tmp = self._run_argv("example.com")
        try:
            with mock.patch.object(cli.RemoteHost, "check", side_effect=SSHCommandError("нет ключа")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(argv)
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("Ошибка подключения", out.getvalue())

    def test_install_failure_returns_1(self) -> None:
        argv, tmp = self._run_argv("example.com")
        try:
            with mock.patch.object(cli.RemoteHost, "check"), \
                 mock.patch.object(cli, "install", side_effect=RuntimeError("сломалось")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(argv)
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("Ошибка установки", out.getvalue())

    def test_config_file_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('host = "cfg.example.com"\ncaddy = true\n', encoding="utf-8")
            seen: list[str] = []
            with mock.patch.object(cli.RemoteHost, "check"), \
                 mock.patch.object(cli, "install", side_effect=lambda c, h: seen.append(c.host)):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["--config", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["cfg.example.com"])

    def test_missing_config_file_does_not_fall_back_to_repo(self) -> None:
        """Несуществующий --config не должен молча взять config.toml из репозитория."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "нет-такого.toml")
            out = io.StringIO()
            with mock.patch.object(cli.RemoteHost, "check", side_effect=SSHCommandError("стоп")), \
                 mock.patch.object(cli, "install", side_effect=lambda c, h: None):
                with contextlib.redirect_stdout(out):
                    code = cli.main(["--config", missing, "example.com"])
        self.assertEqual(code, 1)
        self.assertIn("example.com", out.getvalue())
        self.assertNotIn("dev.fly-server.ru", out.getvalue())


class ScopedListingWarnTests(unittest.TestCase):
    """Листинг всего сайта рядом с точечным — предупреждение, а не «ОК».

    Опции сайта берутся настоящим парсером: у него «весь сайт» — это
    пустой список, а «каталог» — список с путём. Руками выдуманные
    значения здесь уже проскакивали мимо проверок.
    """

    TEXT = (
        'dir-listing.activate = "enable"\n'
        '$HTTP["url"] =~ "^/etc($|/)" { server.dir-listing = "disable" }\n'
    )

    def _opts(self, raw: str) -> dict:
        return installer._static_options(raw)

    def _rule(self, sites: list[tuple[str, str, str]]):
        parsed = [(d, root, self._opts(raw)) for d, root, raw in sites]
        return lighttpd._rule_scoped_listing(
            lighttpd._uncommented(self.TEXT), parsed, "/var/www"
        )

    def test_scoped_browse_is_ok(self) -> None:
        rule = self._rule([("d.example.com", "/var/www", "browse=/install/")])
        self.assertEqual(rule.level, lighttpd.OK)

    def test_whole_browse_next_to_scoped_is_warn(self) -> None:
        rule = self._rule([
            ("a.example.com", "/var/www", "browse"),
            ("b.example.com", "/var/www", "browse=/install/"),
        ])
        self.assertEqual(rule.level, lighttpd.WARN)

    def test_whole_browse_instead_of_scoped_is_lost(self) -> None:
        rule = self._rule([("a.example.com", "/var/www", "browse")])
        self.assertEqual(rule.level, lighttpd.LOST)

    def test_no_browse_at_all_is_lost(self) -> None:
        rule = self._rule([("a.example.com", "/var/www", "index=a.html")])
        self.assertEqual(rule.level, lighttpd.LOST)

    def test_no_root_site_is_warn(self) -> None:
        rule = self._rule([("a.example.com", "/var/www/other", "browse=/i/")])
        self.assertEqual(rule.level, lighttpd.WARN)

    def test_other_root_site_does_not_count(self) -> None:
        """Зеркала etc*/update* — отдельные корни, их листинг не считается."""
        rule = self._rule([
            ("etc.example.com", "/var/www/etc", "browse"),
            ("d.example.com", "/var/www", "browse=/install/"),
        ])
        self.assertEqual(rule.level, lighttpd.OK)

    def test_no_disable_paths_returns_none(self) -> None:
        self.assertIsNone(
            lighttpd._rule_scoped_listing(
                lighttpd._uncommented('dir-listing.activate = "enable"\n'), [], "/var/www"
            )
        )

    def test_no_sites_at_all_is_warn(self) -> None:
        rule = self._rule([])
        self.assertEqual(rule.level, lighttpd.WARN)

    def test_without_activate_returns_none(self) -> None:
        """Без dir-listing.activate гасить нечего — правило не проверяем."""
        text = '$HTTP["url"] =~ "^/etc($|/)" { server.dir-listing = "disable" }\n'
        self.assertIsNone(
            lighttpd._rule_scoped_listing(
                lighttpd._uncommented(text),
                [("d.example.com", "/var/www", {"browse": []})],
                "/var/www",
            )
        )


class MigrationConsistencyTests(unittest.TestCase):
    """Сквозная инварианта: сайт, собранный установщиком, проходит аудит.

    Аудит и генератор Caddy живут в разных модулях и по-разному понимают опции
    сайта. Расхождение не выглядит как поломка: файлы собираются, аудит молчит.
    Тест берёт опции настоящим парсером `_static_options` и требует, чтобы
    конфиг, который в реальности считается завершённой миграцией, давал
    ноль `ПОТЕРЯНО` (правило 14 AGENTS.md).
    """

    LTPD = (
        'server.document-root        = "/var/www"\n'
        'dir-listing.activate = "enable"\n'
        '$HTTP["url"] =~ "^/update($|/)" { server.dir-listing = "disable" }\n'
        '$HTTP["url"] =~ "^/etc($|/)" { server.dir-listing = "disable" }\n'
        'index-file.names := ( "index.php", "index.html", "index.lighttpd.html" )\n'
        'url.access-deny             = ( "~", ".inc" )\n'
        'static-file.exclude-extensions = ( ".php", ".pl", ".fcgi" )\n'
    )
    DENY = "deny=*.inc,*.php,*.pl,*.fcgi"

    def _sites(self, cfg: Config) -> list:
        sites = []
        for raw in cfg.caddy_sites:
            domain, upstream, options = _split_site(raw)
            if not upstream.startswith(("file:", "static:")):
                continue
            root = upstream.split(":", 1)[1]
            sites.append((domain, root, installer._static_options(options)))
        return sites

    def test_installers_own_output_passes_audit(self) -> None:
        cfg = Config(
            caddy=True,
            caddy_sites=[
                f"www.fly-server.ru=file:/var/www|browse=/install/|{self.DENY}",
                f"etc.fly-server.ru=file:/var/www/etc|browse|{self.DENY}",
                f"update.fly-server.ru=file:/var/www/update|browse|{self.DENY}",
            ],
        )
        rules = lighttpd.audit(self.LTPD, self._sites(cfg))
        lost = [r for r in rules if r.level == lighttpd.LOST]
        self.assertEqual(
            [(r.name, r.hint) for r in lost], [],
            "собранный установщиком конфиг не должен терять правила lighttpd",
        )

    def test_denied_extension_is_really_denied(self) -> None:
        """Контроль полноты аудита: без deny правило обязано быть ПОТЕРЯНО."""
        cfg = Config(
            caddy=True,
            caddy_sites=["www.fly-server.ru=file:/var/www|browse=/install/"],
        )
        rules = lighttpd.audit(self.LTPD, self._sites(cfg))
        lost = [r for r in rules if r.level == lighttpd.LOST]
        self.assertTrue(any("deny" in r.name or "доступ" in r.name for r in lost),
                        [(r.name, r.level) for r in rules])

    def test_mirror_without_deny_is_flagged(self) -> None:
        """Правило 14: deny нужен на каждом зеркале, а не только на корне.

        Частичное покрытие — это WARN (не LOST), потому
        что корень закрыт. Важно тут другое: зеркало обязано быть **замечено**,
        а не молча признано нормальным — именно этим был сломан FlyLinkDC.
        """
        cfg = Config(
            caddy=True,
            caddy_sites=[
                f"www.fly-server.ru=file:/var/www|browse=/install/|{self.DENY}",
                "etc.fly-server.ru=file:/var/www/etc|browse",
            ],
        )
        rules = lighttpd.audit(self.LTPD, self._sites(cfg))
        deny_rule = next(r for r in rules if r.name == "доступ к исполняемым файлам")
        self.assertNotEqual(deny_rule.level, lighttpd.OK)
        self.assertEqual(deny_rule.level, lighttpd.WARN)
        self.assertIn("etc.fly-server.ru", deny_rule.hint)

    def test_all_sites_without_deny_is_lost(self) -> None:
        cfg = Config(caddy=True, caddy_sites=["www.fly-server.ru=file:/var/www|browse"])
        rules = lighttpd.audit(self.LTPD, self._sites(cfg))
        deny_rule = next(r for r in rules if r.name == "доступ к исполняемым файлам")
        self.assertEqual(deny_rule.level, lighttpd.LOST)


if __name__ == "__main__":
    unittest.main()
