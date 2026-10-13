"""End-to-end behavioral verification against the binary cargo just built.

Static heuristics cannot tell a wired feature from a placeholder: both compile,
both read well, and both pass ``cargo test`` when the tests are weak. The only
distinguishing evidence is execution. This tool closes that gap by building the
workspace with the real toolchain and driving the produced executable through
its public interface, comparing what the process *actually* did (exit code,
stdout, stderr, files written) against what the project *declared* it should do
(``[[e2e.scenarios]]`` in ``veyra.toml``).

Honesty rules that shape every branch below:

- A scenario that asserts nothing observable is a finding, not a pass (``E2E00``).
  Otherwise a binary that merely launches would satisfy the whole suite.
- No declared scenarios means SKIPPED, never OK. Silence is not evidence.
- A timeout is INCONCLUSIVE. It is neither a pass nor a failure.
- Only an executable cargo produced in this run is executed, never a path taken
  on faith, and never through a shell.
- Every expectation failure records the value that was observed, so the finding
  can be re-checked without rerunning the audit.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from veyra_proof.config import E2eScenario
from veyra_proof.models import DetectionResult, Finding, Severity
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso

RULE_VACUOUS = "E2E00"
RULE_EXIT = "E2E01"
RULE_OUTPUT = "E2E02"
RULE_REACH = "E2E03"
RULE_CONFIG = "E2ECFG"

# Lints that mean "this code is compiled but nothing calls it". They are
# reported as informational context only: they prove nothing on their own, and
# treating them as a verdict would condemn every library crate.
REACHABILITY_LINTS = {"dead_code", "unreachable_code"}

OBSERVED_CAP = 2000
"""Bytes of a stream kept in evidence. Enough to re-check, bounded by design."""


def build_command(context: AuditContext) -> list[str]:
    e2e = context.config.e2e
    cmd = ["cargo", "build", "--message-format", "json"]
    if e2e.build_profile == "release":
        cmd.append("--release")
    if e2e.no_default_features:
        cmd.append("--no-default-features")
    for feature in e2e.features:
        cmd.extend(["--features", feature])
    return cmd


def _excerpt(text: str, limit: int = OBSERVED_CAP) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit} chars)"


def _in_workspace(root: Path, file_name: str) -> bool:
    """Diagnostics from dependencies are not this workspace's business."""
    if not file_name or Path(file_name).is_absolute():
        return False
    try:
        (root / file_name).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def parse_build_output(text: str, root: Path) -> tuple[dict[str, str], list[Finding]]:
    """Split cargo's JSONL stream into built executables and reachability notes."""
    executables: dict[str, str] = {}
    findings: list[Finding] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reason = message.get("reason")
        if reason == "compiler-artifact":
            target = message.get("target") or {}
            kinds = target.get("kind") or []
            executable = message.get("executable")
            name = target.get("name") or ""
            if "bin" in kinds and executable and name:
                executables[name] = executable
        elif reason == "compiler-message":
            inner = message.get("message") or {}
            if inner.get("level") != "warning":
                continue
            code = inner.get("code")
            lint = code.get("code") if isinstance(code, dict) else code
            if lint not in REACHABILITY_LINTS:
                continue
            spans = inner.get("spans") or []
            primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else {})
            file_name = primary.get("file_name") or ""
            if not _in_workspace(root, file_name):
                continue
            findings.append(
                Finding(
                    rule=RULE_REACH,
                    severity="info",
                    confidence="medium",
                    file=file_name,
                    line=primary.get("line_start"),
                    excerpt=(inner.get("message") or "")[:200],
                    explanation=(
                        f"rustc reports `{lint}` in this workspace's build of the shipped "
                        "binary. Informational only: it shows the item was compiled and "
                        "nothing in this configuration calls it. The end-to-end scenarios "
                        "below, not this lint, are what demonstrate behavior."
                    ),
                    recommendation=(
                        "Confirm the item is reachable from a real entry point, or remove it."
                    ),
                )
            )
    return executables, findings


