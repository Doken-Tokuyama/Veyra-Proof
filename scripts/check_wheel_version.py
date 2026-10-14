#!/usr/bin/env python3
"""Install the freshly built wheel and read the version back out of it.

The suite already compares `pyproject.toml`, `src/veyra_proof/__init__.py` and the
skill's frontmatter — all of them *source* files, in this checkout.

0.4.1 is why that is not enough. It shipped a wheel whose `__version__` still read
`0.3.0`: the distribution was 0.4.1, `veyra --version` answered `veyra 0.3.0`, and
every `manifest.json` that wheel wrote signed its own evidence with the wrong
version. Two source files agreed with each other and one did not, and the build
never asked. The artefact is what people install, so the artefact is what this
check asks.

It creates a throwaway virtual environment, installs the wheel that was just
built into it, and then asks the *installed* distribution three independent
questions:

* ``metadata`` — what pip believes it installed (``importlib.metadata``);
* ``module``   — what the installed package says about itself (``__version__``);
* ``cli``      — what a user is told, through the console script the wheel
  declared, and again through ``python -m veyra_proof``.

All of them have to equal the version `pyproject.toml` declares, and the import
has to resolve inside the venv's site-packages: the environment is scrubbed of
`PYTHONPATH` first, because an answer read out of this checkout's `src/` would be
exactly the confusion the check exists to remove. A missed match is reported with
both values, never as a bare failure.

The venv is temporary and removed on success; on failure it is left behind and
its path printed, because that is when somebody may want to look inside it.

Exit 0 when the artefact and the declaration agree, 1 when they do not, 2 on
misuse (no pyproject, no wheel, unreadable version).

Usage::

    python3 scripts/check_wheel_version.py
    python3 scripts/check_wheel_version.py --root /path/to/checkout
    python3 scripts/check_wheel_version.py --venv-dir /tmp/wheel-venv   # reuse one
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

DIST_NAME = "veyra-proof"
MODULE_NAME = "veyra_proof"
CONSOLE_SCRIPT = "veyra"

# Kept quiet and offline: the wheel carries no mandatory dependency, so nothing
# has to be fetched and a check that phones home would be slower and less honest.
PIP_ENVIRONMENT = {
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PYTHONNOUSERSITE": "1",
}


def declared_version(root: Path) -> str:
    """The version `pyproject.toml` declares, which is what a wheel must be built as."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def wheels_for(dist: Path, version: str) -> list[Path]:
    """The wheels in ``dist/`` that claim this version — by name, as pip reads it.

    Matched on the *file name*, because that is what the index and `pip install`
    see. A wheel left from another release is not this release's evidence.
    """
    if not dist.is_dir():
        return []
    return sorted(
        path
        for path in dist.glob("*.whl")
        if path.name.startswith(f"{DIST_NAME.replace('-', '_')}-{version}-")
        or path.name.startswith(f"{DIST_NAME}-{version}-")
    )


def venv_python(venv: Path) -> Path:
    """The interpreter inside a venv, spelled the way this platform spells it."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_script(venv: Path, name: str) -> Path:
    """A console script the wheel installed, on either platform."""
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def clean_environment() -> dict[str, str]:
    """This process's environment, without the variables that redirect imports.

    `PYTHONPATH` and `PYTHONHOME` are how a check like this passes for the wrong
    reason: the version would be read out of a source tree while the artefact in
    the venv is never consulted.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env.update(PIP_ENVIRONMENT)
    return env


def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def _inside(path: Path, directory: Path) -> bool:
    """Whether ``path`` really sits under ``directory``.

    Both sides are resolved first. macOS hands out temp directories under `/var`,
    which is a symlink to `/private/var`: the venv is created at one spelling of
    the path and the imported module reports the other, so an unresolved compare
    declares a perfectly good wheel to be importing from somewhere else. That is
    the worst kind of check — one that fails on the platform it is run on most.
    """
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return False


