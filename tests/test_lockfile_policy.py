"""The lockfile policy: an audit may not rewrite Cargo.lock behind the operator.

Every cargo build invocation used to run bare, so `cargo check` would resolve
and write a stale Cargo.lock during the audit. The auditor then reported the
rewrite as an unauthorized change and rejected — a self-inflicted failure on any
project whose lockfile had drifted. These tests pin the fix: --locked whenever a
lock exists, nothing added when it does not, and the plan saying what will run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veyra_proof.command_runner import CommandRunner, apply_lock_policy


def _lock(root: Path) -> Path:
    path = root / "Cargo.lock"
    path.write_text("version = 4\n", encoding="utf-8")
    return path


class TestApplyLockPolicy:
    def test_locked_is_added_when_a_lockfile_exists(self, tmp_path):
        _lock(tmp_path)
        argv = apply_lock_policy(["cargo", "check", "--workspace"], cwd=tmp_path)
        assert argv == ["cargo", "check", "--locked", "--workspace"]

    def test_nothing_is_added_without_a_lockfile(self, tmp_path):
        # `--locked` on a project with no lock makes cargo refuse to build, so
        # the flag may only ever follow the file's existence.
        argv = apply_lock_policy(["cargo", "check", "--workspace"], cwd=tmp_path)
        assert argv == ["cargo", "check", "--workspace"]

    def test_authorized_updates_leave_the_command_alone(self, tmp_path):
        _lock(tmp_path)
        argv = apply_lock_policy(
            ["cargo", "check", "--workspace"], cwd=tmp_path, allow_update=True
        )
        assert argv == ["cargo", "check", "--workspace"]

    def test_a_flag_that_already_decides_is_not_duplicated(self, tmp_path):
        _lock(tmp_path)
        for flag in ("--locked", "--frozen", "--offline"):
            argv = apply_lock_policy(
                ["cargo", "build", flag, "--release"], cwd=tmp_path
            )
            assert argv == ["cargo", "build", flag, "--release"]

    def test_version_probes_are_not_locked(self, tmp_path):
        # A probe builds nothing. `cargo nextest --version` rejects the flag at
        # the top level (exit 2), so detection reported an installed
        # cargo-nextest as "not installed" on any workspace with a lockfile.
        _lock(tmp_path)
        for argv in (
            ["cargo", "nextest", "--version"],
            ["cargo", "build", "--version"],
            ["cargo", "+nightly", "clippy", "-V"],
        ):
            assert apply_lock_policy(argv, cwd=tmp_path) == argv

    def test_version_flags_after_the_harness_separator_still_lock(self, tmp_path):
        # `cargo test -- --version` filters tests by that name; it still builds.
        _lock(tmp_path)
        argv = apply_lock_policy(
            ["cargo", "test", "--workspace", "--", "--version"], cwd=tmp_path
        )
        assert argv == ["cargo", "test", "--locked", "--workspace", "--", "--version"]

    def test_plugin_shape_keeps_its_own_subcommand(self, tmp_path):
        # `cargo nextest run --workspace` would break if the flag were inserted
        # between the plugin and its subcommand.
        _lock(tmp_path)
        argv = apply_lock_policy(
            ["cargo", "nextest", "run", "--workspace"], cwd=tmp_path
        )
        assert argv == ["cargo", "nextest", "run", "--locked", "--workspace"]

    def test_flag_lands_before_trailing_test_harness_args(self, tmp_path):
        _lock(tmp_path)
        argv = apply_lock_policy(
            ["cargo", "test", "--workspace", "--", "--nocapture"], cwd=tmp_path
        )
        assert argv == ["cargo", "test", "--locked", "--workspace", "--", "--nocapture"]

    def test_bare_command_gets_the_flag_appended(self, tmp_path):
        _lock(tmp_path)
        assert apply_lock_policy(["cargo", "build"], cwd=tmp_path) == [
            "cargo",
            "build",
            "--locked",
        ]

    def test_read_only_subcommands_are_never_touched(self, tmp_path):
        _lock(tmp_path)
        for argv in (
            ["cargo", "metadata", "--format-version", "1"],
            ["cargo", "fmt", "--all", "--", "--check"],
            ["cargo", "audit"],
            ["cargo", "deny", "check"],
            ["cargo", "install", "cargo-audit", "--locked"],
            ["cargo", "update"],
        ):
            assert apply_lock_policy(argv, cwd=tmp_path) == argv

    def test_non_cargo_commands_are_untouched(self, tmp_path):
        _lock(tmp_path)
        for argv in (["rustc", "--version"], ["semgrep", "--version"], ["cargo.exe"]):
            assert apply_lock_policy(argv, cwd=tmp_path) == argv

    def test_toolchain_override_is_skipped_when_finding_the_subcommand(self, tmp_path):
        _lock(tmp_path)
        argv = apply_lock_policy(
            ["cargo", "+nightly", "build", "--release"], cwd=tmp_path
        )
        assert argv == ["cargo", "+nightly", "build", "--locked", "--release"]

    def test_windows_executable_name_is_recognised(self, tmp_path):
        _lock(tmp_path)
        argv = apply_lock_policy(
            [str(Path("C:/cargo/bin/cargo.EXE")), "check"], cwd=tmp_path
        )
        assert argv == [str(Path("C:/cargo/bin/cargo.EXE")), "check", "--locked"]


class TestRunnerAppliesPolicy:
    def test_the_argv_recorded_as_evidence_is_the_argv_executed(self, tmp_path, monkeypatch):
        _lock(tmp_path)
        seen: list[list[str]] = []

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            return _Completed()

        monkeypatch.setattr("subprocess.run", fake_run)
        result = CommandRunner().run(["cargo", "check", "--workspace"], cwd=tmp_path)
        assert seen == [["cargo", "check", "--locked", "--workspace"]]
        assert result.command == ["cargo", "check", "--locked", "--workspace"]

    def test_dry_run_reports_the_effective_argv(self, tmp_path):
        _lock(tmp_path)
        result = CommandRunner(dry_run=True).run(
            ["cargo", "test", "--workspace"], cwd=tmp_path
        )
        assert result.command == ["cargo", "test", "--locked", "--workspace"]
        assert result.error == "dry-run"


class TestUndecodableOutputIsStillCaptured:
    """A verdict must never rest on an empty log.

    Rust writes UTF-8. On a Windows console whose locale is cp1252, one
    undecodable byte killed the `subprocess` reader thread, which left
    `stdout`/`stderr` as ``None``: the tool wrote two empty logs and reported
    "Non-zero exit code 101 without structured findings". Reproduced here with
    a child that emits a byte cp1252 cannot decode.
    """

    def test_a_byte_the_locale_cannot_decode_does_not_lose_the_output(self, tmp_path):
        import sys

        script = (
            "import sys; sys.stdout.buffer.write(b'before'); "
            "sys.stdout.buffer.write(b'\\x8f'); "
            "sys.stdout.buffer.write(b'after'); sys.exit(7)"
        )
        result = CommandRunner().run(
            [sys.executable, "-c", script], cwd=tmp_path, timeout=60
        )
        assert result.exit_code == 7
        assert result.stdout is not None
        assert result.stdout.startswith("before")
        assert result.stdout.endswith("after")
        assert "\ufffd" in result.stdout

    def test_the_same_holds_for_stderr(self, tmp_path):
        import sys

        script = "import sys; sys.stderr.buffer.write(b'\\x8f'); sys.exit(3)"
        result = CommandRunner().run([sys.executable, "-c", script], cwd=tmp_path, timeout=60)
        assert result.stderr is not None
        assert "\ufffd" in result.stderr

    def test_streaming_capture_does_not_lose_it_either(self, tmp_path, capsys):
        import sys

        script = "import sys; sys.stdout.buffer.write(b'\\x8f'); sys.exit(0)"
        result = CommandRunner().run(
            [sys.executable, "-c", script], cwd=tmp_path, timeout=60, stream=True
        )
        assert result.exit_code == 0
        assert "\ufffd" in result.stdout


class TestToolsDeclareTheirEffectiveCommand:
    """The plan, the execution and the report have to agree.

    A tool that builds its argv by hand would execute something the plan never
    showed and the report never named, which is precisely the invisible
    behaviour this project exists to catch.
    """

    @staticmethod
    def _planned(tool_id: str, root: Path) -> list[list[str]]:
        from veyra_proof.command_runner import CommandRunner
        from veyra_proof.config import VeyraConfig
        from veyra_proof.models import PlatformOS, WorkspaceInfo
        from veyra_proof.platform_info import PlatformInfo
        from veyra_proof.tool_registry import tool_by_id
        from veyra_proof.tools.base import AuditContext

        platform = PlatformInfo(
            os=PlatformOS.LINUX,
            os_raw="Linux",
            architecture="x86_64",
            architecture_raw="x86_64",
            python_version="3.12",
            python_executable="python",
            cargo_path="cargo",
            cargo_version="cargo 1.98.0",
            rustc_path="rustc",
            rustc_version="rustc 1.98.0",
        )
        workspace = WorkspaceInfo(
            path=str(root),
            workspace_root=str(root),
            packages=[],
            workspace_members=[],
            target_directory=str(root / "target"),
            default_members=[],
            root_package=None,
            has_lockfile=True,
            lockfile_path=str(root / "Cargo.lock"),
            metadata_raw={},
        )
        context = AuditContext(
            workspace=workspace,
            platform=platform,
            config=VeyraConfig(),
            runner=CommandRunner(dry_run=True),
            evidence_dir=root / "evidence",
            profile="quick",
            timeout_seconds=60.0,
        )
        return tool_by_id()[tool_id].planned_commands(context)

    @pytest.mark.parametrize(
        "tool_id,expected_head",
        [
            ("cargo-check", ["cargo", "check"]),
            ("clippy", ["cargo", "clippy"]),
            ("cargo-test", ["cargo", "test"]),
            ("nextest", ["cargo", "nextest"]),
            ("reproducible-build", ["cargo", "build"]),
        ],
    )
    def test_planned_commands_carry_the_lock_flag(self, tool_id, expected_head, tmp_path):
        _lock(tmp_path)
        cmds = self._planned(tool_id, tmp_path)
        assert cmds, f"{tool_id} plans no command"
        first = cmds[0]
        assert first[: len(expected_head)] == expected_head
        assert "--locked" in first, f"{tool_id} would build without --locked: {first}"

    def test_no_cargo_build_plans_without_the_lock_flag(self, tmp_path):
        from veyra_proof.tool_registry import all_tools

        _lock(tmp_path)
        offenders = []
        for tool in all_tools():
            for cmd in self._planned(tool.id, tmp_path):
                if not cmd or Path(cmd[0]).name.lower() not in {"cargo", "cargo.exe"}:
                    continue
                sub = next((p for p in cmd[1:] if not p.startswith(("+", "-"))), None)
                if sub in {"build", "check", "test", "bench", "clippy", "nextest"}:
                    if "--locked" not in cmd:
                        offenders.append(f"{tool.id}: {cmd}")
        assert not offenders, "cargo builds planned without --locked:\n" + "\n".join(offenders)