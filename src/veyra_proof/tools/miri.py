"""Miri via nightly toolchain."""
from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult, Finding, InstallPlan, InstallStep
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class MiriTool(Tool):
    id = "miri"
    display_name = "miri"
    homepage = "https://github.com/rust-lang/miri"
    command = "cargo +nightly miri"
    version_command = ["cargo", "+nightly", "miri", "--version"]
    install_method = "rustup_toolchain_component"
    required_rust_toolchain = "nightly"
    rustup_components = ["miri"]
    default_profiles = ["hardened"]
    prerequisites = ["cargo", "rustup", "nightly"]
    safety_level = "hardened"

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo") or not shutil.which("rustup"):
            return DetectionResult(self.id, "MISSING", False, message="cargo/rustup required", prerequisites_met=False, missing_prerequisites=["cargo", "rustup"])
        r = context.runner.run(["cargo", "+nightly", "miri", "--version"], timeout=30)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        missing = []
        if not context.platform.nightly_available:
            missing.append("nightly toolchain")
        return DetectionResult(self.id, "MISSING", False, message="Miri not available on nightly", missing_prerequisites=missing, prerequisites_met=not missing)

    def install_plan(self, context: AuditContext):
        if not shutil.which("rustup"):
            return None
        return InstallPlan(
            tool_id=self.id,
            display_name=self.display_name,
            method="rustup_toolchain_component",
            target_version="nightly",
            steps=[
                InstallStep("Install nightly toolchain", ["rustup", "toolchain", "install", "nightly"]),
                InstallStep("Add miri component", ["rustup", "component", "add", "miri", "--toolchain", "nightly"]),
                InstallStep("Miri setup", ["cargo", "+nightly", "miri", "setup"]),
            ],
            notes="Miri only reports UB on executed paths; it does not prove total correctness.",
        )


    def planned_commands(self, context: AuditContext):
        return [["cargo", "+nightly", "miri", "test", "--workspace"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "+nightly", "miri", "test", "--workspace"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "Miri timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "Miri failed to run"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "No Miri findings on executed test paths (not a total correctness proof)"
        else:
            findings.append(Finding(
                rule="miri-ub",
                severity="error",
                confidence="high",
                file=None, line=None, excerpt=None,
                explanation="Miri reported an error/UB on an executed path; see logs.",
                recommendation="Inspect Miri backtrace and fix undefined behavior.",
            ))
            if "undefined behavior" in text.lower() or "error: unsupported" in text.lower() or "error:" in text.lower():
                pass
            status, severity, summary = "FINDINGS", "error", "Miri reported problems on executed paths"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
