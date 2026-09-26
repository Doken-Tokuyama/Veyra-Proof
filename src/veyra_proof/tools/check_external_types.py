"""cargo-check-external-types."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import library_package_names


class CheckExternalTypesTool(Tool):
    id = "check-external-types"
    display_name = "cargo-check-external-types"
    homepage = "https://github.com/awslabs/cargo-check-external-types"
    command = "cargo check-external-types"
    version_command = ["cargo", "check-external-types", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-check-external-types", "--locked"]
    default_profiles = ["hardened"]
    prerequisites = ["cargo"]
    safety_level = "hardened"

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "check-external-types", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-check-external-types not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-check-external-types")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "check-external-types"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        libs = library_package_names(context.workspace)
        if not libs:
            return self.skipped(context, "no library crates; check-external-types not applicable")
        cmd = ["cargo", "check-external-types"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "check-external-types timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "check-external-types failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "No unexpected external types"
        else:
            findings.append(Finding(
                rule="external-types",
                severity="warning",
                confidence="medium",
                file=None, line=None, excerpt=None,
                explanation="cargo-check-external-types reported issues.",
                recommendation="Review public API exposure of external types.",
            ))
            status, severity, summary = "FINDINGS", "warning", "External type findings"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
