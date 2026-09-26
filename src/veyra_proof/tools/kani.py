"""Kani model checker."""
from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult, Finding, InstallPlan, InstallStep
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import has_kani_harnesses


class KaniTool(Tool):
    id = "kani"
    display_name = "kani"
    homepage = "https://github.com/model-checking/kani"
    license = "MIT OR Apache-2.0"
    command = "cargo kani"
    version_command = ["cargo", "kani", "--version"]
    install_method = "manual"
    install_command = ["cargo", "install", "--locked", "kani-verifier"]
    default_profiles = ["hardened"]
    prerequisites = ["cargo"]
    safety_level = "hardened"
    reasons_to_skip = ["No automatic install unless --install-missing --include kani or explicit config"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "kani", "--version"], timeout=30)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="kani not installed")

    def install_plan(self, context: AuditContext):
        # Only when explicitly included
        include = {x.strip() for x in context.include}
        if "kani" not in include and not context.config.kani.enabled:
            return None
        return InstallPlan(
            tool_id=self.id,
            display_name=self.display_name,
            method="cargo_install",
            target_version=None,
            steps=[
                InstallStep("Install kani-verifier", ["cargo", "install", "--locked", "kani-verifier"]),
                InstallStep("kani setup", ["cargo", "kani", "setup"]),
            ],
            notes="Kani setup may download platform-specific components; review before authorizing.",
        )


    def planned_commands(self, context: AuditContext):
        return [["cargo", "kani"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        include = {x.strip() for x in context.include}
        if not context.config.kani.enabled and "kani" not in include and not has_kani_harnesses(context.root):
            return self.skipped(context, "no #[kani::proof] harnesses or explicit configuration found")
        if not has_kani_harnesses(context.root) and not context.config.kani.enabled and "kani" not in include:
            return self.skipped(context, "no #[kani::proof] harnesses or explicit configuration found")
        if not has_kani_harnesses(context.root):
            return self.skipped(context, "no #[kani::proof] harnesses found; refusing to generate proofs")
        cmd = ["cargo", "kani"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "Kani timed out / resource exhaustion"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "Kani failed"
        elif "Failed Checks" in text or "Verification failed" in text or result.exit_code != 0:
            if result.exit_code == 0:
                status, severity, summary = "OK", "info", "Kani properties proven for configured harnesses"
            else:
                findings.append(Finding(
                    rule="kani-counterexample",
                    severity="error",
                    confidence="high",
                    file=None, line=None, excerpt=None,
                    explanation="Kani reported a counterexample or failed check.",
                    recommendation="Inspect Kani output and fix the failing property.",
                ))
                status, severity, summary = "FINDINGS", "error", "Kani counterexample/failure"
        else:
            status, severity, summary = "OK", "info", "Kani properties proven for configured harnesses"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
