"""cargo-llvm-cov."""
from __future__ import annotations

import re
import shutil

from veyra_proof.models import DetectionResult, Finding, InstallPlan, InstallStep
from veyra_proof.tools.base import AuditContext, Tool, classify_from_command, utc_now_iso

LINE_PCT_RE = re.compile(
    r"(?:lines?|line coverage)[^\d%]*?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
BRANCH_PCT_RE = re.compile(
    r"(?:branches?|branch coverage)[^\d%]*?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)


class LlvmCovTool(Tool):
    id = "llvm-cov"
    display_name = "cargo-llvm-cov"
    homepage = "https://github.com/taiki-e/cargo-llvm-cov"
    command = "cargo llvm-cov"
    version_command = ["cargo", "llvm-cov", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-llvm-cov", "--locked"]
    rustup_components = ["llvm-tools-preview"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo", "rustup"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "llvm-cov", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-llvm-cov not installed")

    def install_plan(self, context: AuditContext):
        steps = []
        if shutil.which("rustup"):
            steps.append(
                InstallStep(
                    description="Add llvm-tools-preview",
                    command=["rustup", "component", "add", "llvm-tools-preview"],
                )
            )
        steps.append(
            InstallStep(
                description="Install cargo-llvm-cov",
                command=["cargo", "install", "cargo-llvm-cov", "--locked"],
            )
        )
        return InstallPlan(self.id, self.display_name, "cargo_install", None, steps)

    def planned_commands(self, context: AuditContext):
        return [["cargo", "llvm-cov", "--workspace", "--summary-only"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        has_nextest = context.runner.run(["cargo", "nextest", "--version"], timeout=15)
        if has_nextest.exit_code == 0 and not has_nextest.timed_out:
            cmd = ["cargo", "llvm-cov", "nextest", "--workspace", "--summary-only"]
        else:
            cmd = ["cargo", "llvm-cov", "--workspace", "--summary-only"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        cov = context.config.coverage
        line_m = LINE_PCT_RE.search(text)
        branch_m = BRANCH_PCT_RE.search(text)
        if cov.minimum_line_percent is not None:
            if line_m:
                pct = float(line_m.group(1))
                if pct < float(cov.minimum_line_percent):
                    findings.append(
                        Finding(
                            rule="coverage-line-below-minimum",
                            severity="error",
                            confidence="high",
                            file=None,
                            line=None,
                            excerpt=f"{pct}%",
                            explanation=(
                                f"Line coverage {pct}% is below configured minimum "
                                f"{cov.minimum_line_percent}%."
                            ),
                            recommendation="Increase tests or adjust coverage.minimum_line_percent.",
                        )
                    )
            elif result.exit_code == 0:
                findings.append(
                    Finding(
                        rule="coverage-line-unparsed",
                        severity="warning",
                        confidence="medium",
                        file=None,
                        line=None,
                        excerpt=None,
                        explanation="minimum_line_percent is set but line coverage % could not be parsed.",
                        recommendation="Inspect llvm-cov summary output format.",
                    )
                )
        if cov.minimum_branch_percent is not None and branch_m:
            pct = float(branch_m.group(1))
            if pct < float(cov.minimum_branch_percent):
                findings.append(
                    Finding(
                        rule="coverage-branch-below-minimum",
                        severity="error",
                        confidence="high",
                        file=None,
                        line=None,
                        excerpt=f"{pct}%",
                        explanation=(
                            f"Branch coverage {pct}% is below configured minimum "
                            f"{cov.minimum_branch_percent}%."
                        ),
                        recommendation="Increase tests or adjust coverage.minimum_branch_percent.",
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
