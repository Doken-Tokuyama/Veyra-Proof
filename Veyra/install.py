#!/usr/bin/env python3
"""Install the Veyra Proof skill into every agent harness on this machine.

Standard library only, so it runs anywhere Python 3.10+ does. No ``curl | sh``.

    python3 install.py                        # install into every detected harness
    python3 install.py --check                # report what would change, write nothing
    python3 install.py --list                 # show detected harnesses and paths
    python3 install.py --harness claude       # only one harness (repeatable)
    python3 install.py --target ~/my/skills   # any other directory
    python3 install.py --with-cli <source>    # also install the auditor CLI
    python3 install.py --uninstall            # remove the copies this script made

``--with-cli`` takes a git URL or a local path to the veyra-proof source.
It is deliberately required: this script never invents a download location.

After ``--with-cli``, export the variable it prints so the skill can find the CLI::

    export VEYRA_PROOF_HOME="$HOME/.veyra"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SKILL_NAME = "veyra-proof"
SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / SKILL_NAME
DEFAULT_CLI_HOME = Path.home() / ".veyra"
IGNORED = {"__pycache__", ".DS_Store", ".git"}


@dataclass(frozen=True)
class Harness:
    """One place a copy of the skill belongs."""

    name: str
    skills_dir: str
    home_dir: str
    global_scope: bool = True
    """False means project-scoped: only written when that directory already exists."""

    def resolve(self, root: Path, home: Path) -> Path:
        return Path(self.skills_dir.format(root=root, home=home)) / SKILL_NAME

    def probe(self, root: Path, home: Path) -> Path:
        return Path(self.home_dir.format(root=root, home=home))

    @property
    def scope(self) -> str:
        return "global" if self.global_scope else "project"


# Locations verified against each vendor's documentation. Keep in sync with the
# table in README.md; `python3 install.py --list` prints the resolved paths.
HARNESSES: tuple[Harness, ...] = (
    Harness("cursor", "{home}/.cursor/skills", "{home}/.cursor"),
    Harness("claude", "{home}/.claude/skills", "{home}/.claude"),
    Harness("codex", "{home}/.codex/skills", "{home}/.codex"),
    Harness("antigravity", "{home}/.gemini/config/skills", "{home}/.gemini"),
    Harness(
        "freebuff",
        "{home}/.config/manicode/.agents/skills",
        "{home}/.config/manicode",
    ),
    Harness("opencode", "{home}/.config/opencode/skills", "{home}/.config/opencode"),
    Harness(
        "copilot", "{root}/.github/skills", "{root}/.github", global_scope=False
    ),
    Harness("windsurf", "{root}/.windsurf/skills", "{root}/.windsurf", global_scope=False),
    Harness("cline", "{home}/.clinerules/skills", "{home}/.clinerules"),
    Harness("goose", "{home}/.goose/skills", "{home}/.goose"),
    Harness("continue", "{root}/.continue/skills", "{root}/.continue", global_scope=False),
    Harness("trae", "{root}/.trae/skills", "{root}/.trae", global_scope=False),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_files() -> list[Path]:
    if not SKILL_SOURCE.is_dir():
        raise SystemExit(f"skill source missing: {SKILL_SOURCE}")
    found = [
        path.relative_to(SKILL_SOURCE)
        for path in sorted(SKILL_SOURCE.rglob("*"))
        if path.is_file()
        and not any(part in IGNORED for part in path.relative_to(SKILL_SOURCE).parts)
    ]
    if not found:
        raise SystemExit(f"skill source is empty: {SKILL_SOURCE}")
    return found


def copy_atomic(source: Path, destination: Path) -> None:
    """Write through a temp file and rename, so a reader never sees a partial skill."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".install.tmp")
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)


def detect(root: Path, home: Path, only: list[str] | None) -> list[tuple[Harness, Path]]:
    pairs: list[tuple[Harness, Path]] = []
    seen: set[Path] = set()
    for harness in HARNESSES:
        if only and harness.name not in only:
            continue
        destination = harness.resolve(root, home)
        if destination in seen:
            continue
        seen.add(destination)
        if not harness.probe(root, home).is_dir():
            continue
        pairs.append((harness, destination))
    return pairs


