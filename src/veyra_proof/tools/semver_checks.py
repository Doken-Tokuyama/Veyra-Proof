"""cargo-semver-checks."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import library_package_names


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
        r = context.runner.run(["cargo", "semver-checks", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-semver-checks not installed")

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
        elif "baseline" in text or "no current" in text or "cannot" in text:
            return self.skipped(context, "semver-checks could not determine a baseline; configure baseline or skip")
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
