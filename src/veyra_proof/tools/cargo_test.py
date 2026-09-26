"""cargo test."""
from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult
from veyra_proof.tools.base import (
    AuditContext,
    Tool,
    classify_from_command,
    parse_ignored_tests,
    parse_test_failures,
    utc_now_iso,
)


class CargoTestTool(Tool):
    id = "cargo-test"
    display_name = "cargo test"
    homepage = "https://doc.rust-lang.org/cargo/commands/cargo-test.html"
    command = "cargo test"
    version_command = ["cargo", "--version"]
    install_method = "none"
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        path = shutil.which("cargo")
        if not path:
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        return DetectionResult(
            self.id, "INSTALLED", True, version=context.platform.cargo_version, path=path
        )

    def planned_commands(self, context: AuditContext):
        return [["cargo", "test", "--workspace"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "test", "--workspace"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings = parse_test_failures(text)
        if context.config.policy.fail_on_ignored_tests:
            findings.extend(parse_ignored_tests(text))
        status, severity, summary = classify_from_command(
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            findings=findings,
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
            findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
