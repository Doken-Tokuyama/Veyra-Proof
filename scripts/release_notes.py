#!/usr/bin/env python3
"""Write the release notes from `changes.md` and the gate's own measurements.

Every number in a release note used to be typed by hand. The v0.4.4 notes are the
worked example: a verification table reading "351 passed", an evidence directory
named by its timestamp, a count of tools that came back `OK`, and a sentence about
which CI jobs were green — each of them copied out of a terminal by whoever was
cutting the release. Copied correctly once, they are right; copied from the wrong
run, or from the run before a rebuild, they are a release that documents a
measurement nobody can reproduce. The v0.4.1 draft shows the other shape of the
same problem: it had to open with a note that its own headline counts disagreed
with the suite, because by publication time they did.

So this file does not measure anything and it does not guess. The prose comes from
`changes.md` — the human part, written by whoever made the change — and every
number comes from the record the *gate* wrote while it ran, in
`.git/veyra-pre-release.json`. If the gate has not measured this exact tree, this
refuses rather than drafting plausible text: a release note that says "417 passed"
because someone read it somewhere is exactly what the rest of this repository
exists to refuse.

What it writes, to `.freebuff/release-notes-v<version>.md` — the path
`pre_release.py` tells you to hand to `gh release create --notes-file`:

* the title, `v<version> — <headline>`, the same string the annotated tag carries;
* the `## Unreleased` bullets of `changes.md`, verbatim;
* a `## Verification` table built entirely from recorded facts, with a row omitted
  when the gate did not measure it, and a `## Not measured` section naming what is
  therefore absent. An absent row is a visible limitation; a plausible number
  would be a lie.

Exit 0 when the notes were written, 1 when the gate's record does not cover this
tree (or there is nothing to release), 2 on misuse.

Usage::

    python3 scripts/release_notes.py --title "the release gate asks the platform"
    python3 scripts/release_notes.py --title "…" --out /tmp/notes.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

CHANGES = "changes.md"

# The record is keyed by checklist *step*, which is the name the gate ran; each step's
# report file names the tool that wrote it. The first version of this file looked the
# facts up by tool name, found nothing, and cheerfully wrote a release note reading
# "nothing was measured" — a wrong answer rather than a refusal, which is why the four
# names below are asserted against the gate's own checklist in the tests.
TESTS = "tests"
PLATFORMS = "platforms"
WHEEL = "wheel"
EVIDENCE = "evidence"

WANTED = (TESTS, PLATFORMS, WHEEL, EVIDENCE)
"""The steps whose measurements a verification table is built from."""

LEDGER_NAME = "veyra-pre-release.json"
"""The gate's record, inside the real git directory — `pre_release.STATE_NAME`."""


def declared_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def _section(root: Path, heading: str) -> list[str] | None:
    """The bullets under one `## ` heading of changes.md, or None if it is absent.

    Continuation lines belong to the bullet above them, so they are joined rather
    than dropped: a bullet reformatted by the generator would be the same defect as
    a number retyped by it. An empty section comes back as an empty list, which is
    how a caller tells "no bullets" from "no such section".
    """
    text = (root / CHANGES).read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return None

    bullets: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("- "):
            bullets.append(line)
        elif bullets and line.strip():
            bullets[-1] += "\n" + line
        elif not line.strip() and bullets:
            bullets[-1] += "\n"
    return [bullet.rstrip() + "\n" for bullet in bullets]


def unreleased(root: Path, version: str) -> list[str]:
    """The prose for the release being cut, from changes.md.

    Two headings can hold it, and which one does depends on where in the cycle you
    are: while the work is in progress the bullets sit under `## Unreleased`, and by
    the time the tag is cut they have been renamed to `## What changed in vX.Y.Z` —
    because a tagged commit should not ship a changelog calling itself unreleased.
    The version-titled section wins when both exist, so a fresh empty `Unreleased`
    for the next cycle cannot blank out the release being described.
    """
    titled = _section(root, f"What changed in v{version}")
    if titled:
        return titled
    return _section(root, "Unreleased") or []


