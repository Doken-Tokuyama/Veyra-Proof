#!/usr/bin/env python3
"""Fan the canonical Veyra skill out to every locally installed agent harness.

The canonical copy lives in this repo under ``.cursor/skills/veyra-proof``.
Every other harness gets a byte-identical copy, so a fix in one place cannot
drift from the rest.

Design follows the guarantees of the existing ``skills-sync.sh``: atomic swap
(never a torn read), hash comparison instead of mtime, and an audit log.

Usage::

    python3 scripts/sync_skill.py                     # write missing or stale copies
    python3 scripts/sync_skill.py --check             # report drift only, exit 1 if stale
    python3 scripts/sync_skill.py --prune             # report copies left by an older name
    python3 scripts/sync_skill.py --prune --apply     # ...and remove them
    python3 scripts/sync_skill.py --project           # also create project-scoped targets
    python3 scripts/sync_skill.py --all               # create targets for absent harnesses too

A harness is *detected* when its home directory exists. Without ``--all`` the
script never creates a directory for a harness you do not have installed.

Renaming the skill would otherwise leave a live copy behind in every harness,
still declaring the old name and still triggering. ``--prune`` closes that gap,
but only for directories this script can prove it owns: every copy it writes is
stamped with a :data:`MARKER` file whose name is deliberately independent of the
skill name, so the stamp survives the rename that made the copy stale. A skills
directory is shared with every other skill the user has, so anything without the
stamp is reported and left alone.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SKILL_NAME = "veyra-proof"
CANONICAL_REL = Path(".cursor") / "skills" / SKILL_NAME

# Ownership stamp for a managed copy. The filename must never track SKILL_NAME:
# it has to still be there after the rename that turned the copy into litter.
# The skill name is written inside it, so a stale copy still names its author.
MARKER = ".veyra-skill-sync"

# Directories that never belong in a materialized skill copy.
IGNORED = {"__pycache__", ".DS_Store", ".git"}


@dataclass(frozen=True)
class Target:
    """One place a copy of the canonical skill should live."""

    label: str
    scope: str
    skills_dir: str
    probe_dir: str
    """Home directory that must already exist for this harness to count as installed."""

    def resolve(self, root: Path, home: Path) -> Path:
        return Path(self.skills_dir.format(root=root, home=home)) / SKILL_NAME

    def probe(self, root: Path, home: Path) -> Path:
        return Path(self.probe_dir.format(root=root, home=home))


# Verified against each vendor's own documentation. Global paths come first so
# they win the de-duplication pass; a project copy is only a fallback.
GLOBAL_TARGETS = (
    Target("cursor", "global", "{home}/.cursor/skills", "{home}/.cursor"),
    Target("claude", "global", "{home}/.claude/skills", "{home}/.claude"),
    Target("codex", "global", "{home}/.codex/skills", "{home}/.codex"),
    Target(
        "antigravity",
        "global",
        "{home}/.gemini/config/skills",
        "{home}/.gemini",
    ),
    Target(
        "freebuff",
        "global",
        "{home}/.config/manicode/.agents/skills",
        "{home}/.config/manicode",
    ),
    Target(
        "opencode",
        "global",
        "{home}/.config/opencode/skills",
        "{home}/.config/opencode",
    ),
    Target("cline", "global", "{home}/.clinerules/skills", "{home}/.clinerules"),
    Target("goose", "global", "{home}/.goose/skills", "{home}/.goose"),
)

PROJECT_TARGETS = (
    Target("cursor", "project", "{root}/.cursor/skills", "{root}/.cursor"),
    Target("claude", "project", "{root}/.claude/skills", "{root}/.claude"),
    Target("antigravity", "project", "{root}/.agents/skills", "{root}/.agents"),
    Target("copilot", "project", "{root}/.github/skills", "{root}/.github"),
    Target("windsurf", "project", "{root}/.windsurf/skills", "{root}/.windsurf"),
    Target("opencode", "project", "{root}/.opencode/skills", "{root}/.opencode"),
    Target("continue", "project", "{root}/.continue/skills", "{root}/.continue"),
    Target("trae", "project", "{root}/.trae/skills", "{root}/.trae"),
)

LOG_PATH = Path.home() / ".local" / "share" / "veyra-skill-sync.log"
"""Audit trail. Tests monkeypatch this; never write anywhere near the real home."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_skill_files(canonical: Path) -> list[Path]:
    """Every file to copy, as paths relative to the canonical skill directory."""
    out: list[Path] = []
    for path in sorted(canonical.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORED for part in path.relative_to(canonical).parts):
            continue
        out.append(path.relative_to(canonical))
    return out


