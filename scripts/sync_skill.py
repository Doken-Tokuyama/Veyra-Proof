#!/usr/bin/env python3
"""Point every locally installed agent harness at the canonical Veyra skill.

The canonical copy lives in this repo under ``.cursor/skills/veyra-proof``, and
it is the only copy anybody edits. Every harness target is a **symlink** to that
directory, so there is nothing to keep in sync: an edit is live in every harness
the moment it is saved, and drift between the repo and a harness is not something
to detect — it is something that cannot happen.

That is the change. This script used to *copy* the skill into each harness and
then compare the copies by SHA-256, which meant a release could still ship a
skill that disagreed with the repo whenever somebody forgot to re-run it. See
``SKILL-COPIES.md`` for what the link costs, which is real: a link is only as
durable as this checkout, and a filesystem that cannot hold one falls back to a
copy.

``--copy`` keeps the old behavior for the machines that need it — a filesystem
without symlinks, a home on a filesystem that does not support them, a Windows
box without Developer Mode. Installed copies carry :data:`MARKER`, which is what
tells a later run that a real directory in a shared skills folder is a
*deliberate* copy rather than something to convert.

Design follows the guarantees of the existing ``skills-sync.sh``: atomic swap
(never a torn read), hash comparison instead of mtime, and an audit log.

Usage::

    python3 scripts/sync_skill.py                     # link every detected harness to canonical
    python3 scripts/sync_skill.py --check             # report only, exit 1 if a target needs repair
    python3 scripts/sync_skill.py --copy              # install real copies instead of links
    python3 scripts/sync_skill.py --prune             # install, then report targets left by an older name
    python3 scripts/sync_skill.py --prune --apply     # ...and remove them (only the removal was a dry run)
    python3 scripts/sync_skill.py --project           # also create project-scoped targets
    python3 scripts/sync_skill.py --all               # create targets for absent harnesses too

A harness is *detected* when its home directory exists. Without ``--all`` the
script never creates a directory for a harness you do not have installed.

Renaming the skill would otherwise leave a live copy behind in every harness,
still declaring the old name and still triggering. ``--prune`` closes that gap,
but only for what this script can prove it owns: a directory carrying the
:data:`MARKER` stamp, or a link whose target is inside this checkout's skills
directory. A skills directory is shared with every other skill the user has, so
anything else is reported and left alone.
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

# Ownership stamp for an installed *copy* — the only mode that writes one. A link
# needs no stamp: its target is the proof, and a stamp inside a linked directory
# would be written into the canonical copy in the repo, where it does not belong.
# The filename must never track SKILL_NAME: it has to still be there after the
# rename that turned the copy into litter. The skill name is written inside it, so
# a stale copy still names its author.
MARKER = ".veyra-skill-sync"

# How a target is installed. ``link`` is the default — the harness reads the
# canonical directory itself. ``copy`` is for the machines that cannot hold a
# symlink, and for anyone who would rather not tie a harness to the lifetime of a
# checkout; it is the only mode that can drift, and the only one that stamps.
MODE_LINK = "link"
MODE_COPY = "copy"

# Directories that never belong in a materialized skill copy.
IGNORED = {"__pycache__", ".DS_Store", ".git"}

# Four places hold this skill on one machine, and this command owns exactly one of
# them. So it says which — and names the two it never writes or reports, because an
# output that covers one place of four and stays silent about the rest invites the
# reader to conclude the rest are fine. `SKILL-COPIES.md` carries the long version;
# this is the part that has to survive on its own in a terminal.
REPORTED_COPY = (
    "harness fan-out — owned by this repository; this script links it to the "
    "canonical directory"
)

# Must match ``check_account_skill.DEFAULT_STORE_REL``; tests/test_skill_document.py
# asserts the two cannot drift apart.
ACCOUNT_STORE_REL = Path(".config") / "freebuff-desktop" / "account-skills"

# The standalone, publishable copy the sdist carries, kept byte-identical to the
# canonical skill by tests/test_skill_document.py.
PUBLISHED_REL = Path("Veyra") / "skills" / SKILL_NAME

# (label, location, who owns it and how to act on it) — printed by every run.
# Rendered with `as_posix()` so the report reads the same on every platform and
# matches SKILL-COPIES.md; a Windows-style `~\.config\…` would be the one place
# the tool names a path differently from the document that describes it.
UNREPORTED_COPIES = (
    (
        "Cloud account copy",
        f"~/{ACCOUNT_STORE_REL.as_posix()}/",
        (
            "the Freebuff Desktop app owns it and re-materializes it from Cloud;",
            "report it with python3 scripts/check_account_skill.py",
        ),
    ),
    (
        "published copy",
        f"{PUBLISHED_REL.as_posix()}/",
        (
            "this repository owns it; the suite keeps it byte-identical to the",
            "canonical skill",
        ),
    ),
)


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
    # The vendor-neutral directory of the open skill standard, shared by
    # `npx skills add` and by Freebuff Desktop's local-skill import. It is listed
    # first because it is the only global target that is not one vendor's private
    # folder, and because Freebuff Desktop reads it *after* ~/.claude/skills — so
    # a stale copy here is the one that actually gets imported.
    Target("agents", "global", "{home}/.agents/skills", "{home}/.agents"),
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


def print_other_copies() -> None:
    """Name the places this command does not own, so its silence is not a pass.

    A reader who finds a file of this skill and runs the check has to be able to
    tell which place the report is about, and what the ones missing from it are.
    """
    print()
    print("other places this skill lives, not owned or reported here:")
    for label, location, note in UNREPORTED_COPIES:
        print(f"  {label:<20} {location}")
        for line in note:
            print(f"  {'':<20} {line}")


def write_marker(destination: Path, skill_name: str) -> None:
    """Stamp a copy with its author and the mode that installed it.

    Two lines, because the stamp answers two different questions. The skill name
    lets a stale copy say who wrote it after a rename; the mode word is what makes
    a copy *declared* — the difference between a duplicate this repository keeps
    converting and a machine whose owner asked for copies.
    """
    write_atomic(destination / MARKER, f"{skill_name}\n{MODE_COPY}\n".encode("utf-8"))


def marker_mode(directory: Path) -> str | None:
    """The mode a stamp declares: ``"copy"``, ``""`` for a legacy stamp, or None.

    ``None`` means no stamp at all. The empty string means a stamp written before
    this change, which recorded only the skill name: it still proves the directory
    is ours — that is what ``--prune`` needs — but not that anybody chose copies,
    so it does not exempt the copy from being converted to a link.
    """
    if not is_managed(directory):
        return None
    try:
        lines = (directory / MARKER).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[1].strip().lower() if len(lines) > 1 else ""


def is_managed(directory: Path) -> bool:
    """True only for real directories this script stamped. Everyone else is off-limits.

    ``directory.is_symlink()`` is checked first and on purpose: a linked target IS
    the canonical skill, so asking whether *it* carries the copy stamp would answer
    about the repo's own directory — and a stray stamp there would then make the
    repo look like an installed copy of itself.
    """
    return not directory.is_symlink() and (directory / MARKER).is_file()


def link_target_of(path: Path) -> str | None:
    """The raw target of a symlink, or None. Raw, so a dangling link is still read.

    ``Path.resolve()`` is the wrong tool here: it returns the path itself for a
    broken link, which would make a link into a checkout that has since moved
    indistinguishable from no link at all — and it collapses a link to *another*
    directory into that directory, hiding which target was recorded.
    """
    try:
        return os.readlink(path)
    except OSError:
        return None


def links_to(path: Path, canonical: Path) -> bool:
    """True when ``path`` is a symlink whose recorded target is ``canonical``.

    Compared as a normalized path, so ``…/veyra-proof/`` and ``…/veyra-proof`` are
    the same target, while a `..`-relative link that lands somewhere else is not.
    """
    target = link_target_of(path)
    if target is None:
        return False
    return Path(os.path.normpath(target)) == Path(os.path.normpath(str(canonical)))


def is_our_link(path: Path, skills_parent: Path) -> bool:
    """True for a link that points into this checkout's canonical skills directory.

    A link needs no stamp, so this is what proves ownership instead: the recorded
    target has to sit under ``<repo>/<CANONICAL_REL>``'s parent. That is also what
    makes a link left behind by a *rename* prunable — the name changed, the target
    did not.
    """
    target = link_target_of(path)
    if target is None:
        return False
    parent = Path(os.path.normpath(str(skills_parent)))
    candidate = Path(os.path.normpath(target)).parent
    return candidate == parent


def drift_in_copy(destination: Path, canonical: Path, files: list[Path]) -> list[str]:
    """Per-file differences between an installed copy and the canonical skill."""
    drifted: list[str] = []
    for rel in files:
        src = canonical / rel
        dst = destination / rel
        if not dst.is_file():
            drifted.append(f"{rel} (missing)")
        elif sha256_bytes(dst.read_bytes()) != sha256_bytes(src.read_bytes()):
            drifted.append(f"{rel} (stale)")
    return drifted


def foreign_files(destination: Path, canonical: Path, files: list[Path]) -> list[str]:
    """Files in a directory that the canonical skill does not have.

    This is the guard that keeps the link mode from deleting somebody's work: a
    directory at the target that holds anything this skill does not ship is not a
    copy of this skill, whatever it is called, and it is reported rather than
    replaced. The marker is excluded because it is this script's own stamp, and
    ignored names because they never travel in a copy.
    """
    known = {rel.as_posix() for rel in files} | {MARKER}
    out: list[str] = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(destination)
        if any(part in IGNORED for part in relative.parts):
            continue
        if relative.as_posix() not in known:
            out.append(relative.as_posix())
    return out


def links_supported() -> bool:
    """Whether this machine can create a symlink at all.

    Asked rather than assumed, because the answer changes what a real directory at
    a target means. Windows refuses ``os.symlink`` without Developer Mode or an
    elevated process, and a home directory can sit on a filesystem that has no
    links (FAT, some SMB mounts). With links unavailable, a matching copy is the
    best available install — so it is ``ok``, not a duplicate to complain about. A
    check that cannot pass on a platform is one people learn to bypass.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "probe-target"
        link = Path(directory) / "probe-link"
        target.mkdir()
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


