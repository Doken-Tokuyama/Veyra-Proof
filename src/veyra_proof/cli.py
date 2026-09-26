"""CLI for Veyra Proof."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from veyra_proof import __version__
from veyra_proof.command_runner import CommandRunner
from veyra_proof.config import load_veyra_config
from veyra_proof.discovery import run_doctor
from veyra_proof.hashing import compare_snapshots, snapshot_project_files
from veyra_proof.installer import confirm_installs, execute_install_plan
from veyra_proof.models import DetectionResult, InstallPlan, ToolResult
from veyra_proof.platform_info import detect_platform
from veyra_proof.reporting import (
    classify_project_changes,
    decide,
    default_evidence_dir,
    render_markdown,
    utc_timestamp_dir,
    write_evidence,
)
from veyra_proof.tool_registry import (
    required_tool_ids,
    registry_rows,
    select_tools,
    unknown_include_ids,
)
from veyra_proof.tools.base import AuditContext, utc_now_iso
from veyra_proof.workspace import WorkspaceError, resolve_workspace


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veyra",
        description="Audit a local Rust workspace against defective, insecure, or theatrical/fake code.",
    )
    parser.add_argument("--version", action="version", version=f"veyra {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Diagnose Python/OS/Cargo/rustup without installing anything")
    sub.add_parser("list-tools", help="List tool registry metadata")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("path", type=str, help="Path to Cargo package or workspace")
        p.add_argument("--profile", choices=["quick", "standard", "hardened"], default="standard")
        p.add_argument("--yes", action="store_true", help="Non-interactive authorization for installs")
        p.add_argument("--install-missing", action="store_true", help="Allow installing compatible missing tools")
        p.add_argument("--update-lockfile", action="store_true", help="Authorize cargo update")
        p.add_argument(
            "--allow-manifest-changes",
            action="store_true",
            help="Authorize future Cargo.toml mutations (none implemented in V1)",
        )
        p.add_argument("--include", type=str, default="", help="Comma-separated tool ids to run only")
        p.add_argument("--exclude", type=str, default="", help="Comma-separated tool ids to exclude")
        p.add_argument("--evidence-dir", type=str, default=None)
        p.add_argument("--format", choices=["terminal", "markdown", "json", "both"], default="both")
        p.add_argument("--fail-on", choices=["none", "findings", "error", "inconclusive"], default="findings")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--timeout", type=float, default=900.0)
        p.add_argument("--verbose", action="store_true")

    plan = sub.add_parser("plan", help="Show execution plan without running checks")
    add_common(plan)

    audit = sub.add_parser("audit", help="Run the audit")
    add_common(audit)
    return parser


def cmd_doctor() -> int:
    report = run_doctor(CommandRunner())
    print(json.dumps(report.to_dict(), indent=2))
    if not report.cargo_ok:
        print("\nCargo is required and was not detected.", file=sys.stderr)
        return 2
    return 0


def cmd_list_tools() -> int:
    print(json.dumps(registry_rows(), indent=2))
    return 0


def _build_plan_dict(
    *,
    ctx: AuditContext,
    tools: list,
    detections: dict[str, DetectionResult],
    install_plans: list[InstallPlan],
    profile: str,
    update_lockfile: bool,
    allow_manifest_changes: bool,
    yes: bool,
    unknown_includes: list[str],
) -> dict:
    commands_preview = []
    for tool in tools:
        det = detections.get(tool.id)
        cmds = tool.planned_commands(ctx) if det and det.available else []
        if det and not det.available:
            note = det.message or "missing"
        elif not cmds:
            note = "no commands (likely config-gated skip at run time)"
        else:
            note = "ready"
        commands_preview.append(
            {
                "tool": tool.id,
                "available": bool(det and det.available),
                "commands": cmds,
                "note": note,
            }
        )
    return {
        "profile": profile,
        "selected_tools": [t.id for t in tools],
        "unknown_includes": unknown_includes,
        "detections": {k: v.to_dict() for k, v in detections.items()},
        "pending_installs": [p.to_dict() for p in install_plans],
        "commands_preview": commands_preview,
        "cargo_lock_policy": {
            "update_lockfile_authorized": update_lockfile,
            "will_run_cargo_update": update_lockfile,
        },
        "cargo_toml_policy": {
            "allow_manifest_changes": allow_manifest_changes,
            "automatic_mutations_implemented": False,
            "message": (
                "Manifest changes are authorized but no automatic manifest mutation is implemented."
                if allow_manifest_changes
                else "Cargo.toml modifications are prohibited by default."
            ),
        },
        "authorization_mode": "non-interactive (--yes)" if yes else "interactive",
        "timestamp_utc": ctx.extras.get("timestamp"),
    }


def _prepare_context(
    args: argparse.Namespace,
    *,
    create_evidence_dir: bool,
) -> tuple[AuditContext | None, dict, list, dict[str, DetectionResult], list[InstallPlan], int | None]:
    platform = detect_platform(CommandRunner(verbose=bool(args.verbose)))
    if not platform.cargo_path:
        print("ERROR: Cargo not found on PATH. Install Rust/Cargo before auditing.", file=sys.stderr)
        return None, {}, [], {}, [], 2

    try:
        workspace = resolve_workspace(args.path, CommandRunner(verbose=bool(args.verbose)))
    except WorkspaceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None, {}, [], {}, [], 2

    config = load_veyra_config(Path(workspace.workspace_root))
    include = _parse_csv(args.include)
    exclude = _parse_csv(args.exclude)
    unknown = unknown_include_ids(include)
    if unknown:
        print(f"WARNING: unknown --include tool id(s): {', '.join(unknown)}", file=sys.stderr)

    tools = select_tools(
        args.profile,
        include=include,
        exclude=exclude,
        config_enable=config.tools.enable,
        config_disable=config.tools.disable,
    )

    timestamp = utc_timestamp_dir()
    if args.evidence_dir:
        evidence = Path(args.evidence_dir)
        # If user path has no timestamp leaf, still record our stamp for reports
    else:
        evidence = default_evidence_dir(Path(workspace.workspace_root), timestamp)

    if create_evidence_dir and not args.dry_run:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "tools").mkdir(exist_ok=True)

    runner = CommandRunner(dry_run=bool(args.dry_run), verbose=bool(args.verbose))
    detect_runner = CommandRunner(verbose=bool(args.verbose), dry_run=False)

    common = dict(
        workspace=workspace,
        platform=platform,
        config=config,
        evidence_dir=evidence,
        profile=args.profile,
        timeout_seconds=float(args.timeout),
        verbose=bool(args.verbose),
        yes=bool(args.yes),
        install_missing=bool(args.install_missing),
        update_lockfile=bool(args.update_lockfile),
        allow_manifest_changes=bool(args.allow_manifest_changes),
        include=include,
        exclude=exclude,
        extras={"timestamp": timestamp},
    )

    ctx = AuditContext(runner=runner, dry_run=bool(args.dry_run), **common)
    detect_ctx = AuditContext(runner=detect_runner, dry_run=False, **common)

    detections: dict[str, DetectionResult] = {}
    install_plans: list[InstallPlan] = []
    for tool in tools:
        if not tool.platform_supported(platform):
            detections[tool.id] = DetectionResult(
                tool.id,
                "SKIPPED",
                False,
                message=f"Incompatible with {platform.os.value}/{platform.architecture}",
            )
            continue
        det = tool.detect(detect_ctx)
        detections[tool.id] = det
        if not det.available and args.install_missing:
            plan = tool.install_plan(detect_ctx)
            if plan is not None:
                install_plans.append(plan)

    plan = _build_plan_dict(
        ctx=detect_ctx,
        tools=tools,
        detections=detections,
        install_plans=install_plans,
        profile=args.profile,
        update_lockfile=bool(args.update_lockfile),
        allow_manifest_changes=bool(args.allow_manifest_changes),
        yes=bool(args.yes),
        unknown_includes=unknown,
    )
    return ctx, plan, tools, detections, install_plans, None


def cmd_plan(args: argparse.Namespace) -> int:
    ctx, plan, tools, detections, install_plans, err = _prepare_context(args, create_evidence_dir=False)
    if ctx is None:
        return err or 2
    for tool in tools:
        det = detections.get(tool.id)
        if det and not det.available and tool.id not in {p.tool_id for p in install_plans}:
            plan.setdefault("auto_skip", []).append({"tool": tool.id, "reason": det.message or "missing"})
    print(json.dumps(plan, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    ctx, plan, tools, detections, install_plans, err = _prepare_context(args, create_evidence_dir=True)
    if ctx is None:
        return err or 2

    timestamp = ctx.extras["timestamp"]

    if args.allow_manifest_changes:
        print(
            "Manifest changes are authorized but no automatic manifest mutation is implemented.",
            file=sys.stderr,
        )

    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2))
        return 0

    lockfile_existed = ctx.workspace.has_lockfile
    before = snapshot_project_files(ctx.root)
    hashes_before = {k: v.to_dict() for k, v in before.items()}

    live = CommandRunner(verbose=bool(args.verbose), dry_run=False)

    if install_plans:
        authorized = confirm_installs(install_plans, yes=bool(args.yes))
        if not authorized:
            print("Installation not authorized. Aborting before mutating the system.", file=sys.stderr)
            return 2
        plan["install_authorization"] = "non-interactive (--yes)" if args.yes else "interactive confirmation"
        for ip in install_plans:
            outcome = execute_install_plan(ip, live, timeout=float(args.timeout))
            plan.setdefault("install_outcomes", []).append(asdict(outcome))
            tool = next(t for t in tools if t.id == ip.tool_id)
            if not outcome.success:
                # Persist install failure as ERROR result
                out, errp = tool.write_logs(
                    ctx,
                    json.dumps(outcome.steps_run, indent=2),
                    outcome.error or "install failed",
                )
                fail = tool.make_result(
                    status="ERROR",
                    severity="error",
                    summary=outcome.error or "installation failed",
                    started_at=utc_now_iso(),
                    command=ip.steps[-1].command if ip.steps else None,
                    exit_code=1,
                    stdout_path=out,
                    stderr_path=errp,
                    environment={"phase": "install"},
                )
                tool.save_result_json(ctx, fail)
                detections[tool.id] = DetectionResult(
                    tool.id, "ERROR", False, message=outcome.error or "install failed"
                )
                # Keep going with other tools; this tool will be ERROR/skipped below
            else:
                detections[tool.id] = tool.detect(AuditContext(
                    workspace=ctx.workspace,
                    platform=ctx.platform,
                    config=ctx.config,
                    runner=live,
                    evidence_dir=ctx.evidence_dir,
                    profile=ctx.profile,
                    timeout_seconds=ctx.timeout_seconds,
                    verbose=ctx.verbose,
                    include=ctx.include,
                    exclude=ctx.exclude,
                    extras=ctx.extras,
                ))

    if args.update_lockfile:
        if not args.yes:
            try:
                answer = input("Authorize `cargo update`? [y/N]: ")
            except EOFError:
                answer = "n"
            if answer.strip().lower() not in {"y", "yes"}:
                print("cargo update not authorized.", file=sys.stderr)
                return 2
        upd = live.run(["cargo", "update"], cwd=ctx.root, timeout=args.timeout)
        plan["cargo_update"] = upd.to_dict()
        if upd.timed_out or upd.exit_code != 0:
            print("cargo update failed; continuing with existing lockfile state.", file=sys.stderr)
        else:
            try:
                ctx.workspace = resolve_workspace(ctx.workspace.workspace_root, live)
            except WorkspaceError as exc:
                print(f"ERROR after cargo update: {exc}", file=sys.stderr)
                return 2

    results: list[ToolResult] = []
    selected_ids = [t.id for t in tools]
    req_ids = required_tool_ids(args.profile, workspace=ctx.workspace, selected_ids=selected_ids)
    missing_required: list[str] = []

    # Ensure evidence runner is live
    ctx.runner = live

    for tool in tools:
        det = detections.get(tool.id)
        if det and det.status == "ERROR" and not det.available:
            # install failed already recorded
            existing = ctx.evidence_dir / "tools" / tool.id / "result.json"
            if existing.is_file():
                data = json.loads(existing.read_text(encoding="utf-8"))
                results.append(
                    ToolResult(
                        tool_id=data["tool_id"],
                        display_name=data["display_name"],
                        status=data["status"],
                        severity=data["severity"],
                        command=data.get("command"),
                        exit_code=data.get("exit_code"),
                        started_at=data["started_at"],
                        finished_at=data["finished_at"],
                        duration_seconds=data["duration_seconds"],
                        stdout_path=data.get("stdout_path"),
                        stderr_path=data.get("stderr_path"),
                        findings=[],
                        summary=data["summary"],
                        environment=data.get("environment") or {},
                        artifact_hashes=data.get("artifact_hashes") or {},
                    )
                )
            continue
        if det and det.status == "SKIPPED":
            results.append(
                tool.skipped(ctx, det.message or "skipped", skip_kind="incompatible")
            )
            continue
        if det and not det.available:
            kind = "missing"
            results.append(tool.skipped(ctx, det.message or "tool missing and not installed", skip_kind=kind))
            if tool.id in req_ids:
                missing_required.append(tool.id)
            continue
        try:
            result = tool.run(ctx)
            if not (ctx.evidence_dir / "tools" / tool.id / "result.json").is_file():
                tool.save_result_json(ctx, result)
        except Exception as exc:  # noqa: BLE001
            out, errp = tool.write_logs(ctx, "", traceback.format_exc())
            result = tool.make_result(
                status="ERROR",
                severity="error",
                summary=f"Internal tool error: {exc}",
                started_at=utc_now_iso(),
                stdout_path=out,
                stderr_path=errp,
            )
            tool.save_result_json(ctx, result)
        results.append(result)

    after = snapshot_project_files(ctx.root)
    raw_changes = compare_snapshots(before, after)
    changes, unauthorized = classify_project_changes(
        raw_changes,
        update_lockfile=bool(args.update_lockfile),
        allow_manifest_changes=bool(args.allow_manifest_changes),
        lockfile_existed_before=lockfile_existed,
    )
    hashes_after = {k: v.to_dict() for k, v in after.items()}

    decision = decide(
        results,
        fail_on=args.fail_on,
        required_tool_ids=req_ids,
        hash_incongruence=bool(unauthorized),
        missing_required=missing_required,
    )
    if unauthorized:
        decision.reason = (
            f"Unauthorized project file changes detected: {', '.join(unauthorized)}. " + decision.reason
        )
        decision.decision = "REJECTED"
        decision.exit_code = 1

    os_arch = f"{ctx.platform.os.value} / {ctx.platform.architecture}"
    toolchain = ctx.platform.active_toolchain or ctx.platform.rustc_version or "unknown"
    md = render_markdown(
        workspace=ctx.workspace.workspace_root,
        timestamp=timestamp,
        os_arch=os_arch,
        toolchain=toolchain,
        profile=args.profile,
        decision=decision,
        results=results,
        changes=changes,
    )
    report_json = {
        "workspace": ctx.workspace.to_dict(),
        "decision": decision.to_dict(),
        "results": [r.to_dict() for r in results],
        "changes": changes,
        "required_tool_ids": req_ids,
        "config_defaults_used": ctx.config.defaults_used,
        "install_authorization": plan.get("install_authorization"),
        "timestamp_utc": timestamp,
    }
    manifest = {
        "version": __version__,
        "timestamp_utc": timestamp,
        "profile": args.profile,
        "fail_on": args.fail_on,
        "decision": decision.to_dict(),
        "platform": ctx.platform.to_dict(),
        "commands": [r.command for r in results],
        "results_status": {r.tool_id: r.status for r in results},
        "hashes_before": hashes_before,
        "hashes_after": hashes_after,
        "required_tool_ids": req_ids,
    }
    write_evidence(
        ctx.evidence_dir,
        report_md=md,
        report_json=report_json,
        manifest=manifest,
        environment=ctx.platform.to_dict(),
        workspace=ctx.workspace.to_dict(),
        changes=changes,
        plan=plan,
        hashes={"before": hashes_before, "after": hashes_after},
    )

    fmt = args.format
    if fmt in {"terminal", "both", "markdown"}:
        print(md)
        print(f"\nEvidence written to: {ctx.evidence_dir}")
    if fmt in {"json"}:
        print(json.dumps(report_json, indent=2))
    if fmt == "both":
        print(f"JSON report: {ctx.evidence_dir / 'report.json'}")

    return decision.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if args.command == "doctor":
            return cmd_doctor()
        if args.command == "list-tools":
            return cmd_list_tools()
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "audit":
            return cmd_audit(args)
        parser.error(f"Unknown command: {args.command}")
        return 3
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 3)
    except Exception as exc:  # noqa: BLE001
        print(f"INTERNAL ERROR: {exc}", file=sys.stderr)
        if "--verbose" in (argv or sys.argv):
            traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
