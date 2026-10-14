"""Guards for scripts/check_skills.py, the single maintainer command.

Two checks used to be two commands a maintainer had to remember separately, so
one of them was always going to be skipped. These tests pin the properties that
make folding them safe: both checks always run, both always report, and the
verdict separates what this repository can repair from what it cannot.

Every case runs in a temp checkout, temp home and temp store, so the suite never
reads the real fan-out targets or the real account store.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMBINED = REPO_ROOT / "scripts" / "check_skills.py"
FANOUT = REPO_ROOT / "scripts" / "sync_skill.py"

SKILL_NAME = "veyra-proof"
CANONICAL_REL = Path(".cursor") / "skills" / SKILL_NAME
OWNER = "a" * 64
REVISION = "b" * 64


@pytest.fixture()
def combined():
    spec = importlib.util.spec_from_file_location("check_skills", COMBINED)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_repo(root: Path) -> Path:
    """A temp checkout holding a canonical skill of its own."""
    directory = root / CANONICAL_REL
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        '---\nname: veyra-proof\nmetadata:\n  version: "0.4.2"\n---\n# Skill\n',
        encoding="utf-8",
    )
    (directory / "reference.md").write_text("# Reference\n", encoding="utf-8")
    return directory


def _canonical(root: Path) -> dict[str, str]:
    directory = root / CANONICAL_REL
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _write_store(store: Path, files: dict[str, str]) -> Path:
    """A store whose snapshot and projection both hold ``files``."""
    store.mkdir(parents=True, exist_ok=True)
    (store / OWNER).mkdir(parents=True, exist_ok=True)
    (store / OWNER / "current.json").write_text(
        json.dumps(
            {
                "revision": 1,
                "settings": {
                    "skills": [
                        {
                            "name": SKILL_NAME,
                            "description": "d",
                            "instructions": files["SKILL.md"],
                            "files": [
                                {"path": name, "contents": body}
                                for name, body in sorted(files.items())
                                if name != "SKILL.md"
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    projection = store / OWNER / REVISION / SKILL_NAME
    projection.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        # Bytes, not text: `write_text` translates newlines to `os.linesep`, so on
        # Windows the fixture materialized CRLF and the store it built no longer
        # matched the canonical file byte for byte — a "current" store that the
        # checker then, correctly, reported as differing.
        (projection / name).write_bytes(body.encode("utf-8"))
    return store


def _sync(root: Path, home: Path) -> None:
    """Materialize the harness copies, so the fan-out starts out clean."""
    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(FANOUT), "--root", str(root), "--home", str(home)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    store = tmp_path / "store"
    root.mkdir()
    home.mkdir()
    _seed_repo(root)
    _sync(root, home)
    return root, home, store


def _run(combined, root: Path, home: Path, store: Path, capsys) -> tuple[int, str]:
    code = combined.main(
        [
            "--root",
            str(root),
            "--home",
            str(home),
            "--store",
            str(store),
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_every_copy_current_passes(combined, tmp_path, capsys) -> None:
    root, home, store = _sandbox(tmp_path)
    _write_store(store, _canonical(root))

    code, out = _run(combined, root, home, store, capsys)

    assert code == 0
    assert "PASS" in out
    assert "WARN" not in out


def test_harness_drift_fails_and_names_the_repair(combined, tmp_path, capsys) -> None:
    """Drift this repository can fix is the one thing that fails."""
    root, home, store = _sandbox(tmp_path)
    skill = root / CANONICAL_REL / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    _write_store(store, _canonical(root))

    code, out = _run(combined, root, home, store, capsys)

    assert code == 1
    assert "FAIL" in out
    assert "python3 scripts/sync_skill.py" in out


def test_account_drift_warns_but_does_not_fail(combined, tmp_path, capsys) -> None:
    """The policy, stated as a test: a cache nobody here can repair must not block.

    A contributor whose Cloud cache is stale would otherwise never see a green
    check, and a check that cannot go green is a check people learn to skip.
    """
    root, home, store = _sandbox(tmp_path)
    stale = _canonical(root)
    stale["SKILL.md"] = re.sub(
        r'(?m)^(\s*version:\s*)"[^"]+"$', r'\g<1>"0.4.0"', stale["SKILL.md"]
    )
    _write_store(store, stale)

    code, out = _run(combined, root, home, store, capsys)

    assert code == 0, "the account copy is machine-local; it must not fail the command"
    assert "PASS  every harness copy matches" in out
    assert "WARN" in out
    assert "skill manager" in out


def test_a_check_that_cannot_run_fails(combined, tmp_path, capsys) -> None:
    """A broken checker is a defect here, not a stale copy."""
    root = tmp_path / "not-a-checkout"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    code, out = _run(combined, root, home, tmp_path / "store", capsys)

    assert code == 2
    assert "could not run" in out


def test_both_reports_are_printed_even_when_the_first_fails(
    combined, tmp_path, capsys
) -> None:
    """Nothing is hidden behind an early exit: the point is not to forget one."""
    root, home, store = _sandbox(tmp_path)
    skill = root / CANONICAL_REL / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")

    _, out = _run(combined, root, home, store, capsys)

    assert "=== harness copies" in out
    assert "=== account copy" in out
    assert "=== verdict" in out


def test_the_reports_name_the_commands_they_ran(combined, tmp_path, capsys) -> None:
    """A verdict a reader cannot reproduce is a claim, not evidence."""
    root, home, store = _sandbox(tmp_path)
    _write_store(store, _canonical(root))

    _, out = _run(combined, root, home, store, capsys)

    assert "python3 scripts/sync_skill.py --check" in out
    assert "python3 scripts/check_account_skill.py" in out
    assert str(store) in out


def test_it_only_reads(tmp_path) -> None:
    """Both sub-checks promise not to write; the combined command inherits it."""
    root, home, store = _sandbox(tmp_path)
    _write_store(store, _canonical(root))
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(COMBINED),
            "--root",
            str(root),
            "--home",
            str(home),
            "--store",
            str(store),
        ],
        capture_output=True,
        text=True,
    )
    after = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert after == before, "the combined command wrote into the tree it was checking"
