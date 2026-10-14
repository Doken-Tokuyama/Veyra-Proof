"""Guards for scripts/release_notes.py, the generated release notes.

The claim is narrow and checkable: the prose comes from `changes.md` and every number
comes from the record the gate wrote, so no measurement in a release is typed by hand.
v0.4.4's notes are the worked example of the alternative — a table reading "351 passed",
an evidence directory named by its timestamp, a count of tools that came back `OK` — and
v0.4.1's draft is the other one: it had to open by admitting its headline counts no
longer matched the suite.

Two failures found while building this are pinned below as tests, because both were the
same shape of mistake: the generator looked its facts up by tool name while the record is
keyed by checklist step (it wrote "nothing was measured" beside a record full of
measurements), and it happily rewrote notes for a tree that had changed since the gate
measured it. A wrong release note is worse than a missing one.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_notes.py"


@pytest.fixture()
def notes():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gate():
    spec = importlib.util.spec_from_file_location(
        "pre_release_for_notes", REPO_ROOT / "scripts" / "pre_release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BULLET = (
    "- **The gate asks the platform.** 0.4.3 shipped while Windows was red.\n"
    "  The step reads the CI run for the commit being tagged.\n"
)


def _record(gate, root: Path, *, version: str = "0.4.9", **facts) -> dict:
    """A gate record for this tree, with whatever facts a test wants it to carry."""
    measured = {
        name: {"at": "2026-10-14T00:00:00+00:00", "duration_s": 1.0, "facts": value}
        for name, value in facts.items()
    }
    return {
        "version": version,
        "head": "a" * 40,
        "fingerprint": gate.tree_fingerprint(root),
        "recorded_at": "2026-10-14T00:00:00+00:00",
        "steps": {name: "2026-10-14T00:00:00+00:00" for name in measured},
        "measured": measured,
    }


def _tree(tmp_path: Path, version: str = "0.4.9") -> Path:
    """A real checkout: the record is tied to a fingerprint, so git has to work.

    `.freebuff/` is ignored here exactly as it is in this repository. Without that the
    notes this generator writes would change the tree they describe, and the second
    run would refuse its own first output as "a different tree".
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / ".gitignore").write_text(".freebuff/\ndist/\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "veyra-proof"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "changes.md").write_text(
        f"## Unreleased\n\n{BULLET}\n## What changed in v0.4.8\n\n- old news\n",
        encoding="utf-8",
    )
    for argv in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)
    return root