def install_skill(dry_run: bool) -> int:
    root = Path.cwd()
    home = Path.home()
    files = skill_files()
    pairs = detect(root, home, None)

    if not pairs:
        print("No supported harness detected.")
        print("Pass --target DIR to install into a directory of your choosing.")
        return 1

    changed = 0
    for harness, destination in pairs:
        drifted = [
            rel
            for rel in files
            if not (destination / rel).is_file()
            or digest(destination / rel) != digest(SKILL_SOURCE / rel)
        ]
        label = f"{harness.name}/{harness.scope}"
        if not drifted:
            print(f"  ok      {label:<18} {destination}")
            continue
        changed += 1
        detail = ", ".join(str(rel) for rel in drifted)
        if dry_run:
            print(f"  update  {label:<18} {destination}  [{detail}]")
            continue
        for rel in files:
            copy_atomic(SKILL_SOURCE / rel, destination / rel)
        print(f"  install {label:<18} {destination}  [{detail}]")

    print()
    if dry_run:
        print(f"{changed} harness(es) out of date.")
    else:
        print(f"{len(pairs)} harness(es) checked, {changed} updated.")
        print("Restart your agent so it reloads skills.")
    return 0


def uninstall(dry_run: bool) -> int:
    root = Path.cwd()
    home = Path.home()
    removed = 0
    for harness, destination in detect(root, home, None):
        if not destination.is_dir():
            continue
        removed += 1
        if dry_run:
            print(f"  would remove  {destination}")
            continue
        shutil.rmtree(destination)
        print(f"  removed       {destination}")
    print()
    print(f"{removed} copy/copies {'would be ' if dry_run else ''}removed.")
    return 0


def list_targets(root: Path, home: Path) -> int:
    rows = detect(root, home, None)
    if not rows:
        print("No supported harness detected.")
        return 1
    width = max(len(h.name) for h, _ in rows)
    print(f"{'harness'.ljust(width)}  scope     path")
    for harness, destination in rows:
        print(f"{harness.name.ljust(width)}  {harness.scope:<9} {destination}")
    print()
    print("Project-scoped harnesses (copilot, windsurf, continue, trae) appear only")
    print("when their directory already exists in the current project.")
    return 0


def install_cli(source: str, cli_home: Path) -> int:
    """Clone the auditor and build its venv so VEYRA_PROOF_HOME resolves."""
    print(f"installing the Veyra CLI into {cli_home}")

    if cli_home.exists() and any(cli_home.iterdir()):
        print(f"  {cli_home} already exists and is not empty")
        print("  remove it first, or pass --cli-home PATH")
        return 1

    try:
        if Path(source).is_dir():
            shutil.copytree(source, cli_home, ignore=shutil.ignore_patterns(".git"))
        else:
            subprocess.run(
                ["git", "clone", "--depth", "1", source, str(cli_home)], check=True
            )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  clone failed: {exc}")
        return 1

    venv = cli_home / ".venv"
    print("  creating the virtualenv (this takes a minute)")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run(
            [str(venv / "bin" / "pip"), "install", "-e", str(cli_home)], check=True
        )
    except subprocess.CalledProcessError as exc:
        print(f"  install failed: {exc}")
        return 1

    print()
    print("Done. Add this to your shell profile so the skill can find the CLI:")
    print()
    print(f'    export VEYRA_PROOF_HOME="{cli_home}"')
    print()
    print("Then verify with:")
    print(f'    "{venv / "bin" / "python"}" -m veyra_proof doctor')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the Veyra Proof skill into your agent harnesses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report which harnesses are out of date without writing anything.",
    )
    parser.add_argument(
        "--harness",
        action="append",
        metavar="NAME",
        help="Restrict to one harness (repeatable).",
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="Also install into this directory, bypassing harness detection.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List detected harnesses and exit."
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="Remove installed copies."
    )
    parser.add_argument(
        "--with-cli",
        metavar="SOURCE",
        help="Also install the auditor CLI from a git URL or local path.",
    )
    parser.add_argument(
        "--cli-home",
        type=Path,
        default=DEFAULT_CLI_HOME,
        help=f"Where to install the CLI (default: {DEFAULT_CLI_HOME}).",
    )
    args = parser.parse_args(argv)

    if args.list:
        return list_targets(Path.cwd(), Path.home())

    if args.uninstall:
        return uninstall(args.check)

    status = 0
    if args.harness or args.target:
        names = args.harness
        if names:
            unknown = set(names) - {h.name for h in HARNESSES}
            if unknown:
                print(f"unknown harness: {', '.join(sorted(unknown))}", file=sys.stderr)
                print(f"known: {', '.join(h.name for h in HARNESSES)}", file=sys.stderr)
                return 2
        status = install_skill(args.check)
        if args.target:
            print(f"  target    {'':<18} {args.target / SKILL_NAME}")
            if not args.check:
                for rel in skill_files():
                    copy_atomic(SKILL_SOURCE / rel, args.target / SKILL_NAME / rel)
    else:
        status = install_skill(args.check)

    if args.with_cli and not args.check:
        cli_status = install_cli(args.with_cli, args.cli_home)
        if cli_status:
            status = cli_status

    return status


if __name__ == "__main__":
    raise SystemExit(main())
