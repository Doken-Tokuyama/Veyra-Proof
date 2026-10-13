"""cargo check tool."""
from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import (
    AuditContext,
    Tool,
    classify_from_command,
    nightly_only_targets,
    utc_now_iso,
)

ALL_TARGETS = ["cargo", "check", "--workspace", "--all-targets"]
# The same check without benches and examples: the targets that can be verified
# on a stable toolchain when the nightly-only ones are the only thing failing.
STABLE_TARGETS = ["cargo", "check", "--workspace", "--lib", "--bins", "--tests"]


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
        return [self.cargo_command(context, list(ALL_TARGETS))]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = self.cargo_command(context, list(ALL_TARGETS))
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)

        if result.exit_code not in (0, None) and not result.timed_out:
            blocked = nightly_only_targets(result.stdout + "\n" + result.stderr)
            if blocked:
                return self._without_nightly_targets(
                    context, started, cmd, result, blocked
                )

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

    def _without_nightly_targets(
        self,
        context: AuditContext,
        started: str,
        cmd: list[str],
        failed,
        blocked: list[str],
    ) -> "ToolResult":
        """Retry without the targets stable cannot build, and report what was missed.

        A nightly-only ``#![feature]`` in a benchmark says nothing about the
        library, the binaries or the tests. Dropping those targets and calling
        it ``OK`` would be a lie; reporting the raw error says nothing useful.
        So the stable targets are checked, the full log is kept, and the verdict
        is INCONCLUSIVE naming every file that was not verified.
        """
        fallback_cmd = self.cargo_command(context, list(STABLE_TARGETS))
        fallback = context.runner.run(
            fallback_cmd, cwd=context.root, timeout=context.timeout_seconds
        )
        total = (failed.duration_seconds or 0.0) + (fallback.duration_seconds or 0.0)
        log = (
            f"# first attempt: {' '.join(cmd)}\n"
            f"# exit {failed.exit_code}: nightly-only feature attributes\n\n"
            f"{failed.stdout}\n{failed.stderr}\n"
            f"# retry without --all-targets: {' '.join(fallback_cmd)}\n"
            f"# exit {fallback.exit_code}\n\n{fallback.stdout}\n{fallback.stderr}\n"
        )
        out, err = self.write_logs(context, log, fallback.stderr)

        if fallback.timed_out:
            status, severity = "INCONCLUSIVE", "warning"
            summary = "cargo check timed out on the stable-target retry"
        elif fallback.exit_code == 0:
            status, severity = "INCONCLUSIVE", "warning"
            summary = (
                f"lib/bins/tests check out, but {len(blocked)} target file(s) need a nightly "
                f"toolchain and were NOT verified: {', '.join(blocked)}"
            )
        else:
            status, severity = "ERROR", "error"
            summary = (
                f"cargo check fails on stable targets too (exit {fallback.exit_code}); "
                "see logs"
            )

        findings = [
            Finding(
                rule="nightly-only-target",
                severity="warning",
                confidence="high",
                file=path,
                line=None,
                excerpt="#![feature(...)]",
                explanation=(
                    "This target uses a nightly-only feature, so --all-targets cannot build it "
                    "on the active toolchain. The library, binaries and tests were checked "
                    "separately; this file's target was not."
                ),
                recommendation=(
                    "Verify it on a nightly toolchain (`cargo +nightly check --all-targets`) "
                    "or keep the benchmark out of the default target set."
                ),
            )
            for path in blocked
        ]

        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=fallback_cmd,
            exit_code=fallback.exit_code,
            duration_seconds=total,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
            environment={"verified_targets": "lib,bins,tests", "unverified_files": ",".join(blocked)},
        )
        self.save_result_json(context, tr)
        return tr