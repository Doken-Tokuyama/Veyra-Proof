"""Guards for scripts/pre_release.py, the pre-release gate.

The gate exists so a release cannot ship a skill that disagrees with the repo.
That claim rests on four behaviours, each pinned here: every step runs and is
reported, ``--skip`` cannot be used to tag, a tag must name the declared version,
and — the one that actually protects a release — a tag is refused while the
tracked skill files have uncommitted changes, because the sdist is built from the
tagged commit rather than from the working tree.

The steps are injected, so these tests gate in milliseconds instead of building a
wheel and running a cargo audit. Only the real checklist is slow, and its shape is
asserted rather than executed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "pre_release.py"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture()
def gate():
    spec = importlib.util.spec_from_file_location("pre_release", GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _passing(name: str, marker: str = "fine"):
    """A step that succeeds, cheaply."""
    return lambda gate: gate.Step(
        name,
        "why",
        (lambda: [sys.executable, "-c", f"print('{marker}')"],),
    )


def _failing(name: str, code: int = 3):
    """A step that fails with a distinguishable status."""
    return lambda gate: gate.Step(
        name,
        "why",
        (lambda: [sys.executable, "-c", f"import sys; print('boom'); sys.exit({code})"],),
    )


def _counting(name: str, counter: Path, code: int = 0):
    """A step that appends to ``counter``, so a second run is visible as a count."""
    body = f"open(r'{counter}', 'a', encoding='utf-8').write('x'); raise SystemExit({code})"
    return lambda gate: gate.Step(name, "why", (lambda: [sys.executable, "-c", body],))


def _runs(counter: Path) -> int:
    """How many times a counting step actually executed."""
    return counter.read_text(encoding="utf-8").count("x") if counter.exists() else 0


def _build(gate, *factories):
    return [factory(gate) for factory in factories]


@pytest.fixture()
def released(tmp_path: Path) -> Path:
    """A committed checkout holding both tracked copies of the skill."""
    root = tmp_path / "repo"
    skill = '---\nname: veyra-proof\nmetadata:\n  version: "0.4.2"\n---\n# Skill\n'
    for relative in (".cursor/skills/veyra-proof", "Veyra/skills/veyra-proof"):
        directory = root / relative
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(skill, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "veyra-proof"\nversion = "0.4.2"\n', encoding="utf-8"
    )
    # `-f` because a global ignore rule may match `.cursor/`, which is exactly the
    # situation the tracked-ness check exists for; the fixture has to commit the
    # file the way the real checkout does.
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A", "-f"],
        ["git", "commit", "-qm", "seed"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    return root


def _tags(root: Path) -> str:
    return subprocess.run(
        ["git", "tag", "-l"], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def _tag_object(root: Path, name: str) -> str:
    """``tag`` for an annotated tag, ``commit`` for a lightweight one."""
    return subprocess.run(
        ["git", "cat-file", "-t", name], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def _tag_subject(root: Path, name: str) -> str:
    """Decoded as UTF-8, which is how git stores and prints an annotation.

    Without `encoding=`, subprocess decodes git's output with the locale encoding —
    cp1252 on Windows — so the em dash in `vX.Y.Z — headline` would come back as
    mojibake and the comparison would fail over the test's decoder, not the code's.
    """
    return subprocess.run(
        ["git", "for-each-ref", f"refs/tags/{name}", "--format=%(subject)"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


# --------------------------------------------------------------------------
# The checklist
# --------------------------------------------------------------------------


def test_a_clean_checklist_passes(gate, released, capsys) -> None:
    code = gate.main(["--root", str(released)], steps=_build(gate, _passing("tests")))

    assert code == 0
    assert "PASS  all 1 step(s) passed" in capsys.readouterr().out


def test_a_failing_step_refuses_the_release(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released)], steps=_build(gate, _passing("tests"), _failing("build"))
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL (3)" in out
    assert "build" in out


def test_every_step_runs_so_one_pass_does_not_hide_two_problems(
    gate, released, capsys
) -> None:
    """A checklist that stops at the first failure makes you release in stages."""
    code = gate.main(
        ["--root", str(released)],
        steps=_build(gate, _failing("copies"), _failing("build"), _passing("tests")),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "2 of 3 step(s) failed" in out
    assert "copies" in out and "build" in out


def test_a_step_stops_at_its_first_failing_command(gate, released, capsys) -> None:
    """`twine check` on artefacts a failed build never wrote is noise, not evidence."""
    two_commands = gate.Step(
        "build",
        "why",
        (
            lambda: [sys.executable, "-c", "import sys; sys.exit(4)"],
            lambda: [sys.executable, "-c", "print('twine-ran')"],
        ),
    )

    code = gate.main(["--root", str(released)], steps=[two_commands])

    out = capsys.readouterr().out
    assert code == 1
    assert "twine-ran" not in out, "the second command ran after the first failed"


def test_a_later_argv_is_built_after_the_earlier_command_ran(gate, tmp_path, capsys) -> None:
    """The bug this shape replaced.

    Building every argv up front made `twine check` validate whatever the previous
    release had left in `dist/`, and on a clean checkout nothing at all — while the
    step still reported PASS. The second command's argv here is decided by the
    thunk, exactly as `twine_command` decides it, so the ordering is what is tested:
    a command that reads the filesystem inside itself would pass either way.
    """
    dist = tmp_path / "dist"
    producer = (
        "import pathlib; d = pathlib.Path('dist'); d.mkdir(exist_ok=True); "
        "(d / 'veyra_proof-9.9.9.tar.gz').write_text('built')"
    )

    def validator() -> list[str]:
        found = sorted(path.name for path in dist.glob("*")) if dist.is_dir() else []
        if not found:
            return [sys.executable, "-c", "import sys; print('nothing to validate'); sys.exit(9)"]
        return [sys.executable, "-c", f"print('validating', {found!r})"]

    assert not dist.exists()
    step = gate.Step(
        "build",
        "why",
        (lambda: [sys.executable, "-c", producer], validator),
    )

    code = gate.main(["--root", str(tmp_path)], steps=[step])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "veyra_proof-9.9.9.tar.gz" in out, (
        "the second command was built before the first one had produced anything"
    )


def test_skipping_a_step_is_reported_as_not_run(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released), "--skip", "evidence"],
        steps=_build(gate, _passing("tests", "ran-tests"), _passing("evidence", "ran-evidence")),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "NOT RUN (skipped)" in out
    assert "ran-evidence" not in out, "a skipped step still executed"
    assert "ran-tests" in out


def test_an_unknown_skip_name_is_misuse(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released), "--skip", "nope"], steps=_build(gate, _passing("tests"))
    )

    assert code == 2
    assert "unknown step" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Refusing the tag
# --------------------------------------------------------------------------


@needs_git
@needs_git
def test_a_wheel_that_misnames_itself_blocks_the_tag(gate, released) -> None:
    """The new step has to be a gate, not a report.

    It is injected rather than run so the test stays in milliseconds, but the
    refusal path is the real one: the same call that would create the tag, with a
    failing `wheel` step in the checklist.
    """
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"],
        steps=_build(gate, _passing("tests"), _failing("wheel")),
    )

    assert code == 1
    assert _tags(released) == "", "a tag was created although the wheel disagreed with itself"


def test_skip_cannot_be_used_to_tag(gate, released, capsys) -> None:
    """The whole point: a partial checklist must not be able to cut a release."""
    code = gate.main(
        ["--root", str(released), "--skip", "evidence", "--tag", "v0.4.2"],
        steps=_build(gate, _passing("tests"), _passing("evidence")),
    )

    assert code == 2
    assert _tags(released) == "", "a tag was created from a partial checklist"


@needs_git
def test_a_tag_needs_a_passing_checklist(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"],
        steps=_build(gate, _passing("tests"), _failing("copies")),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert _tags(released) == "", "a tag was created although a step failed"
    assert "not ready" in out


@needs_git
def test_a_tag_must_name_the_declared_version(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released), "--tag", "v9.9.9"], steps=_build(gate, _passing("tests"))
    )

    out = capsys.readouterr().out
    assert code == 1
    assert _tags(released) == ""
    assert "v0.4.2" in out, "the refusal does not say which version is being built"


@needs_git
def test_a_tag_is_refused_while_the_skill_is_uncommitted(gate, released, capsys) -> None:
    """The sdist is built from the tagged commit, not from this working tree.

    Copies that all agree locally still say nothing about what ships if the
    canonical skill was never committed.
    """
    (released / ".cursor/skills/veyra-proof/SKILL.md").write_text(
        "# edited but not committed\n", encoding="utf-8"
    )

    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"], steps=_build(gate, _passing("tests"))
    )

    out = capsys.readouterr().out
    assert code == 1
    assert _tags(released) == ""
    assert "uncommitted" in out
    assert "SKILL.md" in out, "the refusal does not name the file that is not committed"


@needs_git
def test_a_tag_is_refused_when_git_does_not_track_the_skill(
    gate, tmp_path: Path, capsys
) -> None:
    """The trap: `git status` hides ignored paths, so a clean report proves nothing.

    A skill file matched by an ignore rule reads as clean while git would never
    commit it, and the release built from the tagged commit would silently carry
    the older version that *is* committed.
    """
    root = tmp_path / "repo"
    skill = '---\nname: veyra-proof\nmetadata:\n  version: "0.4.2"\n---\n# Skill\n'
    for relative in (".cursor/skills/veyra-proof", "Veyra/skills/veyra-proof"):
        directory = root / relative
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(skill, encoding="utf-8")
    (root / ".gitignore").write_text(".cursor/\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "veyra-proof"\nversion = "0.4.2"\n', encoding="utf-8"
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "seed"],
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", ".cursor/skills/veyra-proof"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert status == "", "the ignore rule hides the file from git status"

    block = gate.skill_release_block(root)
    assert block is not None and "does not track" in block

    code = gate.main(
        ["--root", str(root), "--tag", "v0.4.2"], steps=_build(gate, _passing("tests"))
    )

    assert code == 1
    assert _tags(root) == ""


@needs_git
def test_an_existing_tag_is_refused(gate, released, capsys) -> None:
    subprocess.run(["git", "tag", "v0.4.2"], cwd=released, check=True, capture_output=True)

    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"], steps=_build(gate, _passing("tests"))
    )

    assert code == 1
    assert "already exists" in capsys.readouterr().out
    assert _tags(released) == "v0.4.2"


@needs_git
def test_a_tag_is_created_when_everything_passes(gate, released, capsys) -> None:
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"], steps=_build(gate, _passing("tests"))
    )

    assert code == 0
    assert _tags(released) == "v0.4.2"
    assert "tagged v0.4.2" in capsys.readouterr().out


@needs_git
def test_the_tracked_check_names_files_the_way_git_prints_them(gate, released) -> None:
    """`git ls-files` prints forward slashes on every OS; `str(Path)` does not.

    Comparing the two made every tracked skill file look untracked on Windows, so
    the gate refused *every* tag there with "git does not track these skill files".
    On POSIX this assertion is a tautology today, which is exactly why it is
    written against the invariant rather than against a test that runs on Windows.
    """
    present = gate.relative_skill_files(released)

    assert present, "the fixture must hold skill files to check"
    assert all("\\" not in name for name in present), present
    assert ".cursor/skills/veyra-proof/SKILL.md" in present


@needs_git
def test_the_gate_creates_the_annotated_tag_the_history_uses(gate, released) -> None:
    """Every existing tag reads `vX.Y.Z — <headline>`; a lightweight one would not.

    `git tag NAME` alone makes a lightweight tag: the one tag in the history that
    carries no name, which `git describe` reduces to a bare hash and which the
    release page then disagrees with. The gate has to create the annotated form,
    because relying on whoever remembers `-a` is how the difference appears.
    """
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2", "--title", "a headline"],
        steps=_build(gate, _passing("tests")),
    )

    assert code == 0
    assert _tag_object(released, "v0.4.2") == "tag", "the tag must be annotated"
    assert _tag_subject(released, "v0.4.2") == "v0.4.2 — a headline"


@needs_git
def test_a_tag_without_a_title_is_annotated_all_the_same(gate, released) -> None:
    """Omitting the headline must not quietly fall back to a lightweight tag."""
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"], steps=_build(gate, _passing("tests"))
    )

    assert code == 0
    assert _tag_object(released, "v0.4.2") == "tag"
    assert _tag_subject(released, "v0.4.2") == "v0.4.2"


@needs_git
def test_a_tag_whose_annotation_git_rewrites_is_removed(gate, released, capsys) -> None:
    """The annotation is the release name GitHub shows, so a mangled one is a defect.

    Git cleans up a message it is given (trailing whitespace here), which is the
    mechanism by which an em dash is lost on Windows. The gate compares what it
    asked for with what was stored and removes the tag it just created rather than
    exiting 0 over an annotation nobody asked for. Nothing has been pushed yet.
    """
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2", "--title", "a headline   "],
        steps=_build(gate, _passing("tests")),
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "stored a different annotation" in out
    assert _tags(released) == "", "the tag it could not annotate had to go"


@needs_git
def test_a_title_without_a_tag_reaches_the_notes(gate, released, capsys) -> None:
    """A headline used to need `--tag` because it did nothing on its own.

    It does something now: the notes step carries it, and the notes are written on
    every passing run. Requiring the tag would mean a release could not be prepared
    and read with its headline until the tag existed — which is the order a release
    is actually reviewed in.
    """
    code = gate.main(
        ["--root", str(released), "--title", "a headline"],
        steps=_build(gate, _passing("tests")),
    )

    assert code == 0, capsys.readouterr().err
    assert _tags(released) == "", "nothing may be tagged without --tag"

    notes = next(
        step for step in gate.default_steps(released, "a headline") if step.name == "notes"
    )
    assert "--title a headline" in " ".join(notes.commands[0]())


# --------------------------------------------------------------------------
# Resuming a release that was interrupted
# --------------------------------------------------------------------------


def _run(gate, root: Path, steps, *extra: str) -> int:
    return gate.main(["--root", str(root), *extra], steps=steps)


@needs_git
def test_a_second_run_resumes_what_this_tree_already_verified(
    gate, released, tmp_path, capsys
) -> None:
    """The release is the point, not the checklist: half a minute becomes nothing.

    Measured on the real checkout, the full checklist took 22.4s, 29.4s and 35.4s and
    the resumed runs 0.14s, 0.15s and 0.13s. Re-running it after an interruption is
    not just slow — it is how a releaser learns to skip the gate.
    """
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))

    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 1
    capsys.readouterr()

    code = _run(gate, released, steps)
    out = capsys.readouterr().out

    assert code == 0
    assert _runs(counter) == 1, "the step ran again although this tree had verified it"
    assert "PASS (resumed)" in out
    assert "verified on this exact tree" in out


def test_only_passes_are_recorded(gate, released, tmp_path) -> None:
    """A failure has no representation in the ledger, so it cannot be inherited."""
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter, code=3))

    assert _run(gate, released, steps) == 1
    assert _runs(counter) == 1

    ledger = json.loads(gate.default_state(released).read_text(encoding="utf-8"))
    assert ledger["steps"] == {}, "a failure was recorded"

    assert _run(gate, released, steps) == 1
    assert _runs(counter) == 2, "a recorded failure was resumed instead of re-run"


@needs_git
def test_a_changed_tree_invalidates_every_record(gate, released, tmp_path) -> None:
    """A cache tied to a tree is the only kind worth having — names *and* bytes.

    The two shapes of change fail differently: a new file changes the list of names,
    an edit changes the bytes behind a name already in it. A fingerprint built from
    names alone would let an edited script reuse a recorded pass, so both are driven
    here deliberately.
    """
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))
    assert _run(gate, released, steps) == 0

    (released / "notes.txt").write_text("a new untracked file\n", encoding="utf-8")
    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 2, "the record survived a new file in the tree"

    (released / "notes.txt").write_text("edited after it was verified\n", encoding="utf-8")
    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 3, "an edit to a file left the record standing"


def test_a_ledger_from_another_version_is_ignored(gate, released, tmp_path) -> None:
    """Records are scoped to the release they were made for."""
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))
    gate.write_ledger(
        gate.default_state(released),
        {
            "version": "9.9.9",
            "fingerprint": gate.tree_fingerprint(released),
            "steps": {"tests": "whenever"},
        },
    )

    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 1


def test_an_unreadable_ledger_is_not_trusted(gate, released, tmp_path) -> None:
    """A cache that cannot be read is not a cache; the steps run again."""
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))
    gate.default_state(released).write_text("{ not json", encoding="utf-8")

    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 1


def test_the_ledger_stays_out_of_the_working_tree(gate, released, tmp_path) -> None:
    """It is the checkout's own note, not part of what gets released."""
    state = gate.default_state(released)
    assert state.parent.name == ".git", "the ledger must not sit where it could be committed"

    assert _run(gate, released, _build(gate, _counting("tests", tmp_path / "r.txt"))) == 0
    assert state.is_file()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=released, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", f"the ledger is visible to git: {status.stdout!r}"


