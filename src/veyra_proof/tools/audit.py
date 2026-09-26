"""cargo-audit."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class CargoAuditTool(Tool):
    id = "audit"
    display_name = "cargo-audit"
    homepage = "https://github.com/rustsec/rustsec/tree/main/cargo-audit"
    license = "Apache-2.0 OR MIT"
    command = "cargo audit"
    version_command = ["cargo", "audit", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-audit", "--locked"]
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "audit", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-audit not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-audit")


    def planned_commands(self, context: AuditContext):
        if not context.workspace.has_lockfile:
            return []
        return [["cargo", "audit"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        if not context.workspace.has_lockfile:
            if context.config.policy.require_lockfile:
                return self.make_result(
                    status="FINDINGS", severity="error", started_at=started,
                    summary="Cargo.lock missing and policy.require_lockfile=true",
                    findings=[Finding(
                        rule="missing-lockfile", severity="error", confidence="high",
                        file="Cargo.lock", line=None, excerpt=None,
                        explanation="Policy requires Cargo.lock but none was found.",
                        recommendation="Commit a Cargo.lock for reproducible dependency audits.",
                    )],
                )
            return self.skipped(context, "no Cargo.lock; cargo-audit skipped")
        cmd = ["cargo", "audit"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-audit timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-audit failed to run"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "No advisories reported"
        else:
            findings.append(Finding(
                rule="rustsec-advisory",
                severity="error",
                confidence="high",
                file="Cargo.lock",
                line=None,
                excerpt=None,
                explanation="cargo-audit reported one or more advisories; see logs.",
                recommendation="Review advisories and upgrade or patch affected crates.",
            ))
            # extract RUSTSEC ids if present
            import re
            for m in re.finditer(r"RUSTSEC-\d{4}-\d+", text):
                findings.append(Finding(
                    rule=m.group(0), severity="error", confidence="high",
                    file="Cargo.lock", line=None, excerpt=m.group(0),
                    explanation=f"Advisory {m.group(0)} reported by cargo-audit.",
                    recommendation="Inspect the advisory details in the audit log.",
                ))
            status, severity, summary = "FINDINGS", "error", "Advisories detected"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
