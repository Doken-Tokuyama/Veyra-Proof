"""cargo-nextest."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult
from veyra_proof.tools.base import AuditContext, Tool, classify_from_command, parse_test_failures, utc_now_iso


class NextestTool(Tool):
    id = "nextest"
    display_name = "cargo-nextest"
    homepage = "https://nexte.st/"
    license = "Apache-2.0 OR MIT"
    command = "cargo nextest"
    version_command = ["cargo", "nextest", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-nextest", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "nextest", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-nextest not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-nextest")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "nextest", "run", "--workspace"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "nextest", "run", "--workspace"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings = parse_test_failures(text)
        # nextest uses different failure lines sometimes
        if result.exit_code not in (0, None) and not findings and not result.timed_out:
            if "FAIL" in text or "failed" in text.lower():
                from veyra_proof.models import Finding
                findings.append(Finding(
                    rule="nextest-failure",
                    severity="error",
                    confidence="medium",
                    file=None, line=None, excerpt=None,
                    explanation="nextest reported failures; see logs for details.",
                    recommendation="Inspect tools/nextest logs and fix failing tests.",
                ))
        status, severity, summary = classify_from_command(
            timed_out=result.timed_out, exit_code=result.exit_code, findings=findings,
            stdout=result.stdout, stderr=result.stderr, error=result.error,
        )
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
