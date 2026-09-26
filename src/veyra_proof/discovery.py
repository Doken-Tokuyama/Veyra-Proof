"""Environment and tool discovery helpers."""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from typing import Any

from veyra_proof.command_runner import CommandRunner
from veyra_proof.platform_info import PlatformInfo, detect_platform
from veyra_proof.tool_registry import all_tools
from veyra_proof.tools.base import AuditContext
from veyra_proof.config import VeyraConfig
from veyra_proof.models import WorkspaceInfo
from pathlib import Path


@dataclass
class DoctorReport:
    platform: dict[str, Any]
    cargo_ok: bool
    rustc_ok: bool
    rustup_ok: bool
    notes: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def which_command(name: str) -> str | None:
    return shutil.which(name)


def version_of(runner: CommandRunner, command: list[str], timeout: float = 20.0) -> str | None:
    result = runner.run(command, timeout=timeout)
    if result.timed_out or result.exit_code != 0:
        return None
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0] if text else None


def run_doctor(runner: CommandRunner | None = None) -> DoctorReport:
    runner = runner or CommandRunner()
    platform_info: PlatformInfo = detect_platform(runner)
    notes: list[str] = []
    if not platform_info.cargo_path:
        notes.append("Cargo not found on PATH. Install Rust from https://rustup.rs (manual; Veyra will not curl|sh).")
    if not platform_info.rustup_path:
        notes.append("rustup not found. Component installs (clippy/rustfmt/miri) will be unavailable.")
    if platform_info.os.value == "Other":
        notes.append(f"Unsupported OS raw={platform_info.os_raw!r}; tool compatibility may be limited.")

    # Probe registered tools without a workspace (synthetic context)
    fake_ws = WorkspaceInfo(
        path=".",
        workspace_root=".",
        packages=[],
        workspace_members=[],
        target_directory="target",
        default_members=[],
        root_package=None,
        has_lockfile=False,
        lockfile_path=None,
        metadata_raw={},
    )
    ctx = AuditContext(
        workspace=fake_ws,
        platform=platform_info,
        config=VeyraConfig(),
        runner=runner,
        evidence_dir=Path("."),
        profile="doctor",
        timeout_seconds=30.0,
    )
    tools_status: list[dict[str, Any]] = []
    for tool in all_tools():
        if tool.id in {"semgrep", "codeql", "reproducible-build", "veyra-anti-theater"}:
            det = tool.detect(ctx)
            tools_status.append(det.to_dict())
            continue
        if not tool.platform_supported(platform_info):
            tools_status.append(
                {
                    "tool_id": tool.id,
                    "status": "SKIPPED",
                    "available": False,
                    "message": "incompatible platform",
                }
            )
            continue
        try:
            det = tool.detect(ctx)
            tools_status.append(det.to_dict())
        except Exception as exc:  # noqa: BLE001
            tools_status.append(
                {
                    "tool_id": tool.id,
                    "status": "ERROR",
                    "available": False,
                    "message": str(exc),
                }
            )

    return DoctorReport(
        platform=platform_info.to_dict(),
        cargo_ok=bool(platform_info.cargo_path and platform_info.cargo_version),
        rustc_ok=bool(platform_info.rustc_path and platform_info.rustc_version),
        rustup_ok=bool(platform_info.rustup_path and platform_info.rustup_version),
        notes=notes,
        tools=tools_status,
    )