def test_the_record_is_pinned_to_the_tree_it_verified(gate, released, tmp_path) -> None:
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))
    assert _run(gate, released, steps) == 0

    ledger = json.loads(gate.default_state(released).read_text(encoding="utf-8"))
    assert ledger["fingerprint"] == gate.tree_fingerprint(released)
    assert ledger["version"] == "0.4.2"
    assert set(ledger["steps"]) == {"tests"}


@needs_git
def test_the_gate_runs_from_a_linked_worktree(gate, released, tmp_path) -> None:
    """Preparing a release from a worktree is normal; crashing there is not.

    In a linked worktree `.git` is a *file* pointing at the checkout's real git
    directory, so the ledger path assumed here was unmakeable — `mkdir` raised
    `FileExistsError` and the gate died with a traceback before running a single
    step. Found by driving the gate at another commit from a worktree, which is the
    first thing anyone does when they want to check an older release.
    """
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=released,
        check=True,
        capture_output=True,
    )

    assert (worktree / ".git").is_file(), "the fixture is not a linked worktree"
    state = gate.default_state(worktree)
    assert state.parent.name != ".git", (
        "the ledger must follow the real git directory, not the .git file"
    )

    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))

    assert _run(gate, worktree, steps) == 0
    assert state.is_file(), "the run passed but recorded nothing"
    assert _run(gate, worktree, steps) == 0
    assert _runs(counter) == 1, "a worktree run did not resume from its own ledger"


