"""Guards for scripts/check_account_skill.py, the read-only account-copy check.

The Freebuff Desktop account store is a cache the app rewrites from its Cloud
snapshot, so the check reports and never repairs. These tests pin that: the
verdicts, the exit codes, and the promise that inspecting the store changes
nothing in it. Every case runs against a store built from the canonical skill in
a temp directory, so the suite never reads or writes the real one.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_account_skill.py"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_skill.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_STEP_NAME = "Report whether the account copy matches the canonical skill"

SKILL_NAME = "veyra-proof"
CANONICAL_REL = Path(".cursor") / "skills" / SKILL_NAME

# Must match the constants in both scripts; asserted below.
OWNER = "a" * 64
REVISION = "b" * 64


def _load(path: Path, name: str):
    """Import a script that is not a package module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def check():
    return _load(CHECK_SCRIPT, "check_account_skill")


@pytest.fixture()
def sync():
    return _load(SYNC_SCRIPT, "sync_skill")


def _canonical() -> dict[str, str]:
    """The canonical skill as text, so a fixture cannot rot when it changes."""
    directory = REPO_ROOT / CANONICAL_REL
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _snapshot(files: dict[str, str]) -> dict:
    """A Cloud snapshot shaped the way the app writes it."""
    return {
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


def _write_store(store: Path, files: dict[str, str]) -> Path:
    """A store holding one snapshot and the projection the app derives from it."""
    store.mkdir(parents=True, exist_ok=True)
    (store / OWNER).mkdir(parents=True, exist_ok=True)
    (store / OWNER / "current.json").write_text(
        json.dumps(_snapshot(files)), encoding="utf-8"
    )
    projection = store / OWNER / REVISION / SKILL_NAME
    projection.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (projection / name).write_text(body, encoding="utf-8")
    return store


def _version(text: str, version: str) -> str:
    return re.sub(r'(?m)^(\s*version:\s*)"[^"]+"$', rf'\g<1>"{version}"', text)


def _tree(store: Path) -> dict[str, str]:
    """Hash every file under a store, so a write is detectable."""
    return {
        str(path.relative_to(store)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(store.rglob("*"))
        if path.is_file()
    }


def _run(check, store: Path, capsys) -> tuple[int, str]:
    code = check.main(["--store", str(store)])
    return code, capsys.readouterr().out


# --------------------------------------------------------------------------
# Constants shared with the fan-out
# --------------------------------------------------------------------------


def test_constants_match_the_fan_out_script(check, sync) -> None:
    """Two scripts that disagree about what the skill is called check nothing."""
    assert check.SKILL_NAME == sync.SKILL_NAME
    assert check.CANONICAL_REL == sync.CANONICAL_REL
    assert (REPO_ROOT / check.CANONICAL_REL).is_dir()


# --------------------------------------------------------------------------
# The verdicts
# --------------------------------------------------------------------------


def test_a_matching_store_is_current(check, tmp_path, capsys) -> None:
    store = _write_store(tmp_path / "store", _canonical())

    code, out = _run(check, store, capsys)

    assert code == 0
    assert "current" in out
    assert "OLDER" not in out and "DIFFERS" not in out


def test_a_stale_cloud_snapshot_is_reported_older(check, tmp_path, capsys) -> None:
    """The defect this script exists for: Cloud serving the pre-0.4.0 text."""
    stale = dict(_canonical())
    stale["SKILL.md"] = _version(stale["SKILL.md"], "0.4.0")
    store = _write_store(tmp_path / "store", stale)

    code, out = _run(check, store, capsys)

    assert code == 1
    assert "OLDER" in out
    assert "account 0.4.0 vs canonical" in out
    assert "SKILL.md (differs" in out


def test_a_newer_or_unordered_version_is_not_called_older(check, tmp_path, capsys) -> None:
    """'Older' is a claim about versions; a different one is only a difference."""
    ahead = dict(_canonical())
    ahead["SKILL.md"] = _version(ahead["SKILL.md"], "0.9.9")
    store = _write_store(tmp_path / "store", ahead)

    code, out = _run(check, store, capsys)

    assert code == 1
    assert "DIFFERS" in out
    assert "OLDER" not in out


def test_a_dropped_version_is_older_not_merely_different(check, tmp_path, capsys) -> None:
    """The real pre-0.4.0 text declared no version at all."""
    without = dict(_canonical())
    without["SKILL.md"] = re.sub(r'(?m)^\s*version:\s*"[^"]+"\n', "", without["SKILL.md"])
    store = _write_store(tmp_path / "store", without)

    code, out = _run(check, store, capsys)

    assert code == 1
    assert "OLDER" in out
    assert "account no metadata.version" in out


def test_a_missing_resource_is_reported(check, tmp_path, capsys) -> None:
    files = _canonical()
    del files["reference.md"]
    store = _write_store(tmp_path / "store", files)

    code, out = _run(check, store, capsys)

    assert code == 1
    assert "reference.md (absent)" in out


def test_a_stale_projection_is_reported_even_when_the_snapshot_matches(
    check, tmp_path, capsys
) -> None:
    """A projection can lag its own snapshot; an agent reads the projection."""
    store = _write_store(tmp_path / "store", _canonical())
    projection = store / OWNER / REVISION / SKILL_NAME / "SKILL.md"
    projection.write_text("stale\n", encoding="utf-8")

    code, out = _run(check, store, capsys)

    assert code == 1
    assert str(Path(OWNER) / REVISION / SKILL_NAME) in out


def test_a_projection_with_an_extra_file_is_reported(check, tmp_path, capsys) -> None:
    store = _write_store(tmp_path / "store", _canonical())
    (store / OWNER / REVISION / SKILL_NAME / "notes.md").write_text("x", encoding="utf-8")

    code, out = _run(check, store, capsys)

    assert code == 1
    assert "only here: notes.md" in out


def test_absent_store_is_not_drift(check, tmp_path, capsys) -> None:
    """A machine without the app is not a machine with a stale skill."""
    code, out = _run(check, tmp_path / "nope", capsys)

    assert code == 0
    assert "no account store" in out


def test_a_store_holding_no_copy_of_this_skill_is_not_drift(
    check, tmp_path, capsys
) -> None:
    store = tmp_path / "store"
    (store / OWNER).mkdir(parents=True)
    (store / OWNER / "current.json").write_text(
        json.dumps({"revision": 1, "settings": {"skills": [{"name": "other"}]}}),
        encoding="utf-8",
    )

    code, out = _run(check, store, capsys)

    assert code == 0
    assert "not in this snapshot" in out


def test_a_missing_canonical_skill_is_a_usage_error_not_drift(
    check, tmp_path, capsys
) -> None:
    """Exit 1 means drift. A checker that cannot read the source has not found any.

    Reporting this as 1 would let a broken checkout wear the verdict of a stale
    account copy, and the two need different actions.
    """
    store = _write_store(tmp_path / "store", _canonical())
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()

    code = check.main(["--root", str(empty), "--store", str(store)])
    captured = capsys.readouterr()

    assert code == 2
    assert "canonical skill not found" in captured.err
    assert "OLDER" not in captured.out and "DIFFERS" not in captured.out


def test_an_unreadable_snapshot_is_skipped_but_still_reported(
    check, tmp_path, capsys
) -> None:
    """A corrupt cache is not a claim about the skill, so it cannot fail a check."""
    store = tmp_path / "store"
    (store / OWNER).mkdir(parents=True)
    (store / OWNER / "current.json").write_text("{ not json", encoding="utf-8")

    code, out = _run(check, store, capsys)

    assert code == 0
    assert "nothing to compare" in out


# --------------------------------------------------------------------------
# The read-only promise
# --------------------------------------------------------------------------


def test_the_check_writes_nothing_to_the_store(check, tmp_path, capsys) -> None:
    """The store belongs to the app; a check that wrote would be a fan-out."""
    stale = dict(_canonical())
    stale["SKILL.md"] = _version(stale["SKILL.md"], "0.4.0")
    store = _write_store(tmp_path / "store", stale)
    before = _tree(store)

    code, _ = _run(check, store, capsys)

    assert code == 1
    assert _tree(store) == before, "the check modified the store it was inspecting"


def test_the_remedy_is_stated_when_drift_is_found(check, tmp_path, capsys) -> None:
    """A report that cannot be acted on is noise."""
    stale = dict(_canonical())
    stale["SKILL.md"] = _version(stale["SKILL.md"], "0.4.0")
    store = _write_store(tmp_path / "store", stale)

    _, out = _run(check, store, capsys)

    assert "skill manager" in out
    assert "re-imports the corrected copy" in out


# --------------------------------------------------------------------------
# The CI policy: visible, and never red because of a machine-local cache
# --------------------------------------------------------------------------


def _ci_step_body(step_name: str = CI_STEP_NAME, workflow_text: str | None = None) -> str:
    """The `run:` script of one CI step, taken by indentation.

    Deliberately not a YAML parser: the workflow is the artifact under test and
    PyYAML is not a dependency of this project.

    Both spellings are read. A flattened `run: python scripts/...` is exactly the
    edit that would turn this advisory step into one that fails the build on a
    stale cache, so the policy tests have to see it rather than trip over it.
    """
    text = WORKFLOW.read_text(encoding="utf-8") if workflow_text is None else workflow_text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- name: {step_name}":
            continue
        for offset in range(index, len(lines)):
            stripped = lines[offset].strip()
            if not stripped.startswith("run:"):
                continue
            inline = stripped[len("run:") :].strip()
            if inline and not inline.startswith(("|", ">")):
                return inline + "\n"
            indent = len(lines[offset]) - len(lines[offset].lstrip())
            body: list[str] = []
            for following in lines[offset + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following)
            # Dedented so the result is the script, not the YAML block that
            # happens to hold it; relative indentation is preserved.
            return textwrap.dedent("\n".join(body)) + "\n"
    raise AssertionError(f"no `run:` script for a step named {step_name!r} in {WORKFLOW}")


def test_the_extractor_reads_both_run_forms() -> None:
    """A flattened step must be visible to the policy tests, not invisible."""
    block = "steps:\n  - name: S\n    run: |\n      one\n      two\n  - name: T\n    run: echo hi\n"
    flat = "steps:\n  - name: S\n    run: python scripts/check_account_skill.py\n"

    assert _ci_step_body("S", block) == "one\ntwo\n"
    assert _ci_step_body("T", block) == "echo hi\n"
    assert _ci_step_body("S", flat) == "python scripts/check_account_skill.py\n"


def test_the_extractor_refuses_a_step_that_is_not_there() -> None:
    """Silently returning nothing would let every policy test pass vacuously."""
    with pytest.raises(AssertionError, match="no `run:`"):
        _ci_step_body("Nonexistent step", "steps:\n  - name: S\n    run: echo hi\n")


def test_ci_runs_the_account_check() -> None:
    assert WORKFLOW.is_file()
    body = _ci_step_body()
    assert "scripts/check_account_skill.py" in body, (
        "the CI step no longer runs the account-copy check"
    )


def test_the_ci_step_does_not_let_drift_fail_the_build() -> None:
    """The point of the job: a stale machine-local cache must not go red.

    Calling the script directly would make a contributor's Cloud cache fail
    somebody else's pull request, which is the outcome this step exists to avoid.
    """
    body = _ci_step_body()
    assert 'case "$status"' in body, "exit codes are no longer translated"
    assert "exit 0" in body, "no path through the step stays green"
    assert "::warning" in body, (
        "drift is not annotated, so staying green would also mean staying silent"
    )


def test_a_flattened_step_is_seen_by_the_policy_guard() -> None:
    """The regression this guards: `run: python scripts/check_account_skill.py`.

    That edit runs the check — so a test looking only for the script's name stays
    green — while deleting the exit-code mapping that keeps a stale cache from
    failing the build.
    """
    flat = (
        "steps:\n"
        f"  - name: {CI_STEP_NAME}\n"
        "    run: python scripts/check_account_skill.py\n"
    )
    body = _ci_step_body(CI_STEP_NAME, flat)

    assert "scripts/check_account_skill.py" in body, "the flattened step still runs it"
    assert 'case "$status"' not in body, "and the policy guard can see that it does not"


def test_the_ci_step_still_fails_when_the_check_itself_breaks() -> None:
    """Advisory about the cache, not about a checker that cannot run."""
    body = _ci_step_body()
    assert 'exit "$status"' in body
    assert "::error" in body


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs the workflow step through bash, as the ubuntu runner does",
)
@pytest.mark.parametrize(
    ("status", "expected_code", "annotation"),
    [
        ("0", 0, None),
        ("1", 0, "::warning"),
        ("2", 2, "::error"),
    ],
)
def test_the_ci_step_behaves_as_advertised(
    tmp_path: Path, status: str, expected_code: int, annotation: str | None
) -> None:
    """Run the committed step text with the check stubbed to each exit code."""
    step = tmp_path / "step.sh"
    step.write_text(_ci_step_body(), encoding="utf-8")

    shim = tmp_path / "bin"
    shim.mkdir()
    stub = shim / "python"
    stub.write_text('#!/bin/sh\nexit "${FAKE_EXIT:-0}"\n', encoding="utf-8")
    stub.chmod(0o755)

    summary = tmp_path / "summary.md"
    env = dict(
        os.environ,
        PATH=f"{shim}{os.pathsep}{os.environ.get('PATH', '')}",
        FAKE_EXIT=status,
        GITHUB_STEP_SUMMARY=str(summary),
    )
    proc = subprocess.run(
        ["bash", "-e", "-o", "pipefail", str(step)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert proc.returncode == expected_code, (
        f"the step exited {proc.returncode} for the check exiting {status}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    if annotation is None:
        assert "::warning" not in proc.stdout and "::error" not in proc.stdout
    else:
        assert annotation in proc.stdout, (
            f"the check exited {status} and the step said nothing about it: {proc.stdout!r}"
        )
    assert "### Account skill copy" in summary.read_text(encoding="utf-8"), (
        "the result is not in the run summary, so it is invisible without the log"
    )
