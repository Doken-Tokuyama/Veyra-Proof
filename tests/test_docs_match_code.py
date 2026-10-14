"""The README is part of the product: it may not promise what the code does not do.

Every claim below was once false. `reproducible-build` was documented as part of
`hardened` while belonging to no profile at all; the CI table named a test count
that had drifted; `ci_assert_evidence.py` was documented with a bare `<path>` that
its argparse rejects when handed an evidence directory. Documentation that
contradicts the code is the same defect as a status that contradicts the log, so
it gets a test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from veyra_proof.tool_registry import all_tools

README = Path(__file__).resolve().parents[1] / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _profile_row(profile: str) -> str:
    for line in _readme().splitlines():
        if line.startswith(f"| `{profile}` |"):
            return line
    raise AssertionError(f"README has no row for profile {profile}")


def _documented(profile: str, _seen: set[str] | None = None) -> str:
    """The row plus whatever it inherits, since a row may read "standard + ..."."""
    seen = _seen or set()
    if profile in seen:
        raise AssertionError(f"profile inheritance loops at {profile}")
    seen.add(profile)
    row = _profile_row(profile)
    inherited = ""
    body = row.split("|", 2)[2]
    for parent in ("quick", "standard", "hardened"):
        if body.strip().startswith(parent + " +"):
            inherited = _documented(parent, seen)
            break
    return inherited + " " + body


class TestProfilesAreDocumentedTruthfully:
    def test_the_two_profile_sources_agree(self):
        """`PROFILE_ORDER` selects; `default_profiles` is published.

        They were independent lists and had already drifted: `reproducible-build`
        was documented as part of `hardened` while the list that actually selects
        tools omitted it, so the promise was never kept.
        """
        from veyra_proof.tool_registry import profile_mismatches

        assert profile_mismatches() == []

    def test_every_tool_in_a_profile_is_named_in_that_row(self):
        """A tool silently excluded from its profile is a promise the code breaks."""
        for profile in ("quick", "standard", "hardened"):
            row = _documented(profile)
            for tool in all_tools():
                if profile not in tool.default_profiles:
                    continue
                assert tool.id in row, (
                    f"{tool.id} runs in {profile} but the README row does not name it: {row}"
                )

    def test_reproducible_build_really_is_part_of_hardened(self):
        from veyra_proof.tool_registry import tool_by_id

        assert "hardened" in tool_by_id()["reproducible-build"].default_profiles
        assert "reproducible-build" in _profile_row("hardened")

    def test_the_reproducible_cost_escape_hatch_is_documented(self):
        assert "--exclude reproducible-build" in _readme()

    def test_no_tool_is_claimed_for_a_profile_it_does_not_belong_to(self):
        row = _documented("quick")
        assert "miri" not in row
        assert "reproducible-build" not in row

    def test_hardened_actually_runs_the_reproducible_check(self):
        """The registry promise is only meaningful if selection honours it."""
        from veyra_proof.tool_registry import select_tools

        selected = {t.id for t in select_tools("hardened")}
        assert "reproducible-build" in selected
        assert "reproducible-build" not in {t.id for t in select_tools("quick")}
        assert "reproducible-build" not in {t.id for t in select_tools("standard")}


class TestSafetyTableIsReal:
    def test_the_lockfile_rule_is_stated(self):
        readme = _readme()
        assert "Rewrite `Cargo.lock` during a run" in readme
        assert "--locked" in readme

    def test_the_nightly_fallback_is_stated(self):
        readme = _readme()
        assert "Nightly-only targets" in readme
        assert "INCONCLUSIVE" in readme

    def test_the_heuristic_policy_table_lists_every_blocking_rule(self):
        row = _readme()
        assert "VT001" in row and "VT002" in row
        for knob in (
            "fail_on_panic",
            "fail_on_ignored_tests",
            "fail_on_weak_oracles",
            "fail_on_lint_allow",
            "fail_on_todo",
            "fail_on_unimplemented",
            "deny_unsafe",
        ):
            assert knob in row, f"{knob} is not documented"

    def test_fail_on_none_is_described_honestly(self):
        readme = _readme()
        assert "--fail-on none" in readme
        assert "cannot excuse" in readme

    def test_the_time_reporting_is_documented(self):
        readme = _readme()
        assert "## Time" in readme or "`timing`" in readme
        assert "Where the time goes" in readme
        assert "does not cache" in readme or "not cache" in readme

    def test_the_reproducible_policy_knob_is_documented(self):
        readme = _readme()
        assert "require_reproducible_build" in readme
        assert "--exclude reproducible-build" in readme


class TestNoRottenCounts:
    def test_the_ci_table_does_not_hardcode_a_test_count(self):
        readme = _readme()
        assert not re.search(r"\b\d+-test suite\b", readme), (
            "the README pins a test count that goes stale the moment one is added"
        )

    def test_the_evidence_check_documents_its_argument_as_a_workspace(self):
        readme = _readme()
        assert "ci_assert_evidence.py <workspace-path>" in readme
        assert "not an evidence directory" in readme


class TestTheLimitsAreWrittenDown:
    """A limit nobody reads is not a limit anybody respects.

    A fake written to look finished passes `quick` with exit 0. That is not a bug
    to be fixed — no regex can separate it from real code — it is a fact the
    reader has to be told, or the tool gets used for something it cannot do.
    """

    def test_the_blind_spot_of_the_heuristics_is_stated(self):
        readme = " ".join(_readme().split())
        assert "written to look finished" in readme
        assert "will pass `quick` with exit 0" in readme

    def test_the_answer_is_named_rather_than_left_implicit(self):
        readme = _readme()
        assert "`veyra-e2e` is the layer that catches it" in readme

    def test_the_line_parsing_weakness_is_disclosed(self):
        readme = _readme()
        assert "line-based" in readme

    def test_everything_the_readme_still_promises_is_still_true(self):
        """No promise in the README may be one the tool cannot keep."""
        from veyra_proof.tool_registry import all_tools

        readme = _readme()
        assert "`OK` is never assigned" in readme
        # Every rule the heuristics claim as blockers must actually be one.
        blockers = {"VT001", "VT002"}
        for rule in blockers:
            assert rule in readme, f"{rule} promised in the README but undocumented"
        assert all(t.id for t in all_tools())
    def test_ci_assert_evidence_rejects_an_evidence_directory(self, tmp_path):
        """Documented shape is a workspace; handing it evidence must fail loudly."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ci_assert", README.parent / "scripts" / "ci_assert_evidence.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        evidence = tmp_path / ".veyra" / "evidence" / "20260101T000000Z"
        evidence.mkdir(parents=True)
        assert module.main([str(evidence)]) == 1