def test_a_ledger_that_cannot_be_written_does_not_end_the_run(
    gate, released, tmp_path, capsys
) -> None:
    """A cache write may cost a resume; it may not cost the release.

    The step results are the product, the ledger is an optimisation. A run that died
    on the write would report nothing at all, so the failure is reported instead.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    assert gate.write_ledger(blocker / "ledger.json", {"steps": {}}) is False

    monkeypatch_state = blocker / "ledger.json"
    original = gate.default_state
    gate.default_state = lambda root, _state=monkeypatch_state: _state
    try:
        code = _run(gate, released, _build(gate, _counting("tests", tmp_path / "r.txt")))
    finally:
        gate.default_state = original

    out = capsys.readouterr().out
    assert code == 0, "an unwritable ledger failed the release"
    assert "could not be written" in out
    assert "PASS  all 1 step(s) passed" in out


@needs_git
def test_a_rerun_after_the_tag_does_not_replay_the_checklist(
    gate, released, tmp_path, capsys
) -> None:
    """The interruption this exists for: the tag was cut, the release was not published.

    Before, a re-run refused with `already exists` and the only way forward was to
    replay everything by hand.
    """
    counter = tmp_path / "runs.txt"
    steps = _build(gate, _counting("tests", counter))

    assert _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline") == 0
    assert _runs(counter) == 1
    capsys.readouterr()

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert _runs(counter) == 1, "the checklist replayed although the tag was already cut"
    assert "PASS (resumed)" in out
    assert "already tagged" in out
    assert _tags(released) == "v0.4.2"


def test_the_gate_says_what_is_left_and_what_it_will_not_do(gate, released, tmp_path, capsys) -> None:
    """It stops at the tag on purpose; the remaining commands are spelled out."""
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))
    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "=== release state" in out
    assert "git push origin v0.4.2" in out
    assert "gh release create v0.4.2" in out
    assert "publish.yml" in out
    assert "cannot be undone" in out, "the gate must say why it stops here"
    assert "is unproven, not assumed" in out, (
        "this fixture has no remote, so the gate must admit the push state is unknown "
        "rather than answer from refs it only fetched once"
    )


def test_the_remaining_report_names_the_notes_file_it_can_see(gate, released, tmp_path, capsys) -> None:
    """When the release body exists, the command to publish it is copy-pasteable."""
    (released / ".freebuff").mkdir()
    (released / ".freebuff" / "release-notes-v0.4.2.md").write_text("notes\n", encoding="utf-8")
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "--notes-file .freebuff/release-notes-v0.4.2.md" in out

    (released / ".freebuff" / "release-notes-v0.4.2.md").unlink()


# --------------------------------------------------------------------------
# What origin actually holds
# --------------------------------------------------------------------------


def _bare_origin(released: Path, tmp_path: Path) -> Path:
    """A real remote for the fixture: an empty bare repository wired up as origin.

    Not a stub — `git ls-remote` and `git push` reach it over the same transport
    they use for a URL on GitHub, so the assertions below are about what a remote
    answered, not about what a fake one was told to answer.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )
    for argv in (
        ["git", "remote", "add", "origin", str(bare)],
        ["git", "push", "-q", "origin", "HEAD:main"],
    ):
        subprocess.run(argv, cwd=released, check=True, capture_output=True)
    return bare


