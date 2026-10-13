"""Optional dual clean-build hash comparison of final crate artifacts."""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path

from veyra_proof.models import DetectionResult, Finding, Severity
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso

SKIP_SUFFIXES = {".d", ".rmeta", ".timestamp", ".o", ".pdb"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _final_artifacts(target_dir: Path, package_names: list[str]) -> dict[str, str]:
    """Hash final binaries/libs for workspace packages under debug/ (not the whole tree)."""
    out: dict[str, str] = {}
    debug = target_dir / "debug"
    if not debug.is_dir():
        return out
    is_windows = platform.system().lower() == "windows"
    for name in package_names:
        candidates = [
            debug / name,
            debug / f"lib{name}.rlib",
            debug / f"lib{name}.so",
            debug / f"lib{name}.dylib",
            debug / f"lib{name}.a",
            debug / f"{name}.exe",
            debug / f"{name}.dll",
        ]
        if is_windows:
            candidates.insert(0, debug / f"{name}.exe")
        for cand in candidates:
            if cand.is_file() and cand.suffix not in SKIP_SUFFIXES:
                rel = str(cand.relative_to(target_dir))
                out[rel] = _sha256_file(cand)
                break
    return out


class ReproducibleBuildTool(Tool):
    id = "reproducible-build"
    display_name = "reproducible-build"
    homepage = ""
    license = "MIT"
    command = "cargo build"
    version_command = ["cargo", "--version"]
    install_method = "none"
    default_profiles: list[str] = ["hardened"]
    prerequisites = ["cargo"]
    safety_level = "hardened"
    # Two clean builds into throwaway target directories. Expensive on a large
    # workspace, so `--exclude reproducible-build` is a supported escape hatch.
    reasons_to_skip = [
        "Two clean builds are required; pass --exclude reproducible-build to skip the cost"
    ]

    def detect(self, context: AuditContext) -> DetectionResult:
        return DetectionResult(self.id, "INSTALLED", True, version="builtin")

    def install_plan(self, context: AuditContext):
        return None

    def planned_commands(self, context: AuditContext):
        evidence = context.evidence_dir / "tools" / self.id
        return [
            self.cargo_command(
                context,
                ["cargo", "build", "--workspace", "--target-dir", str(evidence / "target_a")],
            ),
            self.cargo_command(
                context,
                ["cargo", "build", "--workspace", "--target-dir", str(evidence / "target_b")],
            ),
        ]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        evidence = context.evidence_dir / "tools" / self.id
        evidence.mkdir(parents=True, exist_ok=True)
        t1 = evidence / "target_a"
        t2 = evidence / "target_b"
        findings: list[Finding] = []
        cmds: list[list[str]] = []
        last_out = last_err = ""
        spent = 0.0
        for target_dir in (t1, t2):
            cmd = self.cargo_command(
                context,
                ["cargo", "build", "--workspace", "--target-dir", str(target_dir)],
            )
            cmds.append(cmd)
            result = context.runner.run(cmd, cwd=context.root, timeout=context.timeout_seconds)
            spent += result.duration_seconds or 0.0
            last_out, last_err = result.stdout, result.stderr
            out, err = self.write_logs(context, result.stdout, result.stderr)
            if result.timed_out:
                tr = self.make_result(
                    status="INCONCLUSIVE",
                    severity="warning",
                    summary="Reproducible build comparison timed out",
                    started_at=started,
                    command=cmd,
                    exit_code=result.exit_code,
                    duration_seconds=spent,
                    stdout_path=out,
                    stderr_path=err,
                )
                self.save_result_json(context, tr)
                return tr
            if result.exit_code != 0:
                tr = self.make_result(
                    status="ERROR",
                    severity="error",
                    summary=f"Build failed during reproducibility check (exit {result.exit_code})",
                    started_at=started,
                    command=cmd,
                    exit_code=result.exit_code,
                    duration_seconds=spent,
                    stdout_path=out,
                    stderr_path=err,
                )
                self.save_result_json(context, tr)
                return tr

        names = [p.get("name", "") for p in context.workspace.packages if p.get("name")]
        a_hashes = _final_artifacts(t1, names)
        b_hashes = _final_artifacts(t2, names)
        out, err = self.write_logs(context, last_out, last_err)

        if not a_hashes or not b_hashes:
            status, severity, summary = (
                "INCONCLUSIVE",
                "warning",
                "Could not locate final crate artifacts for hash comparison",
            )
        elif a_hashes != b_hashes:
            differing = sorted(
                key for key in set(a_hashes) | set(b_hashes)
                if a_hashes.get(key) != b_hashes.get(key)
            )
            # The digests below are always pinned as evidence, whatever the
            # severity: the fact that two clean builds disagree is a fact.
            mismatch_sev: Severity = (
                "error" if context.config.policy.require_reproducible_build else "info"
            )
            findings.append(
                Finding(
                    rule="repro-mismatch",
                    severity=mismatch_sev,
                    confidence="high",
                    file=None,
                    line=None,
                    excerpt=", ".join(differing[:5]),
                    explanation=(
                        "Two clean builds of the same commit produced different bytes for "
                        f"{len(differing)} artifact(s). That is a fact about the build, not "
                        "proof of a defect: MSVC/PE output is routinely not bit-reproducible. "
                        "Reproducibility is NOT established."
                    ),
                    recommendation=(
                        "Investigate non-determinism if bit-identical builds are required, or "
                        "set policy.require_reproducible_build=true to make this a hard "
                        "requirement for this project."
                    ),
                )
            )
            status, severity, summary = (
                "INCONCLUSIVE",
                "warning",
                f"Final artifact hashes differ for {len(differing)} artifact(s); "
                "reproducibility not established"
                + (
                    ""
                    if mismatch_sev == "info"
                    else " (policy.require_reproducible_build=true)"
                ),
            )
        else:
            status, severity, summary = (
                "OK",
                "info",
                f"Matching final artifact hashes for {len(a_hashes)} package artifact(s)",
            )

        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=cmds[-1] if cmds else None,
            exit_code=0,
            duration_seconds=spent,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
            environment={"builds": "2", "target_dirs": f"{t1.name},{t2.name}"},
            artifact_hashes={f"a:{k}": v for k, v in a_hashes.items()}
            | {f"b:{k}": v for k, v in b_hashes.items()},
        )
        self.save_result_json(context, tr)
        return tr
