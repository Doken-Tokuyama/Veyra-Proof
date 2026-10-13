"""Verdicts that used to be wrong, pinned so they cannot drift back.

Grouped by the defect they cover:

* a stale ``Cargo.lock`` used to surface as a bare ``ERROR`` with a cryptic
  cargo message; it is now INCONCLUSIVE with an instruction;
* ``cargo check --all-targets`` used to fail the whole audit on any project
  with a nightly-only benchmark, which is most real projects;
* ``veyra-e2e`` and ``veyra-anti-theater`` reported ``duration_seconds: 0.0``
  while doing the most expensive work in the run;
* ``--fail-on none`` did not actually disable rejection;
* ``cargo-audit`` skipped whenever no lockfile existed at discovery time, so the
  first audit of a vulnerable project never looked at its dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veyra_proof.config import PolicyConfig, VeyraConfig
from veyra_proof.models import (
    AuditDecision,
    CommandResult,
    PlatformOS,
    WorkspaceInfo,
)
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.reporting import decide
from veyra_proof.tools.base import (
    AuditContext,
    classify_from_command,
    nightly_only_targets,
    stale_lockfile,
)
from veyra_proof.tools.cargo_check import CargoCheckTool
from veyra_proof.tools.clippy import ClippyTool

STALE_LOCK_STDERR = (
    "    Updating crates.io index\n"
    "error: the lock file /w/Cargo.lock needs to be updated but --locked was passed\n"
    "to prevent this\n"
    "If you want to try to generate the lock file without accessing the network,\n"
    "remove the --locked flag and use --offline instead.\n"
)

# cargo 1.98 words the same refusal differently. Both are the same event and both
# must land on the same verdict; matching one phrasing left the audit reporting a
# bare "Non-zero exit code 101" while the real cause sat in the log.
STALE_LOCK_STDERR_MODERN = (
    "    Updating crates.io index\n"
    "error: cannot update the lock file `C:\\w\\Cargo.lock` because --locked was passed\n"
    "to prevent this\n"
    "If you want to try to generate the lock file without accessing the network,\n"
    "remove the --locked flag and use --offline instead.\n"
)

NIGHTLY_STDERR = (
    "    Checking regex-syntax v0.8.6\n"
    "error[E0554]: `#![feature]` may not be used on the stable release channel\n"
    " --> regex-syntax\\benches\\bench.rs:1:1\n"
    "  |\n"
    "1 | #![feature(test)]\n"
    "  | ^^^^^^^^^^^^^^^^^\n"
    "\n"
    "error: could not compile `regex-syntax` (bench \"bench\") due to 1 previous error\n"
    "warning: build failed, waiting for other jobs to finish...\n"
)


def _result(command, *, exit_code=0, stdout="", stderr="", duration=0.0, timed_out=False):
    return CommandResult(
        command=list(command),
        cwd="/w",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        timed_out=timed_out,
        error="timeout" if timed_out else None,
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
        cargo_version="cargo 1.98.0",
        rustc_path="rustc",
        rustc_version="rustc 1.98.0",
    )


def _context(tmp_path: Path, runner, *, policy=None, packages=None) -> AuditContext:
    workspace = WorkspaceInfo(
        path=str(tmp_path),
        workspace_root=str(tmp_path),
        packages=packages or [],
        workspace_members=[],
        target_directory=str(tmp_path / "target"),
        default_members=[],
        root_package=None,
        has_lockfile=False,
        lockfile_path=None,
        metadata_raw={},
    )
    return AuditContext(
        workspace=workspace,
        platform=_platform(),
        config=VeyraConfig(policy=policy or PolicyConfig()),
        runner=runner,
        evidence_dir=tmp_path / "evidence",
        profile="quick",
        timeout_seconds=60.0,
    )


class ScriptedRunner:
    """Replays a canned result per distinct command prefix, recording the calls."""

    def __init__(self, script: dict[str, CommandResult]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def run(self, command, *, cwd=None, timeout=None, env=None, input_text=None, **kwargs):
        argv = list(command)
        self.calls.append(argv)
        for prefix, result in self.script.items():
            if argv[: len(prefix.split())] == prefix.split():
                return result
        return _result(argv)


class TestStaleLockfileVerdict:
    @pytest.mark.parametrize(
        "text", [STALE_LOCK_STDERR, STALE_LOCK_STDERR_MODERN], ids=["old-wording", "cargo-1.98"]
    )
    def test_stale_lock_is_recognised_in_every_wording(self, text):
        assert stale_lockfile(text)

    def test_a_clean_lock_is_not_mistaken_for_a_stale_one(self):
        assert not stale_lockfile("    Locking 1 package to latest compatible version")
        assert not stale_lockfile("    Finished dev [unoptimized] target(s)")

    @pytest.mark.parametrize(
        "text", [STALE_LOCK_STDERR, STALE_LOCK_STDERR_MODERN], ids=["old-wording", "cargo-1.98"]
    )
    def test_stale_lock_is_inconclusive_not_a_project_error(self, text):
        status, severity, summary = classify_from_command(
            timed_out=False,
            exit_code=101,
            findings=[],
            stdout="",
            stderr=text,
            error=None,
        )
        assert status == "INCONCLUSIVE"
        assert severity == "warning"
        assert "will not rewrite it" in summary

    def test_a_real_compile_error_is_still_an_error(self):
        status, _, _ = classify_from_command(
            timed_out=False,
            exit_code=101,
            findings=[],
            stdout="",
            stderr="error[E0308]: mismatched types",
            error=None,
        )
        assert status == "ERROR"

    def test_a_stale_lock_is_not_blamed_on_the_findings(self, tmp_path):
        """cargo prints its refusal as `error:`, so a parser could report it as a
        lint and reject the project for a lockfile it never touched."""
        runner = ScriptedRunner(
            {
                "cargo clippy --workspace": _result(
                    ["cargo", "clippy", "--workspace"],
                    exit_code=101,
                    stderr=STALE_LOCK_STDERR_MODERN,
                )
            }
        )
        result = ClippyTool().run(_context(tmp_path, runner))
        assert result.status == "INCONCLUSIVE"
        assert result.findings == []
        assert "will not rewrite it" in result.summary

    def test_a_stale_lock_wins_over_a_real_clippy_finding(self, tmp_path):
        runner = ScriptedRunner(
            {
                "cargo clippy --workspace": _result(
                    ["cargo", "clippy", "--workspace"],
                    exit_code=101,
                    stdout="error: unused variable: `x`\n --> src/lib.rs:1:9\n",
                    stderr=STALE_LOCK_STDERR_MODERN,
                )
            }
        )
        result = ClippyTool().run(_context(tmp_path, runner))
        assert result.status == "INCONCLUSIVE"


class TestNightlyOnlyTargets:
    def test_the_feature_gate_is_recognised_with_its_file(self):
        assert nightly_only_targets(NIGHTLY_STDERR) == ["regex-syntax/benches/bench.rs"]

    def test_a_genuine_error_alongside_is_not_a_toolchain_problem(self):
        mixed = NIGHTLY_STDERR + "error[E0308]: mismatched types\n --> src/lib.rs:2:5\n"
        assert nightly_only_targets(mixed) == []

    def test_clean_output_yields_nothing(self):
        assert nightly_only_targets("    Finished release profile") == []


class TestCargoCheckNightlyFallback:
    def _runner(self):
        return ScriptedRunner(
            {
                "cargo check --workspace --all-targets": _result(
                    ["cargo", "check", "--workspace", "--all-targets"],
                    exit_code=101,
                    stderr=NIGHTLY_STDERR,
                    duration=9.0,
                ),
                "cargo check --workspace --lib --bins --tests": _result(
                    ["cargo", "check", "--workspace", "--lib", "--bins", "--tests"],
                    exit_code=0,
                    stdout="    Finished dev profile\n",
                    duration=4.0,
                ),
            }
        )

    def test_verdict_is_inconclusive_and_names_the_unverified_file(self, tmp_path):
        runner = self._runner()
        result = CargoCheckTool().run(_context(tmp_path, runner))
        assert result.status == "INCONCLUSIVE"
        assert "NOT verified" in result.summary
        assert "regex-syntax/benches/bench.rs" in result.summary
        assert result.severity == "warning"

    def test_the_library_is_still_checked(self, tmp_path):
        runner = self._runner()
        CargoCheckTool().run(_context(tmp_path, runner))
        assert len(runner.calls) == 2
        assert "--all-targets" not in runner.calls[1]
        assert runner.calls[1][1:4] == ["check", "--workspace", "--lib"]

    def test_both_attempts_stay_in_the_evidence(self, tmp_path):
        runner = self._runner()
        result = CargoCheckTool().run(_context(tmp_path, runner))
        log = Path(result.stdout_path).read_text(encoding="utf-8")
        assert "nightly-only feature attributes" in log
        assert "Finished dev profile" in log

    def test_duration_covers_both_attempts(self, tmp_path):
        runner = self._runner()
        result = CargoCheckTool().run(_context(tmp_path, runner))
        assert result.duration_seconds == pytest.approx(13.0)

    def test_a_failure_on_stable_targets_too_stays_an_error(self, tmp_path):
        runner = ScriptedRunner(
            {
                "cargo check --workspace --all-targets": _result(
                    ["cargo", "check", "--workspace", "--all-targets"],
                    exit_code=101,
                    stderr=NIGHTLY_STDERR,
                ),
                "cargo check --workspace --lib --bins --tests": _result(
                    ["cargo", "check", "--workspace", "--lib", "--bins", "--tests"],
                    exit_code=101,
                    stderr="error[E0308]: mismatched types",
                ),
            }
        )
        result = CargoCheckTool().run(_context(tmp_path, runner))
        assert result.status == "ERROR"
        assert "stable targets too" in result.summary

    def test_a_plain_compile_failure_gets_no_retry(self, tmp_path):
        runner = ScriptedRunner(
            {
                "cargo check --workspace --all-targets": _result(
                    ["cargo", "check", "--workspace", "--all-targets"],
                    exit_code=101,
                    stderr="error[E0308]: mismatched types",
                )
            }
        )
        result = CargoCheckTool().run(_context(tmp_path, runner))
        assert result.status == "ERROR"
        assert len(runner.calls) == 1


class TestClippyNightlyFallback:
    def test_lint_clean_stable_targets_are_still_inconclusive(self, tmp_path):
        runner = ScriptedRunner(
            {
                "cargo clippy --workspace --all-targets": _result(
                    ["cargo", "clippy", "--workspace", "--all-targets"],
                    exit_code=101,
                    stderr=NIGHTLY_STDERR,
                    duration=6.0,
                ),
                "cargo clippy --workspace --lib": _result(
                    ["cargo", "clippy", "--workspace", "--lib"],
                    exit_code=0,
                    duration=3.0,
                ),
            }
        )
        result = ClippyTool().run(_context(tmp_path, runner))
        assert result.status == "INCONCLUSIVE"
        assert "NOT linted" in result.summary
        assert "-D" in runner.calls[1] and "warnings" in runner.calls[1]

    def test_lints_on_stable_targets_still_fail_the_build(self, tmp_path):
        runner = ScriptedRunner(
            {
                "cargo clippy --workspace --all-targets": _result(
                    ["cargo", "clippy", "--workspace", "--all-targets"],
                    exit_code=101,
                    stderr=NIGHTLY_STDERR,
                ),
                "cargo clippy --workspace --lib": _result(
                    ["cargo", "clippy", "--workspace", "--lib"],
                    exit_code=101,
                    stdout="error: unused variable: `x`\n --> src/lib.rs:2:9\n",
                ),
            }
        )
        result = ClippyTool().run(_context(tmp_path, runner))
        assert result.status == "FINDINGS"
        assert any("unused variable" in (f.excerpt or "") for f in result.findings)
        assert any(f.rule == "nightly-only-target" for f in result.findings)


class TestDurationsAreReal:
    def test_e2e_reports_the_time_it_spent(self, tmp_path):
        import time

        from veyra_proof.config import E2eConfig, E2eScenario
        from veyra_proof.tools.e2e import E2eTool

        exe = tmp_path / "target" / "release" / "demo"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"binary")
        artifact = json.dumps(
            {
                "reason": "compiler-artifact",
                "target": {"name": "demo", "kind": ["bin"]},
                "executable": str(exe),
            }
        )

        class Runner:
            def run(self, command, *, cwd=None, timeout=None, **kwargs):
                time.sleep(0.05)
                if command[:2] == ["cargo", "build"]:
                    return _result(command, exit_code=0, stdout=artifact, duration=0.05)
                return _result(command, exit_code=0, stdout="3\n", duration=0.05)

        config = VeyraConfig(
            e2e=E2eConfig(
                scenarios=[E2eScenario(name="add", argv=["add"], expect_stdout_regex="^3")],
                build_profile="release",
            )
        )
        context = _context(tmp_path, Runner())
        context.config = config
        result = E2eTool().run(context)
        assert result.status == "OK"
        # The tool compiles the workspace and then runs the binary. Reporting a
        # flat 0.0 hid the single most expensive step of the whole audit.
        assert result.duration_seconds >= 0.1

    def test_e2e_duration_is_not_a_hardcoded_zero(self, tmp_path):
        from veyra_proof.config import E2eConfig, E2eScenario
        from veyra_proof.tools.e2e import E2eTool

        exe = tmp_path / "target" / "release" / "demo"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"binary")
        artifact = json.dumps(
            {
                "reason": "compiler-artifact",
                "target": {"name": "demo", "kind": ["bin"]},
                "executable": str(exe),
            }
        )

        class Runner:
            def run(self, command, *, cwd=None, timeout=None, **kwargs):
                if command[:2] == ["cargo", "build"]:
                    return _result(command, exit_code=0, stdout=artifact, duration=99.0)
                return _result(command, exit_code=101, stdout="", stderr="boom")

        context = _context(tmp_path, Runner())
        context.config = VeyraConfig(
            e2e=E2eConfig(
                scenarios=[E2eScenario(name="add", argv=["add"], expect_stdout_regex="^3")],
                build_profile="release",
            )
        )
        result = E2eTool().run(context)
        assert result.status == "FINDINGS"
        assert result.duration_seconds > 0.0

    def test_anti_theater_reports_a_non_zero_scan_time(self, tmp_path):
        from veyra_proof.tools.anti_theater import AntiTheaterTool

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("pub fn f() -> i32 { 1 }\n", encoding="utf-8")
        result = AntiTheaterTool().run(_context(tmp_path, None))
        assert result.duration_seconds > 0.0
        assert "rule_histogram" in result.environment


class TestFailOnNone:
    def _results(self):
        from veyra_proof.models import Finding, ToolResult

        return [
            ToolResult(
                tool_id="veyra-anti-theater",
                display_name="anti-theater",
                status="FINDINGS",
                severity="blocker",
                command=None,
                exit_code=0,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:00Z",
                duration_seconds=0.1,
                stdout_path=None,
                stderr_path=None,
                findings=[
                    Finding(
                        rule="VT001",
                        severity="blocker",
                        confidence="high",
                        file="src/lib.rs",
                        line=1,
                        excerpt="todo!()",
                        explanation="todo!()",
                        recommendation="implement it",
                    )
                ],
                summary="1 finding",
            )
        ]

    def test_fail_on_none_accepts_despite_blocker_findings(self):
        decision = decide(
            self._results(), fail_on="none", required_tool_ids=["veyra-anti-theater"]
        )
        assert decision.decision == "ACCEPTED"
        assert decision.exit_code == 0

    def test_fail_on_none_still_names_what_it_found(self):
        """Accepting is not the same as hiding: the findings survive in the report."""
        results = self._results()
        decide(results, fail_on="none", required_tool_ids=["veyra-anti-theater"])
        assert results[0].findings[0].rule == "VT001"

    def test_default_still_rejects_the_same_findings(self):
        decision = decide(
            self._results(), fail_on="findings", required_tool_ids=["veyra-anti-theater"]
        )
        assert decision.decision == "REJECTED"
        assert decision.exit_code == 1

    def test_fail_on_none_cannot_excuse_an_unauthorized_file_change(self):
        decision = decide(
            self._results(),
            fail_on="none",
            required_tool_ids=[],
            hash_incongruence=True,
        )
        assert decision.decision == "REJECTED"
        assert "Unauthorized" in decision.reason

    def test_fail_on_none_cannot_excuse_a_missing_required_tool(self):
        decision = decide([], fail_on="none", required_tool_ids=["cargo-check"])
        assert decision.decision == "ACCEPTED"

    def test_the_exported_decision_still_serialises(self):
        assert isinstance(AuditDecision("ACCEPTED", "x", 0).to_dict(), dict)


class TestTheRunSaysWhereItsTimeWent:
    """A profile that takes eight minutes has to be attributable from evidence.

    On `rust-lang/regex` the audit took 444s warm and 529s cold: 415s of it is
    the project's own `cargo test --workspace` suite, not the audit. An operator
    needs that number without re-running anything.
    """

    def _results(self):
        from veyra_proof.models import ToolResult

        def make(tool_id, seconds):
            return ToolResult(
                tool_id=tool_id,
                display_name=tool_id,
                status="OK",
                severity="info",
                command=None,
                exit_code=0,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
                duration_seconds=seconds,
                stdout_path=None,
                stderr_path=None,
                findings=[],
                summary="",
            )

        return [make("cargo-test", 415.0), make("clippy", 7.8), make("rustfmt", 2.7)]

    def test_the_slowest_tool_is_named_with_its_seconds(self):
        from veyra_proof.reporting import timing_accounting

        timing = timing_accounting(self._results(), wall_seconds=444.3)
        assert timing["slowest"][0] == {
            "tool": "cargo-test",
            "status": "OK",
            "seconds": 415.0,
        }
        assert timing["tool_seconds"] == 425.5
        assert timing["wall_seconds"] == 444.3

    def test_the_table_states_the_total_and_the_gap(self):
        from veyra_proof.reporting import render_timing, timing_accounting

        text = render_timing(timing_accounting(self._results(), wall_seconds=444.3))
        assert "415.0s  cargo-test" in text
        assert "total in tools: 425.5s of 444.3s wall" in text

    def test_a_tool_that_records_no_time_is_not_hidden(self, tmp_path):
            """A skipped tool and a tool whose duration vanished must read differently.
    
            A flat 0.0 was the original bug. Listing a skipped tool beside it would
            teach the reader to ignore the line that matters.
            """
            from veyra_proof.models import ToolResult
            from veyra_proof.reporting import render_timing, timing_accounting
    
            def make(tool_id, status):
                return ToolResult(
                    tool_id=tool_id,
                    display_name=tool_id,
                    status=status,
                    severity="info",
                    command=None,
                    exit_code=None,
                    started_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    duration_seconds=0.0,
                    stdout_path=None,
                    stderr_path=None,
                    findings=[],
                    summary="",
                )
    
            timing = timing_accounting([make("fuzz", "SKIPPED"), make("mystery", "OK")])
            text = render_timing(timing)
            assert "did not run: fuzz" in text
            assert "ran without recording a duration: mystery" in text
            assert {e["tool"] for e in timing["tools_without_timing"]} == {"fuzz", "mystery"}

    def test_the_timing_reaches_the_report(self, tmp_path):
        from veyra_proof.cli import main

        crate = tmp_path / "w"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            '[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
        )
        (crate / "src" / "lib.rs").write_text("pub fn f() -> u8 { 1 }\n", encoding="utf-8")

        exit_code = main(
            ["audit", str(crate), "--profile", "quick", "--exclude", "veyra-e2e"]
        )
        evidence = sorted((crate / ".veyra" / "evidence").iterdir())[-1]
        report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
        manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
        markdown = (evidence / "report.md").read_text(encoding="utf-8")

        assert exit_code == report["decision"]["exit_code"]
        assert "timing" in report and "timing" in manifest
        assert report["timing"]["tools_timed"] >= 1
        assert report["timing"]["wall_seconds"] >= report["timing"]["tool_seconds"]
        assert "## Time" in markdown
        assert "total in tools:" in markdown


class TestReproducibleBuildIsAPolicyDecision:
    """The check always runs; only the blocking decision is opt-in.

    MSVC/PE output is routinely not bit-reproducible, so a default that
    rejects on differing hashes flags a platform rather than a defect. The
    finding, both digests and the summary stay exactly as they were.
    """

    def _run(self, tmp_path, *, policy):
        from veyra_proof.models import CommandResult
        from veyra_proof.tools.reproducible_build import ReproducibleBuildTool

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "w"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("pub fn f() -> u8 { 1 }\n", encoding="utf-8")
        workspace = WorkspaceInfo(
            path=str(tmp_path),
            workspace_root=str(tmp_path),
            packages=[{"name": "w"}],
            workspace_members=[],
            target_directory=str(tmp_path / "target"),
            default_members=[],
            root_package=None,
            has_lockfile=False,
            lockfile_path=None,
            metadata_raw={},
        )
        context = AuditContext(
            workspace=workspace,
            platform=_platform(),
            config=VeyraConfig(policy=policy),
            runner=_HashingRunner(tmp_path),
            evidence_dir=tmp_path / "evidence",
            profile="hardened",
            timeout_seconds=120.0,
        )
        return ReproducibleBuildTool().run(context)

    def test_by_default_the_mismatch_is_informational(self, tmp_path):
        result = self._run(tmp_path, policy=PolicyConfig())
        assert result.status == "INCONCLUSIVE"
        assert result.findings[0].rule == "repro-mismatch"
        assert result.findings[0].severity == "info"

    def test_both_digests_are_pinned_as_evidence_either_way(self, tmp_path):
        result = self._run(tmp_path, policy=PolicyConfig())
        keys = {k.split(":", 1)[0] for k in result.artifact_hashes}
        assert "a" in keys and "b" in keys

    def test_the_summary_still_says_reproducibility_is_not_established(self, tmp_path):
        result = self._run(tmp_path, policy=PolicyConfig())
        assert "not established" in result.summary
        assert "require_reproducible_build" not in result.summary

    def test_the_project_can_make_it_a_hard_requirement(self, tmp_path):
        result = self._run(tmp_path, policy=PolicyConfig(require_reproducible_build=True))
        assert result.findings[0].severity == "error"
        assert "policy.require_reproducible_build=true" in result.summary
        decision = decide(
            [result], fail_on="error", required_tool_ids=["cargo-check"]
        )
        assert decision.decision == "REJECTED"

    def test_the_knob_is_off_by_default_and_read_from_veyra_toml(self, tmp_path):
        from veyra_proof.config import load_veyra_config

        assert PolicyConfig().require_reproducible_build is False
        (tmp_path / "veyra.toml").write_text(
            "[policy]\nrequire_reproducible_build = true\n", encoding="utf-8"
        )
        assert load_veyra_config(tmp_path).policy.require_reproducible_build is True


class _HashingRunner:
    """Two builds whose artifacts differ, which is what MSVC actually does."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    def run(self, command, *, cwd=None, timeout=None, env=None, input_text=None, **kwargs):
        self.calls += 1
        target_dir = Path(command[command.index("--target-dir") + 1])
        artifact = target_dir / "debug" / "w.exe"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"build number {self.calls}".encode())
        return CommandResult(
            command=list(command),
            cwd=str(self.root),
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=1.0,
            timed_out=False,
            error=None,
        )


