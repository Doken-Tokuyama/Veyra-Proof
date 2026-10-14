"""cargo clippy."""
from __future__ import annotations

import shutil

from veyra_proof.installer import rustup_component_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import (
    AuditContext,
    Tool,
    classify_from_command,
    nightly_only_targets,
    parse_clippy_output,
    stale_lockfile,
    utc_now_iso,
)

ALL_TARGETS = ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"]
STABLE_TARGETS = ["cargo", "clippy", "--workspace", "--lib", "--bins", "--tests", "--", "-D", "warnings"]


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
        prereq = [] if shutil.which("rustup") else ["rustup"]
        return self.detect_via_version(
            context, ["cargo", "clippy", "--version"],
            absent_message="clippy not available", absent_prerequisites=prereq,
        )

    def install_plan(self, context: AuditContext):
        if not shutil.which("rustup"):
            return None
        return rustup_component_plan(self.id, self.display_name, "clippy")

    def planned_commands(self, context: AuditContext):
        return [self.cargo_command(context, list(ALL_TARGETS))]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = self.cargo_command(context, list(ALL_TARGETS))
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        text = result.stdout + "\n" + result.stderr

        if result.exit_code not in (0, None) and not result.timed_out:
            blocked = nightly_only_targets(text)
            if blocked:
                return self._without_nightly_targets(context, started, cmd, result, blocked)

        out, err = self.write_logs(context, result.stdout, result.stderr)
        # cargo's lockfile refusal is printed as `error: ...`; parsing it as a
        # lint would blame clippy for a lockfile decision nobody asked it for.
        findings = (
            []
            if stale_lockfile(text)
            else parse_clippy_output(text)
        )
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

    def _without_nightly_targets(self, context, started, cmd, failed, blocked):
        """Re-lint the targets stable can build; the rest stays unverified.

        A benchmark written with `#![feature(test)]` cannot be linted on stable.
        That is a toolchain fact, not a lint defect, so the run is reported as
        INCONCLUSIVE naming the unverified files rather than as a clippy failure.
        """
        fallback_cmd = self.cargo_command(context, list(STABLE_TARGETS))
        fallback = context.runner.run(
            fallback_cmd, cwd=context.root, timeout=context.timeout_seconds
        )
        fallback_text = fallback.stdout + "\n" + fallback.stderr
        log = (
            f"# first attempt: {' '.join(cmd)}\n"
            f"# exit {failed.exit_code}: nightly-only feature attributes\n\n"
            f"{failed.stdout}\n{failed.stderr}\n"
            f"# retry without --all-targets: {' '.join(fallback_cmd)}\n"
            f"# exit {fallback.exit_code}\n\n{fallback.stdout}\n{fallback.stderr}\n"
        )
        out, err = self.write_logs(context, log, fallback.stderr)
        findings = ([] if stale_lockfile(fallback_text) else parse_clippy_output(fallback_text)) + [
            Finding(
                rule="nightly-only-target",
                severity="warning",
                confidence="high",
                file=path,
                line=None,
                excerpt="#![feature(...)]",
                explanation=(
                    "Target uses a nightly-only feature and was not linted on this toolchain."
                ),
                recommendation="Lint it on nightly (`cargo +nightly clippy --all-targets`).",
            )
            for path in blocked
        ]
        if fallback.timed_out:
            status, severity, summary = (
                "INCONCLUSIVE", "warning", "cargo clippy timed out on the stable-target retry",
            )
        elif fallback.exit_code == 0:
            status, severity = "INCONCLUSIVE", "warning"
            summary = (
                f"lib/bins/tests are lint-clean, but {len(blocked)} target file(s) need a nightly "
                f"toolchain and were NOT linted: {', '.join(blocked)}"
            )
        else:
            status, severity = "FINDINGS", "error"
            summary = (
                f"{len(findings)} clippy finding(s) on the stable targets; nightly-only targets "
                "were not linted"
            )
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=fallback_cmd, exit_code=fallback.exit_code,
            duration_seconds=(failed.duration_seconds or 0.0) + (fallback.duration_seconds or 0.0),
            stdout_path=out, stderr_path=err, findings=findings,
            environment={"verified_targets": "lib,bins,tests", "unverified_files": ",".join(blocked)},
        )
        self.save_result_json(context, tr)
        return tr