@needs_git
def test_a_tag_that_went_up_is_reported_as_pushed(gate, released, tmp_path, capsys) -> None:
    """After a real `git push`, the footer must stop asking for one.

    The defect this replaces: a footer that reads local refs cannot tell a tag that
    went up hours ago from one that exists nowhere else — `refs/tags/` looks the
    same either way — so it printed `git push` always and called the state a limit.
    """
    _bare_origin(released, tmp_path)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))
    assert _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline") == 0
    capsys.readouterr()  # drain the first run: it printed the tag as unpushed, truthfully
    subprocess.run(
        ["git", "push", "-q", "origin", "v0.4.2"],
        cwd=released,
        check=True,
        capture_output=True,
    )

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "the tag is on origin as" in out and "nothing to push" in out
    assert "git push origin v0.4.2" not in out, "it asks for a push the remote does not need"


@needs_git
def test_a_tag_only_this_clone_has_is_reported_as_unpushed(gate, released, tmp_path, capsys) -> None:
    """The other half: same report, tag local-only, so the push command is what is left."""
    _bare_origin(released, tmp_path)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "the tag is not on origin — git push origin v0.4.2" in out
    assert "nothing to push" not in out


@needs_git
def test_a_different_tag_on_origin_refuses_the_tag_instead_of_cutting_it(
    gate, released, tmp_path, capsys
) -> None:
    """Origin holds a v0.4.2 this gate did not make: same commit, another annotation.

    The gap the push-state work exposed: the release state *named* the two objects
    — after the gate had cut a tag `git push` would refuse. Now the tag is not
    created at all, and the refusal names both objects the way the report did.
    """
    _bare_origin(released, tmp_path)
    for argv in (
        ["git", "tag", "-a", "v0.4.2", "-m", "someone else's annotation"],
        ["git", "push", "-q", "origin", "v0.4.2"],
        ["git", "tag", "-d", "v0.4.2"],
    ):
        subprocess.run(argv, cwd=released, check=True, capture_output=True)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 1
    assert _tags(released) == "", "no tag may be cut under a name origin already holds"
    assert "origin already holds a different v0.4.2" in out
    assert "the same commit, a different annotation" in out
    assert "git push would refuse the name" in out


@needs_git
def test_a_tag_this_gate_already_made_still_reports_a_divergent_origin(
    gate, released, tmp_path, capsys
) -> None:
    """When the gate creates nothing, the footer still names both objects.

    Refusing is for the tag being cut; a resumed tag is a step already done, and
    the divergent origin is the release state's `push` row to name.
    """
    _bare_origin(released, tmp_path)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))
    assert _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline") == 0
    capsys.readouterr()
    for argv in (
        ["git", "tag", "-a", "other", "-m", "someone else's annotation"],
        ["git", "push", "-q", "origin", "other:refs/tags/v0.4.2"],
    ):
        subprocess.run(argv, cwd=released, check=True, capture_output=True)

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "already tagged" in out
    assert "origin holds a different v0.4.2" in out
    assert "the same commit, a different annotation" in out
    assert "resolve it before pushing" in out


@needs_git
def test_an_origin_tag_on_another_commit_refuses_the_tag_naming_the_other_commit(
    gate, released, tmp_path, capsys
) -> None:
    """Same name, different history: never call it the same release — or cut over it."""
    bare = _bare_origin(released, tmp_path)
    scratch = tmp_path / "scratch"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(scratch)], check=True, capture_output=True
    )
    for argv in (
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(argv, cwd=scratch, check=True, capture_output=True)
    (scratch / "other.txt").write_text("released elsewhere\n", encoding="utf-8")
    for argv in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "another release"],
        ["git", "tag", "-a", "v0.4.2", "-m", "released elsewhere"],
        ["git", "push", "-q", "origin", "main", "v0.4.2"],
    ):
        subprocess.run(argv, cwd=scratch, check=True, capture_output=True)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 1
    assert _tags(released) == "", "no tag may be cut under a name origin already holds"
    assert "origin already holds a different v0.4.2" in out
    assert "a different commit" in out


