from __future__ import annotations

import subprocess
from dataclasses import dataclass


class SSHCommandError(RuntimeError):
    pass


@dataclass
class RemoteHost:
    host: str
    user: str
    port: int
    key_path: str
    accept_new: bool = True

    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def base_cmd(self) -> list[str]:
        cmd = ["ssh", "-p", str(self.port), "-i", self.key_path]
        if self.accept_new:
            cmd += ["-o", "StrictHostKeyChecking=accept-new"]
        cmd += ["-o", "BatchMode=yes"]
        return cmd

    def run_script(self, script: str) -> None:
        """Выполняет bash-скрипт на хосте, передавая его через stdin.

        Потоково печатает объединённый stdout/stderr в локальную консоль.
        """
        target = self.target()
        ssh_args = self.base_cmd()
        proc = subprocess.Popen(
            [*ssh_args, target, "bash", "-s"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(script.rstrip("\n") + "\n")
        proc.stdin.close()

        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")

        rc = proc.wait()
        if rc != 0:
            raise SSHCommandError(
                f"SSH-сессия {target} завершилась с кодом {rc}"
            )

    def check(self) -> None:
        """Проверка подключения: выполняет лёгкую команду."""
        cmd = [*self.base_cmd(), self.target(), "true"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            msg = proc.stderr.strip()
            raise SSHCommandError(
                f"Не удалось подключиться к {self.target()} — {msg}"
            )