def gate_module(root: Path):
    """The gate, loaded by path.

    Not a copy of it: `tree_fingerprint` decides whether a record describes the tree
    being released, and a second implementation of that question would be a second
    answer to it — the failure this repository keeps finding in its own tooling.
    """
    import importlib.util

    # The gate that produced this record, which is the one in the checkout being
    # released. The fallback is for a root that carries no `scripts/` of its own —
    # the fingerprint question is about that root either way, since the function is
    # handed it as an argument.
    candidates = [root / "scripts" / "pre_release.py", Path(__file__).resolve().parent / "pre_release.py"]
    path = next((path for path in candidates if path.is_file()), candidates[0])
    spec = importlib.util.spec_from_file_location("veyra_pre_release", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ledger_path(root: Path) -> Path:
    """Where the gate keeps its record, asked of the gate itself."""
    return gate_module(root).default_state(root)


def measurements(record: dict) -> dict[str, dict]:
    """The facts per step, as the gate recorded them: ``{step: {"facts": …}}``."""
    measured = record.get("measured")
    return measured if isinstance(measured, dict) else {}


def _facts(measured: dict[str, dict], tool: str) -> dict | None:
    """The facts one tool recorded, or None when it did not run."""
    entry = measured.get(tool)
    if not isinstance(entry, dict):
        return None
    facts = entry.get("facts")
    return facts if isinstance(facts, dict) else None


def _relative(path: str, root: Path) -> str:
    """The path as the release should read it: inside this checkout when it can."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return path


def verification_rows(
    measured: dict[str, dict], root: Path, steps: list[str]
) -> tuple[list[str], list[str]]:
    """The table rows, and the names of the steps that measured nothing.

    Split out from the writer so the tests can drive both halves — a row that
    appeared without a measurement, or a missing measurement that produced no
    mention, is a defect in a release note rather than a formatting choice.
    """
    rows: list[str] = []
    unmeasured: list[str] = []

    platforms = _facts(measured, PLATFORMS)
    if platforms is None:
        unmeasured.append(PLATFORMS)
    else:
        seen = platforms.get("platforms", {})
        if seen:
            rendered = ", ".join(
                f"{platform} ({', '.join(versions) or 'no python recorded'})"
                if versions
                else platform
                for platform, versions in seen.items()
                if versions
            )
            missing = [platform for platform, versions in seen.items() if not versions]
            cell = rendered
            if missing:
                cell += f" — **no job succeeded on {', '.join(missing)}**"
            rows.append(
                f"| CI run for `{str(platforms.get('commit', ''))[:12]}` "
                f"(`{platforms.get('workflow', '')}`) | {cell} — {platforms.get('jobs', 0)} jobs |"
            )

    wheel = _facts(measured, WHEEL)
    if wheel is None:
        unmeasured.append(WHEEL)
    else:
        places = ", ".join(f"`{place}`" for place in wheel.get("places", []))
        rows.append(
            f"| The built wheel, installed in a throwaway venv | "
            f"`{wheel.get('wheel', '')}` reports **{wheel.get('version', '')}** from {places} |"
        )

    evidence = _facts(measured, EVIDENCE)
    if evidence is None:
        unmeasured.append(EVIDENCE)
    else:
        # The command has to be the one that ran: the first version of this printed the
        # *evidence directory* where the Cargo workspace belongs, which is a command line
        # no one could run — a fabricated detail in a generated document.
        tooling = ""
        if evidence.get("tools_ok") is not None:
            not_ok = evidence.get("tools_not_ok") or {}
            tooling = f", {len(evidence['tools_ok'])} tools `OK`"
            if not_ok:
                tooling += ", " + ", ".join(
                    f"`{name}` `{status}`" for name, status in sorted(not_ok.items())
                )
        version = evidence.get("auditor_version")
        auditor = f" (veyra {version})" if version else ""
        rows.append(
            f"| `veyra audit {_relative(str(evidence.get('target', '')), root)}` "
            f"(`--profile {evidence.get('profile', '?')}`) | "
            f"**{evidence.get('verdict', '?')}**, exit {evidence.get('exit_code', '?')}"
            f"{auditor}, evidence "
            f"`{_relative(str(evidence.get('evidence', '')), root)}`{tooling} |"
        )

    tests = _facts(measured, TESTS)
    if tests is None:
        unmeasured.append(TESTS)
    else:
        counts = f"**{tests.get('passed', 0)} passed**"
        for name in ("failed", "errors", "skipped"):
            value = tests.get(name, 0)
            if value:
                counts += f", {value} {name}"
        seconds = tests.get("duration_s")
        timing = f" in {seconds:.1f} s" if isinstance(seconds, (int, float)) else ""
        rows.append(
            f"| `.venv/bin/python -m pytest` (full suite) | {counts}{timing}, "
            f"{tests.get('cases', 0)} cases collected |"
        )

    # Only these four are wanted. `build` and `copies` prove facts that carry no number
    # into a release note, and naming them here as unmeasured would be noise that makes
    # the real gaps harder to see. Reported in checklist order rather than in the order
    # the branches happen to run, so the section reads the same every time.
    return rows, [name for name in WANTED if name in set(unmeasured)]


def render(
    version: str,
    title: str,
    bullets: list[str],
    rows: list[str],
    unmeasured: list[str],
    record: dict,
) -> str:
    """The notes, assembled. Nothing here invents a value it was not handed."""
    head = f"# v{version} — {title}" if title else f"# v{version}"
    head += (
        f"\n\n> Generated from `changes.md` and this checkout's own gate record "
        f"(`{LEDGER_NAME}`), measured on `{str(record.get('head', ''))[:12]}` at "
        f"{record.get('recorded_at', 'an unrecorded time')}. The change description below "
        "is the changelog, verbatim; the verification table is read out of that record, "
        "not written by hand."
    )

    out = [head, "\n## What changed\n"]
    out += [f"\n{bullet}" for bullet in bullets]

    out.append("\n## Verification\n")
    out.append(
        "\nEvery value in this table was measured by the release gate on this exact "
        "tree; a check that measured nothing has no row, and is named below instead.\n"
    )
    if rows:
        out.append("\n| Check | Result |\n|---|---|")
        out += [f"\n{row}" for row in rows]
    else:
        out.append("\nNothing was measured for this tree.\n")

    if unmeasured:
        out.append("\n## Not measured\n\n")
        out.append(
            "These steps left no facts in the record, so they are absent from the "
            "table above rather than described from memory:\n"
        )
        out += [f"\n- `{name}`" for name in unmeasured]
        out.append("\n")

    # The headline is passed through only when there is one, so the command this
    # prints is one a reader can run rather than one that needs editing first.
    command = f"python scripts/pre_release.py --tag v{version}"
    if title:
        command += f' --title "{title}"'

    out.append(
        "\n\n## How to reproduce\n\n"
        "```bash\n"
        f"{command}\n"
        "```\n\n"
        f"The gate refuses the tag when the suite fails on any platform this project "
        f"claims, when the wheel does not report the version it was built as, or when the "
        f"audit's evidence disagrees with its verdict. Git has no pre-tag hook: a plain "
        f"`git tag` bypasses all of it.\n"
    )
    return "".join(out)


def generate(
    root: Path, title: str, out: Path | None = None, record: dict | None = None
) -> tuple[int, str, Path | None]:
    """Write the notes. Returns (exit code, message, path)."""
    try:
        version = declared_version(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        return 2, f"cannot read the declared version: {error}", None

    bullets = unreleased(root, version)
    if not bullets:
        return 1, (
            f"neither `## What changed in v{version}` nor `## Unreleased` has any "
            f"bullets in {CHANGES}: there is nothing to release"
        ), None

    ledger = ledger_path(root)
    if record is None:
        try:
            record = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
    if not isinstance(record, dict) or not record:
        return 1, (
            f"the gate has no record at {ledger}: run `python scripts/pre_release.py` "
            "first, so the numbers can be read rather than guessed"
        ), None

    if record.get("version") != version:
        return 1, (
            f"the gate's record is for {record.get('version')!r}, and this tree declares "
            f"{version!r}: the numbers would describe a different release"
        ), None

    # A record taken on another tree describes a different commit, and the numbers in
    # it are not this release's. The fingerprint is the gate's own, so there is one
    # answer to "is this the same tree" rather than one per script.
    try:
        current = gate_module(root).tree_fingerprint(root)
    except ImportError as error:
        return 2, str(error), None
    if not current:
        return 2, f"cannot fingerprint {root}: is it a git checkout?", None
    if record.get("fingerprint") != current:
        return 1, (
            "the gate's record was taken on a different tree "
            f"({str(record.get('fingerprint'))[:12]}… recorded, {current[:12]}… now): "
            "run `python scripts/pre_release.py` again so the numbers describe what "
            "you are releasing"
        ), None

    measured = measurements(record)
    rows, unmeasured = verification_rows(measured, root, list(WANTED))

    body = render(version, title, bullets, rows, unmeasured, record)
    destination = out or root / ".freebuff" / f"release-notes-v{version}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, destination)
    return 0, body, destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Write the release notes from changes.md and the gate's measurements. "
            "The prose is human; the numbers are recorded, never typed."
        )
    )
    parser.add_argument("--root", type=Path, default=None, help="Checkout to describe.")
    parser.add_argument(
        "--title",
        default="",
        help="Headline for the release, the same string the annotated tag carries.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write (default: .freebuff/release-notes-v<version>.md).",
    )
    parser.add_argument(
        "--print",
        dest="show",
        action="store_true",
        help="Print the notes as well as writing them.",
    )
    args = parser.parse_args(argv)

    root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    code, message, path = generate(root, args.title, args.out)

    if code != 0:
        print(message, file=sys.stderr)
        return code

    print(message if args.show else f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