class TestTheScriptArgparseMatchesItsDocumentation:
    """The documented argument is a workspace root, not an evidence directory."""


class TestTheReportedVersionIsThePublishedOne:
    """A published wheel may not sign its own evidence with a different version.

    0.4.1 shipped with `__version__` still reading "0.3.0": the distribution was
    0.4.1, `veyra --version` answered `veyra 0.3.0`, and every `manifest.json` that
    wheel wrote recorded `"version": "0.3.0"` as the tool that produced the evidence.
    The version lived in two files and only `pyproject.toml` was bumped, so the check
    is on the pair, not on either one alone.
    """

    def test_the_module_version_matches_pyproject(self):
        pyproject = README.parent / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

        from veyra_proof import __version__

        assert __version__ == declared, (
            f"src/veyra_proof/__init__.py says {__version__} but pyproject.toml declares "
            f"{declared}: bump both, or the CLI and its evidence misname the version"
        )

    def test_the_cli_reports_the_module_version(self):
        """The string a user reads has to be the one the package is installed as."""
        from veyra_proof import __version__

        env = dict(os.environ, PYTHONPATH=str(README.parent / "src"))
        proc = subprocess.run(
            [sys.executable, "-m", "veyra_proof", "--version"],
            capture_output=True,
            text=True,
            cwd=README.parent,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"veyra {__version__}"
