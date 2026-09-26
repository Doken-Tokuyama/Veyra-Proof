"""cargo-mutants."""
from __future__ import annotations

import shutil

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso


class CargoMutantsTool(Tool):
    id = "mutants"
    display_name = "cargo-mutants"
    homepage = "https://mutants.rs/"
    command = "cargo mutants"
    version_command = ["cargo", "mutants", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-mutants", "--locked"]
    default_profiles = ["hardened"]
    prerequisites = ["cargo"]
    safety_level = "hardened"

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "mutants", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-mutants not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-mutants")


    def planned_commands(self, context: AuditContext):
        paths = list(context.config.mutants.paths)
        if not paths:
            return []
        cmd = ["cargo", "mutants", "--timeout", str(context.config.mutants.timeout_seconds)]
        for p in paths:
            cmd.extend(["--file", p])
        return [cmd]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        paths = list(context.config.mutants.paths)
        if not paths:
            return self.skipped(context, "no mutants.paths configuration; refusing to scan entire workspace silently")
        cmd = ["cargo", "mutants", "--timeout", str(context.config.mutants.timeout_seconds)]
        for p in paths:
            cmd.extend(["--file", p])
        result = context.runner.run(cmd, cwd=context.root, timeout=max(context.timeout_seconds, float(context.config.mutants.timeout_seconds) * 10))
        out, err = self.write_logs(context, result.stdout, result.stderr)
        text = result.stdout + "\n" + result.stderr
        findings: list[Finding] = []
        if result.timed_out:
            status, severity, summary = "INCONCLUSIVE", "warning", "cargo-mutants timed out"
        elif result.exit_code is None:
            status, severity, summary = "ERROR", "error", result.error or "cargo-mutants failed"
        elif "MISSED" in text or "survived" in text.lower() or result.exit_code != 0:
            if result.exit_code == 0 and "MISSED" not in text and "survived" not in text.lower():
                status, severity, summary = "OK", "info", "No surviving mutants reported"
            else:
                findings.append(Finding(
                    rule="mutant-survived",
                    severity="error",
                    confidence="high",
                    file=None, line=None, excerpt=None,
                    explanation="One or more mutants survived or mutants run reported failures; see logs.",
                    recommendation="Strengthen tests to kill surviving mutants.",
                ))
                status, severity, summary = "FINDINGS", "error", "Surviving mutants or mutant run failures"
        else:
            status, severity, summary = "OK", "info", "No surviving mutants reported"
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=cmd, exit_code=result.exit_code, duration_seconds=result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
