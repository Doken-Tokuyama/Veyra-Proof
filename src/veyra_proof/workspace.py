"""Cargo workspace discovery via ``cargo metadata``."""

from __future__ import annotations

import json
from pathlib import Path

from veyra_proof.command_runner import CommandRunner
from veyra_proof.models import WorkspaceInfo


class WorkspaceError(Exception):
    """Raised when the path is not a resolvable Cargo workspace."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def find_cargo_toml(path: Path) -> Path | None:
    path = path.resolve()
    if path.is_file() and path.name == "Cargo.toml":
        return path
    if path.is_dir():
        candidate = path / "Cargo.toml"
        if candidate.is_file():
            return candidate
        # Walk parents up to filesystem root for nested crates
        for parent in path.parents:
            candidate = parent / "Cargo.toml"
            if candidate.is_file():
                return candidate
    return None


def resolve_workspace(path: str | Path, runner: CommandRunner | None = None) -> WorkspaceInfo:
    runner = runner or CommandRunner()
    target = Path(path).resolve()
    cargo_toml = find_cargo_toml(target)
    if cargo_toml is None:
        raise WorkspaceError(
            f"No Cargo.toml found at or above {target}. Provide a Cargo package or workspace path."
        )

    start_dir = cargo_toml.parent
    cargo = "cargo"
    result = runner.run(
        [cargo, "metadata", "--format-version", "1", "--no-deps"],
        cwd=start_dir,
        timeout=120,
    )
    if result.timed_out:
        raise WorkspaceError(f"cargo metadata timed out in {start_dir}")
    if result.exit_code != 0:
        detail = (result.stderr or result.stdout or result.error or "unknown error").strip()
        raise WorkspaceError(f"cargo metadata failed (exit {result.exit_code}): {detail}")

    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"cargo metadata returned invalid JSON: {exc}") from exc

    workspace_root = Path(meta["workspace_root"]).resolve()
    lockfile = workspace_root / "Cargo.lock"
    packages = meta.get("packages", [])
    root_package = None
    resolve = meta.get("resolve")
    if resolve and resolve.get("root"):
        root_id = resolve["root"]
        for pkg in packages:
            if pkg.get("id") == root_id:
                root_package = pkg
                break
    if root_package is None and len(packages) == 1:
        root_package = packages[0]

    return WorkspaceInfo(
        path=str(target),
        workspace_root=str(workspace_root),
        packages=packages,
        workspace_members=list(meta.get("workspace_members", [])),
        target_directory=str(Path(meta.get("target_directory", workspace_root / "target")).resolve()),
        default_members=list(meta.get("workspace_default_members", meta.get("workspace_members", []))),
        root_package=root_package,
        has_lockfile=lockfile.is_file(),
        lockfile_path=str(lockfile) if lockfile.is_file() else None,
        metadata_raw=meta,
    )


def library_package_names(workspace: WorkspaceInfo) -> list[str]:
    names: list[str] = []
    for pkg in workspace.packages:
        if pkg.get("id") not in workspace.workspace_members and pkg.get("id") not in set(
            workspace.workspace_members
        ):
            # packages list with --no-deps only includes workspace packages
            pass
        targets = pkg.get("targets", [])
        if any(t.get("kind") and "lib" in t["kind"] for t in targets):
            names.append(pkg.get("name", ""))
    return [n for n in names if n]


def has_dylint_config(workspace_root: Path) -> bool:
    markers = [
        workspace_root / "dylint.toml",
        workspace_root / ".dylint.toml",
        workspace_root / "Cargo.toml",
    ]
    cargo = workspace_root / "Cargo.toml"
    if cargo.is_file():
        text = cargo.read_text(encoding="utf-8", errors="replace")
        if "[workspace.metadata.dylint]" in text or "[package.metadata.dylint]" in text:
            return True
    return (workspace_root / "dylint.toml").is_file() or (workspace_root / ".dylint.toml").is_file()


def has_kani_harnesses(workspace_root: Path) -> bool:
    for path in workspace_root.rglob("*.rs"):
        # Skip target directory
        parts = set(path.parts)
        if "target" in parts or ".veyra" in parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "#[kani::proof]" in text or "kani::proof" in text:
            return True
    return False


def fuzz_directory(workspace_root: Path) -> Path | None:
    fuzz = workspace_root / "fuzz"
    return fuzz if fuzz.is_dir() else None
