"""cargo test."""

from __future__ import annotations

import shutil
from pathlib import Path

from veyra_proof.models import DetectionResult, ToolResult
from veyra_proof.tools.base import (
    AuditContext,
    Tool,
    classify_from_command,
    locked_build_target,
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

    def planned_commands(self, context: AuditContext) -> list[list[str]]:
        return [self.cargo_command(context, ["cargo", "test", "--workspace"])]

    def run(self, context: AuditContext) -> ToolResult:
        started = utc_now_iso()
        cmd = self.cargo_command(context, ["cargo", "test", "--workspace"])
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        locked = locked_build_target(text, Path(context.workspace.target_directory))
        if locked:
            # The suite never started: cargo could not replace the executable.
            # Reporting a subset of tests as ERROR would hide that distinction.
            tr = self.make_result(
                status="INCONCLUSIVE",
                severity="warning",
                summary=(
                    f"cargo test could not start because {locked} is held by another "
                    "process. Close the process that holds it and re-run; no test "
                    "result is available."
                ),
                started_at=started,
                command=cmd,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                stdout_path=out,
                stderr_path=err,
                findings=[],
                environment={"build_blocker": "locked-target", "locked_path": locked},
            )
            self.save_result_json(context, tr)
            return tr
        findings = parse_test_failures(text)
        # An ignored test is reported either way. The policy decides whether it
        # is an error or a note, never whether it is hidden.
        findings.extend(
            parse_ignored_tests(
                text,
                severity="error" if context.config.policy.fail_on_ignored_tests else "info",
            )
        )
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