@needs_git
def test_an_origin_that_cannot_be_asked_says_unproven_rather_than_guessing(
    gate, released, tmp_path, capsys
) -> None:
    """Offline or no remote is a state to report, not one to fill in from memory."""
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "gone.git")],
        cwd=released,
        check=True,
        capture_output=True,
    )
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "cannot ask it" in out
    assert "is unproven, not assumed" in out
    assert "git push origin v0.4.2" in out, "the command stays copy-pasteable when the state does not"
    capsys.readouterr()

    assert _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline") == 0
    assert "there is no notes file" in capsys.readouterr().out


@needs_git
def test_an_existing_tag_that_is_not_ours_still_refuses(gate, released, capsys) -> None:
    """Resuming recognises one tag: the annotated one this gate would create."""
    subprocess.run(
        ["git", "tag", "-a", "v0.4.2", "-m", "something else entirely"],
        cwd=released,
        check=True,
        capture_output=True,
    )

    code = _run(gate, released, _build(gate, _passing("tests")), "--tag", "v0.4.2")
    out = capsys.readouterr().out

    assert code == 1
    assert "different annotation" in out
    assert "something else entirely" in out, "the refusal does not say what is there"


@needs_git
def test_a_tag_for_another_commit_still_refuses(gate, released, capsys) -> None:
    """A tag left on an older commit would name a release nobody verified."""
    subprocess.run(
        ["git", "tag", "-a", "v0.4.2", "-m", "v0.4.2"], cwd=released, check=True, capture_output=True
    )
    (released / "later.txt").write_text("moved on\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=released, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "later"],
        cwd=released,
        check=True,
        capture_output=True,
    )

    code = _run(gate, released, _build(gate, _passing("tests")), "--tag", "v0.4.2")
    out = capsys.readouterr().out

    assert code == 1
    assert "points at" in out and "not at this commit" in out


@needs_git
def test_without_tag_nothing_is_tagged(gate, released, capsys) -> None:
    """A passing checklist is not a release; it says the release is ready."""
    code = gate.main(["--root", str(released)], steps=_build(gate, _passing("tests")))

    assert code == 0
    assert _tags(released) == ""


# --------------------------------------------------------------------------
# The real checklist
# --------------------------------------------------------------------------


def test_the_checklist_is_the_documented_eight(gate) -> None:
    assert [step.name for step in gate.default_steps(REPO_ROOT)] == [
        "tests",
        "platforms",
        "copies",
        "build",
        "wheel",
        "evidence",
        "published",
        "notes",
    ]


def test_the_notes_step_writes_them_from_the_record(gate) -> None:
    """The last step is the release notes, and it is run by this gate.

    It is a step rather than a suggestion because that is what makes "no number was
    typed by hand" mechanical: the notes are generated by the same command that
    refuses the tag, and a run that cannot generate them does not tag.
    """
    notes = next(step for step in gate.default_steps(REPO_ROOT) if step.name == "notes")
    argv = " ".join(notes.commands[0]())

    assert "release_notes.py" in argv
    assert f"--root {REPO_ROOT}" in argv

    titled = next(
        step
        for step in gate.default_steps(REPO_ROOT, "a headline")
        if step.name == "notes"
    )
    assert '--title a headline' in " ".join(titled.commands[0]()), (
        "the notes carry the same headline as the tag, and it comes from --title"
    )


def test_the_steps_that_report_a_measurement_are_the_ones_that_can(gate) -> None:
    """Facts only exist where a step states them; the rest are absent, not zero."""
    reporting = {
        step.name: step.report.name
        for step in gate.default_steps(REPO_ROOT)
        if step.report is not None
    }

    assert set(reporting) == {"tests", "platforms", "wheel", "evidence", "published"}, reporting
    assert reporting["tests"] == "tests.xml", "pytest's counts come from its JUnit XML"


@needs_git
def test_a_deferred_step_runs_after_the_record_is_written(gate, released, capsys) -> None:
    """`notes` is last for a reason: it reads what the run measured.

    Driven with a step that fails unless the ledger exists, because that is the
    ordering claim — not merely that the names are sorted a particular way.
    """
    state = gate.default_state(released)
    body = (
        f"import pathlib, sys; "
        f"sys.exit(0 if pathlib.Path(r'{state}').is_file() else 3)"
    )

    def deferred_step(gate_module):
        return gate_module.Step(
            "notes", "why", (lambda: [sys.executable, "-c", body],)
        )

    code = gate.main(
        ["--root", str(released)],
        steps=_build(gate, _passing("tests"), deferred_step),
    )

    assert code == 0, "the deferred step saw no record to read"
    assert "notes: PASS" in capsys.readouterr().out


def test_a_step_whose_product_is_a_file_is_never_resumed(gate, released, tmp_path) -> None:
    """Deleting the notes must not leave a record claiming they were generated.

    A measurement is a fact about a run and stays true; a file can be deleted between
    runs, so the step is re-derived every time. Driven with a counting step marked the
    same way the notes step is, since the real one needs a full release to run.
    """
    counter = tmp_path / "derived.txt"

    def derived(gate_module):
        factory = _counting("notes", counter)
        step = factory(gate_module)
        return gate_module.Step(step.name, step.why, step.commands, resumable=False)

    steps = _build(gate, _counting("tests", tmp_path / "tests.txt"), derived)

    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 1
    assert _run(gate, released, steps) == 0
    assert _runs(counter) == 2, "a derived file was assumed to still be there"

    assert next(
        step for step in gate.default_steps(REPO_ROOT) if step.name == "notes"
    ).resumable is False, "the real notes step has to carry that flag too"


@needs_git
def test_a_failing_notes_step_refuses_the_tag(gate, released) -> None:
    """Notes that cannot be generated from the record stop the release."""
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"],
        steps=_build(gate, _passing("tests"), _failing("notes")),
    )

    assert code == 1
    assert _tags(released) == "", "a tag was created with no generated notes"


