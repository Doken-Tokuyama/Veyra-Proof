#!/usr/bin/env python3
"""The pre-release checklist as one command, and the only safe way to cut the tag.

The checklist used to be a sentence in the README — build, `twine check`, then a
real audit — with no mention of the skill copies, which is how a release can ship
a skill that disagrees with the repository that produced it. This runs every step
and refuses the release when one fails.

``--tag NAME`` performs the tag itself, after the gate passes, so the refusal is
mechanical rather than a promise. The tag is annotated — ``NAME — TITLE`` with
``--title`` — because every tag in this repository is, and ``git tag`` without
``-a`` is what quietly produces the lightweight one:

* every step must have run and passed — ``--skip`` is accepted for a quick look,
  and disqualified the moment you ask for a tag, because a tag gated by a partial
  checklist is not gated at all;
* ``NAME`` must be ``v`` plus the version ``pyproject.toml`` declares, or the tag
  would name a release that is not the one being built;
* the tag must not already exist;
* the **tracked** skill files must be committed. This is the one that actually
  protects a release: `publish.yml` builds the sdist from the tagged commit, so a
  working-tree skill that matches every local copy is still not what ships.

What this gate does not do, stated plainly because the README promises nothing it
cannot keep: git has no pre-tag hook. Anyone can run ``git tag`` directly and
bypass all of this. The gate is real only for people who tag through it.

Exit 0 when the release is ready (and tagged, if asked), 1 when the gate refuses,
2 on misuse.

Usage::

    python3 scripts/pre_release.py
    python3 scripts/pre_release.py --skip evidence       # inspection only
    python3 scripts/pre_release.py --tag v0.4.3 --title "one line the release reads by"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

SKILL_PATHS = (".cursor/skills/veyra-proof", "Veyra/skills/veyra-proof")


@dataclass(frozen=True)
class Step:
    """One item on the checklist, as the commands that carry it out.

    Each command is a thunk, called immediately before it runs rather than when
    the step is built. That is what lets ``twine check`` see the artefacts
    `python -m build` just wrote: building every argv up front made the check
    validate whatever a *previous* release had left in ``dist/``, and on a clean
    checkout it would have validated nothing at all while still reporting PASS.
    """

    name: str
    why: str
    commands: tuple[Callable[[], list[str]], ...]


@dataclass(frozen=True)
class Result:
    step: Step
    status: int
    output: str

    @property
    def ok(self) -> bool:
        return self.status == 0


def release_artifacts(root: Path) -> list[str]:
    """The artefacts of the version being released, and nothing older.

    A ``dist/`` left over from a previous release contains files that are not this
    release's evidence; validating them says nothing about what was just built.
    """
    version = declared_version(root)
    return sorted(
        str(path)
        for path in (root / "dist").glob(f"*{version}*")
        if path.is_file()
    )


def twine_command(root: Path) -> list[str]:
    """Validate the artefacts just built, or fail saying there were none.

    Without this, an empty ``dist/`` sends twine its own usage error and a stale
    ``dist/`` sends it another release's files; both would read as a build check
    that passed.
    """
    artifacts = release_artifacts(root)
    if not artifacts:
        return [
            sys.executable,
            "-c",
            "import sys; print('no sdist or wheel was written to dist/ for this "
            "version'); sys.exit(1)",
        ]
    return [sys.executable, "-m", "twine", "check", *artifacts]


def default_steps(root: Path) -> list[Step]:
    """The four steps of the release checklist, in the order they are run."""
    scripts = root / "scripts"
    return [
        Step(
            "tests",
            "the suite, which carries the copy-parity, version and docs guards",
            (lambda: [sys.executable, "-m", "pytest"],),
        ),
        Step(
            "copies",
            "every harness copy of the skill against the canonical one",
            (lambda: [sys.executable, str(scripts / "check_skills.py")],),
        ),
        Step(
            "build",
            "an sdist and a wheel, then twine's validation of both",
            (
                lambda: [sys.executable, "-m", "build"],
                lambda: twine_command(root),
            ),
        ),
        Step(
            "evidence",
            "a real audit whose on-disk evidence matches its own verdict",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "ci_assert_evidence.py"),
                    "tests/fixtures/demo_crate",
                ],
            ),
        ),
    ]


def run_step(step: Step, root: Path) -> Result:
    """Run every command of one step, stopping at the first that fails."""
    output: list[str] = []
    status = 0
    for build_argv in step.commands:
        argv = build_argv()
        output.append("$ " + " ".join(argv))
        completed = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        tail = (completed.stdout + completed.stderr).rstrip()
        if tail:
            output.append(tail)
        status = completed.returncode
        if status != 0:
            break
    return Result(step, status, "\n".join(output))


def declared_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def skill_release_block(root: Path) -> str | None:
    """Why the skill is not fit to tag, or None when it is committed and clean.

    Tracking is checked before cleanliness on purpose. ``git status`` hides ignored
    paths, so a skill file matched by someone's global ignore rule reads as clean
    while git would never commit it — and a release built from the tagged commit
    would then carry whatever older version *is* committed, silently.
    """
    present: list[str] = []
    for base in SKILL_PATHS:
        directory = root / base
        if not directory.is_dir():
            return f"{base} is not in this checkout, so nothing would be released"
        present += [
            str(path.relative_to(root))
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        ]

    tracked = set(
        subprocess.run(
            ["git", "ls-files", "--", *SKILL_PATHS],
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    untracked = [path for path in present if path not in tracked]
    if untracked:
        return (
            "git does not track these skill files, so a tag would release whatever "
            "older version is committed instead:\n" + "\n".join(untracked)
        )

    completed = subprocess.run(
        ["git", "status", "--porcelain", "--", *SKILL_PATHS],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return completed.stderr.strip() or "git could not report the skill's status"
    if completed.stdout.strip():
        return (
            "the tracked skill files have uncommitted changes, so the tag would "
            "release something other than what this gate checked:\n"
            + completed.stdout.strip()
        )
    return None


def tag_refusal(root: Path, name: str, skipped: list[str]) -> str | None:
    """Why the tag must not be created, or None when it may be."""
    if skipped:
        return (
            f"--skip was used ({', '.join(skipped)}), so this release was not gated "
            "by the complete checklist"
        )
    declared = declared_version(root)
    if name != f"v{declared}":
        return f"{name} does not name the declared version v{declared}"
    existing = subprocess.run(
        ["git", "tag", "-l", name], cwd=root, capture_output=True, text=True
    )
    if existing.stdout.strip():
        return f"{name} already exists"
    return skill_release_block(root)


def tag_message(name: str, title: str | None) -> str:
    """The annotation every tag in this repository carries.

    ``vX.Y.Z — <headline>``, which is the same string GitHub shows as the release
    name. A lightweight tag here would be the one tag in the history that
    ``git describe`` and the release page disagree about — and `git tag` without
    ``-a`` is what produces it, so the gate creates the annotated form itself
    rather than leaving it to whoever remembers.
    """
    return f"{name} — {title}" if title else name


def create_tag(root: Path, name: str, title: str | None = None) -> int:
    message = tag_message(name, title)
    completed = subprocess.run(
        ["git", "tag", "-a", name, "-m", message],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        print(f"  could not create {name}: {completed.stderr.strip()}")
        return 1
    print(f"  tagged {name} — {title}" if title else f"  tagged {name}")
    return 0


def main(argv: list[str] | None = None, steps: list[Step] | None = None) -> int:
    """Run the checklist. ``steps`` exists so tests can gate without building a wheel."""
    parser = argparse.ArgumentParser(
        description="The pre-release checklist, in one command, with tag gating."
    )
    parser.add_argument("--root", type=Path, default=None, help="Checkout to release.")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STEP",
        help="Skip a step for a quick look. A tag is refused when this is used.",
    )
    parser.add_argument(
        "--tag",
        default=None,
        metavar="NAME",
        help="Create this tag once every step has passed.",
    )
    parser.add_argument(
        "--title",
        default=None,
        metavar="TEXT",
        help=(
            "Headline for the tag annotation, the same string the release uses "
            "(`vX.Y.Z — TEXT`). Without it the annotation is bare `NAME`."
        ),
    )
    args = parser.parse_args(argv)

    root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    checklist = default_steps(root) if steps is None else steps

    known = [step.name for step in checklist]
    unknown = [name for name in args.skip if name not in known]
    if unknown:
        print(f"unknown step(s) {', '.join(unknown)}; known: {', '.join(known)}", file=sys.stderr)
        return 2
    if args.tag and args.skip:
        print(
            "--skip and --tag cannot be combined: a tag gated by a partial checklist "
            "is not gated",
            file=sys.stderr,
        )
        return 2
    if args.title and not args.tag:
        print(
            "--title describes a tag, so it needs --tag; nothing was titled",
            file=sys.stderr,
        )
        return 2

    skipped = [step.name for step in checklist if step.name in args.skip]
    if skipped:
        print(f"skipping: {', '.join(skipped)} — inspection only, cannot tag")
        print()

    results = [
        run_step(step, root) for step in checklist if step.name not in args.skip
    ]

    print("=== checklist")
    for result in results:
        mark = "PASS" if result.ok else f"FAIL ({result.status})"
        print()
        print(f"--- {result.step.name}: {mark}")
        print(f"    {result.step.why}")
        print(result.output if result.output else "    (no output)")
    for step in checklist:
        if step.name in args.skip:
            print()
            print(f"--- {step.name}: NOT RUN (skipped)")
            print(f"    {step.why}")

    failed = [result for result in results if not result.ok]

    print()
    print("=== verdict")
    if failed:
        print(f"  FAIL  {len(failed)} of {len(results)} step(s) failed: "
              f"{', '.join(result.step.name for result in failed)}")
        print("        The release is not ready; a tag would name artifacts that fail these checks.")
        return 1

    print(f"  PASS  all {len(results)} step(s) passed.")
    print(
        "        A WARN about the Freebuff Desktop account copy is not a blocker: that\n"
        "        copy is a machine-local cache of the Cloud snapshot and does not ship\n"
        "        with a release."
    )

    if args.tag:
        print()
        refusal = tag_refusal(root, args.tag, skipped)
        if refusal:
            print(f"  REFUSED  {refusal}")
            return 1
        return create_tag(root, args.tag, args.title)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
