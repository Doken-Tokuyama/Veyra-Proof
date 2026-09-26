"""Optional veyra.toml loading with safe defaults."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class ProjectConfig:
    critical_paths: list[str] = field(default_factory=list)


@dataclass
class PolicyConfig:
    deny_unsafe: bool = False
    deny_ffi: bool = False
    deny_process: bool = False
    deny_network_in_pure_modules: bool = False
    fail_on_ignored_tests: bool = True
    fail_on_todo: bool = True
    fail_on_unimplemented: bool = True
    require_lockfile: bool = False


@dataclass
class ToolsConfig:
    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)


@dataclass
class MutantsConfig:
    paths: list[str] = field(default_factory=list)
    timeout_seconds: int = 120


@dataclass
class FuzzConfig:
    targets: list[str] = field(default_factory=list)
    max_total_time_seconds: int = 120


@dataclass
class KaniConfig:
    enabled: bool = False


@dataclass
class CoverageConfig:
    minimum_line_percent: float | None = None
    minimum_branch_percent: float | None = None
    critical_paths_minimum_percent: float | None = None


@dataclass
class ReportConfig:
    include_logs_for_failed_tools: bool = True


@dataclass
class CodeQLConfig:
    database: str | None = None
    query_suite: str | None = None


@dataclass
class VeyraConfig:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mutants: MutantsConfig = field(default_factory=MutantsConfig)
    fuzz: FuzzConfig = field(default_factory=FuzzConfig)
    kani: KaniConfig = field(default_factory=KaniConfig)
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    codeql: CodeQLConfig = field(default_factory=CodeQLConfig)
    source_path: str | None = None
    defaults_used: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def load_veyra_config(workspace_root: Path) -> VeyraConfig:
    path = workspace_root / "veyra.toml"
    if not path.is_file():
        return VeyraConfig(defaults_used=True, source_path=None)

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    project = _section(raw, "project")
    policy = _section(raw, "policy")
    tools = _section(raw, "tools")
    mutants = _section(raw, "mutants")
    fuzz = _section(raw, "fuzz")
    kani = _section(raw, "kani")
    coverage = _section(raw, "coverage")
    report = _section(raw, "report")
    codeql = _section(raw, "codeql")

    return VeyraConfig(
        project=ProjectConfig(critical_paths=list(project.get("critical_paths", []) or [])),
        policy=PolicyConfig(
            deny_unsafe=bool(policy.get("deny_unsafe", False)),
            deny_ffi=bool(policy.get("deny_ffi", False)),
            deny_process=bool(policy.get("deny_process", False)),
            deny_network_in_pure_modules=bool(policy.get("deny_network_in_pure_modules", False)),
            fail_on_ignored_tests=bool(policy.get("fail_on_ignored_tests", True)),
            fail_on_todo=bool(policy.get("fail_on_todo", True)),
            fail_on_unimplemented=bool(policy.get("fail_on_unimplemented", True)),
            require_lockfile=bool(policy.get("require_lockfile", False)),
        ),
        tools=ToolsConfig(
            enable=list(tools.get("enable", []) or []),
            disable=list(tools.get("disable", []) or []),
        ),
        mutants=MutantsConfig(
            paths=list(mutants.get("paths", []) or []),
            timeout_seconds=int(mutants.get("timeout_seconds", 120)),
        ),
        fuzz=FuzzConfig(
            targets=list(fuzz.get("targets", []) or []),
            max_total_time_seconds=int(fuzz.get("max_total_time_seconds", 120)),
        ),
        kani=KaniConfig(enabled=bool(kani.get("enabled", False))),
        coverage=CoverageConfig(
            minimum_line_percent=coverage.get("minimum_line_percent"),
            minimum_branch_percent=coverage.get("minimum_branch_percent"),
            critical_paths_minimum_percent=coverage.get("critical_paths_minimum_percent"),
        ),
        report=ReportConfig(
            include_logs_for_failed_tools=bool(report.get("include_logs_for_failed_tools", True)),
        ),
        codeql=CodeQLConfig(
            database=codeql.get("database"),
            query_suite=codeql.get("query_suite"),
        ),
        source_path=str(path.resolve()),
        defaults_used=False,
    )