@needs_git
def test_deferred_steps_do_not_run_when_an_earlier_step_failed(gate, released) -> None:
    """A deferred step documents a run; a failed run has nothing to document.

    Without this the notes step passed beside a failing suite and left a notes file
    reading "nothing was measured" where a releaser looks for the release notes.
    """
    counter = released.parent / "notes-ran.txt"
    code = gate.main(
        ["--root", str(released)],
        steps=_build(gate, _failing("build"), _counting("notes", counter)),
    )

    assert code == 1
    assert not counter.exists(), "the deferred step ran after a step had failed"


def test_the_platforms_step_asks_the_other_platforms_for_this_commit(gate) -> None:
    """The suite passing here says nothing about Windows, so the step asks.

    It must read the checkout it was pointed at, because `--commit` defaults to the
    HEAD of the repository the script is looking at, and the gate can be pointed at
    another one.
    """
    platforms = next(
        step for step in gate.default_steps(REPO_ROOT) if step.name == "platforms"
    )
    commands = " ".join(" ".join(build_argv()) for build_argv in platforms.commands)

    assert "check_platforms.py" in commands
    assert f"--root {REPO_ROOT}" in commands


@needs_git
def test_platforms_that_never_ran_block_the_tag(gate, released) -> None:
    """A commit whose CI is red or absent must not become a release.

    This is v0.4.3: it was tagged and published here, and only then did CI report
    that both Windows jobs had failed — so the fix had to be shipped as the next
    version instead. Injected, like the wheel refusal, so the test costs nothing
    and still exercises the real path that would create the tag.
    """
    code = gate.main(
        ["--root", str(released), "--tag", "v0.4.2"],
        steps=_build(gate, _passing("tests"), _failing("platforms")),
    )

    assert code == 1
    assert _tags(released) == "", "a tag was created although the other platforms never passed"


def test_the_wheel_step_checks_the_artefact_that_was_just_built(gate) -> None:
    """The wheel step is only meaningful on the artefact the build step wrote.

    It runs `check_wheel_version.py` against *this* checkout, which finds the wheel
    in `dist/` by the version `pyproject.toml` declares. Were it ordered before
    `build` it would inspect whatever the previous release left behind, which is
    the failure the build step's own `twine` check was fixed for.
    """
    names = [step.name for step in gate.default_steps(REPO_ROOT)]
    assert names.index("build") < names.index("wheel"), (
        "the wheel step must run after the build that produces it"
    )

    wheel = next(step for step in gate.default_steps(REPO_ROOT) if step.name == "wheel")
    commands = " ".join(" ".join(build_argv()) for build_argv in wheel.commands)

    assert "check_wheel_version.py" in commands
    assert f"--root {REPO_ROOT}" in commands, (
        "the step must check the checkout it was pointed at, not the one it lives in"
    )


def test_the_index_is_asked_after_the_audit_and_before_the_notes(gate) -> None:
    """It compares what was built, and the notes carry its measurement."""
    names = [step.name for step in gate.default_steps(REPO_ROOT)]
    published = next(
        step for step in gate.default_steps(REPO_ROOT) if step.name == "published"
    )
    commands = " ".join(" ".join(build_argv()) for build_argv in published.commands)

    assert names.index("wheel") < names.index("published") < names.index("notes"), names
    assert "check_published.py" in commands
    assert f"--root {REPO_ROOT}" in commands, (
        "the step must compare the checkout it was pointed at, not the one it lives in"
    )
    assert published.report.name == "published.json", "its measurement is read by the notes"


def test_the_index_step_is_never_resumed(gate) -> None:
    """Its answer changes while the tree does not, which is the whole point.

    The run that cuts the tag records "nothing published yet" — publishing comes after —
    and the run where there is something to compare is a later one on an identical tree.
    A resumed answer would keep the release notes saying nothing was published about a
    version that is.
    """
    published = next(
        step for step in gate.default_steps(REPO_ROOT) if step.name == "published"
    )

    assert published.resumable is False


def test_a_step_that_asks_the_world_again_replaces_what_it_measured(
    gate, released, tmp_path
) -> None:
    """Driven, not just declared: a second run has to record the new answer.

    Same shape as the real step — non-resumable, and it states its facts in a report —
    with an answer that flips between runs, because that is what the index does.
    """
    report = tmp_path / "published.json"
    answer = tmp_path / "answer.txt"
    answer.write_text("absent", encoding="utf-8")
    body = (
        "import json, pathlib; "
        f"value = pathlib.Path(r'{answer}').read_text(); "
        f"pathlib.Path(r'{report}').write_text(json.dumps({{'tool': 'x', 'ok': True, "
        "'facts': {'published': value == 'present'}}))"
    )

    def asking(gate_module):
        return gate_module.Step(
            "published",
            "why",
            (lambda: [sys.executable, "-c", body],),
            report=report,
            resumable=False,
        )

    steps = _build(gate, _passing("tests"), asking)

    assert _run(gate, released, steps) == 0
    ledger = json.loads(gate.default_state(released).read_text(encoding="utf-8"))
    assert ledger["measured"]["published"]["facts"] == {"published": False}


    answer.write_text("present", encoding="utf-8")
    assert _run(gate, released, steps) == 0
    ledger = json.loads(gate.default_state(released).read_text(encoding="utf-8"))

    assert ledger["measured"]["published"]["facts"] == {"published": True}, (
        "the second run inherited the first run's answer"
    )


def test_the_checklist_includes_the_copy_check(gate) -> None:
    """Without this step the gate cannot notice a skill copy that disagrees."""
    copies = next(
        step for step in gate.default_steps(REPO_ROOT) if step.name == "copies"
    )
    commands = " ".join(" ".join(build_argv()) for build_argv in copies.commands)

    assert "check_skills.py" in commands


def test_the_build_step_builds_then_validates(gate) -> None:
    build = next(step for step in gate.default_steps(REPO_ROOT) if step.name == "build")

    assert len(build.commands) == 2
    assert "-m build" in " ".join(build.commands[0]())


def test_twine_validates_only_the_version_being_released(gate, released) -> None:
    """A previous release left in `dist/` is not this release's evidence."""
    dist = released / "dist"
    dist.mkdir()
    (dist / "veyra_proof-0.4.1.tar.gz").write_text("old")
    (dist / "veyra_proof-0.4.2.tar.gz").write_text("new")
    (dist / "veyra_proof-0.4.2-py3-none-any.whl").write_text("new")

    argv = gate.twine_command(released)

    assert "-m twine" == " ".join(argv[1:3])
    assert sum("0.4.2" in argument for argument in argv) == 2
    assert not any("0.4.1" in argument for argument in argv)


