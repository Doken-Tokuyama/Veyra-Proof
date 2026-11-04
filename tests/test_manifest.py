"""The sdist must carry the fixtures, not what running them produced.

The live tests build a copy of each fixture in a temporary directory, but a
maintainer who audits one in place leaves ``target/`` and a generated
``Cargo.lock`` behind. Setuptools reads the working tree, so without an explicit
prune those files rode into the sdist — platform-specific build state published
as if it were source. The release gate found it once; this keeps it found.
"""

from __future__ import annotations

from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "MANIFEST.in"


def _manifest() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def test_every_fixture_build_directory_is_pruned() -> None:
    text = _manifest()
    for fixture in ("demo_crate", "broken_crate"):
        assert f"prune tests/fixtures/{fixture}/target" in text
        assert f"prune tests/fixtures/{fixture}/.veyra" in text


def test_the_generated_broken_crate_lockfile_is_not_published() -> None:
    assert "exclude tests/fixtures/broken_crate/Cargo.lock" in _manifest()
