"""cargo-semver-checks."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
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
            context, ["cargo", "semver-checks", "--version"], absent_message="cargo-semver-checks not installed"
        )

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-semver-checks")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "semver-checks", "check-release"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        libs = library_package_names(context.workspace)
        include = {x.strip() for x in context.include}
        if not libs and "semver-checks" not in include:
            return self.skipped(context, "no public library crates detected for semver-checks")
        # Without an explicit baseline, semver-checks may fail — treat as SKIPPED not ERROR
        cmd = ["cargo", "semver-checks", "check-release"]
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
        elif any(m in text for m in NO_BASELINE_MARKERS):
            # Crates with no published version have no registry baseline to diff
            # against; the check cannot run, so it is skipped with the reason,
            # never reported as a pass or as a violation.
            return self.skipped(
                context,
                "semver-checks has no baseline for unpublished crates (no registry version to diff); configure a baseline or skip",
            )
        else:
            findings.append(Finding(
                rule="semver-violation",
                severity="error",
                confidence="high",
                file=None, line=None, excerpt=None,
                explanation="cargo-semver-checks reported API breakage.",
                recommendation="Review breaking changes and version accordingly.",
            ))
            status, severity, summary = "FINDINGS", "error", "semver violations"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
