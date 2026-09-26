"""cargo-fuzz."""
from __future__ import annotations

import shutil
from pathlib import Path

from veyra_proof.installer import cargo_install_plan
from veyra_proof.models import DetectionResult, Finding
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso
from veyra_proof.workspace import fuzz_directory


class CargoFuzzTool(Tool):
    id = "fuzz"
    display_name = "cargo-fuzz"
    homepage = "https://github.com/rust-fuzz/cargo-fuzz"
    command = "cargo fuzz"
    version_command = ["cargo", "fuzz", "--version"]
    install_method = "cargo_install"
    install_command = ["cargo", "install", "cargo-fuzz", "--locked"]
    required_rust_toolchain = "nightly"
    default_profiles = ["hardened"]
    prerequisites = ["cargo", "nightly", "fuzz/"]
    safety_level = "hardened"

    def detect(self, context: AuditContext) -> DetectionResult:
        if not shutil.which("cargo"):
            return DetectionResult(self.id, "MISSING", False, message="cargo not on PATH")
        r = context.runner.run(["cargo", "fuzz", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver)
        return DetectionResult(self.id, "MISSING", False, message="cargo-fuzz not installed")

    def install_plan(self, context: AuditContext):
        return cargo_install_plan(self.id, self.display_name, "cargo-fuzz")


    def planned_commands(self, context: AuditContext):
        targets = list(context.config.fuzz.targets)
        budget = context.config.fuzz.max_total_time_seconds
        return [["cargo", "+nightly", "fuzz", "run", t, "--", f"-max_total_time={budget}"] for t in targets]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        fuzz_dir = fuzz_directory(context.root)
        if fuzz_dir is None:
            return self.skipped(context, "no fuzz/ directory or configured targets found")
        targets = list(context.config.fuzz.targets)
        if not targets:
            return self.skipped(context, "no fuzz.targets listed in veyra.toml; refusing to invent targets")
        if not context.platform.nightly_available:
            # still try +nightly; may fail
            pass
        findings: list[Finding] = []
        last_cmd = None
        last_result = None
        budget = context.config.fuzz.max_total_time_seconds
        for target in targets:
            cmd = ["cargo", "+nightly", "fuzz", "run", target, "--", f"-max_total_time={budget}"]
            last_cmd = cmd
            result = context.runner.run(cmd, cwd=context.root, timeout=max(context.timeout_seconds, float(budget) + 60))
            last_result = result
            out, err = self.write_logs(context, result.stdout, result.stderr)
            text = result.stdout + "\n" + result.stderr
            if result.timed_out:
                tr = self.make_result(
                    status="INCONCLUSIVE", severity="warning", summary=f"fuzz target {target} timed out",
                    started_at=started, command=cmd, exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds, stdout_path=out, stderr_path=err,
                )
                self.save_result_json(context, tr)
                return tr
            if result.exit_code not in (0, None) and ("crash" in text.lower() or "panic" in text.lower() or "ERROR: libFuzzer" in text):
                findings.append(Finding(
                    rule="fuzz-crash",
                    severity="blocker",
                    confidence="high",
                    file=str(Path("fuzz") / "fuzz_targets" / f"{target}.rs"),
                    line=None, excerpt=target,
                    explanation=f"Fuzz target {target} produced a crash/panic/sanitizer finding.",
                    recommendation="Minimize the crashing input and fix the defect.",
                ))
            elif result.exit_code not in (0, None) and result.exit_code is not None:
                # target may not exist
                if "does not exist" in text.lower() or "no such" in text.lower():
                    return self.skipped(context, f"fuzz target {target!r} does not exist")
                findings.append(Finding(
                    rule="fuzz-error",
                    severity="error",
                    confidence="medium",
                    file=None, line=None, excerpt=target,
                    explanation=f"Fuzz target {target} exited non-zero; see logs.",
                    recommendation="Inspect fuzz logs.",
                ))
        if findings:
            status, severity, summary = "FINDINGS", "blocker", "Fuzz findings detected"
        else:
            status, severity, summary = "OK", "info", (
                "No crash found within configured fuzzing budget; this is not a proof of absence of defects."
            )
        assert last_result is not None and last_cmd is not None
        out, err = self.write_logs(context, last_result.stdout, last_result.stderr)
        tr = self.make_result(
            status=status, severity=severity, summary=summary, started_at=started,
            command=last_cmd, exit_code=last_result.exit_code, duration_seconds=last_result.duration_seconds,
            stdout_path=out, stderr_path=err, findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