@dataclass(frozen=True)
class Plan:
    """What is at one target, and what a write run would do about it.

    ``action`` is the only field a write run acts on: ``none`` means the target is
    already what this mode wants. ``blocked`` is why ``--check`` fails — a target
    that is not the canonical skill, or that must not be replaced automatically.
    """

    tag: str
    note: str
    action: str
    blocked: bool


def plan_target(
    destination: Path,
    canonical: Path,
    files: list[Path],
    mode: str,
    can_link: bool,
) -> Plan:
    """Decide what a target needs, without changing anything.

    The order of the tests is the policy. A real directory holding files this
    skill does not ship is somebody else's work and is never replaced, whatever
    mode is in force; a stamped copy is a *deliberate* install and is left alone
    when it matches; anything else at the target is this repo's own material and
    is made canonical. Reading and writing both go through this one function, so
    ``--check`` cannot describe a state a write run would not resolve.
    """
    if destination.is_symlink():
        target = link_target_of(destination)
        if links_to(destination, canonical):
            return Plan("ok", "link → the canonical directory", "none", False)
        # Two different problems wear the same shape, and the note says which: a
        # link into a checkout that moved is dead on arrival, while a link that
        # points at somebody's working copy is alive and reading the wrong text.
        state = (
            "the target does not exist: this checkout moved or was deleted"
            if not os.path.exists(target)
            else "points somewhere else"
        )
        if mode == MODE_COPY:
            return Plan("copy", f"replaces the link to {target} ({state})", "copy", True)
        return Plan("relink", f"link → {target} ({state})", "link", True)

    if destination.is_dir():
        managed = is_managed(destination)
        drifted = drift_in_copy(destination, canonical, files)
        foreign = foreign_files(destination, canonical, files)
        if not managed and foreign:
            shown = ", ".join(foreign[:3]) + ("…" if len(foreign) > 3 else "")
            return Plan(
                "skip",
                f"holds {len(foreign)} file(s) this skill does not have: {shown}",
                "none",
                True,
            )

        declared = marker_mode(destination) == MODE_COPY
        if mode == MODE_COPY:
            if drifted:
                return Plan("copy", f"copy [{', '.join(drifted)}]", "copy", True)
            if declared:
                return Plan("ok", "copy, matching the canonical skill", "none", False)
            # Matching, but nothing has declared it a copy: a legacy stamp, or none
            # at all. Writing the declaration is what stops the next run from
            # treating the same directory as a duplicate.
            return Plan("copy", "matching; stamping it as a deliberate copy", "copy", False)

        if not can_link:
            if not drifted:
                return Plan(
                    "ok",
                    "copy, matching the canonical skill (no symlink support here)",
                    "none",
                    False,
                )
            return Plan(
                "copy",
                f"copy [{', '.join(drifted)}] (no symlink support here)",
                "copy",
                True,
            )

        if not drifted and declared:
            return Plan("ok", "copy, installed on purpose with --copy", "none", False)
        # Anything else here is this repo's own material that has not been declared
        # a copy: an install from the older sync_skill.py, or a duplicate somebody
        # made by hand. Both are converged onto the canonical directory.
        reason = (
            ", ".join(drifted)
            if drifted
            else "stamped by an earlier sync_skill.py"
            if managed
            else "no marker"
        )
        return Plan("link", f"replaces a copy [{reason}]", "link", True)

    if destination.exists():
        return Plan("skip", "not a directory or a link: left alone", "none", True)

    if mode == MODE_COPY or not can_link:
        return Plan("copy", "absent", "copy", True)
    return Plan("link", "absent", "link", True)


