"""cargo-geiger."""
from __future__ import annotations

import re
import shlex
import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


def _coverage_warning_run(stdout: str, stderr: str) -> bool:
    """cargo-geiger exits 1 with FoundWarningsError ("error: Found N warnings")
    when the scan completed but some files could not be parsed or scanned.
    That is an incomplete inventory, not a tool failure — but never a pass.
    A real build/compile error always wins over the warning classification."""
    text = (stdout or "") + "\n" + (stderr or "")
    if re.search(r"error: could not compile|error\[E\d+\]", text):
        return False
    return bool(re.search(r"^error: Found \d+ warnings\s*$", text, re.M))


class CargoGeigerTool(Tool):
    id = "geiger"
    display_name = "cargo-geiger"
    homepage = "https://github.com/rust-secure-code/cargo-geiger"
    command = "cargo geiger"
    version_command = ["cargo", "geiger", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-geiger", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        return self.detect_via_version(
            context, ["cargo", "geiger", "--version"], absent_message="cargo-geiger not installed"
        )

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-geiger")


    def planned_commands(self, context: AuditContext):
        return self._commands(context)

    def _commands(self, context: AuditContext) -> list[list[str]]:
        """cargo-geiger needs a real package manifest.

        A virtual workspace root has no ``[package]``, so ``cargo geiger`` there
        is not a valid invocation: run it once per workspace package instead.
        """
        if context.workspace.root_package is not None:
            return [["cargo", "geiger"]]
        cmds = [
            ["cargo", "geiger", "--manifest-path", str(pkg["manifest_path"])]
            for pkg in context.workspace.packages
            if pkg.get("manifest_path")
        ]
        return cmds or [["cargo", "geiger"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        commands = self._commands(context)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_codes: list[int | None] = []
        timed_out = False
        error: str | None = None
        duration = 0.0
        for cmd in commands:
            result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
            stdout_parts.append(result.stdout or "")
            stderr_parts.append(result.stderr or "")
            exit_codes.append(result.exit_code)
            duration += result.duration_seconds or 0.0
            timed_out = timed_out or result.timed_out
            error = error or result.error
        out, err = self.write_logs(context, "\n".join(stdout_parts), "\n".join(stderr_parts))
        text = "\n".join(stdout_parts) + "\n" + "\n".join(stderr_parts)
        cmd_str = " && ".join(shlex.join(c) for c in commands)
        failures = [c for c in exit_codes if c not in (None, 0)]
        agg_exit: int | None = (
            failures[0] if failures else (None if any(c is None for c in exit_codes) else 0)
        )
        hard_failures = [
            code
            for code, so, se in zip(exit_codes, stdout_parts, stderr_parts)
            if code not in (None, 0) and not _coverage_warning_run(so, se)
        ]
        coverage_warning_runs = len(failures) - len(hard_failures)
        findings: list[Finding] = []
        unsafe_used = bool(re.search(r"\bunsafe\b", text, re.I)) and ("Functions" in text or "Expressions" in text or "!" in text)
        if timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-geiger timed out"
        elif any(c is None for c in exit_codes):
            status, severity, summary = "ERROR", "error", error or "cargo-geiger failed"
        elif hard_failures:
            status, severity, summary = "ERROR", "error", f"cargo-geiger exit {hard_failures[0]}"
        elif context.config.policy.deny_unsafe and unsafe_used:
            findings.append(Finding(
                rule="unsafe-usage",
                severity="error",
                confidence="medium",
                file=None, line=None, excerpt=None,
                explanation="cargo-geiger reported unsafe usage and policy.deny_unsafe=true.",
                recommendation="Review unsafe blocks or relax deny_unsafe only with documented justification.",
            ))
            status, severity, summary = "FINDINGS", "error", "Unsafe usage under deny_unsafe policy"
        elif coverage_warning_runs:
            status, severity, summary = (
                "INCONCLUSIVE",
                "warning",
                f"cargo-geiger completed the scan but reported coverage warning(s) in "
                f"{coverage_warning_runs} run(s): some dependency files were not scanned or "
                "could not be parsed, so unsafe counts are a lower bound — this is not a pass",
            )
        else:
            status, severity, summary = "OK", "info", "cargo-geiger completed; see JSON for unsafe summary"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=[cmd_str], exit_code=agg_exit, duration_seconds=duration,
            stdout_path=out, stderr_path=err, findings=findings,
            environment={
                "geiger_stdout_present": "yes",
                "geiger_runs": str(len(commands)),
                "virtual_workspace": "yes" if context.workspace.root_package is None else "no",
                "coverage_warning_runs": str(coverage_warning_runs),
            },
        )
        self.save_result_json(context, tr)
        return tr
