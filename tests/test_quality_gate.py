"""The quality gate itself must not be silently weakened."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mypy_requires_annotations_in_every_area() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "disallow_untyped_defs = true" in pyproject
    assert "disallow_untyped_defs = false" not in pyproject
    assert 'files = ["src/veyra_proof", "tests", "scripts"]' in pyproject
    assert "[[tool.mypy.overrides]]" not in pyproject


def test_ci_and_hook_enforce_formatting_and_types() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")

    assert "python -m ruff check src tests scripts" in ci
    assert "python -m ruff format --check src tests scripts" in ci
    assert "python -m mypy" in ci
    assert "-m ruff check src tests scripts" in hook
    assert "-m ruff format --check src tests scripts" in hook
    assert "-m mypy" in hook