def scenario_asserts_observable(scenario: E2eScenario) -> bool:
    return bool(
        scenario.expect_failure
        or scenario.expect_exit not in (None, 0)
        or scenario.expect_stdout_regex
        or scenario.expect_stderr_regex
        or scenario.expect_files
    )


def _scenario_env(sandbox: Path) -> dict[str, str]:
    """Point every user/config directory at the sandbox.

    A binary under test that reads ``~/.config`` must not read the auditor's.
    """
    home = sandbox / "home"
    tmp = sandbox / "tmp"
    config_home = sandbox / "xdg-config"
    cache_home = sandbox / "xdg-cache"
    for path in (home, tmp, config_home, cache_home):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_CACHE_HOME": str(cache_home),
    }


def run_scenario(
    context: AuditContext,
    executable: Path,
    scenario: E2eScenario,
) -> dict[str, Any]:
    """Execute one declared journey and compare with what was observed."""
    sandbox = Path(tempfile.mkdtemp(prefix="veyra-e2e-"))
    argv = [str(executable), *scenario.argv]
    try:
        result = context.runner.run(
            argv,
            cwd=sandbox,
            timeout=float(context.config.e2e.timeout_seconds),
            env=_scenario_env(sandbox),
            input_text=scenario.stdin or None,
        )
        findings = evaluate_scenario(scenario, result, sandbox)
        observed = {
            "name": scenario.name,
            "argv": argv[1:],
            "observed_exit": result.exit_code,
            "expect_exit": scenario.expect_exit,
            "expect_failure": scenario.expect_failure,
            "duration_seconds": round(result.duration_seconds, 4),
            "timed_out": result.timed_out,
            "error": result.error,
            "observed_stdout": result.stdout,
            "observed_stderr": result.stderr,
            "files_after": sorted(
                str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file()
            ),
        }
        observed["findings"] = [f.rule for f in findings]
        return {"observed": observed, "findings": findings}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def evaluate_scenario(
    scenario: E2eScenario, result: Any, sandbox: Path
) -> list[Finding]:
    """Pure comparison of declared expectations against one observed run."""
    findings: list[Finding] = []

    def add(rule: str, severity: Severity, explanation: str, recommendation: str) -> None:
        findings.append(
            Finding(
                rule=rule,
                severity=severity,
                confidence="high",
                file="veyra.toml",
                line=None,
                excerpt=scenario.name,
                explanation=explanation,
                recommendation=recommendation,
            )
        )

    if result.timed_out:
        # Not a pass and not a failure: the verdict does not exist yet.
        return findings

    if result.exit_code is None:
        add(
            RULE_EXIT,
            "error",
            f"Scenario '{scenario.name}' could not be executed: {result.error or 'no exit code'}.",
            "Verify the binary runs on this platform and that the declared argv is valid.",
        )
        return findings

    if scenario.expect_failure:
        if scenario.expect_exit is None:
            if result.exit_code == 0:
                add(
                    RULE_EXIT,
                    "error",
                    f"Scenario '{scenario.name}' expects a non-zero exit for invalid input or a "
                    f"failed dependency, but the binary exited 0. Observed exit=0, "
                    f"stdout={_excerpt(result.stdout)!r}.",
                    "Make the failure path report the error and exit non-zero instead of "
                    "succeeding silently.",
                )
        elif result.exit_code != scenario.expect_exit:
            add(
                RULE_EXIT,
                "error",
                f"Scenario '{scenario.name}' expects exit={scenario.expect_exit} on failure but "
                f"the binary exited {result.exit_code}. Observed "
                f"stderr={_excerpt(result.stderr)!r}.",
                "Reconcile the binary's failure exit code with the declared scenario.",
            )
    else:
        expected = 0 if scenario.expect_exit is None else scenario.expect_exit
        if result.exit_code != expected:
            add(
                RULE_EXIT,
                "error",
                f"Scenario '{scenario.name}' expects exit={expected} but the binary "
                f"exited {result.exit_code}. Observed stderr={_excerpt(result.stderr)!r}.",
                "Reconcile the binary's exit behavior with the declared scenario, or correct "
                "the declaration if the behavior is intended.",
            )

    if scenario.expect_stdout_regex and not re.search(
        scenario.expect_stdout_regex, result.stdout, re.MULTILINE
    ):
        add(
            RULE_OUTPUT,
            "error",
            f"Scenario '{scenario.name}' expected stdout to match "
            f"{scenario.expect_stdout_regex!r}; observed {_excerpt(result.stdout)!r}.",
            "Fix the feature so its observable output matches the declared contract.",
        )

    if scenario.expect_stderr_regex and not re.search(
        scenario.expect_stderr_regex, result.stderr, re.MULTILINE
    ):
        add(
            RULE_OUTPUT,
            "error",
            f"Scenario '{scenario.name}' expected stderr to match "
            f"{scenario.expect_stderr_regex!r}; observed {_excerpt(result.stderr)!r}.",
            "Fix the error path so it reports the failure it declares.",
        )

    for relative in scenario.expect_files:
        candidate = (sandbox / relative).resolve()
        if not candidate.is_relative_to(sandbox.resolve()):
            add(
                RULE_CONFIG,
                "error",
                f"Scenario '{scenario.name}' declares expect_files entry {relative!r} outside "
                "the scenario working directory.",
                "Declare paths relative to the scenario's working directory.",
            )
            continue
        if not candidate.exists():
            add(
                RULE_OUTPUT,
                "error",
                f"Scenario '{scenario.name}' expected file {relative!r} to exist after the run; "
                "it was not created.",
                "Make the feature write the artifact the scenario declares, or correct "
                "the scenario.",
            )

    if (
        scenario.expect_failure
        and not scenario.expect_stderr_regex
        and not result.stderr.strip()
    ):
        add(
            RULE_OUTPUT,
            "warning",
            f"Scenario '{scenario.name}' failed with exit={result.exit_code} but wrote nothing "
            "to stderr. A silent failure is observable but unexplained.",
            "Report why the run failed on stderr so operators can act on it.",
        )

    return findings


