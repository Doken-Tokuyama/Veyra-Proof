#!/usr/bin/env python3
"""Report whether the Freebuff Desktop account copy of the skill is current.

Freebuff Desktop keeps its own copy of every account skill under
``~/.config/freebuff-desktop/account-skills/<account>/<revision>/``. That store
is **not** a fan-out target and this script never writes to it: the app owns it,
re-materializes it from the Cloud snapshot on every sync, and overwrites anything
a local writer leaves there. It is a cache, so it is checked, not synced.

It goes stale in a way the fan-out cannot see. On every sync the app imports the
local skills in ``~/.claude/skills`` and ``~/.agents/skills`` and adds only the
names that are *missing* from the Cloud snapshot. An already-present name is
never re-imported, so a skill the repo improves after its first import keeps the
older text in Cloud indefinitely — while ``sync_skill.py --check`` stays green,
because every harness target already reads the canonical skill.

Two things are reported, because they fail differently:

* ``current.json`` — the Cloud snapshot the app last materialized. This is the
  source of truth: it is what the next sync serves to every machine.
* each materialized ``veyra-proof`` directory — a projection of that snapshot,
  and the file an agent actually reads today.

Nothing is repaired here. The only way to update the Cloud snapshot is the app's
own skill manager; the remedy is named in the output.

Exit 0 when the account copy matches the canonical skill byte for byte, or when
this machine has no account store at all (absence is not drift), 1 when it does
not match, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILL_NAME = "veyra-proof"

# Must match sync_skill.CANONICAL_REL and sync_skill.SKILL_NAME;
# `tests/test_check_account_skill.py` asserts the three cannot drift.
CANONICAL_REL = Path(".cursor") / "skills" / SKILL_NAME

DEFAULT_STORE_REL = Path(".config") / "freebuff-desktop" / "account-skills"

VERSION_LINE = re.compile(r'^\s*version:\s*"([^"]+)"\s*$', re.M)

REMEDY = (
    "Remove the synced {name} entry in the app's skill manager; the next sync\n"
    "re-imports the corrected copy from ~/.claude/skills or ~/.agents/skills.\n"
    "Nothing here is repaired locally on purpose: the store is a cache the app\n"
    "rewrites, so a local edit would be discarded within seconds."
)


class UsageError(Exception):
    """The checker cannot read the canonical skill, so it cannot judge drift.

    Raised rather than returned so ``main()`` keeps returning an exit code the way
    ``sync_skill.py`` does. It must never surface as exit 1: calling a broken
    checkout drift would be the false verdict this project exists to refuse.
    """


def skills_dir(root: Path) -> Path:
    return root / CANONICAL_REL


def canonical_files(root: Path) -> dict[str, bytes]:
    """Every file of the canonical skill, keyed by name."""
    directory = skills_dir(root)
    if not directory.is_dir():
        raise UsageError(f"canonical skill not found: {directory}")
    files = {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    if not files:
        raise UsageError(f"canonical skill is empty: {directory}")
    return files


def version_of(text: str) -> str | None:
    """The ``metadata.version`` a skill declares, or None when it declares none."""
    if not text.startswith("---\n"):
        return None
    match = VERSION_LINE.search(text.split("---", 2)[1])
    return match.group(1) if match else None


def version_key(version: str) -> tuple[int, ...]:
    """``0.10.0`` outranks ``0.4.2``; anything unparseable sorts lowest."""
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts) if parts else ()


def snapshot_skill(snapshot: object) -> dict[str, str] | None:
    """The account entry for this skill as ``{filename: text}``, or None.

    ``instructions`` is the skill body (the app writes it back out as
    ``SKILL.md``) and ``files`` are the resources beside it.
    """
    if not isinstance(snapshot, dict):
        return None
    settings = snapshot.get("settings")
    if not isinstance(settings, dict):
        return None
    for entry in settings.get("skills") or []:
        if not isinstance(entry, dict) or entry.get("name") != SKILL_NAME:
            continue
        instructions = entry.get("instructions")
        if not isinstance(instructions, str):
            return None
        skill = {"SKILL.md": instructions}
        for resource in entry.get("files") or []:
            if isinstance(resource, dict) and isinstance(resource.get("path"), str):
                contents = resource.get("contents")
                if isinstance(contents, str):
                    skill[resource["path"]] = contents
        return skill
    return None


def compare(actual: dict[str, str], expected: dict[str, bytes]) -> list[str]:
    """Every way one account copy differs from the canonical skill."""
    differences: list[str] = []
    for name in sorted(expected):
        want = expected[name]
        got = actual.get(name)
        if got is None:
            differences.append(f"{name} (absent)")
        elif got.encode("utf-8") != want:
            differences.append(
                f"{name} (differs, {len(got.encode('utf-8'))} B vs {len(want)} B)"
            )
    for name in sorted(set(actual) - set(expected)):
        differences.append(f"{name} (not in the canonical skill)")
    return differences


def verdict(actual: dict[str, str], expected: dict[str, bytes]) -> tuple[str, list[str]]:
    """``current``, ``OLDER`` or ``DIFFERS`` plus the per-file differences."""
    differences = compare(actual, expected)
    if not differences:
        return "current", differences

    theirs = version_of(actual.get("SKILL.md", ""))
    ours = version_of(expected["SKILL.md"].decode("utf-8"))
    if ours and (theirs is None or version_key(theirs) < version_key(ours)):
        return "OLDER", differences
    return "DIFFERS", differences


def describe_versions(actual: dict[str, str], expected: dict[str, bytes]) -> str:
    theirs = version_of(actual.get("SKILL.md", "")) or "no metadata.version"
    ours = version_of(expected["SKILL.md"].decode("utf-8")) or "unknown"
    return f"account {theirs} vs canonical {ours}"


def read_snapshots(store: Path) -> list[tuple[Path, object]]:
    """Every cached Cloud snapshot under the store, oldest path first."""
    found: list[tuple[Path, object]] = []
    for path in sorted(store.glob("*/current.json")):
        try:
            found.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return found


def read_projections(store: Path) -> list[Path]:
    """Every materialized copy of this skill under the store."""
    return sorted(path for path in store.glob(f"*/*/{SKILL_NAME}") if path.is_dir())


def projection_files(directory: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            files[path.name] = path.read_text(encoding="utf-8")
    return files


def report(root: Path, store: Path) -> int:
    expected = canonical_files(root)
    canonical_version = version_of(expected["SKILL.md"].decode("utf-8")) or "unknown"
    sizes = ", ".join(f"{name} {len(body)} B" for name, body in expected.items())

    print(f"canonical  {skills_dir(root)}")
    print(f"           version {canonical_version}  ({sizes})")
    print()

    if not store.is_dir():
        print(f"no account store at {store}")
        print("Not drift: this machine has no Freebuff Desktop skill store.")
        return 0

    print(f"store      {store}")
    print()

    stale = 0
    checked = 0

    snapshots = read_snapshots(store)
    for path, snapshot in snapshots:
        skill = snapshot_skill(snapshot)
        relative = path.relative_to(store)
        if skill is None:
            print(f"  absent   {relative}  {SKILL_NAME} is not in this snapshot")
            continue
        checked += 1
        state, differences = verdict(skill, expected)
        if state != "current":
            stale += 1
            detail = ", ".join(differences)
            print(
                f"  {state:<8} {relative}  "
                f"{describe_versions(skill, expected)}  [{detail}]"
            )
        else:
            print(f"  current  {relative}  version {canonical_version}")

    for directory in read_projections(store):
        files = projection_files(directory)
        relative = directory.relative_to(store)
        if set(files) != set(expected):
            extra = ", ".join(sorted(set(files) - set(expected)))
            missing = ", ".join(sorted(set(expected) - set(files)))
            checked += 1
            stale += 1
            print(f"  DIFFERS  {relative}  [only here: {extra or '—'}; missing: {missing or '—'}]")
            continue
        checked += 1
        state, differences = verdict(files, expected)
        if state != "current":
            stale += 1
            detail = ", ".join(differences)
            print(
                f"  {state:<8} {relative}  "
                f"{describe_versions(files, expected)}  [{detail}]"
            )
        else:
            print(f"  current  {relative}  version {canonical_version}")

    print()
    if not checked:
        print("nothing to compare: no snapshot or projection holds this skill")
        return 0

    if not stale:
        print(f"the account copy matches the canonical skill ({checked} checked)")
        return 0

    print(
        f"{stale} of {checked} checked cop{'y' if checked == 1 else 'ies'} "
        "do not match the canonical skill."
    )
    print()
    print(REMEDY.format(name=SKILL_NAME))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report whether the Freebuff Desktop account copy of the Veyra skill "
            "is current. Read-only: never writes to the store."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repo root holding the canonical copy (default: parent of this script).",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help=(
            "Account-skills store to inspect "
            f"(default: ~/{DEFAULT_STORE_REL})."
        ),
    )
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    store = (
        args.store.expanduser().resolve()
        if args.store is not None
        else (Path.home() / DEFAULT_STORE_REL)
    )
    try:
        return report(root, store)
    except UsageError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
