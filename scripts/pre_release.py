#!/usr/bin/env python3
"""The pre-release checklist as one command, and the only safe way to cut the tag.

The checklist used to be a sentence in the README — build, `twine check`, then a
real audit — with no mention of the skill copies, which is how a release can ship
a skill that disagrees with the repository that produced it. This runs every step
and refuses the release when one fails.

The steps are the suite, that same suite on the other platforms via the CI run for
this commit, the skill fan-out, a build validated by `twine check`, that same wheel
installed into a throwaway virtual environment where it has to report the version it
was built as, a real audit whose evidence has to match its verdict, and the question
no local step can answer: what the *index* serves for this version, compared with the
artifact this gate checked. Three of those exist because of releases that went out
wrong: 0.4.1 shipped a wheel whose `__version__` still said `0.3.0` while the
distribution said 0.4.1, so every manifest that wheel wrote signed the evidence with
the wrong version; 0.4.3 was tagged and published here, after which CI came back red
on both Windows jobs, which is how the fix had to become 0.4.4; and 0.5.0 shipped with
the index comparison made by hand, once, in a terminal, because nothing in the gate
had ever opened the file a user actually installs. The local machine cannot see the
first of those at all — it runs one platform — and it cannot see the last one either,
unless it asks.

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

It also resumes. A step that passes is recorded against a fingerprint of the tree
(HEAD plus the contents of the tracked and untracked-but-unignored files), so a run
that dies between the checklist and the release does not start over: the next run
reports what was already verified on that exact tree as ``PASS (resumed)`` and goes
straight on to the tag. A tag this gate created on an earlier attempt is recognised
as its own and resumed rather than refused. Any change to the tree invalidates every
record, and a failure is never recorded at all, so resuming can only ever skip a
success — never inherit one. Two steps opt out of resuming, because their answer is not
a property of the tree: the notes are re-derived every run (they are a file somebody can
delete), and the index is re-asked every run (it changes while the tree does not, which
is how the release notes can state what the index serves on a run that made no change).

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
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

SKILL_PATHS = (".cursor/skills/veyra-proof", "Veyra/skills/veyra-proof")

STATE_NAME = "veyra-pre-release.json"
"""The resume ledger's filename, inside ``.git`` — see :func:`default_state`."""

REPORTS_DIR_NAME = "veyra-release-reports"
"""Where each step states its measurements, beside the ledger, out of the tree."""

DEFERRED = ("notes",)
"""Steps that read what the other steps measured, so they run after the record exists.

`notes` is the release notes: they are assembled from the facts the run just
recorded, which means the record has to be written before they can be. It is a
real checklist step all the same — it can fail, and then the tag is refused.
"""


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
    report: Path | None = None
    """Where this step states what it measured, in machine-readable form.

    A step that can prove a number writes it here — as JSON, or as the JUnit XML the
    test runner produces — and the gate records it. The release notes are then built
    out of recorded facts rather than out of paragraphs somebody read off a terminal.
    """
    collect: Callable[[Path], dict | None] | None = None
    """How to read that report, when a step does not write the shared JSON shape."""
    resumable: bool = True
    """Whether the resume ledger may skip this step on an unchanged tree.

    False for a step whose product is a file somebody could delete: the record would
    then say "verified" about a note that is not there. `notes` is cheap (0.17 s) and
    derived from the record, so it is simply re-derived on every run.
    """


@dataclass(frozen=True)
class Result:
    step: Step
    status: int
    output: str
    resumed_at: str | None = None
    """When this step was verified on this exact tree, if it was not run again."""
    duration_s: float | None = None
    facts: dict | None = None
    """What the step measured, when it could say so. Absent is not zero."""

    @property
    def ok(self) -> bool:
        return self.status == 0


def default_state(root: Path) -> Path:
    """Where the resume ledger lives: inside ``.git``.

    Not in the working tree, because it is not part of the product: a file nobody
    commits, nobody sees in ``git status``, and that disappears with the clone is
    exactly what this is — a note the checkout keeps for itself about what it has
    already verified.

    Asked of git rather than assumed to be ``root/.git``, because the two differ in
    a linked worktree: there ``.git`` is a *file* pointing at the checkout's real git
    directory, so appending to it raised ``FileExistsError`` and the gate died with a
    traceback instead of running the checklist. Releasing from a worktree is exactly
    how a release branch gets prepared, which is how that was found.
    """
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    return (Path(git_dir) if git_dir else root / ".git") / STATE_NAME


