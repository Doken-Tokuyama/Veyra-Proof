"""Profile requirements: core vs config-gated tools."""

from __future__ import annotations

from typing import Any, Iterable

from veyra_proof.models import PlatformOS, WorkspaceInfo
from veyra_proof.tools import ALL_TOOL_CLASSES
from veyra_proof.tools.base import Tool

# Tools that always must execute successfully for a profile (when selected).
CORE_REQUIRED: dict[str, set[str]] = {
    "quick": {"rustfmt", "cargo-check", "clippy", "cargo-test", "veyra-anti-theater"},
    "standard": {"rustfmt", "cargo-check", "clippy", "cargo-test", "veyra-anti-theater"},
    "hardened": {"rustfmt", "cargo-check", "clippy", "cargo-test", "veyra-anti-theater"},
}

# May be SKIPPED with an explicit reason without making the audit INCONCLUSIVE.
CONFIG_GATED: set[str] = {
    "audit",  # needs Cargo.lock
    "dylint",
    "semver-checks",
    "miri",
    "mutants",
    "fuzz",
    "kani",
    "vet",
    "check-external-types",
    "reproducible-build",
    "semgrep",
    "codeql",
}

# Run when installed; missing without --install-missing is intentional soft-skip.
SOFT_OPTIONAL: set[str] = {
    "nextest",
    "llvm-cov",
    "deny",
    "geiger",
    "machete",
}

PROFILE_ORDER = {
    "quick": [
        "rustfmt",
        "cargo-check",
        "clippy",
        "cargo-test",
        "audit",
        "veyra-anti-theater",
    ],
    "standard": [
        "rustfmt",
        "cargo-check",
        "clippy",
        "cargo-test",
        "audit",
        "veyra-anti-theater",
        "nextest",
        "llvm-cov",
        "deny",
        "geiger",
        "machete",
        "dylint",
        "semver-checks",
    ],
    "hardened": [
        "rustfmt",
        "cargo-check",
        "clippy",
        "cargo-test",
        "audit",
        "veyra-anti-theater",
        "nextest",
        "llvm-cov",
        "deny",
        "geiger",
        "machete",
        "dylint",
        "semver-checks",
        "miri",
        "mutants",
        "fuzz",
        "kani",
        "vet",
        "check-external-types",
        # reproducible-build is opt-in only (--include / tools.enable): full target
        # tree hashing is not a reliable bit-reproducibility oracle by default.
    ],
}


def all_tools() -> list[Tool]:
    return [cls() for cls in ALL_TOOL_CLASSES]


def tool_by_id() -> dict[str, Tool]:
    return {t.id: t for t in all_tools()}


def required_tool_ids(
    profile: str,
    *,
    workspace: WorkspaceInfo | None = None,
    selected_ids: Iterable[str] | None = None,
) -> list[str]:
    """Tools that must run (not intentional-skip) for ACCEPTED."""
    selected = set(selected_ids or [])
    core = set(CORE_REQUIRED.get(profile, CORE_REQUIRED["standard"]))
    if selected:
        core &= selected
    required = set(core)
    if workspace is not None and workspace.has_lockfile and "audit" in (selected or PROFILE_ORDER.get(profile, [])):
        if not selected or "audit" in selected:
            required.add("audit")
    return sorted(required)


def registry_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.append(
        {
            "id": "cargo",
            "display_name": "cargo",
            "open_source": True,
            "license": "MIT OR Apache-2.0",
            "homepage": "https://doc.rust-lang.org/cargo/",
            "command": "cargo",
            "version_command": ["cargo", "--version"],
            "install_method": "none",
            "install_command": [],
            "supported_platforms": [p.value for p in (PlatformOS.WINDOWS, PlatformOS.MACOS, PlatformOS.LINUX)],
            "architectures": ["x86_64", "aarch64"],
            "required_rust_toolchain": None,
            "rustup_components": [],
            "prerequisites": [],
            "default_profiles": ["quick", "standard", "hardened"],
            "safety_level": "required",
            "timeout_seconds": None,
            "reasons_to_skip": [
                "Rust/Cargo must already exist; Veyra does not install rustup via curl|sh"
            ],
        }
    )
    for tool in all_tools():
        rows.append(
            {
                "id": tool.id,
                "display_name": tool.display_name,
                "open_source": tool.open_source,
                "license": tool.license,
                "homepage": tool.homepage,
                "command": tool.command,
                "version_command": tool.version_command,
                "install_method": tool.install_method,
                "install_command": tool.install_command,
                "supported_platforms": [p.value for p in tool.supported_platforms],
                "architectures": list(tool.architectures),
                "required_rust_toolchain": tool.required_rust_toolchain,
                "rustup_components": list(tool.rustup_components),
                "prerequisites": list(tool.prerequisites),
                "default_profiles": list(tool.default_profiles),
                "safety_level": tool.safety_level,
                "timeout_seconds": tool.timeout_seconds,
                "reasons_to_skip": list(tool.reasons_to_skip),
                "decision_class": (
                    "core"
                    if tool.id in set().union(*CORE_REQUIRED.values())
                    else "config_gated"
                    if tool.id in CONFIG_GATED
                    else "soft_optional"
                    if tool.id in SOFT_OPTIONAL
                    else "optional"
                ),
            }
        )
    return rows


def select_tools(
    profile: str,
    *,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    config_enable: Iterable[str] | None = None,
    config_disable: Iterable[str] | None = None,
) -> list[Tool]:
    include_set = {x.strip() for x in (include or []) if x.strip()}
    exclude_set = {x.strip() for x in (exclude or []) if x.strip()}
    enable_set = {x.strip() for x in (config_enable or []) if x.strip()}
    disable_set = {x.strip() for x in (config_disable or []) if x.strip()}

    by_id = tool_by_id()
    unknown = sorted(include_set - set(by_id) - {"cargo"})
    if include_set:
        registry_order = [t.id for t in all_tools()]
        ordered_ids = [i for i in registry_order if i in include_set]
    else:
        ordered_ids = list(PROFILE_ORDER.get(profile, PROFILE_ORDER["standard"]))
        for extra in enable_set:
            if extra in by_id and extra not in ordered_ids:
                ordered_ids.append(extra)

    selected: list[Tool] = []
    for tid in ordered_ids:
        if tid in exclude_set or tid in disable_set:
            continue
        if tid == "cargo":
            continue
        tool = by_id.get(tid)
        if tool is None:
            continue
        selected.append(tool)
    # Stash unknowns on a sentinel attribute for CLI warnings
    for tool in selected:
        setattr(tool, "_unknown_includes", unknown)
    if selected:
        setattr(selected[0], "_unknown_includes", unknown)
    elif unknown:
        # return empty but CLI can check via select_tools_meta
        pass
    return selected


def unknown_include_ids(include: Iterable[str] | None) -> list[str]:
    include_set = {x.strip() for x in (include or []) if x.strip()}
    known = set(tool_by_id()) | {"cargo"}
    return sorted(include_set - known)
