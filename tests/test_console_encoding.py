"""The reports these scripts print have to survive the console they are printed to.

Both halves of this file come from one failing CI run, on the two Windows jobs of the
v0.5.0 release commit, and they compound because one hid the other.

``links_to`` compared a recorded symlink target as a raw string. Windows records an
absolute target with the extended-length prefix — a link made to ``C:\\repo\\...`` reads
back as ``\\\\?\\C:\\repo\\...`` — so ``--check`` called every healthy link a link to
*somewhere else*, and the note that difference produces carried ``→``. The Windows ANSI
codepage, which is what a piped ``stdout`` gets there, has no ``→``: the report died with
``UnicodeEncodeError`` instead of printing it. A check that fails and a diagnosis that
cannot be printed is the worst pair to ship, and on this platform it was every run.

So there are two rules here, and the second is the reason the first exists.

1. No string a script can print contains a character the Windows pipe codepage cannot
   encode. The scan reads string literals out of the AST rather than grepping bytes, so
   a comment or docstring may still show the character and explain it, while an
   ``argparse`` description — which really is printed — is checked.
2. A report that cannot be encoded degrades instead of ending the run. ``sync_skill.py``
   relaxes its streams' error handler and prints ``\\u2192`` where it cannot print the
   glyph, which is what the third test drives with a path the codepage cannot spell.

Neither rule claims Windows is covered in general: cp1252 is the codepage these runners
use, a machine set to another ANSI codepage can still disagree, and the scripts other than
``sync_skill.py`` have no run-time guard at all. What is pinned is the shape of the
failure that reached a release commit, on every platform we can run.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
SYNC = SCRIPTS / "sync_skill.py"

# The codepage Windows gives a piped stdout — how CI runs these scripts — and the
# encoding these tests force onto them on every other platform.
WINDOWS_PIPE_ENCODING = "cp1252"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of string constants that are docstrings, and so are never printed."""
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _printable_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_script_can_print_a_character_the_windows_console_cannot_encode() -> None:
    """The defect CI caught, as an invariant rather than as one arrow.

    A literal that cannot be encoded is not a slow report or an ugly one: the run stops
    inside ``print``, which is why this is a test and not a code-review habit.
    """
    problems: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        for line, text in _printable_literals(path):
            for char in text:
                try:
                    char.encode(WINDOWS_PIPE_ENCODING)
                except UnicodeEncodeError:
                    problems.append(
                        f"{path.name}:{line} contains {char!r} (U+{ord(char):04X}), "
                        f"which {WINDOWS_PIPE_ENCODING} cannot encode"
                    )
                    break

    assert not problems, (
        "these strings are printed and a Windows console cannot encode them; "
        "use an ASCII spelling:\n  " + "\n  ".join(problems)
    )


def _sandbox(tmp_path: Path, home_name: str = "home") -> tuple[Path, Path]:
    """A checkout holding the canonical skill, and a home with one harness installed."""
    canonical = tmp_path / "repo" / ".cursor" / "skills" / "veyra-proof"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\nname: veyra-proof\n---\n# Skill\n", encoding="utf-8"
    )
    (canonical / "reference.md").write_text("# Reference\n", encoding="utf-8")
    home = tmp_path / home_name
    (home / ".cursor").mkdir(parents=True)
    return tmp_path / "repo", home


def _run_sync(
    root: Path, home: Path, *extra: str, tmp_path: Path
) -> subprocess.CompletedProcess[bytes]:
    """Run the fan-out out of process, on a console that cannot encode much.

    ``PYTHONIOENCODING`` is what makes this the Windows case on every platform: it
    leaves the encoding at cp1252 exactly as a pipe on Windows does, so a character the
    codepage lacks fails here too. ``HOME`` moves the audit log into the sandbox, since
    the script resolves it from the user's home rather than from ``--home``.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHONIOENCODING": WINDOWS_PIPE_ENCODING,
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
    }
    return subprocess.run(
        [sys.executable, str(SYNC), "--root", str(root), "--home", str(home), *extra],
        capture_output=True,
        env=env,
    )


def _text(completed: subprocess.CompletedProcess[bytes]) -> str:
    both = completed.stdout + completed.stderr
    return both.decode(WINDOWS_PIPE_ENCODING, errors="backslashreplace")


def test_the_fan_out_report_survives_a_console_that_cannot_encode_it(tmp_path) -> None:
    """A healthy ``--check`` is the run every Windows contributor makes, every day.

    It is also the run that used to die: the note for a link that *is* the canonical
    directory carried the arrow, so the most ordinary report in the tool was the one
    that crashed — after the install had already written links.
    """
    root, home = _sandbox(tmp_path)

    installed = _run_sync(root, home, tmp_path=tmp_path)
    assert installed.returncode == 0, _text(installed)

    checked = _run_sync(root, home, "--check", tmp_path=tmp_path)
    out = _text(checked)

    assert checked.returncode == 0, out
    assert "mode       link" in out, "the report did not get as far as its mode line"
    assert "link -> the canonical directory" in out
    assert "all targets in sync" in out


def test_a_path_the_console_cannot_encode_is_escaped_not_fatal(tmp_path) -> None:
    """The other half of the rule: a path this script was handed is not a crash.

    ``--root`` and ``--home`` are the user's to choose, and a home directory spelling a
    character cp1252 lacks used to end the run inside its own report — after some targets
    were linked and before the rest were looked at. Escaped is worse-looking and honest;
    a traceback is neither.
    """
    root, home = _sandbox(tmp_path, home_name="home-\u5bb6")

    installed = _run_sync(root, home, tmp_path=tmp_path)
    out = _text(installed)

    assert installed.returncode == 0, out
    assert "\\u5bb6" in out, "the unencodable path should be reported escaped"
    assert "audit log" in out, "the run stopped before it said what it had done"
