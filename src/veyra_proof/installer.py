"""Safe installation of open-source Rust analysis tools."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from veyra_proof.command_runner import CommandRunner
from veyra_proof.models import InstallPlan, InstallStep


@dataclass
class InstallOutcome:
    tool_id: str
    success: bool
    steps_run: list[dict] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


def confirm_installs(plans: list[InstallPlan], *, yes: bool, prompt_fn: Callable[[str], str] | None = None) -> bool:
    if not plans:
        return True
    if yes:
        return True
    lines = ["The following installations are pending:"]
    for plan in plans:
        lines.append(f"- {plan.display_name} ({plan.tool_id}) via {plan.method}")
        if plan.target_version:
            lines.append(f"  target version: {plan.target_version}")
        for step in plan.steps:
            lines.append(f"  command: {' '.join(step.command)}")
        if plan.notes:
            lines.append(f"  notes: {plan.notes}")
    lines.append("")
    lines.append(
        "Note: cargo install compiles from source and can take 10–40+ minutes "
        "with little output unless you re-run with --verbose for live progress."
    )
    lines.append("Authorize these installations? [y/N]: ")
    message = "\n".join(lines)
    if prompt_fn is None:
        try:
            answer = input(message)
        except EOFError:
            return False
    else:
        answer = prompt_fn(message)
    return answer.strip().lower() in {"y", "yes"}


def execute_install_plan(
    plan: InstallPlan,
    runner: CommandRunner,
    *,
    timeout: float = 1800.0,
) -> InstallOutcome:
    steps_run: list[dict] = []
    stream = bool(runner.verbose)
    for step in plan.steps:
        print(
            f"[veyra] Installing {plan.display_name}: {' '.join(step.command)}",
            file=sys.stderr,
            flush=True,
        )
        if stream:
            print(
                f"[veyra] Live install output enabled (--verbose). Timeout={timeout:.0f}s.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                "[veyra] Compiling… this may take a long time with no further output. "
                "Re-run with --verbose to stream cargo progress.",
                file=sys.stderr,
                flush=True,
            )

        result = runner.run(
            step.command,
            cwd=step.cwd,
            timeout=timeout,
            stream=stream,
            stream_prefix="[install] ",
        )
        steps_run.append(
            {
                "description": step.description,
                "command": step.command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stderr_tail": (result.stderr or "")[-2000:],
                "stdout_tail": (result.stdout or "")[-2000:],
                "error": result.error,
                "streamed": stream,
            }
        )
        if result.timed_out:
            print(f"[veyra] Install timed out: {plan.display_name}", file=sys.stderr, flush=True)
            return InstallOutcome(
                tool_id=plan.tool_id,
                success=False,
                steps_run=steps_run,
                error=f"install timed out: {' '.join(step.command)}",
            )
        if result.exit_code != 0:
            print(
                f"[veyra] Install failed (exit {result.exit_code}): {plan.display_name}",
                file=sys.stderr,
                flush=True,
            )
            return InstallOutcome(
                tool_id=plan.tool_id,
                success=False,
                steps_run=steps_run,
                error=f"install failed (exit {result.exit_code}): {' '.join(step.command)}",
            )
        print(f"[veyra] Install OK: {plan.display_name}", file=sys.stderr, flush=True)
    return InstallOutcome(tool_id=plan.tool_id, success=True, steps_run=steps_run)


def rustup_component_plan(tool_id: str, display: str, component: str, toolchain: str | None = None) -> InstallPlan:
    cmd = ["rustup", "component", "add", component]
    if toolchain:
        cmd.extend(["--toolchain", toolchain])
    return InstallPlan(
        tool_id=tool_id,
        display_name=display,
        method="rustup_component",
        target_version=None,
        steps=[InstallStep(description=f"Add rustup component {component}", command=cmd)],
    )


def cargo_install_plan(tool_id: str, display: str, crate: str) -> InstallPlan:
    return InstallPlan(
        tool_id=tool_id,
        display_name=display,
        method="cargo_install",
        target_version=None,
        steps=[
            InstallStep(
                description=f"cargo install {crate} --locked",
                command=["cargo", "install", crate, "--locked"],
            )
        ],
    )
