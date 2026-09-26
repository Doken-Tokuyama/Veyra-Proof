"""cargo check tool."""
from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult
from veyra_proof.tools.base import AuditContext, Tool, classify_from_command, utc_now_iso


class CargoCheckTool(Tool):
    id = "cargo-check"
    display_name = "cargo check"
    homepage = "https://doc.rust-lang.org/cargo/commands/cargo-check.html"
    command = "cargo"
    version_command = ["cargo", "--version"]
    install_method = "none"
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        path = shutil.which("cargo")
        if not path:
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        ver = context.platform.cargo_version
        return DetectionResult(self.id, "INSTALLED", True, version=ver, path=path)

    def install_plan(self, context: AuditContext):
        return None


    def planned_commands(self, context: AuditContext):
        return [["cargo", "check", "--workspace", "--all-targets"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "check", "--workspace", "--all-targets"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        status, severity, summary = classify_from_command(
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            findings=[],
            stdout=result.stdout,
            stderr=result.stderr,
            error=result.error,
        )
        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=cmd,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            stdout_path=out,
            stderr_path=err,
        )
        self.save_result_json(context, tr)
        return tr
