"""Evidence writing, Markdown/JSON reports, and global decision engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veyra_proof.models import AuditDecision, FailOn, ToolResult
from veyra_proof.tool_registry import CONFIG_GATED, SOFT_OPTIONAL


def utc_timestamp_dir() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_evidence_dir(workspace_root: Path, timestamp: str | None = None) -> Path:
    ts = timestamp or utc_timestamp_dir()
    return workspace_root / ".veyra" / "evidence" / ts


def _skip_kind(result: ToolResult) -> str:
    return result.environment.get("skip_kind", "unknown")


def decide(
    results: list[ToolResult],
    *,
    fail_on: FailOn,
    required_tool_ids: list[str],
    hash_incongruence: bool = False,
    missing_required: list[str] | None = None,
) -> AuditDecision:
    """
    Decision rules:
    - CORE required tools must run as OK (or FINDINGS handled by fail_on).
    - Intentional/config-gated SKIPPED does not make INCONCLUSIVE.
    - Missing required executable (skip_kind=missing) → exit 2.
    - Blocker/error findings → REJECTED.
    - Optional tool ERROR still rejects when fail_on != none.
    - Optional INCONCLUSIVE only rejects when fail_on=inconclusive.
    """
    missing = list(missing_required or [])
    by_id = {r.tool_id: r for r in results}
    required = set(required_tool_ids)

    blockers: list[str] = []
    error_findings: list[str] = []
    warning_findings = False
    tool_errors: list[str] = []
    inconclusive_required: list[str] = []
    inconclusive_optional: list[str] = []
    missing_skips: list[str] = []

    for tid in required:
        if tid not in by_id:
            missing.append(tid)

    for r in results:
        if r.status == "ERROR":
            tool_errors.append(r.tool_id)
        if r.status == "INCONCLUSIVE":
            if r.tool_id in required:
                inconclusive_required.append(r.tool_id)
            else:
                inconclusive_optional.append(r.tool_id)
        if r.status == "SKIPPED" and r.tool_id in required:
            kind = _skip_kind(r)
            if kind in {"missing", "incompatible", "unknown"}:
                missing_skips.append(r.tool_id)
            # intentional / config_gated skips for non-core should not be in required
        if r.status == "FINDINGS":
            for f in r.findings:
                label = f"{r.tool_id}:{f.rule}"
                if f.severity == "blocker":
                    blockers.append(label)
                elif f.severity == "error":
                    error_findings.append(label)
                elif f.severity == "warning":
                    warning_findings = True
                # info findings do not affect fail_on=findings

    def _join(items: list[str], limit: int = 8) -> str:
        return ", ".join(sorted(set(items))[:limit])

    if hash_incongruence:
        return AuditDecision("REJECTED", "Unauthorized or incongruent project file changes detected", 1)

    if blockers:
        return AuditDecision("REJECTED", f"Blocker finding(s): {_join(blockers)}", 1)

    if error_findings:
        return AuditDecision("REJECTED", f"Error finding(s): {_join(error_findings)}", 1)

    if fail_on == "findings" and (warning_findings or error_findings or blockers):
        return AuditDecision("REJECTED", "Findings reported under --fail-on findings", 1)

    # Also catch FINDINGS with only warnings already handled; FINDINGS with empty visible?
    if fail_on == "findings" and any(
        r.status == "FINDINGS" and any(f.severity in {"warning", "error", "blocker"} for f in r.findings)
        for r in results
    ):
        return AuditDecision("REJECTED", "Findings reported under --fail-on findings", 1)

    if tool_errors and fail_on != "none":
        return AuditDecision("REJECTED", f"Tool error(s): {_join(tool_errors)}", 1)

    if missing or missing_skips:
        return AuditDecision(
            "INCONCLUSIVE",
            "Required tool(s) missing or not executable: " + _join(missing + missing_skips, 12),
            2,
        )

    if inconclusive_required:
        if fail_on == "inconclusive":
            return AuditDecision("REJECTED", f"Inconclusive required checks: {_join(inconclusive_required)}", 1)
        if fail_on != "none":
            return AuditDecision(
                "INCONCLUSIVE",
                f"Inconclusive required tool result(s): {_join(inconclusive_required)}",
                1,
            )

    if inconclusive_optional and fail_on == "inconclusive":
        return AuditDecision("REJECTED", f"Inconclusive optional checks: {_join(inconclusive_optional)}", 1)

    if fail_on == "none":
        return AuditDecision("ACCEPTED", "fail-on=none; no hard rejection criteria applied", 0)

    for tid in required:
        r = by_id.get(tid)
        if r is None:
            return AuditDecision("INCONCLUSIVE", f"Required tools not executed: {tid}", 2)
        if r.status == "SKIPPED":
            return AuditDecision(
                "INCONCLUSIVE",
                f"Required tool skipped unexpectedly: {tid} ({_skip_kind(r)})",
                2,
            )
        if r.status not in {"OK", "FINDINGS"}:
            return AuditDecision("INCONCLUSIVE", f"Required tool did not complete cleanly: {tid}", 2)

    return AuditDecision(
        "ACCEPTED",
        "All required profile tools executed without blocking findings",
        0,
    )


def classify_project_changes(
    changes: list[dict[str, Any]],
    *,
    update_lockfile: bool,
    allow_manifest_changes: bool,
    lockfile_existed_before: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Return (annotated_changes, unauthorized_paths).

    Cargo.lock *creation* by cargo when none existed is reported but not unauthorized.
    Cargo.lock *modification* without --update-lockfile is unauthorized.
    Cargo.toml changes without --allow-manifest-changes are unauthorized.
    """
    unauthorized: list[str] = []
    annotated: list[dict[str, Any]] = []
    for ch in changes:
        path = ch.get("path") or ""
        kind = ch.get("change") or ""
        entry = dict(ch)
        if path == "Cargo.toml" or path.endswith("/Cargo.toml"):
            if not allow_manifest_changes:
                unauthorized.append(path)
                entry["authorization"] = "unauthorized"
            else:
                entry["authorization"] = "authorized_but_no_auto_mutation_expected"
        elif path == "Cargo.lock" or path.endswith("/Cargo.lock"):
            if kind == "created" and not lockfile_existed_before:
                entry["authorization"] = "informational_cargo_side_effect"
            elif not update_lockfile:
                unauthorized.append(path)
                entry["authorization"] = "unauthorized"
            else:
                entry["authorization"] = "authorized"
        else:
            entry["authorization"] = "tracked"
        annotated.append(entry)
    return annotated, unauthorized


