"""cargo-geiger."""
from __future__ import annotations

import re
import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class CargoGeigerTool(Tool):
    id = "geiger"
    display_name = "cargo-geiger"
    homepage = "https://github.com/rust-secure-code/cargo-geiger"
    command = "cargo geiger"
    version_command = ["cargo", "geiger", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-geiger", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "geiger", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-geiger not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-geiger")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "geiger"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "geiger"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        unsafe_used = bool(re.search(r"\bunsafe\b", text, re.I)) and ("Functions" in text or "Expressions" in text or "!" in text)
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-geiger timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-geiger failed"
        elif result.exit_code != 0:
            status, severity, summary = "ERROR", "error", f"cargo-geiger exit {result.exit_code}"
        elif context.config.policy.deny_unsafe and unsafe_used:
            findings.append(Finding(
                rule="unsafe-usage",
                severity="error",
                confidence="medium",
                file=None, line=None, excerpt=None,
                explanation="cargo-geiger reported unsafe usage and policy.deny_unsafe=true.",
                recommendation="Review unsafe blocks or relax deny_unsafe only with documented justification.",
            ))
            status, severity, summary = "FINDINGS", "error", "Unsafe usage under deny_unsafe policy"
        else:
            status, severity, summary = "OK", "info", "cargo-geiger completed; see JSON for unsafe summary"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
            environment={"geiger_stdout_present": "yes"},
        )
        self.save_result_json(context, tr)
        return tr
