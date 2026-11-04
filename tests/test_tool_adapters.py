"""Failure-path tests for the adapters the live audit never reaches.

The live audit exercises the quick/standard tools on a clean crate. Fuzz, Miri,
Kani, llvm-cov, mutants, machete, deny, dylint, vet, external types and the
optional SAST adapters are only selected by ``hardened`` or explicit
configuration, so their timeout, non-zero-exit and skip paths were untested.
Those are exactly the paths where "the tool failed" can turn into an ``OK``.
"""

from __future__ import annotations

from pathlib import Path

from veyra_proof.config import (
    CodeQLConfig,
    CoverageConfig,
    FuzzConfig,
    KaniConfig,
    MutantsConfig,
    PolicyConfig,
    VeyraConfig,
)
from veyra_proof.models import CommandResult, PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.tools.base import (
    AuditContext,
    parse_ignored_tests,
    parse_test_failures,
)
from veyra_proof.tools.cargo_test import CargoTestTool
from veyra_proof.tools.check_external_types import CheckExternalTypesTool
from veyra_proof.tools.deny import CargoDenyTool, parse_deny_output
from veyra_proof.tools.dylint import DylintTool
from veyra_proof.tools.fuzz import CargoFuzzTool
from veyra_proof.tools.kani import KaniTool
from veyra_proof.tools.llvm_cov import LlvmCovTool
from veyra_proof.tools.machete import CargoMacheteTool
from veyra_proof.tools.miri import MiriTool
from veyra_proof.tools.mutants import CargoMutantsTool
from veyra_proof.tools.sast_optional import CodeQLTool, SemgrepTool
from veyra_proof.tools.vet import CargoVetTool


class ScriptedRunner:
    """Records every command and replays canned results in order."""

    def __init__(self, results: list[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def run(self, command, cwd=None, timeout=None, input_text=None) -> CommandResult:
        self.calls.append(list(command))
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def result(
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    *,
    timed_out: bool = False,
    error: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=["cargo"],
        cwd=".",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        timed_out=timed_out,
        error=error,
    )


def _workspace(root: Path, packages: list[dict] | None = None) -> WorkspaceInfo:
    return WorkspaceInfo(
        path=str(root),
        workspace_root=str(root),
        packages=packages
        if packages is not None
        else [
            {
                "name": "demo",
                "id": "demo-id",
                "manifest_path": str(root / "Cargo.toml"),
                "targets": [{"kind": ["lib"]}],
            }
        ],
        workspace_members=[],
        target_directory=str(root / "target"),
        default_members=[],
        root_package=None,
        has_lockfile=False,
        lockfile_path=None,
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
    packages: list[dict] | None = None,
    fuzz: FuzzConfig | None = None,
    kani: KaniConfig | None = None,
    mutants: MutantsConfig | None = None,
    coverage: CoverageConfig | None = None,
    codeql: CodeQLConfig | None = None,
    include: list[str] | None = None,
) -> AuditContext:
    return AuditContext(
        workspace=_workspace(tmp_path, packages),
        platform=_platform(),
        config=VeyraConfig(
            policy=PolicyConfig(),
            fuzz=fuzz or FuzzConfig(),
            kani=kani or KaniConfig(),
            mutants=mutants or MutantsConfig(),
            coverage=coverage or CoverageConfig(),
            codeql=codeql or CodeQLConfig(),
        ),
        runner=runner,
        evidence_dir=tmp_path / "evidence",
        profile="hardened",
        timeout_seconds=60.0,
        include=include or [],
    )


# --- cargo-fuzz -----------------------------------------------------------


def test_fuzz_without_a_fuzz_directory_skips_before_running(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner, fuzz=FuzzConfig(targets=["parse"]))

    tool_result = CargoFuzzTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_fuzz_without_declared_targets_refuses_to_invent_them(tmp_path) -> None:
    (tmp_path / "fuzz").mkdir()
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner, fuzz=FuzzConfig(targets=[]))

    tool_result = CargoFuzzTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert "refusing to invent" in tool_result.summary
    assert runner.calls == []


def test_fuzz_crash_is_a_blocker(tmp_path) -> None:
    (tmp_path / "fuzz").mkdir()
    runner = ScriptedRunner(
        [result(exit_code=1, stderr="ERROR: libFuzzer: deadly signal\n")]
    )
    ctx = make_context(tmp_path, runner, fuzz=FuzzConfig(targets=["parse"]))

    tool_result = CargoFuzzTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert tool_result.severity == "blocker"
    assert [f.rule for f in tool_result.findings] == ["fuzz-crash"]


def test_fuzz_timeout_is_inconclusive(tmp_path) -> None:
    (tmp_path / "fuzz").mkdir()
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner, fuzz=FuzzConfig(targets=["parse"]))

    tool_result = CargoFuzzTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"
    assert tool_result.findings == []