def _log_lines(executable: Path, digest: str, outcomes: list[dict[str, Any]]) -> str:
    """Evidence a human can re-check without rerunning the audit."""
    lines = [f"binary: {executable} (sha256={digest})"]
    for outcome in outcomes:
        observed = outcome["observed"]
        failed = len([f for f in outcome["findings"] if f.severity != "info"])
        if observed["timed_out"]:
            verdict = "INCONCLUSIVE (timed out)"
        elif failed:
            verdict = f"FAILED ({failed} expectation(s) not met)"
        else:
            verdict = "PASS"
        lines.append(
            'SCENARIO name={name!r} argv={argv!r} observed_exit={observed_exit} '
            "expect_exit={expect_exit} expect_failure={expect_failure} "
            "duration={duration_seconds}s verdict={verdict}".format(
                name=observed["name"],
                argv=observed["argv"],
                observed_exit=observed["observed_exit"],
                expect_exit=(
                    "non-zero"
                    if observed["expect_exit"] is None and observed["expect_failure"]
                    else (0 if observed["expect_exit"] is None else observed["expect_exit"])
                ),
                expect_failure=observed["expect_failure"],
                duration_seconds=observed["duration_seconds"],
                verdict=verdict,
            )
        )
        lines.append(f"    observed_stdout: '{_excerpt(observed['observed_stdout'])}'")
        lines.append(f"    observed_stderr: '{_excerpt(observed['observed_stderr'])}'")
        if observed.get("files_after"):
            lines.append(f"    files_after: {observed['files_after']}")
    return "\n".join(lines) + "\n"