def probe(
    python: Path,
    venv: Path,
    declared: str,
) -> tuple[list[str], list[str]]:
    """Ask the installed distribution what it is. Returns (problems, confirmed).

    Split out from :func:`main` so each question can be answered — and made to
    fail — without rebuilding anything: a check nobody can drive to a failure is
    decoration, and this file is a check.
    """
    problems: list[str] = []
    confirmed: list[str] = []
    env = clean_environment()

    script = (
        "import importlib.metadata as m, sys;"
        f"sys.stdout.write(m.version({DIST_NAME!r}))"
    )
    completed = run([str(python), "-c", script], env)
    reported = completed.stdout.strip()
    if completed.returncode != 0:
        problems.append(
            f"metadata: the wheel did not install as {DIST_NAME}: "
            f"{completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else 'no output'}"
        )
    elif reported != declared:
        problems.append(
            f"metadata: {DIST_NAME} installs as {reported!r}, pyproject.toml declares {declared!r}"
        )
    else:
        confirmed.append(f"metadata {reported}")

    # The module's own report, plus where it was read from. The path is the check
    # that the answer came from the venv and not from a source tree on PYTHONPATH.
    script = (
        f"import {MODULE_NAME} as m, sys;"
        "sys.stdout.write(m.__version__ + chr(10) + (m.__file__ or ''))"
    )
    completed = run([str(python), "-c", script], env)
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0:
        problems.append(f"module: importing {MODULE_NAME} in the venv failed: {completed.stderr.strip()}")
    else:
        reported = lines[0].strip() if lines else ""
        origin = Path(lines[1].strip()) if len(lines) > 1 else None
        if reported != declared:
            problems.append(
                f"module: {MODULE_NAME}.__version__ is {reported!r}, "
                f"pyproject.toml declares {declared!r}"
            )
        elif origin is not None and not _inside(origin, venv):
            problems.append(
                f"module: imported from {origin.as_posix()}, outside the venv — "
                "the version read back was not the wheel's"
            )
        else:
            # Rendered POSIX-style so the line reads the same on every platform: this
            # is the report a release cites, and `Lib\site-packages\…` and
            # `lib/site-packages/…` are the same finding spelled two ways.
            confirmed.append(
                f"module {reported} from "
                f"{origin.as_posix() if origin is not None else 'nowhere'}"
            )

    # Two ways a user can ask, so a wheel with a broken entry point is not hidden
    # by the module runner working.
    console = venv_script(venv, CONSOLE_SCRIPT)
    if not console.is_file():
        problems.append(f"cli: the wheel installed no {CONSOLE_SCRIPT!r} console script")
    else:
        completed = run([str(console), "--version"], env)
        reported = completed.stdout.strip()
        expected = f"{CONSOLE_SCRIPT} {declared}"
        if completed.returncode != 0:
            problems.append(f"cli: {CONSOLE_SCRIPT} --version exited {completed.returncode}")
        elif reported != expected:
            # The entry point is named the way the wheel declares it, not the way this
            # platform spells the file it lands in (`veyra.exe` on Windows): the line
            # says what a user typed and what they got, which is the claim being checked.
            problems.append(
                f"cli: {CONSOLE_SCRIPT} --version says {reported!r}, expected {expected!r}"
            )
        else:
            confirmed.append(f"cli {reported!r}")

    completed = run([str(python), "-m", MODULE_NAME, "--version"], env)
    reported = completed.stdout.strip()
    expected = f"{CONSOLE_SCRIPT} {declared}"
    if completed.returncode != 0:
        problems.append(
            f"module entry: python -m {MODULE_NAME} --version exited {completed.returncode}"
        )
    elif reported.splitlines()[:1] != [expected]:
        problems.append(
            f"module entry: python -m {MODULE_NAME} --version says {reported!r}, "
            f"expected {expected!r}"
        )
    else:
        confirmed.append(f"entry point {reported!r}")

    return problems, confirmed