def tree_fingerprint(root: Path) -> str:
    """A hash of everything a step could read: HEAD, plus the file contents.

    Tracked *and* untracked-but-unignored files, because a step can depend on a file
    that is not committed yet. Ignored paths — build output, the virtualenv, the
    audit's evidence directory — are left out on purpose: those are results, not
    inputs, and hashing them would invalidate the ledger every time a step ran.

    Returns ``""`` when the tree cannot be hashed. An empty fingerprint resumes
    nothing, because a cache that cannot be tied to an exact tree is not evidence.
    """
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if head.returncode != 0 or listing.returncode != 0:
        return ""

    digest = hashlib.sha256()
    digest.update(head.stdout.strip().encode())
    for name in sorted(filter(None, listing.stdout.split("\0"))):
        digest.update(name.encode("utf-8"))
        try:
            digest.update((root / name).read_bytes())
        except OSError:
            digest.update(b"\0unreadable")
    return digest.hexdigest()


def read_ledger(path: Path) -> dict:
    """The ledger, or an empty one: a cache that cannot be read is not a cache."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_ledger(path: Path, ledger: dict) -> bool:
    """Write the ledger through a temp file, so a reader never sees half of it.

    Returns whether it was recorded. A ledger that cannot be written costs a resume
    and nothing else, so it must not cost the release: this is a cache, and a cache
    that can end a run is a worse bug than the one it was added to prevent.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def resumed_measurement(ledger: dict, step: str) -> dict:
    """The (duration, facts) a resumed step already has, if it recorded any."""
    measured = (ledger.get("measured") or {}).get(step)
    if not isinstance(measured, dict):
        return {}
    return {"duration_s": measured.get("duration_s"), "facts": measured.get("facts")}


def resumed_at(ledger: dict, version: str | None, fingerprint: str, step: str) -> str | None:
    """When this step was verified on exactly this tree, or None.

    Three things have to agree: the version being released, the fingerprint of the
    tree, and the record itself. Only passes are ever stored, so a failure has no
    representation to be resumed from — a resumed run cannot turn a failure into a
    pass, it can only skip repeating a success.
    """
    if not fingerprint or version is None:
        return None
    if ledger.get("version") != version or ledger.get("fingerprint") != fingerprint:
        return None
    record = (ledger.get("steps") or {}).get(step)
    return record if isinstance(record, str) else None


def _git(root: Path, *argv: str) -> str:
    """One git command's trimmed stdout, or "" when it fails."""
    completed = subprocess.run(
        ["git", *argv], cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def release_artifacts(root: Path) -> list[str]:
    """The artefacts of the version being released, and nothing older.

    A ``dist/`` left over from a previous release contains files that are not this
    release's evidence; validating them says nothing about what was just built.
    """
    version = declared_version(root)
    return sorted(str(path) for path in (root / "dist").glob(f"*{version}*") if path.is_file())


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


def reports_dir(root: Path) -> Path:
    """Where the steps write their measurements: beside the ledger, never in the tree.

    Not in the working tree, for the same reason the ledger is not: these are notes a
    run keeps about itself. Keeping them out also keeps the tree fingerprint honest —
    a measurement written into the tree would invalidate the record it belongs to.
    """
    return default_state(root).parent / REPORTS_DIR_NAME


def default_steps(root: Path, title: str = "") -> list[Step]:
    """The eight steps of the release checklist, in the order they are run.

    ``title`` is the release headline the notes carry. It is a parameter rather than a
    closure over argv so that a caller who only wants the shape of the checklist (the
    tests, and anyone reading the gate) can ask for it without inventing one.
    """
    scripts = root / "scripts"
    reports = reports_dir(root)
    return [
        Step(
            "tests",
            "the suite, which carries the copy-parity, version and docs guards",
            (
                lambda: [
                    sys.executable,
                    "-m",
                    "pytest",
                    # The counts and the time as data: a release note that carries a
                    # test count should read it out of a file, not out of a summary line.
                    f"--junitxml={reports / 'tests.xml'}",
                ],
            ),
            report=reports / "tests.xml",
            collect=read_junit_facts,
        ),
        Step(
            "platforms",
            "the same suite, on this commit, as the other platforms actually ran it",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "check_platforms.py"),
                    "--root",
                    str(root),
                    "--report",
                    str(reports / "platforms.json"),
                ],
            ),
            report=reports / "platforms.json",
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
            "wheel",
            "the wheel just built, installed in a throwaway venv, reporting its own version",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "check_wheel_version.py"),
                    "--root",
                    str(root),
                    "--report",
                    str(reports / "wheel.json"),
                ],
            ),
            report=reports / "wheel.json",
        ),
        Step(
            "evidence",
            "a real audit whose on-disk evidence matches its own verdict",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "ci_assert_evidence.py"),
                    "tests/fixtures/demo_crate",
                    "--report",
                    str(reports / "evidence.json"),
                ],
            ),
            report=reports / "evidence.json",
        ),
        Step(
            "published",
            "what the index serves for this version, against the artifact this gate checked",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "check_published.py"),
                    "--root",
                    str(root),
                    "--report",
                    str(reports / "published.json"),
                ],
            ),
            report=reports / "published.json",
            # Never resumed, because the answer moves without the tree moving: this step
            # records "not published yet" on the run that cuts the tag — publishing comes
            # after — and a later run on the same tree is exactly the run where there is
            # something to compare. Inheriting the earlier answer would keep the release
            # notes saying nothing was published about a version that is.
            resumable=False,
        ),
        Step(
            "notes",
            "the release notes, every number in them read out of this run's record",
            (
                lambda: [
                    sys.executable,
                    str(scripts / "release_notes.py"),
                    "--root",
                    str(root),
                    *(["--title", title] if title else []),
                ],
            ),
            resumable=False,
        ),
    ]


