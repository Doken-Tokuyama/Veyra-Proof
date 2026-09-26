"""Base tool plugin interface and shared helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veyra_proof.command_runner import CommandRunner
from veyra_proof.config import VeyraConfig
from veyra_proof.models import (
    DetectionResult,
    Finding,
    InstallPlan,
    PlatformOS,
    ResultStatus,
    Severity,
    ToolResult,
    WorkspaceInfo,
)
from veyra_proof.platform_info import PlatformInfo


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AuditContext:
    workspace: WorkspaceInfo
    platform: PlatformInfo
    config: VeyraConfig
    runner: CommandRunner
    evidence_dir: Path
    profile: str
    timeout_seconds: float
    verbose: bool = False
    yes: bool = False
    install_missing: bool = False
    update_lockfile: bool = False
    allow_manifest_changes: bool = False
    dry_run: bool = False
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return Path(self.workspace.workspace_root)


class Tool(ABC):
    id: str = ""
    display_name: str = ""
    open_source: bool = True
    license: str = "Apache-2.0 OR MIT"
    homepage: str = ""
    command: str = ""
    version_command: list[str] = []
    install_method: str = "none"
    install_command: list[str] = []
    supported_platforms: list[PlatformOS] = [PlatformOS.WINDOWS, PlatformOS.MACOS, PlatformOS.LINUX]
    architectures: list[str] = ["x86_64", "aarch64"]
    required_rust_toolchain: str | None = None
    rustup_components: list[str] = []
    prerequisites: list[str] = []
    default_profiles: list[str] = []
    safety_level: str = "standard"
    timeout_seconds: float | None = None
    reasons_to_skip: list[str] = []

    @abstractmethod
    def detect(self, context: AuditContext) -> DetectionResult:
        ...

    def install_plan(self, context: AuditContext) -> InstallPlan | None:
        return None

    def planned_commands(self, context: AuditContext) -> list[list[str]]:
        """Exact argv lists that ``run`` would execute (no side effects)."""
        return []

    @abstractmethod
    def run(self, context: AuditContext) -> ToolResult:
        ...

    def platform_supported(self, platform: PlatformInfo) -> bool:
        if self.supported_platforms and platform.os not in self.supported_platforms:
            return False
        if self.architectures and platform.architecture not in self.architectures:
            return False
        return True

    def make_result(
        self,
        *,
        status: ResultStatus,
        severity: Severity,
        summary: str,
        started_at: str,
        finished_at: str | None = None,
        command: list[str] | None = None,
        exit_code: int | None = None,
        duration_seconds: float = 0.0,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        findings: list[Finding] | None = None,
        environment: dict[str, str] | None = None,
        artifact_hashes: dict[str, str] | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.id,
            display_name=self.display_name,
            status=status,
            severity=severity,
            command=command,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at or utc_now_iso(),
            duration_seconds=duration_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            findings=findings or [],
            summary=summary,
            environment=environment or {},
            artifact_hashes=artifact_hashes or {},
        )

    def skipped(
        self,
        context: AuditContext,
        reason: str,
        *,
        skip_kind: str = "intentional",
        severity: Severity = "info",
    ) -> ToolResult:
        now = utc_now_iso()
        out, err = self.write_logs(context, reason + "\n", "")
        tr = self.make_result(
            status="SKIPPED",
            severity=severity,
            summary=reason,
            started_at=now,
            finished_at=now,
            stdout_path=out,
            stderr_path=err,
            environment={"skip_kind": skip_kind},
        )
        self.save_result_json(context, tr)
        return tr

    def write_logs(
        self,
        context: AuditContext,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        tool_dir = context.evidence_dir / "tools" / self.id
        tool_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = tool_dir / "stdout.log"
        stderr_path = tool_dir / "stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return str(stdout_path), str(stderr_path)

    def save_result_json(self, context: AuditContext, result: ToolResult) -> None:
        import json

        tool_dir = context.evidence_dir / "tools" / self.id
        tool_dir.mkdir(parents=True, exist_ok=True)
        (tool_dir / "result.json").write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


CLIPPY_LOC_RE = re.compile(
    r"^\s*(?:--> )? (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)",
    re.MULTILINE,
)
CLIPPY_MSG_RE = re.compile(
    r"^(?P<level>error|warning)(?:\[(?P<code>[^\]]+)\])?: (?P<msg>.+)$",
    re.MULTILINE,
)
TEST_FAIL_RE = re.compile(r"^test (?P<name>.+) \.\.\. FAILED$", re.MULTILINE)
TEST_IGN_RE = re.compile(r"^test (?P<name>.+) \.\.\. ignored$", re.MULTILINE)
RUSTC_ERROR_RE = re.compile(r"^error(\[E\d+\])?:", re.MULTILINE)


def parse_clippy_output(text: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = CLIPPY_MSG_RE.match(lines[i])
        if m:
            level = m.group("level")
            code = m.group("code") or "clippy"
            msg = m.group("msg")
            file_path = None
            line_no = None
            col = None
            if i + 1 < len(lines):
                loc = re.search(r"-->\s+([^:]+):(\d+):(\d+)", lines[i + 1])
                if loc:
                    file_path, line_no, col = loc.group(1), int(loc.group(2)), int(loc.group(3))
            severity: Severity = "error" if level == "error" else "warning"
            findings.append(
                Finding(
                    rule=code,
                    severity=severity,
                    confidence="high",
                    file=file_path,
                    line=line_no,
                    column=col,
                    excerpt=msg[:200],
                    explanation=msg,
                    recommendation="Address the Clippy diagnostic or justify with documented policy.",
                )
            )
        i += 1
    return findings


def parse_test_failures(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for m in TEST_FAIL_RE.finditer(text):
        findings.append(
            Finding(
                rule="test-failure",
                severity="error",
                confidence="high",
                file=None,
                line=None,
                excerpt=m.group("name"),
                explanation=f"Test failed: {m.group('name')}",
                recommendation="Inspect the test failure output and fix the failing assertion or panic.",
            )
        )
    return findings


def parse_ignored_tests(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for m in TEST_IGN_RE.finditer(text):
        findings.append(
            Finding(
                rule="ignored-test",
                severity="error",
                confidence="high",
                file=None,
                line=None,
                excerpt=m.group("name"),
                explanation=f"Ignored test observed: {m.group('name')} (policy.fail_on_ignored_tests=true).",
                recommendation="Un-ignore the test or document an approved exception.",
            )
        )
    return findings


def classify_from_command(
    *,
    timed_out: bool,
    exit_code: int | None,
    findings: list[Finding],
    stdout: str,
    stderr: str,
    error: str | None,
) -> tuple[ResultStatus, Severity, str]:
    if timed_out:
        return "INCONCLUSIVE", "warning", "Tool timed out; result is inconclusive."
    if exit_code is None:
        return "ERROR", "error", error or "Command did not produce an exit code."
    if findings:
        max_sev = max(
            (f.severity for f in findings),
            key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
        )
        return "FINDINGS", max_sev, f"{len(findings)} finding(s) reported."
    if exit_code == 0:
        return "OK", "info", "Completed with exit code 0 and no parsed findings."
    combined = f"{stdout}\n{stderr}"
    if RUSTC_ERROR_RE.search(combined) or "error:" in combined.lower():
        return "ERROR", "error", f"Non-zero exit code {exit_code}; see logs."
    return (
        "FINDINGS",
        "error",
        f"Non-zero exit code {exit_code} without structured findings; treated as findings/error.",
    )
