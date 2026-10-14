"""cargo-vet."""
from __future__ import annotations

import shutil
from pathlib import Path

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class CargoVetTool(Tool):
    id = "vet"
    display_name = "cargo-vet"
    homepage = "https://mozilla.github.io/cargo-vet/"
    command = "cargo vet"
    version_command = ["cargo", "vet", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-vet", "--locked"]
    default_profiles = ["hardened"]
    prerequisites = ["cargo"]
    safety_level = "hardened"

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        return self.detect_via_version(
            context, ["cargo", "vet", "--version"], absent_message="cargo-vet not installed"
        )

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-vet")


    def planned_commands(self, context: AuditContext):
        return [["cargo", "vet", "check"]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        config = context.root / "supply-chain" / "config.toml"
        if not config.is_file():
            return self.skipped(context, "no supply-chain/config.toml; refusing to initialize cargo-vet without authorization")
        cmd = ["cargo", "vet", "check"]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
        out, err = self.write_logs(context, result.stdout, result.stderr)
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-vet timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-vet failed"
        elif result.exit_code == 0:
            status, severity, summary = "OK", "info", "cargo-vet check passed"
        else:
            findings.append(Finding(
                rule="vet-unreviewed",
                severity="error",
                confidence="high",
                file="supply-chain/config.toml", line=None, excerpt=None,
                explanation="cargo-vet reported unreviewed/unaudited dependencies.",
                recommendation="Review and audit crates according to cargo-vet workflow.",
            ))
            status, severity, summary = "FINDINGS", "error", "cargo-vet findings"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