def run_step(step: Step, root: Path) -> Result:
    """Run every command of one step, stopping at the first that fails.

    Also keeps the clock and the measurement: how long the step took, and whatever
    it stated about what it proved. Both are read after the step, never inferred —
    a run that failed records no facts, because there is nothing to record.
    """
    output: list[str] = []
    status = 0
    started = time.monotonic()
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
    elapsed = time.monotonic() - started

    facts = None
    if status == 0 and step.report is not None:
        reader = step.collect or read_json_facts
        facts = reader(step.report)

    return Result(step, status, "\n".join(output), duration_s=elapsed, facts=facts)


def read_json_facts(path: Path) -> dict | None:
    """The facts a check wrote for itself, or None when it wrote nothing readable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    facts = payload.get("facts") if isinstance(payload, dict) else None
    return facts if isinstance(facts, dict) else None


def read_junit_facts(path: Path) -> dict | None:
    """What pytest reported, from the XML it writes rather than from its prose.

    ``-q`` prints ``417 passed in 31.90s``, and a summary line is a thing to read,
    not a thing to trust with a release. The JUnit file carries the counts and the
    time as data, and this refuses to invent a count it cannot find there.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    # A `<testsuites>` wrapper has to be unwrapped before its counts can be read.
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return None

    def number(name: str) -> int | None:
        value = suite.get(name)
        return int(value) if value is not None and value.strip().isdigit() else None

    cases = number("tests")
    failed = number("failures")
    errors = number("errors")
    if cases is None or failed is None or errors is None:
        return None
    skipped = number("skipped") or 0
    facts: dict[str, int | float] = {
        "cases": cases,
        "passed": cases - failed - errors - skipped,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }
    try:
        facts["duration_s"] = round(float(suite.get("time") or ""), 2)
    except ValueError:
        pass
    return facts


def declared_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def relative_skill_files(root: Path) -> list[str]:
    """Every skill file, named the way git names it.

    ``as_posix()`` here is not cosmetic. ``git ls-files`` prints forward slashes on
    every platform, while ``str(Path)`` on Windows yields backslashes — so comparing
    the two made every *tracked* skill file look untracked there, and the gate then
    refused every tag with "git does not track these skill files". A check that
    cannot pass on a platform is worse than no check: it teaches people to bypass
    the gate.
    """
    present: list[str] = []
    for base in SKILL_PATHS:
        directory = root / base
        if not directory.is_dir():
            continue
        present += [
            path.relative_to(root).as_posix()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        ]
    return present


def skill_release_block(root: Path) -> str | None:
    """Why the skill is not fit to tag, or None when it is committed and clean.

    Tracking is checked before cleanliness on purpose. ``git status`` hides ignored
    paths, so a skill file matched by someone's global ignore rule reads as clean
    while git would never commit it — and a release built from the tagged commit
    would then carry whatever older version *is* committed, silently.
    """
    for base in SKILL_PATHS:
        if not (root / base).is_dir():
            return f"{base} is not in this checkout, so nothing would be released"
    present = relative_skill_files(root)

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
            "release something other than what this gate checked:\n" + completed.stdout.strip()
        )
    return None


def tag_exists(root: Path, name: str) -> bool:
    return bool(_git(root, "tag", "-l", name))


