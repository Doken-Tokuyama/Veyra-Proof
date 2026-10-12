"""Regression tests for two real tool-invocation bugs.

1. cargo-geiger cannot run against a virtual workspace manifest (a root with no
   ``[package]``); it must be invoked per package with ``--manifest-path``.
2. cargo-semver-checks on an unpublished crate fails with "not found in
   registry"; that is a missing baseline (SKIPPED with a reason), not a
   violation. Real violations must still be FINDINGS even when lint names
   contain words like "cannot" (e.g. ``function_cannot_unwind``).
"""

from __future__ import annotations

from pathlib import Path

from veyra_proof.config import PolicyConfig, VeyraConfig
from veyra_proof.models import CommandResult, PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.tools.base import AuditContext
from veyra_proof.tools.geiger import CargoGeigerTool
from veyra_proof.tools.semver_checks import SemverChecksTool
from veyra_proof.tools.vet import CargoVetTool


class ScriptedRunner:
    """Records every command and replays canned results in order."""

    def __init__(self, results: list[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def run(self, command, cwd=None, timeout=None, input_text=None) -> CommandResult:
        self.calls.append(list(command))
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def result(exit_code: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(
        command=["cargo"],
        cwd=".",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
    )


def _workspace(root: Path, *, root_package: dict | None, packages: list[dict]) -> WorkspaceInfo:
    return WorkspaceInfo(
        path=str(root),
        workspace_root=str(root),
        packages=packages,
        workspace_members=[],
        target_directory=str(root / "target"),
        default_members=[],
        root_package=root_package,
        has_lockfile=True,
        lockfile_path=str(root / "Cargo.lock"),
        metadata_raw={},
    )


def _platform() -> PlatformInfo:
    return PlatformInfo(
        os=PlatformOS.LINUX,
        os_raw="Linux",
        architecture="x86_64",
        architecture_raw="x86_64",
        python_version="3.12",
        python_executable="python",
        cargo_path="cargo",
    )


def make_context(
    tmp_path: Path,
    runner: ScriptedRunner,
    *,
    root_package: dict | None = None,
    packages: list[dict] | None = None,
    policy: PolicyConfig | None = None,
) -> AuditContext:
    return AuditContext(
        workspace=_workspace(
            tmp_path,
            root_package=root_package,
            packages=packages
            if packages is not None
            else [
                {"name": "a", "manifest_path": "/ws/a/Cargo.toml", "targets": [{"kind": ["lib"]}]},
                {"name": "b", "manifest_path": "/ws/b/Cargo.toml", "targets": [{"kind": ["bin"]}]},
            ],
        ),
        platform=_platform(),
        config=VeyraConfig(policy=policy or PolicyConfig()),
        runner=runner,
        evidence_dir=tmp_path / "evidence",
        profile="standard",
        timeout_seconds=60.0,
    )


def test_geiger_virtual_manifest_runs_per_package_with_manifest_path(tmp_path) -> None:
    runner = ScriptedRunner([result(), result()])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoGeigerTool().run(ctx)

    assert runner.calls == [
        ["cargo", "geiger", "--manifest-path", "/ws/a/Cargo.toml"],
        ["cargo", "geiger", "--manifest-path", "/ws/b/Cargo.toml"],
    ]
    assert tool_result.status == "OK"
    assert tool_result.environment["virtual_workspace"] == "yes"
    assert tool_result.environment["geiger_runs"] == "2"


def test_geiger_with_root_package_keeps_plain_invocation(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner, root_package={"name": "root"})

    tool_result = CargoGeigerTool().run(ctx)

    assert runner.calls == [["cargo", "geiger"]]
    assert tool_result.status == "OK"
    assert tool_result.environment["virtual_workspace"] == "no"


def test_geiger_aggregates_failures_across_packages(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=101), result(exit_code=0)])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "ERROR"
    assert "exit 101" in tool_result.summary


def test_geiger_coverage_warnings_are_inconclusive_not_error(tmp_path) -> None:
    """Scan completed but some dependency files were not scanned/parsed: the
    unsafe inventory is a lower bound, so this can never be OK nor ERROR."""
    runner = ScriptedRunner(
        [
            result(exit_code=0, stdout="0/0  10/10  0/0  0/0  0/0  :)"),
            result(
                exit_code=1,
                stdout="0/0  1/1  0/0  0/0  0/0  !",
                stderr=(
                    "WARNING: Dependency file was never scanned: /reg/dep/README.md\n"
                    "Failed to parse file: /reg/signal-hook-registry/src/lib.rs\n"
                    "Scanning done\n"
                    "error: Found 2 warnings\n"
                ),
            ),
        ]
    )
    ctx = make_context(tmp_path, runner)

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"
    assert "not a pass" in tool_result.summary
    assert "lower bound" in tool_result.summary
    assert tool_result.environment["coverage_warning_runs"] == "1"


def test_geiger_compile_error_is_still_error_even_with_warning_text(tmp_path) -> None:
    """A real compile failure must not be masked as a coverage warning."""
    runner = ScriptedRunner(
        [
            result(
                exit_code=101,
                stderr=(
                    "error: could not compile `eqproxy-core` (lib) due to 1 previous error\n"
                    "error[E0308]: mismatched types\n"
                    "error: Found 1 warnings\n"
                ),
            ),
        ]
    )
    ctx = make_context(tmp_path, runner)

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "ERROR"
    assert "exit 101" in tool_result.summary


def test_geiger_deny_unsafe_violation_outranks_coverage_warnings(tmp_path) -> None:
    """A definite deny_unsafe violation is a FINDINGS even with coverage gaps."""
    runner = ScriptedRunner(
        [
            result(
                exit_code=1,
                stdout=GEIGER_UNSAFE_OUTPUT,
                stderr="error: Found 1 warnings\n",
            ),
        ]
    )
    ctx = make_context(tmp_path, runner, policy=PolicyConfig(deny_unsafe=True))

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["unsafe-usage"]


def test_geiger_empty_run_output_with_exit_1_is_error(tmp_path) -> None:
    """exit 1 without the FoundWarningsError signature stays ERROR."""
    runner = ScriptedRunner([result(exit_code=1, stderr="some unrelated failure\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "ERROR"


GEIGER_UNSAFE_OUTPUT = (
    "Functions using unsafe: 3 / 10\nExpressions using unsafe: 7 / 40\n"
)


def test_geiger_deny_unsafe_still_reports_findings(tmp_path) -> None:
    runner = ScriptedRunner(
        [
            result(stdout=GEIGER_UNSAFE_OUTPUT),
            result(stdout=GEIGER_UNSAFE_OUTPUT),
        ]
    )
    ctx = make_context(tmp_path, runner, policy=PolicyConfig(deny_unsafe=True))

    tool_result = CargoGeigerTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["unsafe-usage"]


NO_BASELINE_OUTPUT = (
    "error: failed to retrieve index of crate versions from registry\n"
    "\n"
    "Caused by:\n"
    "    eqproxy-config not found in registry (crates.io). For workarounds check "
    "https://docs.rs/about/metadata\n"
)

VIOLATION_OUTPUT = (
    "Summary semver requires new major version: Possible semver violation: "
    "function_cannot_unwind: function `do_work` used to be unwind-safe, "
    "but now may unwind\n"
)


def test_semver_unpublished_crate_is_skipped_with_reason_not_findings(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=100, stderr=NO_BASELINE_OUTPUT)])
    ctx = make_context(tmp_path, runner)

    tool_result = SemverChecksTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert tool_result.environment["skip_kind"] == "intentional"
    assert "baseline" in tool_result.summary
    assert tool_result.findings == []


def test_skip_after_a_run_keeps_the_raw_tool_output_as_evidence(tmp_path) -> None:
    """The skip reason must not overwrite what the command actually printed.

    The skip was classified *from* that output, so erasing it leaves a verdict
    nobody can re-derive from the evidence.
    """
    runner = ScriptedRunner([result(exit_code=100, stderr=NO_BASELINE_OUTPUT)])
    ctx = make_context(tmp_path, runner)

    tool_result = SemverChecksTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    kept = Path(tool_result.stdout_path).read_text(encoding="utf-8")
    kept += Path(tool_result.stderr_path).read_text(encoding="utf-8")
    assert "not found in registry" in kept, (
        "the raw output that justified the skip was overwritten by the reason"
    )
    reason = ctx.evidence_dir / "tools" / "semver-checks" / "skip.txt"
    assert reason.is_file()
    assert "baseline" in reason.read_text(encoding="utf-8")
    assert tool_result.environment["skip_reason_path"] == str(reason)


def test_skip_without_running_records_the_reason_as_the_log(tmp_path) -> None:
    """A tool that skipped before running anything has no output to keep, so the
    reason is all there is to log."""
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoVetTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == [], "the skip has to happen before any command runs"
    reason = ctx.evidence_dir / "tools" / "vet" / "skip.txt"
    assert "supply-chain/config.toml" in reason.read_text(encoding="utf-8")
    assert (
        Path(tool_result.stdout_path).read_text(encoding="utf-8").strip()
        == tool_result.summary
    )


def test_semver_real_violation_is_findings_even_with_cannot_in_lint_name(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=100, stdout=VIOLATION_OUTPUT)])
    ctx = make_context(tmp_path, runner)

    tool_result = SemverChecksTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["semver-violation"]
