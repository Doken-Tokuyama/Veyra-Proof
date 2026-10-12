"""Optional veyra.toml loading with safe defaults."""

from __future__ import annotations

import re
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
class E2eScenario:
    """One declared journey through the built binary.

    ``argv`` is appended to the executable cargo produced; expectations are
    matched against what the process actually printed and returned. A scenario
    that declares nothing beyond "exits 0" is reported as vacuous (E2E00): it
    would pass for a binary that only prints its own version of "hello".
    """

    name: str
    argv: list[str]
    stdin: str = ""
    expect_exit: int | None = None
    """Explicit exit code. ``None`` means "no exact code declared": the default
    is 0, or "any non-zero" when ``expect_failure`` is set."""
    expect_stdout_regex: str | None = None
    expect_stderr_regex: str | None = None
    expect_files: list[str] = field(default_factory=list)
    expect_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E2eConfig:
    build_profile: str = "debug"
    features: list[str] = field(default_factory=list)
    no_default_features: bool = False
    binaries: list[str] = field(default_factory=list)
    timeout_seconds: int = 60
    scenarios: list[E2eScenario] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    """Config defects found while loading. Never silently dropped."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scenarios"] = [s.to_dict() for s in self.scenarios]
        return data


@dataclass
class VeyraConfig:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    e2e: E2eConfig = field(default_factory=E2eConfig)
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


def _parse_e2e(raw: dict[str, Any]) -> E2eConfig:
    """Load ``[e2e]`` and collect every defect instead of dropping it.

    A scenario that loads wrong but is silently ignored would turn a broken
    declaration into an audit that looks green, which is the exact failure this
    tool exists to refuse. Invalid entries land in ``errors`` and surface as
    findings.
    """
    errors: list[str] = []

    build_profile = raw.get("build_profile", "debug")
    if build_profile not in {"debug", "release"}:
        errors.append(
            f"[e2e].build_profile must be 'debug' or 'release', got {build_profile!r}"
        )
        build_profile = "debug"

    try:
        timeout_seconds = int(raw.get("timeout_seconds", 60))
    except (TypeError, ValueError):
        errors.append(f"[e2e].timeout_seconds must be an integer, got {raw.get('timeout_seconds')!r}")
        timeout_seconds = 60
    if timeout_seconds <= 0:
        errors.append(f"[e2e].timeout_seconds must be positive, got {timeout_seconds}")
        timeout_seconds = 60

    def _str_list(key: str) -> list[str]:
        value = raw.get(key, []) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"[e2e].{key} must be a list of strings, got {value!r}")
            return []
        return list(value)

    scenarios: list[E2eScenario] = []
    raw_scenarios = raw.get("scenarios", []) or []
    if not isinstance(raw_scenarios, list):
        errors.append(f"[e2e].scenarios must be an array of tables, got {type(raw_scenarios).__name__}")
        raw_scenarios = []

    seen_names: set[str] = set()
    for index, entry in enumerate(raw_scenarios, start=1):
        where = f"[[e2e.scenarios]] #{index}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table, got {type(entry).__name__}")
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: missing 'name' (a scenario nobody can name cannot be reported)")
            continue
        if name in seen_names:
            errors.append(f"{where}: duplicate scenario name {name!r}")
            continue
        seen_names.add(name)

        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            errors.append(f"{where} ({name}): 'argv' must be a list of strings")
            continue

        expect_exit: int | None = None
        if "expect_exit" in entry:
            declared_exit = entry["expect_exit"]
            if not isinstance(declared_exit, int) or isinstance(declared_exit, bool):
                errors.append(f"{where} ({name}): 'expect_exit' must be an integer")
                continue
            expect_exit = declared_exit

        expect_failure = bool(entry.get("expect_failure", False))
        if expect_failure and expect_exit == 0:
            errors.append(
                f"{where} ({name}): expect_failure=true contradicts an explicit "
                "expect_exit=0 (a failure is a non-zero exit)"
            )
            continue

        regexes: dict[str, str | None] = {}
        for key in ("expect_stdout_regex", "expect_stderr_regex"):
            pattern = entry.get(key)
            if pattern is None:
                regexes[key] = None
                continue
            if not isinstance(pattern, str):
                errors.append(f"{where} ({name}): '{key}' must be a string")
                regexes[key] = None
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{where} ({name}): '{key}' is not a valid regex: {exc}")
                regexes[key] = None
                continue
            regexes[key] = pattern

        files = entry.get("expect_files", []) or []
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            errors.append(f"{where} ({name}): 'expect_files' must be a list of strings")
            files = []

        stdin = entry.get("stdin", "")
        if not isinstance(stdin, str):
            errors.append(f"{where} ({name}): 'stdin' must be a string")
            stdin = ""

        scenarios.append(
            E2eScenario(
                name=name,
                argv=list(argv),
                stdin=stdin,
                expect_exit=expect_exit,
                expect_stdout_regex=regexes["expect_stdout_regex"],
                expect_stderr_regex=regexes["expect_stderr_regex"],
                expect_files=list(files),
                expect_failure=expect_failure,
            )
        )

    return E2eConfig(
        build_profile=build_profile,
        features=_str_list("features"),
        no_default_features=bool(raw.get("no_default_features", False)),
        binaries=_str_list("binaries"),
        timeout_seconds=timeout_seconds,
        scenarios=scenarios,
        errors=errors,
    )


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
    e2e = _parse_e2e(_section(raw, "e2e"))

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
        e2e=e2e,
        source_path=str(path.resolve()),
        defaults_used=False,
    )