def tag_state_block(root: Path, name: str, message: str) -> str | None:
    """Why an existing tag is not this release's, or None when it is (or is absent).

    A re-run after an interruption finds the tag it created on the previous attempt.
    That is not a conflict to refuse: it is the step that was already done, and
    treating it as a conflict is what forced the whole checklist to be replayed. A
    tag that differs from the one this release would create is still refused, and
    the refusal says which of the three ways it differs.
    """
    if not tag_exists(root, name):
        return None

    kind = _git(root, "cat-file", "-t", name)
    if kind != "tag":
        return (
            f"{name} already exists as a {kind or 'unknown'} object, not the annotated "
            "tag this gate creates — delete it deliberately, or cut the release under "
            "another version"
        )

    subject = stored_subject(root, name)
    if subject != message:
        return f"{name} already exists with a different annotation: {subject!r}"

    target = _git(root, "rev-parse", f"{name}^{{commit}}")
    head = _git(root, "rev-parse", "HEAD")
    if target != head:
        return (
            f"{name} already exists and points at {target[:12] or 'nothing'}, not at "
            f"this commit {head[:12] or 'nothing'}"
        )
    return None


def origin_tag_conflict(
    held: str, commit_remote: str, local: str, commit_local: str, name: str
) -> str | None:
    """Why origin's tag contradicts the one this gate would create, or None.

    Pure: the two objects and the commits they point at, as ``git ls-remote`` and
    ``git rev-parse`` report them. ``held`` empty means origin holds nothing under
    the name; ``local`` empty means the tag is about to be created, which can never
    be the object origin already holds — a fresh annotation is a fresh sha either
    way. The same object is not a conflict: that is the interrupted run resuming.
    """
    if not held or (local and held == local):
        return None
    same = (
        "the same commit, a different annotation"
        if commit_remote == commit_local
        else "a different commit"
    )
    mine = (
        f"this one is {local[:12]}"
        if local
        else f"this one would be a new tag of {(commit_local or 'HEAD')[:12]}"
    )
    return (
        f"origin already holds a different {name} ({held[:12]}, {mine}): {same}. "
        "git push would refuse the name, so the tag is not being created — delete "
        "the remote tag, or cut the release under another version"
    )


def origin_tag_block(root: Path, name: str) -> str | None:
    """Origin's verdict on the tag this gate is about to create, asked not remembered.

    The gap this closes: the release state *named* a tag origin already held
    something else under — after the gate had cut a tag ``git push`` would refuse.
    Asked with ``git ls-remote``, the same transport a push uses. An origin that
    cannot be asked refuses nothing: the tag is local and verifiable, and the
    release state reports the question as unproven instead.
    """
    remote, why = ask_origin(root, name)
    if why:
        return None
    held = remote.get(f"refs/tags/{name}", "")
    local = _git(root, "rev-parse", f"refs/tags/{name}")
    commit_local = _git(root, "rev-parse", f"refs/tags/{name}^{{commit}}" if local else "HEAD")
    return origin_tag_conflict(
        held, remote.get(f"refs/tags/{name}^{{}}", "") or held, local, commit_local, name
    )


def tag_refusal(root: Path, name: str, skipped: list[str], message: str) -> str | None:
    """Why the tag must not be created, or None when it may be."""
    if skipped:
        return (
            f"--skip was used ({', '.join(skipped)}), so this release was not gated "
            "by the complete checklist"
        )
    declared = declared_version(root)
    if name != f"v{declared}":
        return f"{name} does not name the declared version v{declared}"
    conflict = tag_state_block(root, name, message)
    if conflict:
        return conflict
    blocked = skill_release_block(root)
    if blocked:
        return blocked
    if not tag_exists(root, name):
        # Only a tag being created can end up one `git push` refuses: a tag this
        # gate recognises is a resumed step, and a divergent object under an
        # already-existing name is the footer's `push` row to name. Cutting a new
        # tag under a name origin already holds something else under is the gap.
        return origin_tag_block(root, name)
    return None


def ask_origin(root: Path, name: str) -> tuple[dict[str, str], str]:
    """What origin holds right now for ``main`` and ``name`` — asked, not remembered.

    ``origin/main`` and ``refs/tags/`` in this clone record the last fetch, and a
    claim about what has been *pushed* cannot be built from that memory: an unpushed
    tag fetched nowhere looks exactly like one that went up hours ago.
    ``git ls-remote`` is the same transport a push uses, so what it prints is what
    the remote holds. On failure it returns the reason instead — no remote, offline,
    no credentials — because that is a state to report, not one to guess out of.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/main",
                f"refs/tags/{name}",
                f"refs/tags/{name}^{{}}",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {}, "git ls-remote origin timed out"
    except OSError as error:
        return {}, f"{error.__class__.__name__}: {error}"
    if completed.returncode != 0:
        lines = [line for line in completed.stderr.strip().splitlines() if line.strip()]
        # The first line names the failure ('origin' does not appear to be a git
        # repository, unable to access …); the last is git's boilerplate advice.
        return {}, lines[0] if lines else "git ls-remote origin failed"
    return origin_refs(completed.stdout), ""


def origin_refs(ls_remote_stdout: str) -> dict[str, str]:
    """``git ls-remote``'s stdout as ref name -> sha; peeled refs keep their ``^{{}}``."""
    found: dict[str, str] = {}
    for line in ls_remote_stdout.splitlines():
        sha, tab, ref = line.partition("\t")
        if tab:
            found[ref.strip()] = sha.strip()
    return found