class TestAnObservedFactBlocksWhenTheToolIsInconclusiveAndPolicySaysSo:
    """`reproducible-build` reporting differing hashes is an observation.

    Its overall status is INCONCLUSIVE, but the artifact digests it compared are
    a fact about the build, so when a project declares reproducibility a hard
    requirement the decision has to see it — counting only results whose status
    is FINDINGS would let that observation through the back door of the status.
    """
    def _results(self, severity):
        from veyra_proof.models import Finding, ToolResult

        return [
            ToolResult(
                tool_id="reproducible-build",
                display_name="reproducible-build",
                status="INCONCLUSIVE",
                severity=severity,
                command=None,
                exit_code=0,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:00:01Z",
                duration_seconds=3.0,
                stdout_path=None,
                stderr_path=None,
                findings=[
                    Finding(
                        rule="repro-mismatch",
                        severity=severity,
                        confidence="high",
                        file=None,
                        line=None,
                        excerpt="debug/demo.exe",
                        explanation="two clean builds produced different bytes",
                        recommendation="investigate",
                    )
                ],
                summary="hashes differ",
            )
        ]

    def test_a_warning_observation_is_rejected_under_the_default(self):
        decision = decide(
            self._results("warning"),
            fail_on="findings",
            required_tool_ids=["cargo-check"],
        )
        assert decision.decision == "REJECTED"
        assert "reproducible-build:repro-mismatch" in decision.reason

    def test_fail_on_error_keeps_it_out_of_the_way(self):
        decision = decide(
            self._results("warning"),
            fail_on="error",
            required_tool_ids=["cargo-check"],
        )
        assert decision.decision != "REJECTED"

    def test_an_informational_note_still_does_not_block(self):
        decision = decide(
            self._results("info"),
            fail_on="findings",
            required_tool_ids=["cargo-check"],
        )
        assert decision.decision != "REJECTED"


