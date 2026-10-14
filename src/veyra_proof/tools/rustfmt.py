"""rustfmt / cargo fmt --check."""
from __future__ import annotations

import shutil

from veyra_proof.installer import rustup_component_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class RustfmtTool(Tool):
    id = "rustfmt"
    display_name = "rustfmt"
    homepage = "https://github.com/rust-lang/rustfmt"
    command = "cargo fmt"
    version_command = ["cargo", "fmt", "--version"]
    install_method = "rustup_component"
    install_command = ["rustup", "component", "add", "rustfmt"]
    rustup_components = ["rustfmt"]
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo", "rustup"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH", prerequisites_met=False, missing_prerequisites=["cargo"])
        has_rustup = bool(shutil.which("rustup"))
        return self.detect_via_version(
            context, ["cargo", "fmt", "--version"],
            absent_message="rustfmt component not installed" if has_rustup else "rustfmt unavailable; rustup missing",
            absent_prerequisites=[] if has_rustup else ["rustup"],
        )

    def install_plan(self, context: AuditContext):
        if not shutil.which("rustup"):
            return None
        return rustup_component_plan(self.id, self.display_name, "rustfmt")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "fmt", "--all", "--", "--check"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "fmt", "--all", "--", "--check"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "rustfmt timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "rustfmt failed to run"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "Formatting check passed"
        else:
            combined = result.stdout + "\n" + result.stderr
            if "Diff in" in combined or "not properly formatted" in combined.lower() or result.exit_code == 1:
                findings.append(Finding(
                    rule="rustfmt-diff",
                    severity="error",
                    confidence="high",
                    file=None,
                    line=None,
                    excerpt=None,
                    explanation="Source files differ from rustfmt output.",
                    recommendation="Run `cargo fmt` locally and review the formatting diff.",
                ))
                status, severity, summary = "FINDINGS", "error", "Formatting differences detected"
            else:
                status, severity, summary = "ERROR", "error", f"rustfmt exited {result.exit_code}"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