def test_fuzz_target_that_does_not_exist_is_skipped(tmp_path) -> None:
    (tmp_path / "fuzz").mkdir()
    runner = ScriptedRunner(
        [result(exit_code=1, stderr="error: target `parse` does not exist\n")]
    )
    ctx = make_context(tmp_path, runner, fuzz=FuzzConfig(targets=["parse"]))

    tool_result = CargoFuzzTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert "does not exist" in tool_result.summary


# --- kani -----------------------------------------------------------------


def test_kani_without_harnesses_skips_before_running(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner)

    tool_result = KaniTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_kani_counterexample_is_findings(tmp_path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        "#[kani::proof]\nfn proof() {}\n", encoding="utf-8"
    )
    runner = ScriptedRunner(
        [result(exit_code=1, stdout="Failed Checks: assertion failed\n")]
    )
    ctx = make_context(tmp_path, runner)

    tool_result = KaniTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["kani-counterexample"]


def test_kani_timeout_is_inconclusive(tmp_path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        "#[kani::proof]\nfn proof() {}\n", encoding="utf-8"
    )
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = KaniTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- cargo-llvm-cov -------------------------------------------------------


def test_llvm_cov_below_the_configured_minimum_is_findings(tmp_path) -> None:
    runner = ScriptedRunner(
        [result(), result(stdout="lines......: 50.00%\nbranches...: 40.00%\n")]
    )
    ctx = make_context(
        tmp_path, runner, coverage=CoverageConfig(minimum_line_percent=90.0)
    )

    tool_result = LlvmCovTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["coverage-line-below-minimum"]


def test_llvm_cov_unparsed_summary_is_not_silent(tmp_path) -> None:
    runner = ScriptedRunner([result(), result(stdout="no percentages here\n")])
    ctx = make_context(
        tmp_path, runner, coverage=CoverageConfig(minimum_line_percent=90.0)
    )

    tool_result = LlvmCovTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert tool_result.severity == "warning"
    assert [f.rule for f in tool_result.findings] == ["coverage-line-unparsed"]


