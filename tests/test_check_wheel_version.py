"""Guards for scripts/check_wheel_version.py, the fifth gate step.

The step exists because two source files agreeing with each other is not the claim
a release makes: 0.4.1 shipped a wheel whose `__version__` still read `0.3.0`
while the distribution said 0.4.1, so `veyra --version` lied and every
`manifest.json` that wheel wrote signed its evidence with the wrong version. The
source guards missed it because the wheel was the only thing that disagreed.

These tests therefore build **wheels**, not source trees: a wheel is a zip with a
known layout, so `_wheel` writes one by hand and the checker installs it for real
inside a venv. That keeps the whole file hermetic — no `build`, no network, no
tools beyond the standard library — while still exercising the end-to-end path
that the step claims: pip install, then read the version back out of four places.

One venv is shared across the venv-backed cases (`--venv-dir`), because creating
one per case would cost several seconds for no extra coverage.

Nothing here touches the real home, the real `dist/` or the real environment.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "scripts" / "check_wheel_version.py"

CONSISTENT = "9.9.9"

CLI_SOURCE = (
    "import sys\n"
    "from veyra_proof import __version__\n"
    "\n"
    "def main() -> int:\n"
    "    if '--version' in sys.argv:\n"
    "        print(f'veyra {__version__}')\n"
    "    return 0\n"
)
MAIN_SOURCE = (
    "import sys\n"
    "from veyra_proof.cli import main\n"
    "\n"
    "raise SystemExit(main())\n"
)


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("check_wheel_version", CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def _wheel(
    directory: Path,
    *,
    metadata_version: str = CONSISTENT,
    module_version: str = CONSISTENT,
    entry_point: bool = True,
    tag: str = "py3-none-any",
) -> Path:
    """Write an installable wheel, as a zip, with a consistent RECORD.

    `metadata_version` is what the distribution claims; `module_version` is what
    the package says about itself. They are separate arguments because the defect
    this check exists for is exactly a disagreement between the two.
    """
    dist_info = f"veyra_proof-{metadata_version}.dist-info"
    files: dict[str, bytes] = {
        "veyra_proof/__init__.py": f'__version__ = "{module_version}"\n'.encode(),
        "veyra_proof/cli.py": CLI_SOURCE.encode(),
        "veyra_proof/__main__.py": MAIN_SOURCE.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: veyra-proof\nVersion: {metadata_version}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            f"Tag: {tag}\n"
        ).encode(),
    }
    if entry_point:
        files[f"{dist_info}/entry_points.txt"] = (
            b"[console_scripts]\nveyra = veyra_proof.cli:main\n"
        )

    lines = [_record_line(name, data) for name, data in sorted(files.items())]
    lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(lines) + "\n").encode()

    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"veyra_proof-{metadata_version}-{tag}.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return wheel


def _root(tmp_path: Path, version: str = CONSISTENT) -> Path:
    """A checkout whose pyproject declares `version`, with nothing else in it."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "veyra-proof"\nversion = "{version}"\n', encoding="utf-8"
    )
    return root