def push_state_lines(
    remote: dict[str, str],
    why: str,
    name: str,
    local: str,
    commit_local: str,
) -> list[str]:
    """The ``push`` line(s) of the release state, decided from what origin answered.

    ``remote`` and ``why`` are exactly what ``ask_origin`` returned: the refs origin
    holds right now, or the reason it could not be asked. Nothing else enters the
    decision — ``local`` and ``commit_local`` are only ever *compared against* what
    the remote said, never taken as evidence of what the remote has.
    """
    if why:
        return [
            f"  push      git push origin {name} — whether the tag is already on origin is",
            "            unproven for the same reason",
        ]
    pushed = remote.get(f"refs/tags/{name}")
    if not pushed:
        return [f"  push      the tag is not on origin — git push origin {name}"]
    if pushed == local:
        return [f"  push      the tag is on origin as {pushed[:12]} — nothing to push"]
    commit_remote = remote.get(f"refs/tags/{name}^{{}}") or pushed
    same = (
        "the same commit, a different annotation"
        if commit_remote == commit_local
        else "a different commit"
    )
    return [
        f"  push      origin holds a different {name} ({pushed[:12]}, this one is {local[:12]}):",
        f"            {same}. git push would refuse the name; resolve it before pushing.",
    ]


def gh_release_presence(returncode: int, stderr: str) -> tuple[str, str]:
    """What ``gh release view``'s exit and stderr mean: exists, absent, or unproven.

    Only GitHub's own ``not found`` is a definite absence. Every other failure —
    no gh, no credentials, no network, no remote that points at GitHub — is a
    question that failed, and a question that failed is not an answer.
    """
    if returncode == 0:
        return "exists", ""
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    reason = lines[0] if lines else "gh release view failed"
    return ("absent", "") if "not found" in reason.lower() else ("unproven", reason)


