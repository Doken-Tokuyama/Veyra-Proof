"""Optional local-only SAST tools (never auto-installed in V1)."""

from __future__ import annotations

import shutil

from veyra_proof.models import DetectionResult
from veyra_proof.tools.base import AuditContext, Tool, classify_from_command, utc_now_iso


class SemgrepTool(Tool):
    id = "semgrep"
    display_name = "semgrep"
    homepage = "https://semgrep.dev/"
    license = "LGPL-2.1"
    command = "semgrep"
    version_command = ["semgrep", "--version"]
    install_method = "none"
    default_profiles: list[str] = []
    prerequisites = ["semgrep", "local rules"]
    reasons_to_skip = ["Not auto-installed in V1; only runs if already present and included"]

    def detect(self, context: AuditContext) -> DetectionResult:
        path = shutil.which("semgrep")
        if not path:
            return DetectionResult(self.id, "MISSING", False, message="semgrep not on PATH (not auto-installed)")
        r = context.runner.run(["semgrep", "--version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver, path=path)
        return DetectionResult(self.id, "MISSING", False, message="semgrep not executable")

    def install_plan(self, context: AuditContext):
        return None

    def planned_commands(self, context: AuditContext):
        return []

    def run(self, context: AuditContext):
        started = utc_now_iso()
        include = {x.strip() for x in context.include}
        if "semgrep" not in include:
            return self.skipped(context, "semgrep not included explicitly; never auto-run without --include semgrep")
        # Only local config
        local_configs = [
            context.root / ".semgrep.yml",
            context.root / ".semgrep.yaml",
            context.root / "semgrep.yml",
        ]
        config = next((p for p in local_configs if p.is_file()), None)
        if config is None:
            return self.skipped(context, "no local Semgrep config found; refusing to download remote rules")
        cmd = ["semgrep", "--config", str(config), "--error", str(context.root)]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
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


class CodeQLTool(Tool):
    id = "codeql"
    display_name = "codeql"
    homepage = "https://codeql.github.com/"
    license = "Various"
    command = "codeql"
    version_command = ["codeql", "version"]
    install_method = "none"
    default_profiles: list[str] = []
    prerequisites = ["codeql", "local database/queries"]
    reasons_to_skip = ["Not auto-installed in V1"]

    def detect(self, context: AuditContext) -> DetectionResult:
        path = shutil.which("codeql")
        if not path:
            return DetectionResult(self.id, "MISSING", False, message="codeql not on PATH (not auto-installed)")
        r = context.runner.run(["codeql", "version"], timeout=20)
        if r.exit_code == 0 and not r.timed_out:
            ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
            return DetectionResult(self.id, "INSTALLED", True, version=ver, path=path)
        return DetectionResult(self.id, "MISSING", False, message="codeql not executable")

    def install_plan(self, context: AuditContext):
        return None

    def planned_commands(self, context: AuditContext):
        db = context.config.codeql.database
        suite = context.config.codeql.query_suite
        if not db or not suite:
            return []
        return [["codeql", "database", "analyze", db, suite, "--format=sarif-latest", "--output=codeql.sarif"]]

    def run(self, context: AuditContext):
        include = {x.strip() for x in context.include}
        if "codeql" not in include and "codeql" not in set(context.config.tools.enable):
            return self.skipped(
                context,
                "codeql not included explicitly; never auto-run without --include codeql",
                skip_kind="intentional",
            )
        db = context.config.codeql.database
        suite = context.config.codeql.query_suite
        if not db or not suite:
            # Also accept local conventional paths
            local_db = context.root / "codeql-db"
            if local_db.is_dir() and suite:
                db = str(local_db)
            else:
                return self.skipped(
                    context,
                    "CodeQL requires veyra.toml [codeql] database + query_suite (local only; no remote download)",
                    skip_kind="intentional",
                )
        started = utc_now_iso()
        out_sarif = context.evidence_dir / "tools" / self.id / "codeql.sarif"
        out_sarif.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "codeql",
            "database",
            "analyze",
            db,
            suite,
            "--format=sarif-latest",
            f"--output={out_sarif}",
        ]
        result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
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
            artifact_hashes={"sarif": str(out_sarif) if out_sarif.is_file() else ""},
        )
        self.save_result_json(context, tr)
        return tr