def _run(
    check,
    root: Path,
    dist: Path,
    *extra: str,
    capsys=None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run the checker in-process, or out of process when an env var matters."""
    if env is None:
        code = check.main(["--root", str(root), "--dist", str(dist), "--no-deps", *extra])
        captured = capsys.readouterr() if capsys is not None else None
        return code, (captured.out + captured.err) if captured else ""

    completed = subprocess.run(
        [
            sys.executable,
            str(CHECK),
            "--root",
            str(root),
            "--dist",
            str(dist),
            "--no-deps",
            *extra,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        cwd=str(REPO_ROOT),
    )
    return completed.returncode, completed.stdout + completed.stderr


@pytest.fixture(scope="module")
def shared_venv(tmp_path_factory) -> Path:
    """One environment, reused by every case that needs an install.

    Creating a venv per case costs seconds each for no extra coverage; the
    `--venv-dir` path is also what somebody debugging a failure wants, so it is
    exercised here rather than left untested.
    """
    return tmp_path_factory.mktemp("wheel-venv")


def test_a_consistent_wheel_passes(check, tmp_path, shared_venv, capsys) -> None:
    """The whole claim, end to end: pip install, then four independent reports.

    The venv is the shared, caller-provided one, so this also covers the two
    properties of `--venv-dir`: it is reused rather than recreated, and it is
    left standing afterwards for whoever wants to look inside it.
    """
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist)
    report = tmp_path / "reports" / "wheel.json"

    code, out = _run(
        check, root, dist, "--venv-dir", str(shared_venv), "--report", str(report), capsys=capsys
    )

    assert code == 0, out
    assert "confirmed  metadata 9.9.9" in out
    assert "confirmed  module 9.9.9" in out
    assert "confirmed  cli 'veyra 9.9.9'" in out
    assert "confirmed  entry point 'veyra 9.9.9'" in out
    assert str(shared_venv) in out, "the module was not reported as coming from the venv"
    assert "site-packages/veyra_proof/__init__.py" in out
    assert shared_venv.is_dir(), "a caller-provided environment must not be deleted"

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["tool"] == "check_wheel_version"
    assert payload["ok"] is True
    assert payload["facts"]["version"] == "9.9.9"
    assert payload["facts"]["wheel"].endswith(".whl")
    assert payload["facts"]["places"] == ["metadata", "module", "cli", "entry"], (
        "the four independent places are the measurement, not a summary of it"
    )


def test_a_failing_wheel_still_writes_what_it_saw(check, tmp_path, shared_venv, capsys) -> None:
    """The report is a record, not a verdict: it is written either way, and the gate
    ignores it unless the step passed. A reader debugging a failure gets the facts."""
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist, module_version="0.1.0")
    report = tmp_path / "reports" / "wheel.json"

    code, out = _run(
        check, root, dist, "--venv-dir", str(shared_venv), "--report", str(report), capsys=capsys
    )

    assert code == 1, out
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    # The 0.4.1 shape, exactly: the distribution's metadata is the only one of the four
    # that still says the version the wheel was built as. The module disagrees, and so do
    # both ways a user asks — which is why the places that *answered* are the measurement
    # and the three that did not are failures rather than facts.
    assert payload["facts"]["places"] == ["metadata"]
    assert payload["facts"]["version"] == "9.9.9", (
        "the declared version is recorded, not the version that disagreed"
    )


def test_a_source_tree_on_pythonpath_cannot_answer_for_the_wheel(
    check, tmp_path, shared_venv
) -> None:
    """The failure this check would otherwise be blind to, and a false *pass* at that.

    A `PYTHONPATH` pointing at a source tree whose `__version__` matches is how the
    check would report the version of the checkout instead of the artefact. It is
    driven out of process on purpose: the variable has to be in the environment of
    the checker itself, which is how a release machine leaks one in.
    """
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    # The wheel lies; the source tree on PYTHONPATH is the honest one.
    _wheel(dist, metadata_version=CONSISTENT, module_version="0.0.0")

    poison = tmp_path / "poison"
    (poison / "veyra_proof").mkdir(parents=True)
    (poison / "veyra_proof" / "__init__.py").write_text(
        f'__version__ = "{CONSISTENT}"\n', encoding="utf-8"
    )

    code, out = _run(
        check,
        root,
        dist,
        "--venv-dir",
        str(shared_venv),
        env={"PYTHONPATH": str(poison)},
    )

    assert code == 1, f"the poisoned PYTHONPATH turned a lying wheel into a pass:\n{out}"
    assert "module: veyra_proof.__version__ is '0.0.0'" in out


def test_a_stale_module_version_fails_and_names_both_values(
    check, tmp_path, shared_venv, capsys
) -> None:
    """The 0.4.1 defect, reproduced: the distribution said 0.4.1, the module 0.3.0."""
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist, module_version="0.3.0")

    code, out = _run(check, root, dist, "--venv-dir", str(shared_venv), capsys=capsys)

    assert code == 1
    assert "module: veyra_proof.__version__ is '0.3.0'" in out
    assert "pyproject.toml declares '9.9.9'" in out
    assert "cli: veyra --version says 'veyra 0.3.0'" in out, (
        "the console script must be checked too: it is what a user actually reads"
    )


def test_a_wheel_without_its_console_script_fails(
    check, tmp_path, shared_venv, capsys
) -> None:
    """A wheel that installs no `veyra` is broken however well it imports."""
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist, entry_point=False)

    code, out = _run(check, root, dist, "--venv-dir", str(shared_venv), capsys=capsys)

    assert code == 1
    assert "installed no 'veyra' console script" in out


def test_a_wheel_for_another_version_is_not_this_release(check, tmp_path, capsys) -> None:
    """A `dist/` left from the previous release says nothing about this one."""
    root = _root(tmp_path, version="9.9.9")
    dist = tmp_path / "dist"
    _wheel(dist, metadata_version="1.2.3", module_version="1.2.3")

    code, out = _run(check, root, dist, capsys=capsys)

    assert code == 1
    assert "no wheel for veyra-proof 9.9.9" in out


def test_two_wheels_for_one_version_are_refused(check, tmp_path, capsys) -> None:
    """Whichever one pip picked, the other is evidence nobody checked."""
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist)
    _wheel(dist, tag="py2.py3-none-any")

    code, out = _run(check, root, dist, capsys=capsys)

    assert code == 1
    assert "more than one wheel" in out
    assert "remove the stale one" in out


def test_a_missing_pyproject_is_misuse(check, tmp_path, capsys) -> None:
    """Exit 2 means the check could not run, which is not a verdict about a wheel."""
    root = tmp_path / "bare"
    root.mkdir()

    code, out = _run(check, root, tmp_path / "dist", capsys=capsys)

    assert code == 2
    assert "no pyproject.toml" in out


def test_a_temporary_environment_is_removed_on_success(
    check, tmp_path, capsys
) -> None:
    """Throwaway means throwaway: the success path leaves nothing behind."""
    root = _root(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist)

    code, out = _run(check, root, dist, capsys=capsys)

    assert code == 0, out
    marker = "venv       "
    line = next(line for line in out.splitlines() if line.startswith(marker))
    created = Path(line[len(marker) :].strip())
    assert not created.exists(), f"the temporary environment survived: {created}"