def test_llvm_cov_uses_nextest_when_it_is_available(tmp_path) -> None:
    runner = ScriptedRunner([result(), result(stdout="lines: 100%\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = LlvmCovTool().run(ctx)

    assert runner.calls[1] == [
        "cargo",
        "llvm-cov",
        "nextest",
        "--workspace",
        "--summary-only",
    ]
    assert tool_result.status == "OK"


def test_llvm_cov_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(), result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = LlvmCovTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- cargo-mutants --------------------------------------------------------


def test_mutants_without_paths_skips_before_running(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner, mutants=MutantsConfig(paths=[]))

    tool_result = CargoMutantsTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_mutants_survivor_is_findings(tmp_path) -> None:
    runner = ScriptedRunner([result(stdout="MISSED  src/lib.rs:12:5\n")])
    ctx = make_context(
        tmp_path, runner, mutants=MutantsConfig(paths=["src/lib.rs"])
    )

    tool_result = CargoMutantsTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["mutant-survived"]


def test_mutants_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(
        tmp_path, runner, mutants=MutantsConfig(paths=["src/lib.rs"])
    )

    tool_result = CargoMutantsTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- cargo-machete --------------------------------------------------------


def test_machete_non_zero_exit_is_a_warning_finding(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=1, stdout="unused: serde\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoMacheteTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert tool_result.severity == "warning"
    assert [f.rule for f in tool_result.findings] == ["unused-dependency"]


def test_machete_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoMacheteTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


def test_machete_detection_falls_back_to_help_when_version_is_rejected(tmp_path) -> None:
    runner = ScriptedRunner(
        [
            result(exit_code=2, stderr="error: unexpected argument '--version' found\n"),
            result(exit_code=0, stdout="Usage: cargo machete\n"),
        ]
    )
    ctx = make_context(tmp_path, runner)

    detection = CargoMacheteTool().detect(ctx)

    assert detection.status == "INSTALLED"
    assert detection.version == "unknown"
    assert runner.calls == [
        ["cargo", "machete", "--version"],
        ["cargo", "machete", "--help"],
    ]


# --- miri -----------------------------------------------------------------


def test_miri_error_is_findings(tmp_path) -> None:
    runner = ScriptedRunner(
        [result(exit_code=1, stderr="error: Undefined Behavior: out-of-bounds\n")]
    )
    ctx = make_context(tmp_path, runner)

    tool_result = MiriTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["miri-ub"]


def test_miri_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = MiriTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- cargo-deny -----------------------------------------------------------


def test_deny_diagnostics_are_parsed_into_findings(tmp_path) -> None:
    runner = ScriptedRunner(
        [result(exit_code=1, stdout="error[unlicensed]: missing license\n")]
    )
    ctx = make_context(tmp_path, runner)

    tool_result = CargoDenyTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["deny:unlicensed"]
    assert tool_result.findings[0].file == "Cargo.toml"


def test_deny_failure_without_diagnostics_still_reports_a_finding(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=1, stderr="some unrelated failure\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoDenyTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["cargo-deny-violation"]


def test_deny_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoDenyTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- dylint / vet / external types ---------------------------------------


def test_dylint_without_configuration_skips_before_running(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner)

    tool_result = DylintTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_dylint_findings_are_reported(tmp_path) -> None:
    (tmp_path / "dylint.toml").write_text("", encoding="utf-8")
    runner = ScriptedRunner([result(exit_code=1, stderr="lint fired\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = DylintTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["dylint-finding"]


def test_vet_findings_are_reported(tmp_path) -> None:
    supply_chain = tmp_path / "supply-chain"
    supply_chain.mkdir()
    (supply_chain / "config.toml").write_text("", encoding="utf-8")
    runner = ScriptedRunner([result(exit_code=1, stdout="unreviewed crate\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoVetTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert [f.rule for f in tool_result.findings] == ["vet-unreviewed"]


def test_external_types_without_libraries_skips_before_running(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(
        tmp_path,
        runner,
        packages=[
            {
                "name": "demo",
                "id": "demo-id",
                "manifest_path": str(tmp_path / "Cargo.toml"),
                "targets": [{"kind": ["bin"]}],
            }
        ],
    )

    tool_result = CheckExternalTypesTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_external_types_timeout_is_inconclusive(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=None, timed_out=True)])
    ctx = make_context(tmp_path, runner)

    tool_result = CheckExternalTypesTool().run(ctx)

    assert tool_result.status == "INCONCLUSIVE"


# --- optional SAST --------------------------------------------------------


def test_semgrep_is_never_run_without_explicit_inclusion(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner)

    tool_result = SemgrepTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_semgrep_refuses_to_run_without_local_rules(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner, include=["semgrep"])

    tool_result = SemgrepTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert "local Semgrep config" in tool_result.summary
    assert runner.calls == []


def test_semgrep_failure_is_not_ok(tmp_path) -> None:
    (tmp_path / ".semgrep.yml").write_text("rules: []\n", encoding="utf-8")
    runner = ScriptedRunner([result(exit_code=1, stderr="error: rule failed\n")])
    ctx = make_context(tmp_path, runner, include=["semgrep"])

    tool_result = SemgrepTool().run(ctx)

    assert tool_result.status == "ERROR"


def test_codeql_is_never_run_without_explicit_inclusion(tmp_path) -> None:
    runner = ScriptedRunner([result()])
    ctx = make_context(tmp_path, runner)

    tool_result = CodeQLTool().run(ctx)

    assert tool_result.status == "SKIPPED"
    assert runner.calls == []


def test_codeql_runs_against_a_local_database_when_configured(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=0)])
    ctx = make_context(
        tmp_path,
        runner,
        include=["codeql"],
        codeql=CodeQLConfig(database="db", query_suite="suite"),
    )

    tool_result = CodeQLTool().run(ctx)

    assert tool_result.status == "OK"
    assert runner.calls[0][:5] == [
        "codeql",
        "database",
        "analyze",
        "db",
        "suite",
    ]


# --- CRLF output ----------------------------------------------------------


def test_parse_test_failures_reads_crlf_output() -> None:
    assert [f.rule for f in parse_test_failures("test add ... FAILED\r\n")] == [
        "test-failure"
    ]


def test_parse_ignored_tests_reads_crlf_output() -> None:
    assert [f.rule for f in parse_ignored_tests("test slow ... ignored\r\n")] == [
        "ignored-test"
    ]


def test_parse_deny_output_reads_crlf_output() -> None:
    findings = parse_deny_output("error[unlicensed]: missing license\r\n")

    assert [f.rule for f in findings] == ["deny:unlicensed"]
    assert findings[0].explanation == "missing license"


def test_cargo_test_reports_a_failure_captured_with_crlf(tmp_path) -> None:
    runner = ScriptedRunner([result(exit_code=101, stdout="test add ... FAILED\r\n")])
    ctx = make_context(tmp_path, runner)

    tool_result = CargoTestTool().run(ctx)

    assert tool_result.status == "FINDINGS"
    assert tool_result.severity == "error"
    assert "test-failure" in [f.rule for f in tool_result.findings]
