"""A version probe has three outcomes, not two.

Reading "any non-zero exit" as "not installed" is the bug that once reported an
installed cargo-nextest as missing: ``cargo nextest --locked --version`` is
rejected at the top level and exits 2. Only an OSError (nothing to exec) or
cargo's "no such command" (the subcommand is genuinely absent) may be called
missing. A probe that ran and answered non-zero, or timed out, is a tool that is
present but unverified — recorded as such and left available so its real run
reports the truth instead of being skipped.
"""

from __future__ import annotations

from pathlib import Path

from veyra_proof.config import PolicyConfig, VeyraConfig
from veyra_proof.models import CommandResult, PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.tools.base import AuditContext
from veyra_proof.tools.nextest import NextestTool


class ScriptedRunner:
    """Replays one canned CommandResult for every call."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def run(self, command, cwd=None, timeout=None, input_text=None) -> CommandResult:
        self.calls.append(list(command))
        return self.result


def probe(
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    error: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=["cargo", "nextest", "--version"],
        cwd=".",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        timed_out=timed_out,
        error=error,
    )


def make_context(tmp_path: Path, runner: ScriptedRunner) -> AuditContext:
    workspace = WorkspaceInfo(
        path=str(tmp_path),
        workspace_root=str(tmp_path),
        packages=[],
        workspace_members=[],
        target_directory=str(tmp_path / "target"),
        default_members=[],
        root_package=None,
        has_lockfile=False,
        lockfile_path=None,
        metadata_raw={},
    )
    platform = PlatformInfo(
        os=PlatformOS.LINUX,
        os_raw="Linux",
        architecture="x86_64",
        architecture_raw="x86_64",
        python_version="3.12",
        python_executable="python",
        cargo_path="cargo",
    )
    return AuditContext(
        workspace=workspace,
        platform=platform,
        config=VeyraConfig(policy=PolicyConfig()),
        runner=runner,
        evidence_dir=tmp_path / "evidence",
        profile="standard",
        timeout_seconds=60.0,
    )


def classify(tmp_path: Path, result: CommandResult):
    runner = ScriptedRunner(result)
    tool = NextestTool()
    return tool.detect_via_version(
        make_context(tmp_path, runner),
        ["cargo", "nextest", "--version"],
        absent_message="cargo-nextest not installed",
    )


def test_a_probe_that_answers_reports_installed_with_its_version(tmp_path):
    det = classify(tmp_path, probe(exit_code=0, stdout="cargo-nextest 0.9.84\n"))
    assert det.status == "INSTALLED"
    assert det.available is True
    assert det.version == "cargo-nextest 0.9.84"


def test_an_oserror_is_absent_not_broken(tmp_path):
    # Nothing to exec: the binary genuinely is not there.
    det = classify(tmp_path, probe(exit_code=None, error="FileNotFoundError: [Errno 2] No such file"))
    assert det.status == "MISSING"
    assert det.available is False
    assert "not installed" in det.message


def test_cargo_no_such_command_is_absent_not_broken(tmp_path):
    # cargo ran but the subcommand is not installed — also absence, not breakage.
    det = classify(tmp_path, probe(exit_code=101, stderr="error: no such command: `nextest`\n"))
    assert det.status == "MISSING"
    assert det.available is False


def test_a_probe_that_ran_and_failed_is_present_not_missing(tmp_path):
    # The exact regression: an installed tool, a rejected probe, exit 2.
    det = classify(
        tmp_path,
        probe(exit_code=2, stderr="error: unexpected argument '--locked' found\n"),
    )
    assert det.status != "MISSING"
    assert det.available is True
    assert det.version is None
    assert "is installed but" in det.message


def test_a_timed_out_probe_is_present_not_missing(tmp_path):
    det = classify(tmp_path, probe(exit_code=None, timed_out=True, error="timeout after 20s"))
    assert det.status != "MISSING"
    assert det.available is True
    assert "timed out" in det.message


def test_nextest_detect_does_not_call_an_installed_tool_missing(tmp_path, monkeypatch):
    # End to end through the tool: the --locked probe rejection must not make
    # detect() report a present cargo-nextest as not installed.
    monkeypatch.setattr("veyra_proof.tools.nextest.shutil.which", lambda name: "/usr/bin/cargo")
    runner = ScriptedRunner(
        probe(exit_code=2, stderr="error: unexpected argument '--locked' found\n")
    )
    det = NextestTool().detect(make_context(tmp_path, runner))
    assert det.status != "MISSING"
    assert det.available is True