def test_an_empty_dist_fails_instead_of_validating_nothing(gate, released) -> None:
    """twine with no arguments is a usage error, not a build that passed."""
    (released / "dist").mkdir()

    argv = gate.twine_command(released)
    completed = subprocess.run(argv, cwd=released, capture_output=True, text=True)

    assert "twine" not in argv
    assert completed.returncode == 1
    assert "no sdist or wheel" in completed.stdout


def test_a_checkout_without_a_version_cannot_be_tagged(gate, tmp_path) -> None:
    """The refusal path must not be reachable with a version it cannot read."""
    bare = tmp_path / "bare"
    bare.mkdir()

    with pytest.raises(FileNotFoundError):
        gate.declared_version(bare)


# --------------------------------------------------------------------------
# What GitHub and the index hold
# --------------------------------------------------------------------------


def test_the_release_presence_is_decided_only_by_githubs_own_answer(gate) -> None:
    """exists / absent / unproven, from gh's exit and words.

    Only GitHub's own `not found` is a definite absence. The words a checkout
    without a GitHub remote produces (`no git remotes found`) are a question that
    failed: reading either of them as absence would hand out create-commands over a
    release that may be out.
    """
    assert gate.gh_release_presence(0, "") == ("exists", "")
    assert gate.gh_release_presence(1, "release not found") == ("absent", "")
    assert gate.gh_release_presence(1, "no git remotes found") == (
        "unproven",
        "no git remotes found",
    )


def test_the_release_line_withholds_the_command_when_the_release_exists(gate) -> None:
    lines = gate.release_state_lines(
        ("exists", ""), "v0.4.2", "a headline", ".freebuff/release-notes-v0.4.2.md", True
    )
    out = "\n".join(lines)

    assert "already exists on GitHub" in out
    assert "gh release create" not in out, "nothing is left to create"


def test_the_release_line_keeps_the_command_when_github_cannot_answer(gate) -> None:
    """A failed question suppresses neither the command nor the honesty."""
    lines = gate.release_state_lines(
        ("unproven", "no git remotes found"),
        "v0.4.2",
        "a headline",
        ".freebuff/release-notes-v0.4.2.md",
        True,
    )
    out = "\n".join(lines)

    assert "is unproven (no git remotes found)" in out
    assert (
        'gh release create v0.4.2 --title "a headline" '
        "--notes-file .freebuff/release-notes-v0.4.2.md"
    ) in out


def test_the_release_line_names_the_absent_release_and_its_notes_gap(gate) -> None:
    """The command names the real notes file; the gap is the footer's own warning.

    It used to print `--notes-file <notes-file>` while the file was missing, which
    made the recorded row and the footer's row two different texts — and handed the
    releaser a command nobody could paste. The path is the file the `notes` step
    writes, a fact about the release rather than about this instant's filesystem.
    """
    lines = gate.release_state_lines(
        ("absent", ""), "v0.4.2", "a headline", ".freebuff/release-notes-v0.4.2.md", False
    )
    out = "\n".join(lines)

    assert "no release for this tag on GitHub yet" in out
    assert "--notes-file .freebuff/release-notes-v0.4.2.md" in out
    assert "there is no notes file at" in out, (
        "the missing file is still reported — as a footer-time warning, not by "
        "rewriting the command"
    )


def test_the_pypi_line_admits_when_nothing_was_measured(gate) -> None:
    """An absent measurement is not an absent release — the whole discipline."""
    no_facts = gate.pypi_state_lines(None)
    unaskable = gate.pypi_state_lines(
        {
            "version": "0.4.2",
            "index": "https://pypi.org",
            "published": None,
            "error": "cannot read https://pypi.org/pypi/veyra-proof/0.4.2/json",
        }
    )

    assert "unproven — this run measured nothing about it" in no_facts[0]
    assert "unproven (cannot read https://pypi.org" in unaskable[0]
    assert "has no files" not in "\n".join(no_facts + unaskable)


def test_the_pypi_line_repeats_the_facts_the_published_step_recorded(gate) -> None:
    """Cited verbatim from the measurement, names and all."""
    empty = gate.pypi_state_lines(
        {"version": "0.4.2", "index": "https://pypi.org", "published": False, "files": []}
    )
    served = gate.pypi_state_lines(
        {
            "version": "0.6.0",
            "index": "https://pypi.org",
            "published": True,
            "files": [
                {"filename": "veyra_proof-0.6.0-py3-none-any.whl"},
                {"filename": "veyra_proof-0.6.0.tar.gz"},
            ],
        }
    )

    assert empty == ["  pypi      https://pypi.org has no files for 0.4.2 yet"]
    assert served == [
        "  pypi      https://pypi.org serves 0.6.0: "
        "veyra_proof-0.6.0-py3-none-any.whl, veyra_proof-0.6.0.tar.gz"
    ]


def test_a_machine_without_gh_reports_the_release_unproven(gate, released, monkeypatch) -> None:
    """No gh is a question that cannot be asked, not a release that does not exist."""
    monkeypatch.setenv("PATH", "")

    state, why = gate.ask_github_release(released, "v0.4.2")

    assert state == "unproven"
    assert "FileNotFoundError" in why


def _published_step(facts: dict, report: Path):
    """A `published` step whose command writes what the real one writes."""
    body = (
        "import json, pathlib; "
        f"pathlib.Path(r'{report}').parent.mkdir(parents=True, exist_ok=True); "
        f"json.dump({{'tool': 'check_published', 'ok': True, 'facts': {facts!r}}}, "
        f"open(r'{report}', 'w'))"
    )
    return lambda gate: gate.Step(
        "published", "why", (lambda: [sys.executable, "-c", body],), report=report
    )


@needs_git
def test_the_footer_cites_the_index_facts_this_run_measured(
    gate, released, tmp_path, capsys
) -> None:
    """The pypi row is the `published` step's measurement, cited — never re-asked."""
    facts = {
        "version": "0.4.2",
        "index": "https://pypi.org",
        "published": True,
        "files": [
            {"filename": "veyra_proof-0.4.2-py3-none-any.whl"},
            {"filename": "veyra_proof-0.4.2.tar.gz"},
        ],
    }
    steps = _build(
        gate,
        _counting("tests", tmp_path / "runs.txt"),
        _published_step(facts, tmp_path / "reports" / "published.json"),
    )

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert (
        "pypi      https://pypi.org serves 0.4.2: "
        "veyra_proof-0.4.2-py3-none-any.whl, veyra_proof-0.4.2.tar.gz"
    ) in out


