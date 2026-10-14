"""Guards for scripts/check_platforms.py, the cross-platform release step.

The gate runs the suite on one machine, and that machine is the one that will not
break. v0.4.3 was tagged and published from a macOS checkout while both of its
Windows jobs were failing, so the fix became 0.4.4 instead of a green 0.4.3. The
only place that evidence exists is the CI run for the commit being tagged, which is
what the step reads.

The verdicts are a pure function of the run GitHub reports, so every refusal below
— red, still running, no run at all, a platform that never ran, a job that was
skipped — is driven without a network in milliseconds. Two live tests then check the
real plumbing against this repository's own history, and skip with a reason when
`gh` cannot reach GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_platforms.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The commit whose release this check exists for: v0.4.3, tagged and published
# while both of its Windows jobs were failing. Asserted against a live run below.
V043_COMMIT = "d4a40924e32b5a6fe2f029c53e4588ec0f56a2f7"


@pytest.fixture()
def check():
    spec = importlib.util.spec_from_file_location("check_platforms", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(**overrides) -> dict:
    """A run of the CI workflow, shaped the way the API reports one."""
    run = {
        "id": 1,
        "name": "CI",
        "run_number": 23,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "html_url": "https://example.test/runs/1",
    }
    run.update(overrides)
    return run


def _job(name: str, conclusion: str = "success") -> dict:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://example.test/jobs/{name}",
    }


def _green() -> list[dict]:
    return [
        _job("py3.10 / ubuntu-latest"),
        _job("py3.13 / ubuntu-latest"),
        _job("py3.10 / macos-latest"),
        _job("py3.13 / macos-latest"),
        _job("py3.10 / windows-latest"),
        _job("py3.13 / windows-latest"),
        _job("live audit of the demo crate"),
        _job("account skill copy (advisory)"),
    ]


class TestTheVerdict:
    def test_a_green_run_on_every_platform_passes(self, check):
        problems, confirmed = check.classify(
            "a" * 40, "CI", _run(), _green(), check.REQUIRED_PLATFORMS
        )

        assert problems == []
        assert any("windows-latest: 2 job(s) success" in line for line in confirmed)

    def test_no_run_is_refused_with_the_instruction_that_fixes_it(self, check):
        """Nothing has run for this commit, so nothing about it is proven.

        The refusal has to be actionable: the release flow is push, let CI finish,
        then tag — and the person reading it has not done that yet.
        """
        problems, confirmed = check.classify(
            "a" * 40, "CI", None, [], check.REQUIRED_PLATFORMS
        )

        assert confirmed == []
        assert len(problems) == 1
        assert "no CI run exists for aaaaaaaaaaaa" in problems[0]
        assert "push the commit" in problems[0]

    def test_a_run_still_in_flight_is_not_a_verdict(self, check):
        """In progress is not a pass, and it is not a failure either.

        Saying "failed" here would be wrong and would teach someone to ignore the
        message; saying nothing would let a release out on an unfinished run.
        """
        run = _run(status="in_progress", conclusion=None)
        problems, confirmed = check.classify(
            "a" * 40, "CI", run, [], check.REQUIRED_PLATFORMS
        )

        assert confirmed == []
        assert len(problems) == 1
        assert "still in_progress" in problems[0]
        assert "run the gate again" in problems[0]

    def test_a_failed_platform_names_the_job_and_its_url(self, check):
        """This is the v0.4.3 shape: ubuntu and macos green, windows red."""
        jobs = [
            job if "windows" not in job["name"] else _job(job["name"], "failure")
            for job in _green()
        ]
        jobs = [dict(job, html_url=f"https://example.test/jobs/{job['name']}") for job in jobs]
        run = _run(conclusion="failure")

        problems, confirmed = check.classify(
            "a" * 40, "CI", run, jobs, check.REQUIRED_PLATFORMS
        )

        assert "ubuntu-latest: 2 job(s) success" in " ".join(confirmed)
        failed = [line for line in problems if line.startswith("job '")]
        assert len(failed) == 2, problems
        assert "py3.10 / windows-latest" in failed[0]
        assert "https://example.test/jobs/py3.10 / windows-latest" in failed[0]
        assert any("concluded 'failure'" in line for line in problems)
        assert any(
            "no successful job on windows-latest" in line for line in problems
        ), "the missing platform must be named on its own, not only as a failed job"

    def test_a_cancelled_job_is_not_a_pass(self, check):
        """Cancelled, skipped and timed-out all leave a platform unproven."""
        jobs = [
            _job("py3.10 / ubuntu-latest"),
            _job("py3.13 / ubuntu-latest"),
            _job("py3.10 / macos-latest"),
            _job("py3.13 / macos-latest"),
            _job("py3.10 / windows-latest", "cancelled"),
            _job("py3.13 / windows-latest"),
        ]

        problems, _ = check.classify(
            "a" * 40, "CI", _run(), jobs, check.REQUIRED_PLATFORMS
        )

        assert any("'cancelled'" in line and "py3.10 / windows-latest" in line for line in problems)

    def test_a_run_with_no_jobs_proves_nothing(self, check):
        problems, _ = check.classify(
            "a" * 40, "CI", _run(), [], check.REQUIRED_PLATFORMS
        )

        assert any("reports no jobs" in line for line in problems)
        assert len([line for line in problems if "no successful job on" in line]) == 3

    def test_a_platform_that_never_ran_is_refused(self, check):
        """A release must not be the first place a supported platform reports in."""
        problems, confirmed = check.classify(
            "a" * 40, "CI", _run(), _green(), ("ubuntu-latest", "freebsd-latest")
        )

        assert "ubuntu-latest: 2 job(s) success" in " ".join(confirmed)
        assert any("no successful job on freebsd-latest" in line for line in problems)

    def test_the_confirmation_says_which_python_versions_ran(self, check):
        """`py3.10 / windows-latest` and `py3.13 / windows-latest` are not the same claim."""
        _, confirmed = check.classify(
            "a" * 40, "CI", _run(), _green(), check.REQUIRED_PLATFORMS
        )

        assert "windows-latest: 2 job(s) success (py3.10, py3.13)" in confirmed


class TestPickingTheRun:
    def test_a_run_of_another_workflow_is_not_the_evidence(self, check):
        """`publish.yml` runs after a tag; it cannot be the proof for cutting one."""
        runs = [
            _run(name="Publish to PyPI"),
            _run(name="Graph Update: pip in /.", id=2),
        ]

        assert check.pick_run(runs, "CI") is None

    def test_the_newest_attempt_of_the_workflow_wins(self, check):
        """A commit can carry an original run and a re-run; only the last counts."""
        runs = [
            _run(run_number=7, conclusion="failure"),
            _run(run_number=8, conclusion="success"),
            _run(run_number=6, conclusion="success"),
        ]

        assert check.pick_run(runs, "CI")["run_number"] == 8


class TestThePromiseAndTheMatrix:
    def test_the_matrix_still_covers_every_platform_a_release_claims(self, check):
        """Two lists that have to agree, held together on purpose.

        `REQUIRED_PLATFORMS` is the promise a release makes. If CI stops building the
        matrix for one of them, the check would otherwise quietly follow the matrix
        down and a release would claim a platform nothing ran on.
        """
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        match = re.search(r"^\s*os:\s*\[([^\]]+)\]", text, flags=re.MULTILINE)
        assert match, "the CI workflow no longer declares an `os:` matrix"

        in_matrix = {item.strip() for item in match.group(1).split(",")}

        assert set(check.REQUIRED_PLATFORMS) <= in_matrix, (
            "a platform a release claims is missing from the CI matrix: "
            f"{sorted(set(check.REQUIRED_PLATFORMS) - in_matrix)}"
        )

    def test_the_default_workflow_is_the_one_that_carries_the_matrix(self, check):
        """The default has to name a real workflow, or nothing is ever found."""
        text = CI_WORKFLOW.read_text(encoding="utf-8")

        assert check.DEFAULT_WORKFLOW == "CI"
        assert re.search(rf"^name:\s*{check.DEFAULT_WORKFLOW}\s*$", text, flags=re.MULTILINE)

    def test_the_job_names_still_carry_what_the_check_looks_for(self, check):
        """The check reads the platform out of the job name.

        That name is a template in the workflow (`py${{ matrix.python }} /
        ${{ matrix.os }}`), so the mapping is asserted here rather than assumed: a
        rename that drops `${{ matrix.os }}` would make every platform look absent
        while every job passed.
        """
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        # Deliberately not `^\s*name:`: that matches the workflow's own `name: CI`
        # first, which is how the first version of this test asserted nothing.
        template = re.search(
            r"^\s*name:\s*(py\$?\{\{\s*matrix\.python\s*\}\}[^\n]*)$",
            text,
            flags=re.MULTILINE,
        )
        assert template, "the matrix job no longer names itself after matrix.python"
        assert "matrix.os" in template.group(1), (
            "the platform has to be part of the job name the check reads: "
            f"{template.group(1)!r}"
        )


# --------------------------------------------------------------------------
# The real plumbing, against this repository's own history
# --------------------------------------------------------------------------


def _gh_reaches_github() -> str | None:
    """None when `gh` can read this repository, else the reason it cannot.

    Checked against *gh itself* rather than against my script: if the transport is
    down the live tests skip with a reason, while a defect in my script still exits
    2 and fails them. Skipping on my script's own exit code would hide exactly the
    bug these tests exist for.
    """
    if shutil.which("gh") is None:
        return "gh is not installed"
    for argv in (["auth", "status"], ["api", "repos/Doken-Tokuyama/Veyra-Proof"]):
        completed = subprocess.run(["gh", *argv], capture_output=True, text=True)
        if completed.returncode != 0:
            return f"gh could not reach GitHub: gh {' '.join(argv)} exited {completed.returncode}"
    return None


def _live(*argv: str, root: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_live_the_check_reads_this_repositorys_own_ci():
    """The plumbing: resolve the commit, find the run, report its jobs."""
    reason = _gh_reaches_github()
    if reason:
        pytest.skip(reason)

    completed = _live()

    assert completed.returncode != 2, (
        "gh can reach GitHub, so an exit 2 is a defect in the check rather than a "
        f"transport problem:\n{completed.stdout}\n{completed.stderr}"
    )
    assert "repo       Doken-Tokuyama/Veyra-Proof" in completed.stdout
    assert re.search(r"commit     [0-9a-f]{40}", completed.stdout)
    if completed.returncode == 0:
        assert "passed on ubuntu-latest, macos-latest, windows-latest" in completed.stdout


def test_live_the_commit_that_shipped_a_red_windows_build_is_refused():
    """v0.4.3: the release the step exists for.

    It was tagged and published from this checkout, and CI reported the failure on
    both Windows jobs only afterwards. The check has to read that same commit, today,
    and refuse it — this is the regression test for the short-SHA query that found
    "no run" for a commit whose run had failed.
    """
    reason = _gh_reaches_github()
    if reason:
        pytest.skip(reason)
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{V043_COMMIT}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if exists.returncode != 0:
        pytest.skip("this clone does not carry the v0.4.3 commit")

    completed = _live("--commit", V043_COMMIT)

    assert completed.returncode == 1, completed.stdout
    assert "py3.10 / windows-latest" in completed.stdout
    assert "concluded 'failure'" in completed.stdout
    assert V043_COMMIT[:12] in completed.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_live_a_commit_github_has_never_seen_is_refused(tmp_path: Path):
    """The pre-push state, which is where a releaser actually starts.

    A commit that exists only here has no run, so nothing about Windows or macOS has
    been tested for what the tag would name. The refusal has to say what to do about
    it rather than reading as a verdict on the platforms.
    """
    reason = _gh_reaches_github()
    if reason:
        pytest.skip(reason)

    repo = tmp_path / "unpushed"
    repo.mkdir()
    (repo / "file.txt").write_text("never pushed\n", encoding="utf-8")
    for argv in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "local"],
    ):
        subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)

    completed = _live(
        "--repo", "Doken-Tokuyama/Veyra-Proof", root=repo
    )

    assert completed.returncode == 1, completed.stdout
    assert "no CI run exists" in completed.stdout
    assert "push the commit" in completed.stdout


class TestTheCommandLine:
    def test_an_unresolvable_commit_is_misuse_not_a_verdict(self, check, tmp_path, capsys, monkeypatch):
        """A typo must not read as "the platforms failed"."""
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: None)

        code = check.main(["--root", str(tmp_path), "--commit", "nope"])

        assert code == 2
        assert "cannot resolve 'nope'" in capsys.readouterr().err

    def test_a_repository_it_cannot_read_is_not_a_pass(self, check, tmp_path, capsys, monkeypatch):
        """Exit 2 exists so an unverifiable release is refused, never assumed green."""
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "a" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(check, "runs_for", lambda slug, sha, root: (1, [], "gh: Not Found"))

        code = check.main(["--root", str(tmp_path)])

        assert code == 2
        error = capsys.readouterr().err
        assert "NOT RUN" in error
        assert "this is not a pass" in error

    def test_a_green_run_exits_zero_and_confirms_each_platform(
        self, check, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(check, "runs_for", lambda slug, sha, root: (0, [_run()], ""))
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, _green(), ""))

        code = check.main(["--root", str(tmp_path)])
        out = capsys.readouterr().out

        assert code == 0
        assert "passed on ubuntu-latest, macos-latest, windows-latest" in out
        assert "bbbbbbbbbbbb" in out

    def test_a_red_run_exits_one_and_says_the_tag_was_refused(
        self, check, tmp_path, capsys, monkeypatch
    ):
        jobs = [_job(job["name"], "failure" if "windows" in job["name"] else "success") for job in _green()]
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(
            check, "runs_for", lambda slug, sha, root: (0, [_run(conclusion="failure")], "")
        )
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, jobs, ""))

        code = check.main(["--root", str(tmp_path)])
        out = capsys.readouterr().out

        assert code == 1
        assert "the tag was refused" in out

    def test_the_report_states_the_commit_platforms_and_jobs(
        self, check, tmp_path, monkeypatch
    ):
        """What the gate records, so a release note never restates it by hand.

        Asserted here rather than in the notes generator for the reason the notes were
        wrong at first: the record is only as good as what this step says about itself.
        """
        report = tmp_path / "reports" / "platforms.json"
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(check, "runs_for", lambda slug, sha, root: (0, [_run()], ""))
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, _green(), ""))

        code = check.main(["--root", str(tmp_path), "--report", str(report)])

        assert code == 0
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["tool"] == "check_platforms"
        assert payload["ok"] is True
        facts = payload["facts"]
        assert facts["commit"] == "b" * 40
        assert facts["workflow"] == "CI"
        assert facts["jobs"] == len(_green())
        assert facts["platforms"] == {
            "ubuntu-latest": ["py3.10", "py3.13"],
            "macos-latest": ["py3.10", "py3.13"],
            "windows-latest": ["py3.10", "py3.13"],
        }

    def test_a_platform_that_failed_is_recorded_as_empty_not_as_absent(
        self, check, tmp_path, monkeypatch
    ):
        """Absence and failure are different claims, so the record keeps both."""
        report = tmp_path / "reports" / "platforms.json"
        jobs = [
            _job(job["name"], "failure") if "windows" in job["name"] else job
            for job in _green()
        ]
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(
            check, "runs_for", lambda slug, sha, root: (0, [_run(conclusion="failure")], "")
        )
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, jobs, ""))

        assert check.main(["--root", str(tmp_path), "--report", str(report)]) == 1

        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["ok"] is False
        assert payload["facts"]["platforms"]["windows-latest"] == []
        assert payload["facts"]["platforms"]["ubuntu-latest"] == ["py3.10", "py3.13"]

    def test_a_report_that_cannot_be_written_is_not_a_failed_verdict(
        self, check, tmp_path, monkeypatch, capsys
    ):
        """It costs a release-note row; it does not overturn a verdict."""
        blocker = tmp_path / "file"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(check, "runs_for", lambda slug, sha, root: (0, [_run()], ""))
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, _green(), ""))

        code = check.main(["--root", str(tmp_path), "--report", str(blocker / "r.json")])

        assert code == 0
        assert "could not write the report" in capsys.readouterr().err

    def test_the_required_platforms_can_be_narrowed_from_the_command_line(
        self, check, tmp_path, capsys, monkeypatch
    ):
        """`--require` is what lets the promise be checked without editing the file."""
        monkeypatch.setattr(check, "resolve_commit", lambda root, ref: "b" * 40)
        monkeypatch.setattr(check, "repo_slug", lambda root: "owner/name")
        monkeypatch.setattr(check, "runs_for", lambda slug, sha, root: (0, [_run()], ""))
        monkeypatch.setattr(check, "jobs_for", lambda slug, run_id, root: (0, _green(), ""))

        assert check.main(["--root", str(tmp_path), "--require", "ubuntu-latest"]) == 0
        assert check.main(["--root", str(tmp_path), "--require", "freebsd-latest"]) == 1
        assert "freebsd-latest" in capsys.readouterr().out

    def test_it_only_reads(self, check, tmp_path, monkeypatch):
        """No `git`/`gh` write command exists in this script, and none may appear.

        The check reports; the gate decides. A step that repaired what it found would
        be one nobody could trust the verdict of.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r'gh\(\s*\["([a-z-]+)"', source) + re.findall(
            r'\["git", "([a-z-]+)"', source
        )

        assert calls, "the extractor found no commands, so this test asserts nothing"
        assert set(calls) <= {"repo", "api", "rev-parse", "remote"}, calls