def write_report(path: Path, facts: dict, ok: bool) -> None:
    """State the measurement as JSON, so a release note never restates it by hand.

    The facts are the four confirmations the check itself observed, not a summary
    of the log. A report that cannot be written costs a release-note row and
    nothing else, which is why it is reported rather than raised.
    """
    payload = {"tool": "check_wheel_version", "ok": ok, "facts": facts}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as error:
        print(f"note: could not write the report to {path}: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the built wheel into a throwaway venv and check that the "
            "installed distribution reports the version it was built as."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Checkout holding pyproject.toml and dist/ (default: parent of this script).",
    )
    parser.add_argument("--dist", type=Path, default=None, help="Where the wheel is.")
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help=(
            "Install the wheel without resolving its dependencies. The default is a "
            "real install — on Python 3.10 that means fetching `tomli` — because a "
            "wheel that only imports with its dependencies absent has not been "
            "checked as a user would install it."
        ),
    )
    parser.add_argument(
        "--venv-dir",
        type=Path,
        default=None,
        help="Use this directory as the environment instead of a temporary one.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write what was measured, as JSON, to this path (used by the gate).",
    )
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    dist = (args.dist or root / "dist").expanduser().resolve()

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        print(f"no pyproject.toml in {root}", file=sys.stderr)
        return 2
    try:
        declared = declared_version(root)
    except (KeyError, tomllib.TOMLDecodeError, OSError) as error:
        print(f"cannot read the declared version from {pyproject}: {error}", file=sys.stderr)
        return 2

    wheels = wheels_for(dist, declared)
    if not wheels:
        print(f"no wheel for {DIST_NAME} {declared} in {dist}", file=sys.stderr)
        print("build one first: python -m build", file=sys.stderr)
        return 1
    if len(wheels) > 1:
        print(f"more than one wheel for {declared} in {dist}:", file=sys.stderr)
        for path in wheels:
            print(f"  {path.name}", file=sys.stderr)
        print("remove the stale one before releasing", file=sys.stderr)
        return 1
    wheel = wheels[0]

    print(f"wheel      {wheel}")
    print(f"declared   {declared}")

    if args.venv_dir is not None:
        venv = args.venv_dir.expanduser().resolve()
        venv.mkdir(parents=True, exist_ok=True)
        temporary = False
    else:
        # Resolved at creation, so the path that is printed is the path the
        # interpreter will report back (see `_inside`).
        venv = Path(tempfile.mkdtemp(prefix="veyra-wheel-")).resolve()
        temporary = True

    env = clean_environment()
    print(f"venv       {venv}")
    created = run([sys.executable, "-m", "venv", "--without-pip", str(venv)], env)
    # `--without-pip` and then bootstrap explicitly, so that a machine whose
    # `venv` cannot reach ensurepip fails here with that message rather than as a
    # confusing "pip not found" two steps later.
    if created.returncode != 0:
        print(
            f"  FAIL  could not create a virtual environment: {created.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    # A reused environment (`--venv-dir`) usually has pip already, and ensurepip
    # on every run is the slowest part of this check by far.
    has_pip = run([str(venv_python(venv)), "-c", "import pip"], env).returncode == 0
    bootstrapped = (
        run([str(venv_python(venv)), "-m", "ensurepip", "--upgrade"], env)
        if not has_pip
        else subprocess.CompletedProcess([], 0, "", "")
    )
    if bootstrapped.returncode != 0:
        print(
            "  FAIL  no pip in the environment; this machine cannot check the wheel: "
            f"{bootstrapped.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    # `--force-reinstall` is what makes the answer be about *this* artefact. Without
    # it pip reads the requirement as `veyra-proof==9.9.9`, finds 9.9.9 already
    # installed, and reports "Requirement already satisfied" — so a rebuilt wheel
    # would be checked against the previous install of the same version, and a
    # re-run in a provided `--venv-dir` would confirm whatever was there before.
    flags = ["--force-reinstall"] + (["--no-deps"] if args.no_deps else [])
    print(f"install    pip install {' '.join(flags + [wheel.name])}".replace("  ", " "))
    installed = run(
        [str(venv_python(venv)), "-m", "pip", "install", *flags, str(wheel)],
        env,
    )
    if installed.returncode != 0:
        print(f"  FAIL  pip could not install the wheel (exit {installed.returncode})")
        print(installed.stdout[-2000:])
        print(installed.stderr[-2000:], file=sys.stderr)
        return 1

    problems, confirmed = probe(venv_python(venv), venv, declared)
    print()
    for line in confirmed:
        print(f"  confirmed  {line}")

    if args.report is not None:
        write_report(
            args.report,
            {
                "version": declared,
                "wheel": wheel.name,
                # The four independent places, each named as it was asked.
                "places": [line.split(" ")[0] for line in confirmed],
            },
            ok=not problems,
        )

    if problems:
        print()
        for problem in problems:
            print(f"  FAIL  {problem}")
        print()
        print(f"{len(problems)} problem(s): the wheel does not report what it was built as")
        if temporary:
            print(f"the environment was left in place to look at: {venv}")
        return 1

    print()
    print(f"  the wheel installs as {declared} and says so in all four places")
    if temporary:
        # Removed only on success: a failed run leaves the environment behind,
        # which is the one time somebody wants to look inside it.
        shutil.rmtree(venv, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
