"""cargo-deny."""
from __future__ import annotations

import re
import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding, Severity
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso

DENY_DIAG_RE = re.compile(
    r"^(?P<level>error|warning)\[(?P<code>[^\]]+)\]:\s*(?P<msg>.+)$",
    re.MULTILINE,
)


def parse_deny_output(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for m in DENY_DIAG_RE.finditer(text):
        level = m.group("level")
        code = m.group("code")
        msg = m.group("msg").strip()
        severity: Severity = "error" if level == "error" else "warning"
        findings.append(
            Finding(
                rule=f"deny:{code}",
                severity=severity,
                confidence="high",
                file="Cargo.toml" if "license" in code or "unlicensed" in code else None,
                line=None,
                excerpt=msg[:200],
                explanation=msg,
                recommendation=(
                    "Add a package.license (or license-file) in Cargo.toml, "
                    "or configure deny.toml licenses policy."
                    if "license" in code or "unlicensed" in code
                    else "Review cargo-deny output and adjust dependencies or deny.toml."
                ),
            )
        )
    return findings


class CargoDenyTool(Tool):
    id = "deny"
    display_name = "cargo-deny"
    homepage = "https://github.com/EmbarkStudios/cargo-deny"
    license = "MIT OR Apache-2.0"
    command = "cargo deny"
    version_command = ["cargo", "deny", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-deny", "--locked"]
    default_profiles = ["standard", "hardened"]
    prerequisites = ["cargo"]

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "deny", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-deny not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-deny")

    def planned_commands(self, context: AuditContext):
        return [["cargo", "deny", "check"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        cmd = ["cargo", "deny", "check"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings = parse_deny_output(text)
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-deny timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-deny failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "cargo-deny check passed"
        else:
            if not findings:
                findings.append(
                    Finding(
                        rule="cargo-deny-violation",
                        severity="error",
                        confidence="high",
                        file="deny.toml" if (context.root / "deny.toml").is_file() else None,
                        line=None,
                        excerpt=None,
                        explanation="cargo-deny reported license/advisory/ban/source violations.",
                        recommendation="Review deny output and adjust dependencies or deny.toml policy.",
                    )
                )
            max_sev = max(
                (f.severity for f in findings),
                key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
            )
            status, severity, summary = "FINDINGS", max_sev, f"{len(findings)} cargo-deny diagnostic(s)"
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