class E2eTool(Tool):
    id = "veyra-e2e"
    display_name = "veyra-e2e"
    homepage = ""
    license = "MIT OR Apache-2.0"
    command = "veyra-e2e"
    version_command = []
    install_method = "none"
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo"]
    open_source = True

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        return DetectionResult(
            self.id,
            "INSTALLED",
            True,
            version="builtin",
            message="Built-in: builds the workspace and drives the produced binary",
        )

    def install_plan(self, context: AuditContext):
        return None

    def planned_commands(self, context: AuditContext):
        e2e = context.config.e2e
        if e2e.errors or not e2e.scenarios:
            return []
        return [self.cargo_command(context, build_command(context))] + [
            ["<built-binary>", *scenario.argv] for scenario in e2e.scenarios
        ]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        clock = time.perf_counter()
        e2e = context.config.e2e
        command = self.cargo_command(context, build_command(context))

        findings: list[Finding] = [
            Finding(
                rule=RULE_CONFIG,
                severity="error",
                confidence="high",
                file="veyra.toml",
                line=None,
                excerpt=error[:200],
                explanation=error,
                recommendation="Fix the [e2e] configuration; invalid declarations are never "
                "silently ignored, because that would report a green audit for scenarios "
                "that never ran.",
            )
            for error in e2e.errors
        ]

        if not e2e.scenarios:
            if findings:
                return self._finish(
                    context,
                    started,
                    command,                    elapsed=time.perf_counter() - clock,
                    findings=findings,
                    log=_log_lines(Path("(not built)"), "-", []),
                    build_stderr="",
                    exit_code=1,
                    status="FINDINGS",
                    summary=f"{len(findings)} [e2e] configuration defect(s); no scenario could run",
                    severity="error",
                )
            return self.skipped(
                context,
                "no [[e2e.scenarios]] declared in veyra.toml; end-to-end behavior "
                "was not verified (this is not a pass)",
                skip_kind="intentional",
            )

        build = context.runner.run(command, cwd=context.root, timeout=context.timeout_seconds)
        executables, reach_findings = parse_build_output(build.stdout, context.root)
        findings.extend(reach_findings)

        if build.timed_out:
            return self._finish(
                context,
                started,
                command,                elapsed=time.perf_counter() - clock,
                findings=findings,
                log=f"build timed out after {build.duration_seconds:.1f}s\n",
                build_stderr=_excerpt(build.stderr),
                exit_code=1,
                status="INCONCLUSIVE",
                summary="cargo build timed out; no binary was verified",
                severity="warning",
            )

        if build.exit_code != 0:
            findings.append(
                Finding(
                    rule=RULE_CONFIG,
                    severity="error",
                    confidence="high",
                    file=None,
                    line=None,
                    excerpt=(build.stderr or "")[:200] or None,
                    explanation=(
                        f"cargo build exited {build.exit_code}; the binary the scenarios would "
                        "exercise does not exist. Nothing was verified end to end."
                    ),
                    recommendation="Make the workspace build in the declared profile before "
                    "declaring end-to-end scenarios.",
                )
            )
            return self._finish(
                context,
                started,
                command,                elapsed=time.perf_counter() - clock,
                findings=findings,
                log=_log_lines(Path("(build failed)"), "-", []),
                build_stderr=_excerpt(build.stderr),
                exit_code=1,
                status="ERROR",
                summary=f"cargo build failed with exit {build.exit_code}",
                severity="error",
            )

        if not executables:
            return self._finish(
                context,
                started,
                command,                elapsed=time.perf_counter() - clock,
                findings=findings,
                log=_log_lines(Path("(no binary produced)"), "-", []),
                build_stderr=_excerpt(build.stderr),
                exit_code=1,
                status="INCONCLUSIVE",
                summary="the workspace builds no binary targets; declared scenarios cannot "
                "be executed",
                severity="warning",
            )

        if e2e.binaries:
            absent = [name for name in e2e.binaries if name not in executables]
            if absent:
                findings.append(
                    Finding(
                        rule=RULE_CONFIG,
                        severity="error",
                        confidence="high",
                        file="veyra.toml",
                        line=None,
                        excerpt=", ".join(absent),
                        explanation=(
                            f"[e2e].binaries declares {absent!r} but the build produced "
                            f"{sorted(executables)!r}."
                        ),
                        recommendation="Declare a binary target the build actually produces.",
                    )
                )
                return self._finish(
                    context,
                    started,
                    command,                    elapsed=time.perf_counter() - clock,
                    findings=findings,
                    log=_log_lines(Path("(declared binary missing)"), "-", []),
                    build_stderr=_excerpt(build.stderr),
                    exit_code=1,
                    status="FINDINGS",
                    summary="declared [e2e].binaries were not produced by the build",
                    severity="error",
                )
            target_name = e2e.binaries[0]
        else:
            target_name = sorted(executables)[0]

        executable = Path(executables[target_name])
        digest = _sha256(executable)

        outcomes: list[dict[str, Any]] = []
        for scenario in e2e.scenarios:
            if not scenario_asserts_observable(scenario):
                findings.append(
                    Finding(
                        rule=RULE_VACUOUS,
                        severity="warning",
                        confidence="high",
                        file="veyra.toml",
                        line=None,
                        excerpt=scenario.name,
                        explanation=(
                            f"Scenario '{scenario.name}' declares no observable expectation "
                            "(exit 0 with no stdout/stderr pattern, no file effect and no "
                            "failure path). It would pass for a binary that only starts."
                        ),
                        recommendation="Assert output, a file effect, or a failure mode so the "
                        "scenario observes behavior.",
                    )
                )
            outcome = run_scenario(context, executable, scenario)
            outcomes.append(outcome)
            findings.extend(outcome["findings"])

        timed_out = [o["observed"]["name"] for o in outcomes if o["observed"]["timed_out"]]
        failed = [
            o["observed"]["name"]
            for o in outcomes
            if any(f.severity != "info" for f in o["findings"])
        ]
        passed = len(outcomes) - len(failed) - len(timed_out)

        log = _log_lines(executable, digest, outcomes)
        summary_scenarios = (
            f"{len(outcomes)} scenario(s) executed against {target_name}: "
            f"{passed} passed, {len(failed)} failed"
        )
        if timed_out:
            summary_scenarios += f", {len(timed_out)} timed out ({', '.join(timed_out)})"

        visible = [f for f in findings if f.severity != "info"]
        if timed_out and not visible:
            status, severity = "INCONCLUSIVE", "warning"
            summary = summary_scenarios + "; timed-out scenarios have no verdict"
        elif visible:
            status = "FINDINGS"
            severity = max(
                (f.severity for f in visible),
                key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
            )
            summary = summary_scenarios + "; expectations not met, see findings"
        else:
            status, severity = "OK", "info"
            summary = summary_scenarios

        return self._finish(
            context,
            started,
            command,            elapsed=time.perf_counter() - clock,
            findings=findings,
            log=log,
            build_stderr=_excerpt(build.stderr),
            exit_code=0 if status == "OK" else 1,
            status=status,
            summary=summary,
            severity=severity,
            binary=executable,
            digest=digest,
            target_name=target_name,
            outcomes=outcomes,
        )

    def _finish(
        self,
        context: AuditContext,
        started: str,
        command: list[str],
        *,
        findings: list[Finding],
        log: str,
        build_stderr: str,
        exit_code: int,
        status: str,
        summary: str,
        severity: Severity,
        binary: Path | None = None,
        digest: str | None = None,
        target_name: str | None = None,
        outcomes: list[dict[str, Any]] | None = None,
        elapsed: float | None = None,
    ):
        out, err = self.write_logs(context, log, build_stderr)
        environment: dict[str, str] = {
            "evidence_kind": "e2e",
            "build_profile": context.config.e2e.build_profile,
            "scenarios_declared": str(len(context.config.e2e.scenarios)),
            "scenarios_executed": str(len(outcomes or [])),
        }
        if target_name:
            environment["binary"] = target_name
        artifact_hashes = {str(binary): digest} if binary and digest else {}
        tr = self.make_result(
            status=status,  # type: ignore[arg-type]
            severity=severity,
            summary=summary,
            started_at=started,
            command=command,
            exit_code=exit_code,
            # This tool compiles the workspace and then executes every scenario.
            # Reporting zero would hide the single most expensive step of the run.
            duration_seconds=elapsed if elapsed is not None else 0.0,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
            environment=environment,
            artifact_hashes=artifact_hashes,
        )
        self.save_result_json(context, tr)
        return tr


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
