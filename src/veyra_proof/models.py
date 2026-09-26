"""Shared data models for Veyra Proof."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal


ToolStatus = Literal[
    "INSTALLED",
    "MISSING",
    "INSTALLING",
    "SKIPPED",
    "ERROR",
    "OK",
    "FINDINGS",
    "INCONCLUSIVE",
]

ResultStatus = Literal["OK", "FINDINGS", "ERROR", "SKIPPED", "INCONCLUSIVE"]
Severity = Literal["info", "warning", "error", "blocker"]
Decision = Literal["ACCEPTED", "REJECTED", "INCONCLUSIVE"]
ProfileName = Literal["quick", "standard", "hardened"]
FailOn = Literal["none", "findings", "error", "inconclusive"]
InstallMethod = Literal[
    "none",
    "rustup_component",
    "cargo_install",
    "rustup_toolchain_component",
    "manual",
]


class PlatformOS(str, Enum):
    WINDOWS = "Windows"
    MACOS = "macOS"
    LINUX = "Linux"
    OTHER = "Other"


@dataclass
class Finding:
    rule: str
    severity: Severity
    confidence: Literal["low", "medium", "high"]
    file: str | None
    line: int | None
    excerpt: str | None
    explanation: str
    recommendation: str
    column: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DetectionResult:
    tool_id: str
    status: ToolStatus
    available: bool
    version: str | None = None
    path: str | None = None
    message: str = ""
    prerequisites_met: bool = True
    missing_prerequisites: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstallStep:
    description: str
    command: list[str]
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstallPlan:
    tool_id: str
    display_name: str
    method: InstallMethod
    target_version: str | None
    steps: list[InstallStep]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "display_name": self.display_name,
            "method": self.method,
            "target_version": self.target_version,
            "steps": [s.to_dict() for s in self.steps],
            "notes": self.notes,
        }


@dataclass
class ToolResult:
    tool_id: str
    display_name: str
    status: ResultStatus
    severity: Severity
    command: list[str] | None
    exit_code: int | None
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout_path: str | None
    stderr_path: str | None
    findings: list[Finding]
    summary: str
    environment: dict[str, str] = field(default_factory=dict)
    artifact_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["findings"] = [f.to_dict() if isinstance(f, Finding) else f for f in self.findings]
        return data


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileHashSnapshot:
    path: str
    exists: bool
    sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceInfo:
    path: str
    workspace_root: str
    packages: list[dict[str, Any]]
    workspace_members: list[str]
    target_directory: str
    default_members: list[str]
    root_package: dict[str, Any] | None
    has_lockfile: bool
    lockfile_path: str | None
    metadata_raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "workspace_root": self.workspace_root,
            "packages": self.packages,
            "workspace_members": self.workspace_members,
            "target_directory": self.target_directory,
            "default_members": self.default_members,
            "root_package": self.root_package,
            "has_lockfile": self.has_lockfile,
            "lockfile_path": self.lockfile_path,
        }


@dataclass
class AuditDecision:
    decision: Decision
    reason: str
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