def remove_for_install(destination: Path) -> None:
    """Clear a target that the plan has already declared this repo's to replace."""
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.is_dir():
        shutil.rmtree(destination)


def install(
    destination: Path,
    action: str,
    canonical: Path,
    files: list[Path],
) -> str:
    """Carry out one plan, and report which of the two modes actually happened.

    A link that the filesystem refuses falls back to a copy rather than failing:
    a harness with the right skill as a copy is better than a harness with no
    skill, and the line `install` prints for that target says which one it got, so
    the fallback is visible instead of implied. The return value is that answer.
    """
    if action == "link":
        remove_for_install(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(canonical, destination, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            return MODE_LINK

    for rel in files:
        write_atomic(destination / rel, (canonical / rel).read_bytes())
    write_marker(destination, SKILL_NAME)
    return MODE_COPY


def find_stale_copies(
    root: Path, home: Path, targets: list[Target]
) -> list[tuple[Target, Path]]:
    """Copies and links this script installed under a name it no longer uses.

    Scans the siblings of each destination rather than the destinations
    themselves, which is why a rename goes unnoticed otherwise: the new name
    is installed and looks healthy while the old one keeps running.

    Two things prove ownership here, because there are two shapes to own: a real
    directory carrying the :data:`MARKER` stamp, and a symlink whose recorded
    target is inside this checkout's skills directory. A link left by a rename has
    no stamp to find — its target is the evidence — and it is also the shape that
    ``child.is_dir()`` would hide when the target no longer exists, which is why
    the existence test accepts a link as well as a directory.
    """
    skills_parent = (root / CANONICAL_REL).parent
    seen: set[str] = set()
    found: list[tuple[Target, Path]] = []

    for target in targets:
        parent = Path(target.skills_dir.format(root=root, home=home))
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            # Never the current name, and never twice: what a prune deletes is a
            # path, so two old names resolving to one target are two entries.
            if child.name == SKILL_NAME or str(child) in seen:
                continue
            if not (child.is_dir() or child.is_symlink()):
                continue
            if not (is_managed(child) or is_our_link(child, skills_parent)):
                continue
            seen.add(str(child))
            found.append((target, child))
    return found


def select_targets(
    root: Path, home: Path, include_project: bool, include_absent: bool
) -> list[Target]:
    targets = list(GLOBAL_TARGETS)
    if include_project:
        targets += list(PROJECT_TARGETS)

    chosen: list[Target] = []
    seen: set[str] = set()
    canonical = Path(os.path.normpath(str(root / CANONICAL_REL)))

    for target in targets:
        # Compared as paths, never as resolved paths. Resolving was the bug that
        # arrived with links: an installed target resolves *to* the canonical
        # directory, so every linked harness looked like the canonical copy, was
        # dropped here, and `--check` then reported "no installed harness" and
        # exited 0 — a green report for a machine it had just stopped looking at.
        literal = Path(os.path.normpath(str(target.resolve(root, home))))
        if literal == canonical or str(literal) in seen:
            continue
        seen.add(str(literal))
        if not include_absent and not target.probe(root, home).is_dir():
            continue
        chosen.append(target)
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Point every installed agent harness at the canonical Veyra skill, as a "
            "link (default) or as a copy (--copy)."
        )
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
        help=(
            "Report only, write nothing. Exit 1 when a target is not the canonical "
            "skill, or is not the install this mode asks for."
        ),
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help=(
            "Install real copies instead of links, for a filesystem or a machine "
            "that cannot hold a symlink. Copies stamp themselves as deliberate, "
            "and are the only shape that can drift."
        ),
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
        help=(
            "After the install pass, report what this script left behind under an "
            "older skill name. Only the deletion is a dry run; use --check to write "
            "nothing at all."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --prune, actually delete the reported targets.",
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

    mode = MODE_COPY if args.copy else MODE_LINK
    # Asked once, not per target: the answer is a property of the machine, and it
    # decides whether a real directory at a target is a fallback the OS forced or
    # a duplicate somebody has to repair.
    can_link = links_supported()

    print(f"copy       {REPORTED_COPY}")
    print(f"canonical  {canonical}")
    print("           the copy you edit; the harness targets below point at it or mirror it")
    print(f"files      {', '.join(str(f) for f in files)}")
    print(f"mode       {mode}" + ("" if can_link else " (this machine cannot create symlinks)"))
    print()

    stale = 0
    targets = select_targets(root, home, args.project, args.include_absent)
    if not targets:
        print("no installed harness detected — nothing to sync")
        print("pass --all to create directories for every supported harness")
        print_other_copies()
        return 0

    for target in targets:
        destination = target.resolve(root, home)
        tag = f"{target.label}/{target.scope}"
        plan = plan_target(destination, canonical, files, mode, can_link)

        if args.check or plan.action == "none":
            # One line per target either way, so a report reads the same whether it
            # is describing work it would do or work it already did.
            print(f"  {plan.tag:<6} {tag:<20} {destination}  [{plan.note}]")
            stale += 1 if plan.blocked else 0
            continue

        performed = install(destination, plan.action, canonical, files)
        audit(f"{tag} {destination} {performed} [{plan.note}]")
        print(f"  {performed:<6} {tag:<20} {destination}  [{plan.note}]")

    print()
    exit_code = 0
    if args.check:
        if stale:
            print(f"{stale} target(s) need repair — run: python3 scripts/sync_skill.py")
            exit_code = 1
        else:
            print("all targets in sync")
    else:
        print(f"audit log  {LOG_PATH}")

    print_other_copies()

    if args.prune:
        stale_copies = find_stale_copies(root, home, targets)
        removing = args.apply
        print()
        if not stale_copies:
            print("prune  no managed copy left behind under an older name")
        for target, path in stale_copies:
            tag = f"{target.label}/{target.scope}"
            if removing:
                # A link is unlinked, a directory is removed: `rmtree` refuses a
                # symlink outright, and following one would delete the canonical
                # skill it points at.
                remove_for_install(path)
                audit(f"prune {tag} {path} removed target left by an older name")
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
