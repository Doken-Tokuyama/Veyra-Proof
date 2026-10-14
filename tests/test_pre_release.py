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
def test_a_title_without_a_tag_is_refused(gate, released, capsys) -> None:
    """A headline describes a tag; accepting it alone would silently drop it."""
    code = gate.main(
        ["--root", str(released), "--title", "a headline"],
        steps=_build(gate, _passing("tests")),
    )

    assert code == 2
    assert "needs --tag" in capsys.readouterr().err
    assert _tags(released) == ""


@needs_git
def test_without_tag_nothing_is_tagged(gate, released, capsys) -> None:
    """A passing checklist is not a release; it says the release is ready."""
    code = gate.main(["--root", str(released)], steps=_build(gate, _passing("tests")))

    assert code == 0
    assert _tags(released) == ""


# --------------------------------------------------------------------------
# The real checklist
# --------------------------------------------------------------------------


def test_the_checklist_is_the_documented_four(gate) -> None:
    assert [step.name for step in gate.default_steps(REPO_ROOT)] == [
        "tests",
        "copies",
        "build",
        "evidence",
    ]


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