def _write(root: Path, record: dict) -> None:
    (root / ".git" / "veyra-pre-release.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )


def _full_facts(**overrides) -> dict:
    facts = {
        "tests": {
            "cases": 417,
            "passed": 417,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "duration_s": 31.9,
        },
        "platforms": {
            "commit": "b" * 40,
            "workflow": "CI",
            "jobs": 8,
            "platforms": {
                "ubuntu-latest": ["py3.13", "py3.10"],
                "macos-latest": ["py3.13", "py3.10"],
                "windows-latest": ["py3.13", "py3.10"],
            },
            "run": "runs/1",
        },
        "wheel": {
            "version": "0.4.9",
            "wheel": "veyra_proof-0.4.9-py3-none-any.whl",
            "places": ["metadata", "module", "cli", "entry"],
        },
        "evidence": {
            "profile": "quick",
            "auditor_version": "0.4.9",
            "target": "fixtures/demo_crate",
            "verdict": "ACCEPTED",
            "exit_code": 0,
            "evidence": "fixtures/demo_crate/.veyra/evidence/20260101T000000Z",
            "tools_ok": ["clippy", "cargo-test"],
            "tools_not_ok": {"semver-checks": "SKIPPED"},
        },
    }
    facts.update(overrides)
    return facts


class TestTheNumbersComeFromTheRecord:
    def test_the_generated_table_is_built_from_what_was_measured(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        code, body, path = notes.generate(root, "a headline")

        assert code == 0
        assert "**417 passed**" in body
        assert "31.9 s" in body
        assert "windows-latest (py3.13, py3.10)" in body
        assert "**ACCEPTED**, exit 0" in body
        assert "`veyra_proof-0.4.9-py3-none-any.whl` reports **0.4.9**" in body
        assert "2 tools `OK`" in body
        assert "`semver-checks` `SKIPPED`" in body
        assert "20260101T000000Z" in body
        # The row names the command that ran, not the directory it wrote to.
        assert "`veyra audit fixtures/demo_crate` (`--profile quick`)" in body
        assert "(veyra 0.4.9)" in body, "the auditor version that produced the evidence"
        assert "`veyra audit fixtures/demo_crate/.veyra/evidence" not in body
        assert path == root / ".freebuff" / "release-notes-v0.4.9.md"
        assert path.is_file(), "the default path is the one the gate hands to `gh`"

    def test_two_records_produce_two_different_reports(self, notes, gate, tmp_path):
        """The strongest form of the claim: change the measurement, the note changes.

        If any of these numbers were typed into the generator, one of the two runs
        below would print the other's values.
        """
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))
        _, first, _ = notes.generate(root, "t")

        facts = _full_facts()
        facts["tests"] = {**facts["tests"], "cases": 99, "passed": 99, "duration_s": 1.25}
        facts["platforms"] = {**facts["platforms"], "jobs": 3}
        facts["evidence"] = {**facts["evidence"], "verdict": "REJECTED", "exit_code": 1}
        _write(root, _record(gate, root, **facts))
        _, second, _ = notes.generate(root, "t")

        assert "**417 passed**" in first and "**417 passed**" not in second
        assert "**99 passed**" in second and "1.2 s" in second
        assert "8 jobs" in first and "3 jobs" in second
        assert "**ACCEPTED**, exit 0" in first and "**REJECTED**, exit 1" in second

    def test_no_digit_in_the_verification_table_is_unaccounted_for(self, notes, gate, tmp_path):
        """The claim, checked as a property rather than by example.

        Every number in the table has to be either recorded in the facts or a count of
        something in them (`len(tools_ok)`, `len(jobs)`, the versions per platform). A
        number that is neither would be one this generator had typed in, which is the
        whole thing being removed.
        """
        import re

        root = _tree(tmp_path)
        record = _record(gate, root, **_full_facts())
        _write(root, record)

        _, body, _ = notes.generate(root, "t")
        section = body.split("## Verification", 1)[1].split("\n## ", 1)[0]
        found = set(re.findall(r"\d+(?:\.\d+)?", section))

        assert len(found) >= 8, f"the check is nearly vacuous: {found}"

        measured = record["measured"]
        haystack = json.dumps(measured, sort_keys=True) + json.dumps(record, sort_keys=True)
        for entry in measured.values():
            facts = entry.get("facts") or {}
            for value in facts.values():
                if isinstance(value, (list, dict, str)):
                    haystack += f" {len(value)}"
                if isinstance(value, dict):
                    for inner in value.values():
                        if isinstance(inner, list):
                            haystack += f" {len(inner)}"

        available = set(re.findall(r"\d+(?:\.\d+)?", haystack))
        assert found <= available, (
            "these numbers appear in the table and nowhere in the record: "
            f"{sorted(found - available)}"
        )

    def test_the_changelog_prose_is_not_claimed_to_be_measured(self, notes, gate, tmp_path):
        """The changelog is human text and may cite history; only the table is measured.

        The first version of the header said "every number below was read out of that
        record", printed above prose like "0.4.1 shipped a wheel whose `__version__`
        still read `0.3.0`" — a claim about the whole document that was false about the
        half a human wrote.
        """
        root = _tree(tmp_path)
        (root / "changes.md").write_text(
            "## Unreleased\n\n- **Historic.** 0.4.1 shipped a wheel reading 0.3.0.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "prose"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        _write(root, _record(gate, root, **_full_facts()))

        _, body, _ = notes.generate(root, "t")

        prose = body.split("## What changed", 1)[1].split("## Verification", 1)[0]
        assert "0.3.0" in prose, "the changelog is copied, not filtered"
        header = body.split("## What changed", 1)[0]
        assert "Every number below" not in header
        assert "verification table is read out of that record" in header
        assert "no row" in body.split("## Verification", 1)[1]

    def test_a_measurement_that_is_absent_is_named_not_guessed(self, notes, gate, tmp_path):
        """No row, and no plausible number in its place."""
        root = _tree(tmp_path)
        facts = _full_facts()
        del facts["wheel"]
        del facts["evidence"]
        _write(root, _record(gate, root, **facts))

        code, body, _ = notes.generate(root, "t")

        assert code == 0
        assert "## Not measured" in body
        assert "- `wheel`" in body
        assert "- `evidence`" in body
        assert body.index("- `wheel`") < body.index("- `evidence`"), (
            "the section reads in checklist order, not in the order branches ran"
        )
        assert "ACCEPTED" not in body, "a verdict appeared with no measurement behind it"
        assert "throwaway venv" not in body, "the wheel row was written without a wheel"

    def test_with_nothing_measured_it_says_so(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        _write(root, _record(gate, root))

        code, body, _ = notes.generate(root, "t")

        assert code == 0
        assert "Nothing was measured for this tree." in body
        assert all(f"- `{name}`" in body for name in notes.WANTED)


class TestItRefusesInsteadOfDrafting:
    def test_a_record_for_another_tree_is_refused(self, notes, gate, tmp_path):
        """The numbers would be right and about the wrong commit."""
        root = _tree(tmp_path)
        record = _record(gate, root, **_full_facts())
        record["fingerprint"] = "0" * 64
        _write(root, record)

        code, message, path = notes.generate(root, "t")

        assert code == 1
        assert "different tree" in message
        assert "run `python scripts/pre_release.py` again" in message
        assert path is None

    def test_a_record_for_another_version_is_refused(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, version="0.4.8", **_full_facts()))

        code, message, _ = notes.generate(root, "t")

        assert code == 1
        assert "0.4.8" in message and "0.4.9" in message

    def test_no_record_at_all_is_refused_with_what_to_run(self, notes, gate, tmp_path):
        root = _tree(tmp_path)

        code, message, _ = notes.generate(root, "t")

        assert code == 1
        assert "no record" in message
        assert "pre_release.py" in message

    def test_nothing_to_release_is_refused(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        (root / "changes.md").write_text("## What changed in v0.4.8\n\n- old\n", encoding="utf-8")
        _write(root, _record(gate, root, **_full_facts()))

        code, message, _ = notes.generate(root, "t")

        assert code == 1
        assert "nothing to release" in message

    def test_a_tree_with_nothing_committed_cannot_be_described(self, notes, tmp_path):
        """No HEAD means no fingerprint, and a record nobody can tie to a tree.

        Misuse (exit 2) rather than a refusal the release could act on: the gate cannot
        have measured this tree, so there is nothing to reconcile.
        """
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "veyra-proof"\nversion = "0.4.9"\n', encoding="utf-8"
        )
        (root / "changes.md").write_text(f"## Unreleased\n\n{BULLET}", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        _write(root, {"version": "0.4.9", "fingerprint": "x", "measured": {}})

        code, message, _ = notes.generate(root, "t")

        assert code == 2
        assert "fingerprint" in message


class TestTheProseIsTheHumanPart:
    def test_the_unreleased_bullets_are_copied_verbatim(self, notes, gate, tmp_path):
        """Continuation lines included: the generator reformats nothing."""
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        _, body, _ = notes.generate(root, "t")

        assert BULLET.strip() in body
        assert "old news" not in body, "the released section is not part of this release"

    def test_the_release_reads_its_own_section_once_the_changelog_is_renamed(
        self, notes, gate, tmp_path
    ):
        """At tag time `Unreleased` is gone: the bullets carry the version heading.

        A tagged commit must not ship a changelog calling itself unreleased, so the
        section is renamed — and the notes have to keep finding the same prose.
        """
        root = _tree(tmp_path)
        (root / "changes.md").write_text(
            f"## What changed in v0.4.9\n\n{BULLET}\n"
            "## What changed in v0.4.8\n\n- old news\n",
            encoding="utf-8",
        )
        _write(root, _record(gate, root, **_full_facts()))

        code, body, _ = notes.generate(root, "t")

        assert code == 0
        assert BULLET.strip() in body
        assert "old news" not in body

    def test_the_version_section_wins_over_an_empty_next_cycle_section(
        self, notes, gate, tmp_path
    ):
        """The fresh `Unreleased` that follows a release cannot blank the notes out."""
        root = _tree(tmp_path)
        (root / "changes.md").write_text(
            f"## Unreleased\n\n## What changed in v0.4.9\n\n{BULLET}",
            encoding="utf-8",
        )
        _write(root, _record(gate, root, **_full_facts()))

        code, body, _ = notes.generate(root, "t")

        assert code == 0, "an empty Unreleased section was treated as the release"
        assert BULLET.strip() in body

    def test_the_unreleased_section_is_used_while_the_work_is_in_progress(
        self, notes, gate, tmp_path
    ):
        """Before the rename there is no version heading, and that is the normal case."""
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        assert notes.unreleased(root, "0.4.9") == [BULLET.rstrip() + "\n"]

    def test_a_changelog_with_neither_section_refuses_with_both_names(
        self, notes, gate, tmp_path
    ):
        root = _tree(tmp_path)
        (root / "changes.md").write_text("## What changed in v0.4.9\n\n## Something else\n", encoding="utf-8")
        _write(root, _record(gate, root, **_full_facts()))

        code, message, _ = notes.generate(root, "t")

        assert code == 1
        assert "What changed in v0.4.9" in message and "Unreleased" in message

    def test_the_title_is_the_tag_annotation(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        _, body, _ = notes.generate(root, "the gate asks the platform")

        assert body.startswith("# v0.4.9 — the gate asks the platform")
        assert '--title "the gate asks the platform"' in body

    def test_without_a_title_the_command_is_still_runnable(self, notes, gate, tmp_path):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        _, body, _ = notes.generate(root, "")

        assert body.startswith("# v0.4.9\n")
        assert "pre_release.py --tag v0.4.9```" in body.replace("\n", "")


class TestTheNamesMatchTheGate:
    def test_the_wanted_steps_are_steps_the_gate_actually_has(self, notes, gate, tmp_path):
        """The record is keyed by step name, not by the tool that wrote the report.

        The first version of this file looked up `check_platforms`, found nothing, and
        wrote release notes reading "nothing was measured" beside a record holding
        eight jobs and four wheel confirmations. This test is that bug.
        """
        steps = {step.name for step in gate.default_steps(tmp_path)}

        assert set(notes.WANTED) <= steps, notes.WANTED
        assert all(name in steps for name in notes.WANTED)

    def test_the_notes_are_run_by_the_gate_as_a_checklist_step(self, notes, gate, tmp_path):
        """Generated by the same command that gates the tag, not by a separate ritual."""
        names = [step.name for step in gate.default_steps(tmp_path)]
        notes_step = next(step for step in gate.default_steps(tmp_path) if step.name == "notes")

        assert names[-1] == "notes", "the notes are written from the measurements, so last"
        assert notes_step.name in gate.DEFERRED
        assert "release_notes.py" in " ".join(notes_step.commands[0]())


class TestTheCommandLine:
    def test_it_writes_the_file_and_says_where(self, notes, gate, tmp_path, capsys):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        code = notes.main(["--root", str(root), "--title", "a headline"])

        out = capsys.readouterr().out
        assert code == 0
        assert "wrote" in out
        assert (root / ".freebuff" / "release-notes-v0.4.9.md").is_file()

    def test_print_shows_the_notes(self, notes, gate, tmp_path, capsys):
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        assert notes.main(["--root", str(root), "--print"]) == 0
        assert "## Verification" in capsys.readouterr().out

    def test_the_refusal_reaches_stderr_with_a_status(self, notes, gate, tmp_path, capsys):
        root = _tree(tmp_path)

        assert notes.main(["--root", str(root)]) == 1
        assert "no record" in capsys.readouterr().err

    def test_it_runs_as_a_script(self, gate, tmp_path):
        """The gate invokes it as a subprocess, so that path is the one to check."""
        root = _tree(tmp_path)
        _write(root, _record(gate, root, **_full_facts()))

        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), "--title", "t"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert completed.returncode == 0, completed.stderr
        body = (root / ".freebuff" / "release-notes-v0.4.9.md").read_text(encoding="utf-8")
        assert "**417 passed**" in body