def render_markdown(
    *,
    workspace: str,
    timestamp: str,
    os_arch: str,
    toolchain: str,
    profile: str,
    decision: AuditDecision,
    results: list[ToolResult],
    changes: list[dict[str, Any]],
    evidence_manifest: str = "manifest.json",
) -> str:
    ok = [r for r in results if r.status == "OK"]
    problems = [r for r in results if r.status == "FINDINGS"]
    skipped = [r for r in results if r.status == "SKIPPED"]
    errors = [r for r in results if r.status == "ERROR"]
    inconclusive = [r for r in results if r.status == "INCONCLUSIVE"]

    lines = [
        "# Veyra Proof Report",
        "",
        f"- Workspace: {workspace}",
        f"- Timestamp UTC: {timestamp}",
        f"- OS / Architecture: {os_arch}",
        f"- Rust toolchain: {toolchain}",
        f"- Profile: {profile}",
        f"- Decision: {decision.decision}",
        f"- Decision reason: {decision.reason}",
        f"- Evidence manifest: {evidence_manifest}",
        "",
        "## OK",
        "",
    ]
    if ok:
        for r in ok:
            lines.append(f"- {r.display_name}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Problems", ""])
    if problems:
        for r in problems:
            lines.append(f"### {r.display_name}")
            visible = [f for f in r.findings if f.severity != "info"]
            if not visible:
                lines.append(f"- {r.summary}")
            for f in visible:
                loc = ""
                if f.file:
                    loc = f"{f.file}"
                    if f.line:
                        loc += f":{f.line}"
                    loc += " — "
                lines.append(
                    f"- {f.rule} [{f.severity}, {f.confidence}] {loc}{f.explanation}"
                )
            lines.append("")
    else:
        lines.append("- (none)")
        lines.append("")

    lines.extend(["## Skipped", ""])
    if skipped:
        for r in skipped:
            lines.append(f"- {r.display_name} — {r.summary}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Errors", ""])
    err_items = errors + inconclusive
    if err_items:
        for r in err_items:
            log = r.stderr_path or r.stdout_path or "(no log)"
            # Prefer path relative to evidence when possible
            label = "error" if r.status == "ERROR" else "inconclusive"
            rel = log
            if "tools/" in log.replace("\\", "/"):
                rel = "tools/" + log.replace("\\", "/").split("tools/", 1)[-1]
            lines.append(f"- {r.display_name} — {r.summary} ({label}); see {rel}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Project Changes", ""])
    if not changes:
        lines.append("- No project manifest or lockfile changes detected.")
    else:
        for c in changes:
            auth = c.get("authorization")
            suffix = f" [{auth}]" if auth else ""
            lines.append(f"- {c.get('path')}: {c.get('change')}{suffix}")

    lines.append("")
    return "\n".join(lines)


def write_evidence(
    evidence_dir: Path,
    *,
    report_md: str,
    report_json: dict[str, Any],
    manifest: dict[str, Any],
    environment: dict[str, Any],
    workspace: dict[str, Any],
    changes: list[dict[str, Any]],
    plan: dict[str, Any],
    hashes: dict[str, Any],
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "tools").mkdir(exist_ok=True)
    (evidence_dir / "hashes").mkdir(exist_ok=True)
    (evidence_dir / "report.md").write_text(report_md, encoding="utf-8")
    (evidence_dir / "report.json").write_text(json.dumps(report_json, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "workspace.json").write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "changes.json").write_text(json.dumps(changes, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    (evidence_dir / "hashes" / "project_files.json").write_text(
        json.dumps(hashes, indent=2) + "\n", encoding="utf-8"
    )
