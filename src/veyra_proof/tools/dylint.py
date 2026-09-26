"""Dylint."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding, InstallPlan, InstallStep
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import has_dylint_config


class DylintTool(Tool):
    id = "dylint"
    display_name = "dylint"
    homepage = "https://github.com/trailofbits/dylint"
    command = "cargo dylint"
    version_command = ["cargo", "dylint", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-dylint", "dylint-link", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "dylint", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-dylint not installed")

    def install_plan(self, context: AuditContext):
        return InstallPlan(
            tool_id=self.id,
            display_name=self.display_name,
            method="cargo_install",
            target_version=None,
            steps=[InstallStep("Install dylint", ["cargo", "install", "cargo-dylint", "dylint-link", "--locked"])],
        )


    def planned_commands(self, context: AuditContext):
        return [["cargo", "dylint", "--all"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        if not has_dylint_config(context.root):
            return self.skipped(context, "no Dylint configuration or lint libraries found; refusing to invent lints")
        cmd = ["cargo", "dylint", "--all"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "dylint timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "dylint failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "dylint passed"
        else:
            findings.append(Finding(
                rule="dylint-finding",
                severity="error",
                confidence="high",
                file=None, line=None, excerpt=None,
                explanation="dylint reported diagnostics; see logs.",
                recommendation="Address dylint findings.",
            ))
            status, severity, summary = "FINDINGS", "error", "dylint findings"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
