from typing import Any

import pytest

from veyra_proof.command_runner import CommandRunner


def test_dry_run_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        called.append(True)
        raise AssertionError("subprocess.run should not be called in dry-run")

    monkeypatch.setattr("veyra_proof.command_runner.subprocess.run", fake_run)
    runner = CommandRunner(dry_run=True)
    result = runner.run(["cargo", "--version"])
    assert result.error == "dry-run"
    assert result.exit_code is None
    assert called == []


def test_command_is_list_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(cmd: Any, **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        captured["shell"] = kwargs.get("shell")
        return Completed()

    monkeypatch.setattr("veyra_proof.command_runner.subprocess.run", fake_run)
    runner = CommandRunner()
    result = runner.run(["echo", "hi"])
    assert captured["cmd"] == ["echo", "hi"]
    assert captured["shell"] is False
    assert result.exit_code == 0


def test_stream_mode_captures_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeStdout:
        def __init__(self, lines: Any) -> None:
            self._lines = list(lines)
            self._idx = 0

        def readline(self) -> Any:
            if self._idx < len(self._lines):
                line = self._lines[self._idx]
                self._idx += 1
                return line
            return ""

        def read(self) -> Any:
            return ""

    class FakeProc:
        def __init__(self) -> None:
            self.stdout = FakeStdout(["Compiling foo\n", "Finished\n"])
            self._polled = False

        def poll(self) -> Any:
            if self.stdout._idx >= 2:
                return 0
            return None

        def wait(self, timeout: Any = None) -> Any:
            return 0

        def kill(self) -> None:
            pass

    def fake_popen(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("shell") is False
        assert kwargs.get("stderr") == __import__("subprocess").STDOUT
        return FakeProc()

    monkeypatch.setattr("veyra_proof.command_runner.subprocess.Popen", fake_popen)
    runner = CommandRunner(verbose=True)
    result = runner.run(["cargo", "install", "x", "--locked"], stream=True, timeout=30)
    assert result.exit_code == 0
    assert "Compiling foo" in result.stdout
    assert result.timed_out is False
    err = capsys.readouterr().err
    assert "cargo install x --locked" in err or "Compiling foo" in err