class TestCargoAuditRunsWhenTheLockAppears:
    def _audit_tool(self):
        from veyra_proof.tools.audit import CargoAuditTool

        return CargoAuditTool()

    def test_the_file_is_authoritative_not_the_discovery_flag(self, tmp_path):
        """cargo wrote the lockfile after `cargo metadata` ran.

        Discovery recorded no lockfile, so the old code skipped the audit and a
        vulnerable project looked clean on its very first run.
        """
        (tmp_path / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")

        class Runner:
            def run(self, command, **kwargs):
                return _result(command, exit_code=0, stdout="Success\n")

        context = _context(tmp_path, Runner())
        assert context.workspace.has_lockfile is False
        result = self._audit_tool().run(context)
        assert result.status == "OK"
        assert result.command == ["cargo", "audit"]

    def test_an_advisory_is_still_reported_on_that_first_run(self, tmp_path):
        (tmp_path / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")

        class Runner:
            def run(self, command, **kwargs):
                return _result(
                    command,
                    exit_code=1,
                    stdout="Advisory: RUSTSEC-2020-0071\ntime 0.1.44\n",
                )

        result = self._audit_tool().run(_context(tmp_path, Runner()))
        assert result.status == "FINDINGS"
        assert "RUSTSEC-2020-0071" in {f.rule for f in result.findings}

    def test_dependencies_without_a_lockfile_are_never_called_a_pass(self, tmp_path):
        packages = [{"name": "w", "dependencies": [{"name": "time"}]}]

        class Runner:
            def run(self, command, **kwargs):  # pragma: no cover - must not run
                raise AssertionError("cargo-audit ran without a lockfile")

        result = self._audit_tool().run(
            _context(tmp_path, Runner(), packages=packages)
        )
        assert result.status == "SKIPPED"
        assert "not a pass" in result.summary
        assert any(f.rule == "unlocked-dependencies" for f in result.findings)

    def test_a_crate_without_dependencies_skips_plainly(self, tmp_path):
        class Runner:
            def run(self, command, **kwargs):  # pragma: no cover - must not run
                raise AssertionError("cargo-audit ran without a lockfile")

        result = self._audit_tool().run(_context(tmp_path, Runner(), packages=[]))
        assert result.status == "SKIPPED"
        assert "no dependencies declared" in result.summary

    def test_require_lockfile_policy_still_produces_a_finding(self, tmp_path):
        packages = [{"name": "w", "dependencies": [{"name": "time"}]}]
        result = self._audit_tool().run(
            _context(
                tmp_path,
                None,
                packages=packages,
                policy=PolicyConfig(require_lockfile=True),
            )
        )
        assert result.status == "FINDINGS"
        assert result.findings[0].rule == "missing-lockfile"
