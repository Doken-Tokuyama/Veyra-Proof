"""cargo clippy."""
from __future__ import annotations

import shutil

from veyra_proof.installer import rustup_component_plan
from veyra_proof.models import DetectionResult
from veyra_proof.tools.base import AuditContext, Tool, classify_from_command, parse_clippy_output, utc_now_iso


class ClippyTool(Tool):
    id = "clippy"
    display_name = "clippy"
    homepage = "https://github.com/rust-lang/rust-clippy"
    command = "cargo clippy"
    version_command = ["cargo", "clippy", "--version"]
    install_method = "rustup_component"
    install_command = ["rustup", "component", "add", "clippy"]
    rustup_components = ["clippy"]
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo", "rustup"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "clippy", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        missing = [] if shutil.which("rustup") else ["rustup"]
        return DetectionResult(self.id, "MISSING", False, message="clippy not available", missing_prerequisites=missing, prerequisites_met=not missing)

    def install_plan(self, context: AuditContext):
        if not shutil.which("rustup"):
            return None
        return rustup_component_plan(self.id, self.display_name, "clippy")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings = parse_clippy_output(text)
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
