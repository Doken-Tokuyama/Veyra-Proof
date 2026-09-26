"""cargo-machete."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class CargoMacheteTool(Tool):
    id = "machete"
    display_name = "cargo-machete"
    homepage = "https://github.com/bnjbvr/cargo-machete"
    command = "cargo machete"
    version_command = ["cargo", "machete", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-machete", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "machete", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        # older versions may not support --version; try running help
        r2 = context.runner.run(["cargo", "machete", "--help"], timeout=20)
        if r2.exit_code == 0 and not r2.timed_out:
            return DetectionResult(self.id, "INSTALLED", True, version="unknown")
        return DetectionResult(self.id, "MISSING", False, message="cargo-machete not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-machete")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "machete"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "machete"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-machete timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-machete failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "No unused dependencies reported"
        else:
            findings.append(Finding(
                rule="unused-dependency",
                severity="warning",
                confidence="medium",
                file="Cargo.toml", line=None, excerpt=None,
                explanation="cargo-machete reported unused dependencies (requires human review).",
                recommendation="Confirm unused crates and remove them carefully.",
            ))
            status, severity, summary = "FINDINGS", "warning", "Possible unused dependencies"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
