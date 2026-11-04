"""cargo-semver-checks."""

from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import (
    DetectionResult,
    Finding,
    InstallPlan,
    ResultStatus,
    Severity,
    ToolResult,
)
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import library_package_names

# Signatures of "the baseline does not exist yet" (unpublished crate). Real API
# violations must never match these: lint names may contain words like
# "cannot" (e.g. function_cannot_unwind), so the markers are precise strings.
NO_BASELINE_MARKERS = (
    "not found in registry",
    "failed to retrieve index",
    "no published version",
)

# What cargo-semver-checks prints when it has *concluded* the crate breaks: the
# summary line and the per-lint marker. Any other non-zero exit is the tool
# failing (unknown --baseline-rev, bad flag, no git history), and calling that an
# API break would accuse the crate of something nobody observed.
VIOLATION_MARKERS = (
    "summary semver requires new",
    "--- failure",
)


class SemverChecksTool(Tool):
    id = "semver-checks"
    display_name = "cargo-semver-checks"
    homepage = "https://github.com/obi1kenobi/cargo-semver-checks"
    command = "cargo semver-checks"
    version_command = ["cargo", "semver-checks", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-semver-checks", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        return self.detect_via_version(
            context,
            ["cargo", "semver-checks", "--version"],
            absent_message="cargo-semver-checks not installed",
        )

    def install_plan(self, context: AuditContext) -> InstallPlan | None:
        return cargo_install_plan(self.id, self.display_name, "cargo-semver-checks")

    @staticmethod
    def _command(context: AuditContext) -> list[str]:
        cmd = ["cargo", "semver-checks", "check-release"]
        baseline = context.config.semver.baseline
        if baseline:
            cmd.extend(["--baseline-rev", baseline])
        release_type = context.config.semver.release_type
        if release_type:
            cmd.extend(["--release-type", release_type])
        return cmd

    def planned_commands(self, context: AuditContext) -> list[list[str]]:
        return [self._command(context)]

    def run(self, context: AuditContext) -> ToolResult:
        started = utc_now_iso()
        status: ResultStatus
        severity: Severity
        libs = library_package_names(context.workspace)
        include = {x.strip() for x in context.include}
        if not libs and "semver-checks" not in include:
            return self.skipped(context, "no public library crates detected for semver-checks")
        cmd = self._command(context)
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = (result.stdout + "\n" + result.stderr).lower()
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "semver-checks timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "semver-checks failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "semver-checks passed"
        elif any(m in text for m in NO_BASELINE_MARKERS) and not context.config.semver.baseline:
            # Crates with no published version have no registry baseline to diff
            # against; the check cannot run, so it is skipped with the reason,
            # never reported as a pass or as a violation. A baseline the project
            # configured is a different question: it was not answered either, and
            # that is the tool failing, below.
            return self.skipped(
                context,
                "semver-checks has no baseline for unpublished crates (no registry version to diff); configure a baseline or skip",
            )
        elif any(m in text for m in VIOLATION_MARKERS):
            findings.append(
                Finding(
                    rule="semver-violation",
                    severity="error",
                    confidence="high",
                    file=None,
                    line=None,
                    excerpt=None,
                    explanation="cargo-semver-checks reported API breakage.",
                    recommendation="Review breaking changes and version accordingly.",
                )
            )
            status, severity, summary = "FINDINGS", "error", "semver violations"
        else:
            # Measured with cargo-semver-checks 0.50.0: an unknown
            # ``--baseline-rev`` exits 101 with "couldn't parse revision", and a
            # rejected flag exits 2. Neither observed a breaking change, so
            # neither may be reported as one.
            status, severity, summary = (
                "ERROR",
                "error",
                (
                    f"semver-checks failed (exit {result.exit_code}) without reporting "
                    "API breakage; check the baseline revision and flags"
                ),
            )
        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=cmd,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