def ask_github_release(root: Path, name: str) -> tuple[str, str]:
    """Whether GitHub already has the release for this tag — asked, not remembered.

    Unlike the index, nothing in this checkout records it: no step measures GitHub's
    releases, so there is no fact to cite and the CLI that would create the release
    is asked instead.
    """
    try:
        completed = subprocess.run(
            ["gh", "release", "view", name],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "unproven", "gh release view timed out"
    except OSError as error:
        return "unproven", f"{error.__class__.__name__}: {error}"
    return gh_release_presence(completed.returncode, completed.stderr)


def footer_row(label: str, body: list[str]) -> list[str]:
    """One row of the release state as the footer prints it: label column, then body.

    The label column is twelve wide, so continuation lines line up under the text
    rather than under the label. This is the only place that shape is decided — the
    body is content, and the same body is what the record stores and the notes show.
    """
    return [f"  {label if index == 0 else '':<10}{line}" for index, line in enumerate(body)]


def release_state_body(
    answer: tuple[str, str], name: str, message: str, notes_label: str
) -> list[str]:
    """The ``release`` row's content: what GitHub holds, and the command when one is left.

    The command is stated whenever a release may still need creating — GitHub said it
    has none, or it could not be asked — and withheld when it is already there. A
    failed question never suppresses what is left to do, and never claims it done.

    The notes file is named by path, always: it is the file the `notes` step writes,
    a fact about this release rather than about whether some file exists at this
    instant. Whether it exists *now* is the footer's own warning to give.
    """
    state, why = answer
    if state == "exists":
        return [f"the release already exists on GitHub — gh release view {name} found it"]
    command = f'gh release create {name} --title "{message}" --notes-file {notes_label}'
    if state == "absent":
        return ["no release for this tag on GitHub yet", command]
    return [f"whether the release exists is unproven ({why})", command]


def release_state_lines(
    answer: tuple[str, str],
    name: str,
    message: str,
    notes_label: str,
    notes_present: bool,
) -> list[str]:
    """The ``release`` line(s) as the footer prints them.

    The body is the recorded row — same words, no second phrasing — with the label
    column around it. The one thing not recorded is the warning about a missing notes
    file: that is the filesystem at this moment, not a fact about the release.
    """
    lines = footer_row("release", release_state_body(answer, name, message, notes_label))
    if not notes_present:
        lines.append(
            f"            there is no notes file at {notes_label}; the release body comes from one"
        )
    return lines


def pypi_state_body(facts: dict | None) -> list[str]:
    """The ``pypi`` row's content: what the index serves, as the ``published`` step measured it.

    Cited rather than re-asked: this run's checklist has just measured the index (the
    step never resumes, exactly because the index changes while the tree does not),
    and a second query beside the recorded fact could contradict it. A run that
    measured nothing says so — an absent measurement is not an absent release.
    """
    if not isinstance(facts, dict) or "published" not in facts:
        return ["what the index serves is unproven — this run measured nothing about it"]
    if facts["published"] is None:
        return [
            "what the index serves is unproven ("
            f"{facts.get('error') or 'the index could not be asked'})"
        ]
    index = facts.get("index") or "the index"
    version = facts.get("version") or "this version"
    if not facts["published"]:
        return [f"{index} has no files for {version} yet"]
    names = [
        str(entry.get("filename"))
        for entry in facts.get("files", [])
        if isinstance(entry, dict) and entry.get("filename")
    ]
    if not names:
        return [f"{index} lists {version} with no files at all"]
    return [f"{index} serves {version}: {', '.join(names)}"]


def pypi_state_lines(facts: dict | None) -> list[str]:
    """The ``pypi`` line as the footer prints it: the recorded body, labeled."""
    return footer_row("pypi", pypi_state_body(facts))


def report_release_state(
    root: Path,
    name: str,
    message: str,
    measured: dict | None = None,
    github: tuple[str, str] = ("unproven", "this run never asked GitHub"),
) -> None:
    """What is left to do, what the outside world actually holds, and what this gate will not do.

    The gate stops at the tag on purpose: publishing the release is what uploads to
    PyPI, and that cannot be undone. What it can do is say which steps remain and
    what the exact commands are, instead of leaving the releaser to remember them
    after a run that died halfway.

    Each row comes from its own source and never from this clone's memory: origin is
    asked (``git ls-remote``), GitHub was asked once earlier in this run and the
    answer is *cited* here — the same ask the record carries, so the footer cannot
    say one thing and the release notes another — and the index is *cited* from the
    ``published`` step's recorded facts. Wherever a source cannot answer, the row
    says unproven rather than filling itself in with a guess.
    """
    notes = root / ".freebuff" / f"release-notes-{name}.md"
    try:
        notes_label = notes.relative_to(root).as_posix()
    except ValueError:
        notes_label = str(notes)

    head = _git(root, "rev-parse", "HEAD")
    remote, why = ask_origin(root, name)

    print()
    print("=== release state")
    print(f"  tag       {message}")
    if why:
        print(f"  origin    cannot ask it ({why})")
        print(f"  main      whether {head[:12]} is on origin/main is unproven, not assumed")
    else:
        origin_main = remote.get("refs/heads/main")
        if not origin_main:
            print("  main      origin has no main branch — git push origin main would create it")
        elif origin_main == head:
            print(f"  main      {head[:12]} is on origin/main")
        else:
            print(f"  main      {head[:12]} is NOT on origin/main — git push origin main")

    for line in push_state_lines(
        remote,
        why,
        name,
        _git(root, "rev-parse", f"refs/tags/{name}"),
        _git(root, "rev-parse", f"refs/tags/{name}^{{commit}}"),
    ):
        print(line)
    for line in release_state_lines(github, name, message, notes_label, notes.is_file()):
        print(line)
    for line in pypi_state_lines((measured or {}).get("published")):
        print(line)
    print("  stops     publishing the release is what triggers publish.yml -> PyPI, and that")
    print("            upload cannot be undone, so this gate ends at the tag.")


def tag_message(name: str, title: str | None) -> str:
    """The annotation every tag in this repository carries.

    ``vX.Y.Z — <headline>``, which is the same string GitHub shows as the release
    name. A lightweight tag here would be the one tag in the history that
    ``git describe`` and the release page disagree about — and `git tag` without
    ``-a`` is what produces it, so the gate creates the annotated form itself
    rather than leaving it to whoever remembers.
    """
    return f"{name} — {title}" if title else name


def stored_subject(root: Path, name: str) -> str:
    """The annotation git actually stored, decoded the way git encodes it."""
    completed = subprocess.run(
        ["git", "for-each-ref", f"refs/tags/{name}", "--format=%(subject)"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def create_tag(root: Path, name: str, title: str | None = None) -> int:
    """Create the annotated tag, and prove git stored the message that was asked for.

    The message is handed over in a UTF-8 file rather than argv. On Windows a
    command-line message is re-encoded through the console codepage, so the em dash
    in `vX.Y.Z — headline` comes back as something else; a file of bytes does not
    make that trip. The stored subject is then compared with what was requested,
    because a tag whose name cannot be reproduced is worse than no tag: it is the
    one string GitHub shows on the release page.
    """
    message = tag_message(name, title)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".tag-message", delete=False
    ) as handle:
        handle.write(message + "\n")
        message_path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["git", "tag", "-a", name, "-F", str(message_path)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    finally:
        message_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        print(f"  could not create {name}: {completed.stderr.strip()}")
        return 1

    stored = stored_subject(root, name)
    if stored != message:
        # Nothing is pushed yet, so removing a tag this call created is the honest
        # end state: better than leaving an annotation nobody asked for, and better
        # than exiting 0 over it.
        subprocess.run(["git", "tag", "-d", name], cwd=root, capture_output=True)
        print(f"  git stored a different annotation for {name}, so the tag was removed")
        print(f"    asked for: {message!r}")
        print(f"    stored:    {stored!r}")
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
            "Headline for the release, used for the tag annotation (`vX.Y.Z — TEXT`, "
            "bare `NAME` without it) and for the release notes. It no longer needs "
            "`--tag`: the notes step reads it on any passing run, so a release can be "
            "prepared and reviewed with its headline before anybody cuts the tag."
        ),
    )
    args = parser.parse_args(argv)

    root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    checklist = default_steps(root, args.title or "") if steps is None else steps

    known = [step.name for step in checklist]
    unknown = [name for name in args.skip if name not in known]
    if unknown:
        print(f"unknown step(s) {', '.join(unknown)}; known: {', '.join(known)}", file=sys.stderr)
        return 2
    if args.tag and args.skip:
        print(
            "--skip and --tag cannot be combined: a tag gated by a partial checklist is not gated",
            file=sys.stderr,
        )
        return 2
    # `--title` used to require `--tag`, because a headline with no tag did nothing.
    # It does something now: the notes step carries it, and those are written on every
    # passing run, so a release can be prepared and read before the tag exists.

    skipped = [step.name for step in checklist if step.name in args.skip]
    if skipped:
        print(f"skipping: {', '.join(skipped)} — inspection only, cannot tag")
        print()

    state = default_state(root)
    try:
        version = declared_version(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        version = None
    fingerprint = tree_fingerprint(root)
    ledger = read_ledger(state)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # The release-state rows are measured in this run and recorded with the rest.
    # GitHub is asked a single time per run — `gh release view`, because no step
    # measures GitHub's releases — and the answer is what the record, the release
    # notes and the footer all report; a second ask beside the recorded one could
    # contradict it. The ask itself happens just before the record is written,
    # because the deferred `notes` step reads the record to build its table.
    release_name = args.tag or (f"v{version}" if version is not None else "")
    message = tag_message(release_name, args.title) if release_name else ""
    notes_file = root / ".freebuff" / f"release-notes-{release_name}.md"
    try:
        notes_label = notes_file.relative_to(root).as_posix()
    except ValueError:
        notes_label = str(notes_file)

    def record(results: list[Result]) -> dict:
        """The ledger for these results: what passed, when, and what it measured.

        Only passes are represented, and only for this tree. A failure has no form
        here at all, so a resumed run can skip a success but can never inherit one.
        A resumed step keeps the measurement the earlier run took — same tree, same
        fingerprint — while a step that ran again replaces it.
        """
        return {
            "version": version,
            "head": _git(root, "rev-parse", "HEAD"),
            "fingerprint": fingerprint,
            "recorded_at": now,
            "steps": {
                result.step.name: result.resumed_at or now for result in results if result.ok
            },
            "measured": {
                result.step.name: {
                    "at": result.resumed_at or now,
                    "duration_s": round(result.duration_s, 2)
                    if result.duration_s is not None
                    else None,
                    "facts": result.facts,
                }
                for result in results
                if result.ok and (result.facts or result.duration_s is not None)
            },
            # The footer's `release` and `pypi` rows, as the facts they are: the
            # outside world's answer to what this release still is and is not.
            # Unlike the steps these are recorded whatever the verdict — the
            # question was asked either way — and the release notes show these
            # exact rows rather than a second phrasing of them.
            "state": {
                "release": release_state_body(github, release_name, message, notes_label),
                "pypi": pypi_state_body(
                    {result.step.name: result.facts for result in results if result.facts}.get(
                        "published"
                    )
                ),
            },
        }

    def done_already(step: Step, ledger: dict) -> str | None:
        """When this step may be skipped, which is never for a non-resumable one."""
        if not step.resumable:
            return None
        return resumed_at(ledger, version, fingerprint, step.name)

    results: list[Result] = []
    deferred = [step for step in checklist if step.name in DEFERRED]
    for step in checklist:
        if step.name in args.skip or step.name in DEFERRED:
            continue
        already = done_already(step, ledger)
        if already:
            results.append(
                Result(
                    step,
                    0,
                    "",
                    resumed_at=already,
                    # The measurement the earlier run took on this same tree. Reusing
                    # it is the whole point of the record: re-measuring would be a
                    # different run, and dropping it would leave the notes blank.
                    **resumed_measurement(ledger, step.name),
                )
            )
        else:
            results.append(run_step(step, root))

    github = (
        ask_github_release(root, release_name)
        if release_name
        else ("unproven", "no tag names this release")
    )
    recorded = True
    if fingerprint and version is not None:
        recorded = write_ledger(state, record(results))

    # The deferred steps read what the others measured, so the record has to exist
    # before they run. They are still steps: one that fails refuses the tag, and the
    # resumed path treats them exactly like the rest.
    # A deferred step reads what the others measured, so it is only meaningful when
    # they all passed. It waits for that rather than documenting a half-finished run:
    # the first version of this ran anyway and wrote release notes reading "nothing was
    # measured" beside a failing suite, which is a misleading artefact to leave in the
    # place a releaser looks for the notes.
    blocked = [result.step.name for result in results if not result.ok]
    deferred_ran = deferred and fingerprint and version is not None and not blocked
    if deferred_ran:
        ledger = read_ledger(state)
        for step in deferred:
            if step.name in args.skip:
                continue
            already = done_already(step, ledger)
            if already:
                results.append(
                    Result(
                        step, 0, "", resumed_at=already, **resumed_measurement(ledger, step.name)
                    )
                )
            else:
                results.append(run_step(step, root))
            recorded = write_ledger(state, record(results)) and recorded

    try:
        state_label = state.relative_to(root).as_posix()
    except ValueError:
        state_label = str(state)

    if not recorded:
        print(f"note: the resume ledger at {state} could not be written, so nothing was")
        print("      recorded and the next run will repeat every step. The checks themselves")
        print("      are unaffected.")
        print()

    print("=== checklist")
    for result in results:
        print()
        if result.resumed_at:
            print(f"--- {result.step.name}: PASS (resumed)")
            print(f"    {result.step.why}")
            print(f"    verified on this exact tree at {result.resumed_at}, and nothing has")
            print(f"    changed since, so it was not run again. Delete {state_label} to re-run all")
            print("    of them.")
            continue
        mark = "PASS" if result.ok else f"FAIL ({result.status})"
        print(f"--- {result.step.name}: {mark}")
        print(f"    {result.step.why}")
        print(result.output if result.output else "    (no output)")
    for step in checklist:
        if step.name in args.skip:
            print()
            print(f"--- {step.name}: NOT RUN (skipped)")
            print(f"    {step.why}")
        elif step.name in DEFERRED and not deferred_ran:
            print()
            print(
                f"--- {step.name}: NOT RUN ({', '.join(blocked)} failed)"
                if blocked
                else f"--- {step.name}: NOT RUN (nothing was measured)"
            )
            print(f"    {step.why}")
            print("    it reads what the other steps measured, and that record is not")
            print("    complete — notes written from it would describe a run that did not")
            print("    happen, so none were written")

    failed = [result for result in results if not result.ok]

    print()
    print("=== verdict")
    if failed:
        print(
            f"  FAIL  {len(failed)} of {len(results)} step(s) failed: "
            f"{', '.join(result.step.name for result in failed)}"
        )
        print(
            "        The release is not ready; a tag would name artifacts that fail these checks."
        )
        return 1

    resumed = [result for result in results if result.resumed_at]
    print(f"  PASS  all {len(results)} step(s) passed.")
    if resumed:
        print(f"        {len(resumed)} of them resumed: already verified on this unchanged tree")
        print("        by an earlier run, matched by fingerprint rather than assumed.")
    print(
        "        A WARN about the Freebuff Desktop account copy is not a blocker: that\n"
        "        copy is a machine-local cache of the Cloud snapshot and does not ship\n"
        "        with a release."
    )

    if args.tag:
        print()
        message = tag_message(args.tag, args.title)
        refusal = tag_refusal(root, args.tag, skipped, message)
        if refusal:
            print(f"  REFUSED  {refusal}")
            return 1
        if tag_exists(root, args.tag):
            print(f"  already tagged  {message}")
            print("        the tag this release needs already exists, and it is the one this")
            print("        gate would create, so it was left alone and the run continued")
        else:
            code = create_tag(root, args.tag, args.title)
            if code != 0:
                return code
        report_release_state(
            root,
            args.tag,
            message,
            {result.step.name: result.facts for result in results if result.facts},
            github,
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