@needs_git
def test_a_run_that_measured_nothing_about_the_index_says_unproven(
    gate, released, tmp_path, capsys
) -> None:
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out

    assert code == 0
    assert "pypi      what the index serves is unproven — this run measured nothing about it" in out


# --------------------------------------------------------------------------
# The footer's rows, recorded — one measurement, two readers
# --------------------------------------------------------------------------


def _ledger(gate, released: Path) -> dict:
    return json.loads(gate.default_state(released).read_text(encoding="utf-8"))


@needs_git
def test_the_record_carries_the_rows_the_footer_prints(
    gate, released, tmp_path, capsys, monkeypatch
) -> None:
    """The footer's `release` and `pypi` rows are facts in the record now.

    Before, the footer asked GitHub at print time and left nothing behind, so the
    release notes could describe the release some other way — or not at all. The
    rows are recorded, and the footer prints the same rows from the same ask.
    """
    monkeypatch.setattr(gate, "ask_github_release", lambda root, name: ("absent", ""))
    facts = {
        "version": "0.4.2",
        "index": "https://pypi.org",
        "published": True,
        "files": [{"filename": "veyra_proof-0.4.2-py3-none-any.whl"}],
    }
    steps = _build(
        gate,
        _counting("tests", tmp_path / "runs.txt"),
        _published_step(facts, tmp_path / "reports" / "published.json"),
    )

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out
    rows = _ledger(gate, released)["state"]

    assert code == 0
    assert rows["release"] == [
        "no release for this tag on GitHub yet",
        "gh release create v0.4.2 --title \"v0.4.2 — a headline\" "
        "--notes-file .freebuff/release-notes-v0.4.2.md",
    ]
    assert rows["pypi"] == [
        "https://pypi.org serves 0.4.2: veyra_proof-0.4.2-py3-none-any.whl"
    ]
    for line in rows["release"] + rows["pypi"]:
        assert line in out, f"the footer printed a row the record does not hold: {line}"


@needs_git
def test_github_is_asked_once_and_cited_by_record_and_footer(
    gate, released, tmp_path, capsys, monkeypatch
) -> None:
    """A second ask beside the recorded one could contradict it — so there is none.

    `check_published` is the pipeline's measuring step and everything cites it;
    GitHub's releases have no step, so the gate's one ask *is* the measurement. A
    footer that asked again at print time could say "exists" about notes saying
    "unproven": the exact defect recording the rows exists to close.
    """
    asks: list[str] = []

    def fake_ask(root, name):
        asks.append(name)
        return ("exists", "")

    monkeypatch.setattr(gate, "ask_github_release", fake_ask)
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out
    rows = _ledger(gate, released)["state"]

    assert code == 0
    assert asks == ["v0.4.2"], "GitHub must be asked once per run, and then only cited"
    assert rows["release"] == [
        "the release already exists on GitHub — gh release view v0.4.2 found it"
    ]
    assert rows["release"][0] in out
    assert "gh release create" not in out, "nothing is left to create"


@needs_git
def test_a_github_question_that_could_not_be_asked_is_recorded_as_unproven(
    gate, released, tmp_path, capsys, monkeypatch
) -> None:
    """The unproven discipline reaches the record: a failed question is no answer."""
    monkeypatch.setattr(
        gate,
        "ask_github_release",
        lambda root, name: ("unproven", "no git remotes found"),
    )
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out
    rows = _ledger(gate, released)["state"]

    assert code == 0
    assert rows["release"][0] == (
        "whether the release exists is unproven (no git remotes found)"
    )
    assert rows["release"][0] in out
    assert any(
        line.startswith("gh release create") for line in rows["release"]
    ), "a question that failed suppresses nothing"


@needs_git
def test_the_recorded_command_names_the_notes_file_whether_or_not_it_exists_yet(
    gate, released, tmp_path, capsys, monkeypatch
) -> None:
    """Recorded before the notes step runs, so the row cannot depend on the file.

    The path is the file this run's `notes` step writes; the row is recorded before
    that step runs. Naming `<notes-file>` instead would have put a different command
    in the record than the footer prints — two descriptions of one release again.
    """
    monkeypatch.setattr(gate, "ask_github_release", lambda root, name: ("absent", ""))
    steps = _build(gate, _counting("tests", tmp_path / "runs.txt"))

    code = _run(gate, released, steps, "--tag", "v0.4.2", "--title", "a headline")
    out = capsys.readouterr().out
    rows = _ledger(gate, released)["state"]

    assert code == 0
    assert not (released / ".freebuff" / "release-notes-v0.4.2.md").exists()
    assert any(
        "--notes-file .freebuff/release-notes-v0.4.2.md" in line
        for line in rows["release"]
    )
    assert "--notes-file <notes-file>" not in out
    assert "there is no notes file at .freebuff/release-notes-v0.4.2.md" in out, (
        "the missing file is still reported — as the footer's own warning"
    )


def _gh_reaches_github() -> str | None:
    """None when `gh` can read this repository's own project, else the reason not.

    Checked against *gh itself*, not against this gate: transport trouble skips with
    a reason, while a defect in how the gate reads gh's answer still fails the test.
    """
    if shutil.which("gh") is None:
        return "gh is not installed"
    completed = subprocess.run(
        ["gh", "api", "repos/Doken-Tokuyama/Veyra-Proof"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        first = (completed.stderr.strip().splitlines() or ["no answer"])[0]
        return f"gh cannot read GitHub: {first}"
    return None


def test_live_the_release_that_is_already_out_reads_as_existing(gate) -> None:
    """v0.6.0: published from this checkout, and GitHub has held it since."""
    reason = _gh_reaches_github()
    if reason:
        pytest.skip(reason)

    state, why = gate.ask_github_release(REPO_ROOT, "v0.6.0")

    assert (state, why) == ("exists", "")


def test_live_a_tag_with_no_release_reads_as_absent_not_as_unproven(gate) -> None:
    reason = _gh_reaches_github()
    if reason:
        pytest.skip(reason)

    state, why = gate.ask_github_release(REPO_ROOT, "v0.6.0-no-such-release")

    assert (state, why) == ("absent", "")
