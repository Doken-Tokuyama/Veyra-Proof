"""Base tool plugin interface and shared helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from veyra_proof.command_runner import CommandRunner, apply_lock_policy
from veyra_proof.config import VeyraConfig
from veyra_proof.hashing import sha256_file
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


# Evidence digests collected while a tool runs, keyed by tool id then by the
# name of the file inside that tool's evidence directory.
_EVIDENCE_DIGESTS_KEY = "evidence_digests"


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

    def cargo_command(self, context: AuditContext, command: list[str]) -> list[str]:
        """The argv this tool will actually run, after the lockfile policy.

        Tools route every cargo invocation through here so that three things
        stay identical: the preview printed by ``plan``, the argv executed, and
        the command recorded in the evidence. A tool that skipped this would run
        something other than what its report claims, which is the exact failure
        this project refuses to ship.
        """
        return apply_lock_policy(
            command,
            cwd=context.root,
            allow_update=context.update_lockfile,
        )

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
        """Record a skip without destroying evidence a run already wrote.

        A tool may decide to skip *after* it ran a command and stored the real
        output with ``write_logs`` (an unpublished crate has no baseline to diff
        against; a declared fuzz target does not exist). The reason must not
        overwrite that output — it would erase what the tool actually said — so
        it is written to ``skip.txt`` and the raw logs are only created when the
        tool never got to write any.
        """
        now = utc_now_iso()
        tool_dir = context.evidence_dir / "tools" / self.id
        tool_dir.mkdir(parents=True, exist_ok=True)
        skip_path = tool_dir / "skip.txt"
        skip_path.write_text(reason + "\n", encoding="utf-8")
        self._pin_evidence(context, skip_path)

        stdout_path = tool_dir / "stdout.log"
        stderr_path = tool_dir / "stderr.log"
        if stdout_path.exists():
            if not stderr_path.exists():
                stderr_path.write_text("", encoding="utf-8")
            out, err = str(stdout_path), str(stderr_path)
        else:
            out, err = self.write_logs(context, reason + "\n", "")

        tr = self.make_result(
            status="SKIPPED",
            severity=severity,
            summary=reason,
            started_at=now,
            finished_at=now,
            stdout_path=out,
            stderr_path=err,
            environment={"skip_kind": skip_kind, "skip_reason_path": str(skip_path)},
        )
        self.save_result_json(context, tr)
        return tr

    def _pin_evidence(self, context: AuditContext, path: Path) -> None:
        """Record the digest of an evidence file this tool just wrote.

        The digest is merged into ``artifact_hashes`` when the result is saved,
        so the report states what its own logs held. An edit afterwards stops
        matching the recorded value instead of passing unnoticed.
        """
        digest = sha256_file(path)
        if digest is None:
            return
        by_tool = context.extras.setdefault(_EVIDENCE_DIGESTS_KEY, {})
        by_tool.setdefault(self.id, {})[path.name] = digest

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
        self._pin_evidence(context, stdout_path)
        self._pin_evidence(context, stderr_path)
        return str(stdout_path), str(stderr_path)

    def save_result_json(self, context: AuditContext, result: ToolResult) -> None:
        import json

        # Whatever this tool wrote as evidence is pinned on the result itself, so
        # the report can be checked against the files without a second source.
        recorded = (context.extras.get(_EVIDENCE_DIGESTS_KEY) or {}).get(self.id) or {}
        for name, digest in recorded.items():
            result.artifact_hashes.setdefault(name, digest)

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
RUSTC_ERROR_HEAD_RE = re.compile(r"^error(\[E\d+\])?:", re.MULTILINE)

# cargo refuses to resolve when the lockfile is stale and --locked was passed.
# The wording has changed across cargo releases ("the lock file ... needs to be
# updated but --locked was passed" vs "cannot update the lock file ... because
# --locked was passed"), so both are matched rather than one guess.
STALE_LOCK_RE = re.compile(
    r"lock file[^\n]*?(?:needs to be updated but|cannot be updated[^\n]*?because)\s*"
    r"--(?:locked|frozen) was passed"
    r"|cannot update the lock file[^\n]*because\s*--(?:locked|frozen) was passed",
    re.IGNORECASE,
)
STALE_LOCK_MESSAGE = (
    "Cargo.lock is out of date with Cargo.toml and Veyra will not rewrite it. "
    "Resolve it yourself (`cargo generate-lockfile` / `cargo update`) or re-run with "
    "--update-lockfile to authorize the change."
)

# `#![feature(..)]` is only accepted on nightly. Benchmarks and examples often
# use it (bench = criterion, test = libtest), so `--all-targets` fails on a
# perfectly healthy stable project.
NIGHTLY_GATE_RE = re.compile(
    r"^error\[E0554\]: `#!\[feature\][^\n]*\n(?:[^\n]*\n)*?[^\n]*-->\s*(?P<file>[^\r\n:]+):(?P<line>\d+):(?P<col>\d+)",
    re.MULTILINE,
)
NIGHTLY_GATE_SUMMARY_RE = re.compile(r"^error: could not compile", re.MULTILINE)


def stale_lockfile(text: str) -> bool:
    """True when cargo refused to build because --locked found an outdated lock."""
    return bool(STALE_LOCK_RE.search(text))


def nightly_only_targets(text: str) -> list[str]:
    """Files whose only compile error is a nightly-only `#![feature]` attribute.

    An empty result means the failure is something else entirely. A non-empty
    result means `--all-targets` cannot work on this toolchain, and the caller
    has to say so instead of reporting a wall of unrelated errors.
    """
    hits = {m.group("file").replace("\\", "/") for m in NIGHTLY_GATE_RE.finditer(text)}
    if not hits:
        return []
    for line in text.splitlines():
        head = RUSTC_ERROR_HEAD_RE.match(line)
        if head is None:
            continue
        code = head.group(1)
        if code == "[E0554]" or code is None:
            # A bare `error:` is cargo's own summary of the failures above it.
            if code is None and not NIGHTLY_GATE_SUMMARY_RE.match(line):
                return []
            continue
        return []
    return sorted(hits)


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


def parse_ignored_tests(text: str, *, severity: Severity = "info") -> list[Finding]:
    findings: list[Finding] = []
    for m in TEST_IGN_RE.finditer(text):
        findings.append(
            Finding(
                rule="ignored-test",
                severity=severity,
                confidence="high",
                file=None,
                line=None,
                excerpt=m.group("name"),
                explanation=(
                    f"Ignored test observed: {m.group('name')} "
                    f"(policy.fail_on_ignored_tests={'true' if severity == 'error' else 'false'})."
                ),
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
    combined = f"{stdout}\n{stderr}"
    if stale_lockfile(combined):
        # Checked before findings on purpose: cargo's refusal is printed as an
        # `error:` line, so a parser would otherwise turn it into a lint or a
        # compile finding and blame the project for a lockfile it never touched.
        # Not the project's fault and not a pass either: the lockfile cannot be
        # checked against the manifest until somebody decides to update it.
        return "INCONCLUSIVE", "warning", STALE_LOCK_MESSAGE
    if findings:
        max_sev = max(
            (f.severity for f in findings),
            key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
        )
        if max_sev == "info":
            # Noted, not blocking. Saying "N finding(s) reported" alone reads as
            # a problem and hides that none of them can fail a build.
            return (
                "FINDINGS",
                "info",
                f"{len(findings)} informational finding(s); none blocking",
            )
        return "FINDINGS", max_sev, f"{len(findings)} finding(s) reported."
    if exit_code == 0:
        return "OK", "info", "Completed with exit code 0 and no parsed findings."
    if RUSTC_ERROR_RE.search(combined) or "error:" in combined.lower():
        return "ERROR", "error", f"Non-zero exit code {exit_code}; see logs."
    return (
        "FINDINGS",
        "error",
        f"Non-zero exit code {exit_code} without structured findings; treated as findings/error.",
    )