def write_atomic(path: Path, data: bytes) -> None:
    """Swap content into place so a reader never observes a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sync.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def audit(line: str) -> None:
    import datetime

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.datetime.now().isoformat()}  {line}\n")


def write_marker(destination: Path, skill_name: str) -> None:
    """Stamp a copy so a later prune can prove the directory is ours."""
    write_atomic(destination / MARKER, f"{skill_name}\n".encode("utf-8"))


def is_managed(directory: Path) -> bool:
    """True only for directories this script stamped. Everyone else is off-limits."""
    return (directory / MARKER).is_file()


def find_stale_copies(
    root: Path, home: Path, targets: list[Target]
) -> list[tuple[Target, Path]]:
    """Managed copies sitting under a name this script no longer uses.

    Scans the siblings of each destination rather than the destinations
    themselves, which is why a rename goes unnoticed otherwise: the new name
    is written and looks healthy while the old one keeps running.
    """
    canonical = (root / CANONICAL_REL).resolve()
    seen: set[Path] = set()
    found: list[tuple[Target, Path]] = []

    for target in targets:
        parent = Path(target.skills_dir.format(root=root, home=home))
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir():
                continue
            resolved = child.resolve()
            # Never the canonical copy, never the current name, never twice.
            if resolved == canonical or child.name == SKILL_NAME or resolved in seen:
                continue
            if is_managed(child):
                seen.add(resolved)
                found.append((target, child))
    return found


def select_targets(
    root: Path, home: Path, include_project: bool, include_absent: bool
) -> list[Target]:
    targets = list(GLOBAL_TARGETS)
    if include_project:
        targets += list(PROJECT_TARGETS)

    chosen: list[Target] = []
    seen: set[Path] = set()
    canonical = (root / CANONICAL_REL).resolve()

    for target in targets:
        resolved = target.resolve(root, home).resolve()
        if resolved == canonical or resolved in seen:
            continue
        seen.add(resolved)
        if not include_absent and not target.probe(root, home).is_dir():
            continue
        chosen.append(target)
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize the canonical Veyra skill into every installed harness."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repo root holding the canonical copy (default: parent of this script).",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home directory for global targets (default: the real home).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report drift without writing anything. Exit 1 if any copy is stale.",
    )
    parser.add_argument(
        "--project",
        action="store_true",
        help="Also target project-scoped directories.",
    )
    parser.add_argument(
        "--all",
        dest="include_absent",
        action="store_true",
        help="Also create directories for harnesses that are not installed.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Report managed copies left behind under an older skill name.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --prune, actually delete the reported copies.",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.prune:
        print(
            "--apply only has a meaning together with --prune; "
            "re-run as: python3 scripts/sync_skill.py --prune --apply",
            file=sys.stderr,
        )
        return 2
    if args.apply and args.check:
        print(
            "--check promises to write nothing, so it cannot honour --apply; "
            "drop one of them",
            file=sys.stderr,
        )
        return 2

    root = args.root.resolve()
    home = args.home.expanduser().resolve()
    canonical = root / CANONICAL_REL

    if not canonical.is_dir():
        print(f"canonical skill not found: {canonical}", file=sys.stderr)
        return 2

    files = iter_skill_files(canonical)
    if not files:
        print(f"canonical skill is empty: {canonical}", file=sys.stderr)
        return 2

    print(f"canonical  {canonical}")
    print(f"files      {', '.join(str(f) for f in files)}")
    print()

    stale = 0
    targets = select_targets(root, home, args.project, args.include_absent)
    if not targets:
        print("no installed harness detected — nothing to sync")
        print("pass --all to create directories for every supported harness")
        return 0

    for target in targets:
        destination = target.resolve(root, home)
        drifted: list[str] = []

        for rel in files:
            src = canonical / rel
            dst = destination / rel
            if not dst.is_file():
                drifted.append(f"{rel} (missing)")
            elif sha256_bytes(dst.read_bytes()) != sha256_bytes(src.read_bytes()):
                drifted.append(f"{rel} (stale)")

        tag = f"{target.label}/{target.scope}"
        if not drifted:
            # Stamp even a healthy copy, otherwise the first install after this
            # change stays unstamped and can never be pruned after a rename.
            if not args.check:
                write_marker(destination, SKILL_NAME)
            print(f"  ok    {tag:<20} {destination}")
            continue

        stale += 1
        detail = ", ".join(drifted)
        if args.check:
            print(f"  DRIFT {tag:<20} {destination}  [{detail}]")
            continue

        for rel in files:
            write_atomic(destination / rel, (canonical / rel).read_bytes())
        write_marker(destination, SKILL_NAME)
        audit(f"{tag} {destination} {detail}")
        print(f"  sync  {tag:<20} {destination}  [{detail}]")

    print()
    exit_code = 0
    if args.check:
        if stale:
            print(f"{stale} target(s) out of sync — run: python3 scripts/sync_skill.py")
            exit_code = 1
        else:
            print("all targets in sync")
    else:
        print(f"audit log  {LOG_PATH}")

    if args.prune:
        stale_copies = find_stale_copies(root, home, targets)
        removing = args.apply
        print()
        if not stale_copies:
            print("prune  no managed copy left behind under an older name")
        for target, path in stale_copies:
            tag = f"{target.label}/{target.scope}"
            if removing:
                shutil.rmtree(path)
                audit(f"prune {tag} {path} removed copy left by an older name")
                print(f"  prune {tag:<20} {path}")
            else:
                print(f"  STALE {tag:<20} {path}")
        if stale_copies and not removing:
            print()
            print(
                f"{len(stale_copies)} stale copy/copies — nothing was deleted; "
                "re-run with: python3 scripts/sync_skill.py --prune --apply"
            )
            return 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
