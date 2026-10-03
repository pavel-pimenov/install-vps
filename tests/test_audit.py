"""Тесты `python3 -m install_vps.audit` (scripts/audit-lighttpd.sh).

Логика раньше жила heredoc'ом внутри шелл-скрипта: ruff её не видел, тесты
не доставали, и сломанный sudo читался как успешная миграция. Здесь закрыты
все исходы чтения конфига, в том числе те, что не срабатывают ни на одном
боевом хосте.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from install_vps import audit
from install_vps.config import Config

# Управляющий ответ хоста, который audit классифицирует как успех.
GOOD_OUTPUT = "### FILE /etc/lighttpd/lighttpd.conf\n$HTTP[\"url\"] =~ \"^/x/\" {\n}\n"

# Ровно то, что стоит на dc: четыре расширения из exclude-extensions, `~` из
# access-deny и корень, равный document-root (тогда legacy не требуется).
DENY_CONFIG = """
$HTTP["url"] =~ "\\.php$" {
    url.access-deny = ("~", ".php")
}
server.static-file.exclude-extensions = (".php", ".pl", ".fcgi", ".inc")
server.document-root = "/var/www"
"""


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> mock.Mock:
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


class SshCommandTests(unittest.TestCase):
    def test_without_sudo_script_goes_through_stdin(self) -> None:
        argv, stdin = audit.ssh_argv(Config(host="h", key_path="/k"), "echo hi")
        self.assertEqual(stdin, "echo hi")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("root@h", argv)
        self.assertEqual(argv[-1], "bash -s")

    def test_sudo_wraps_bash_c_not_the_loop(self) -> None:
        """`sudo for f in ...` — синтаксическая ошибка; оборачиваем bash -c."""
        argv, stdin = audit.ssh_argv(Config(host="h", sudo=True), "for f in a; do :; done")
        self.assertIsNone(stdin)
        self.assertIn("sudo bash -c ", argv[-1])
        self.assertIn("for f in a; do :; done", argv[-1])

    def test_port_key_and_accept_new_are_present(self) -> None:
        argv, _ = audit.ssh_argv(
            Config(host="h", port=2222, key_path="/k", accept_new=True), "s")
        self.assertEqual(argv[1:4], ["-p", "2222", "-i"])
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        argv, _ = audit.ssh_argv(Config(host="h", accept_new=False), "s")
        self.assertNotIn("StrictHostKeyChecking=accept-new", " ".join(argv))


class ClassifyTests(unittest.TestCase):
    """Разбор ответа хоста. Раньше всё это было внутри shell-скрипта."""

    def test_ok(self) -> None:
        self.assertEqual(audit.classify("### FILE /x\n", 0, "", Config(host="h"))[0],
                         audit.OK)

    def test_absent_is_not_a_failure(self) -> None:
        """lighttpd сняли — это конец миграции, а не поломка аудита."""
        outcome, text = audit.classify(audit.ABSENT, 0, "", Config(host="h"))
        self.assertEqual(outcome, audit.ABSENT_OUT)
        self.assertIn("lighttpd удалён", text)

    def test_unreadable_is_an_error_and_says_why(self) -> None:
        """Раньше читался как успешная миграция — молчали и про sudo."""
        outcome, text = audit.classify(audit.UNREADABLE, 0, "", Config(host="h", user="ppa"))
        self.assertEqual(outcome, audit.UNREADABLE_OUT)
        self.assertIn("NOPASSWD", text)
        self.assertIn("ppa", text)

    def test_empty_output_is_an_error(self) -> None:
        outcome, text = audit.classify("   \n", 0, "", Config(host="h"))
        self.assertEqual(outcome, audit.EMPTY)
        self.assertIn("не вывела ничего", text)

    def test_ssh_failure_mentions_sudo_only_for_sudo_config(self) -> None:
        outcome, text = audit.classify("", 255, "sudo: a password is required",
                                       Config(host="h", sudo=True))
        self.assertEqual(outcome, audit.SSH_FAILED)
        self.assertIn("NOPASSWD", text)

    def test_ssh_failure_without_sudo_suggests_migration(self) -> None:
        outcome, text = audit.classify("", 255, "Connection refused", Config(host="h"))
        self.assertEqual(outcome, audit.SSH_FAILED)
        self.assertIn("Миграция", text)
        self.assertNotIn("NOPASSWD", text)

    def test_ssh_failure_wins_over_absent_marker(self) -> None:
        """Иначе оборванный по таймауту ssh с остатками вывода дал бы «удалён»."""
        outcome, _ = audit.classify(audit.ABSENT, 255, "timeout", Config(host="h"))
        self.assertEqual(outcome, audit.SSH_FAILED)


class SitesFromConfigTests(unittest.TestCase):
    def test_proxy_site_has_empty_root(self) -> None:
        cfg = Config(caddy_sites=["portal.example.com=http://127.0.0.1:8080"])
        self.assertEqual(audit.sites_from_config(cfg),
                         [("portal.example.com", "", {})])

    def test_file_and_static_upstreams_become_roots(self) -> None:
        cfg = Config(caddy_sites=[
            "a.example.com=file:/var/www/a|browse",
            "b.example.com=static:/var/www/b|index=index.html",
        ])
        sites = dict((d, r) for d, r, _ in audit.sites_from_config(cfg))
        self.assertEqual(sites, {"a.example.com": "/var/www/a", "b.example.com": "/var/www/b"})
        self.assertEqual(audit.sites_from_config(cfg)[0][2], {"browse": []})

    def test_trailing_pipe_in_options_is_tolerated(self) -> None:
        cfg = Config(caddy_sites=["a.example.com=file:/var/www/a|browse|"])
        self.assertEqual(audit.sites_from_config(cfg)[0][2], {"browse": []})

    def test_no_sites_is_empty_list(self) -> None:
        self.assertEqual(audit.sites_from_config(Config()), [])


class RunAuditTests(unittest.TestCase):
    def _run(self, proc: mock.Mock, cfg: Config | None = None) -> tuple[int, str]:
        cfg = cfg or Config(host="h", caddy_sites=["a.example.com=file:/var/www/a"])
        with mock.patch.object(subprocess, "run", return_value=proc):
            code, out = audit.run_audit(cfg)
        return code, out

    def test_without_host_refuses_to_do_anything(self) -> None:
        code, out = audit.run_audit(Config())
        self.assertEqual(code, audit.EXIT_ERROR)
        self.assertIn("нет host", out)

    def _site(self, options: str = "") -> Config:
        return Config(host="h", caddy_sites=[f"a.example.com=file:/var/www|{options}"])

    def test_clean_migration_is_ok(self) -> None:
        code, out = self._run(_proc(GOOD_OUTPUT + DENY_CONFIG),
                              self._site("deny=*.fcgi,*.inc,*.php,*.pl,*~"))
        self.assertEqual(code, audit.EXIT_OK)
        self.assertIn("все найденные правила перенесены", out)

    def test_lost_rule_gives_exit_one(self) -> None:
        """Код 1 — повод править caddy_site, а не «аудит не запустился»."""
        code, out = self._run(_proc(GOOD_OUTPUT + DENY_CONFIG), self._site("deny=*.php"))
        self.assertEqual(code, audit.EXIT_LOST)
        self.assertIn("ПОТЕРЯНО", out)
        self.assertIn("*~", out)

    def test_legacy_loss_is_reported(self) -> None:
        """Сайт в подкаталоге document-root без legacy= — клиенты уедут в 404."""
        cfg = Config(host="h", caddy_sites=[
            "a.example.com=file:/var/www/etc|deny=*.fcgi,*.inc,*.php,*.pl,*~",
        ])
        code, out = self._run(_proc(GOOD_OUTPUT + DENY_CONFIG), cfg)
        self.assertEqual(code, audit.EXIT_LOST)
        self.assertIn("legacy=/etc", out)

    def test_absent_prints_message_to_stdout_and_succeeds(self) -> None:
        code, out = self._run(_proc(audit.ABSENT))
        self.assertEqual(code, audit.EXIT_OK)
        self.assertIn("аудит не нужен", out)

    def test_unreadable_is_exit_error(self) -> None:
        code, _ = self._run(_proc(audit.UNREADABLE))
        self.assertEqual(code, audit.EXIT_ERROR)

    def test_read_script_asks_for_both_config_and_conf_enabled(self) -> None:
        """conf-enabled держит alias'ы и точечные правила — без него аудит молчит."""
        self.assertIn("/etc/lighttpd/lighttpd.conf", audit.READ_SCRIPT)
        self.assertIn("/etc/lighttpd/conf-enabled/*.conf", audit.READ_SCRIPT)
        self.assertIn("### LIGHTTPD_ABSENT", audit.READ_SCRIPT)
        self.assertIn("### LIGHTTPD_UNREADABLE", audit.READ_SCRIPT)

    def test_read_script_ends_with_exit_zero(self) -> None:
        """Иначе последняя команда цикла задала бы код возврата всего ssh."""
        self.assertTrue(audit.READ_SCRIPT.rstrip().endswith("exit 0"))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "config.toml"
        self.cfg.write_text("", encoding="utf-8")

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_config_explains_what_to_pass(self) -> None:
        code, _, err = self._main(["-c", "/nonexistent/config.toml"])
        self.assertEqual(code, audit.EXIT_ERROR)
        self.assertIn("config.<хост>.local.toml", err)

    def test_error_message_goes_to_stderr_not_stdout(self) -> None:
        """Иначе отчёт об ошибке уедет в пайп и потеряется среди OK-строк."""
        with mock.patch.object(audit, "run_audit", return_value=(audit.EXIT_ERROR, "бум")):
            code, out, err = self._main(["-c", str(self.cfg)])
        self.assertEqual(code, audit.EXIT_ERROR)
        self.assertEqual(err.strip(), "бум")
        self.assertEqual(out, "")

    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_successful_report_goes_to_stdout(self) -> None:
        with mock.patch.object(audit, "run_audit", return_value=(audit.EXIT_LOST, "отчёт")):
            code, out, err = self._main(["-c", str(self.cfg)])
        self.assertEqual(code, audit.EXIT_LOST)
        self.assertEqual(out.strip(), "отчёт")
        self.assertEqual(err, "")

    def test_main_guard_line_actually_executes(self) -> None:
        """Строка `raise SystemExit(main())` — её subprocess не засчитывает.

        Запускаем исходник модуля как `__main__`; `--help` выходит из argparse
        до чтения конфига и до SSH.
        """
        shadow = types.ModuleType("install_vps.audit_as_main")
        shadow.__package__ = "install_vps"
        shadow.__name__ = "__main__"
        shadow.__file__ = audit.__file__
        argv, sys.argv = sys.argv, ["install-vps-audit", "--help"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as ctx:
                exec(compile(Path(audit.__file__).read_text(), audit.__file__, "exec"),
                     shadow.__dict__)
        finally:
            sys.argv = argv
        self.assertEqual(ctx.exception.code, 0)

    def test_module_entrypoint_works(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "install_vps.audit", "--help"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--config", proc.stdout)


if __name__ == "__main__":
    unittest.main()
