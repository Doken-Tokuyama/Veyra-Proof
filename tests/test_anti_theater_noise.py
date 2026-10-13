"""Anti-theater heuristics must not drown real projects in false positives.

Auditing an unmodified checkout of rust-lang/regex produced 277 findings and a
REJECTED verdict: 114 "test has no observable assertion" inside fuzz regression
harnesses, 32 `panic!()` documenting invariants, 10 `#[allow]`, and 5 ignored
tests. A tool that rejects upstream Rust out of the box gets switched off.

The split applied here: `todo!()` and `unimplemented!()` stay blockers because
there is no honest reading of a shipping function that calls them; everything
ambiguous is reported at informational severity and never blocks unless the
project asks for it through `veyra.toml`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veyra_proof.config import PolicyConfig
from veyra_proof.models import CommandResult, PlatformOS, WorkspaceInfo
from veyra_proof.platform_info import PlatformInfo
from veyra_proof.tools.anti_theater import analyze_rust_sources
from veyra_proof.tools.base import AuditContext, parse_ignored_tests
from veyra_proof.config import VeyraConfig


def scan(root: Path, policy: PolicyConfig | None = None):
    return analyze_rust_sources(root, [], policy or PolicyConfig())


def rules(findings, rule: str) -> list:
    return [f for f in findings if f.rule == rule]


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestCommentsAreNotCode:
    """A doc comment naming a placeholder is not a call to it.

    Found the hard way: this crate's own module docs explained that it
    contained no `todo!()` and no `unimplemented!()`, and both rules read the
    sentence as the construct. A blocker raised by prose is the worst way for
    these heuristics to be wrong.
    """

    def test_a_module_doc_naming_the_macros_is_not_a_placeholder(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "//! Pricing engine.\n"
            "//!\n"
            "//! No `todo!()`, no `unimplemented!()`, no `panic!()` in this file.\n"
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
        )
        found = scan(tmp_path)
        assert rules(found, "VT001") == []
        assert rules(found, "VT002") == []
        assert rules(found, "VT003") == []

    def test_a_trailing_comment_does_not_count(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b // no todo!() here yet\n}\n",
        )
        assert rules(scan(tmp_path), "VT001") == []

    def test_a_block_comment_does_not_count(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "/*\n * Previously: pub fn add() -> i32 { todo!() }\n */\n"
            "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
        )
        assert rules(scan(tmp_path), "VT001") == []

    def test_a_block_comment_that_opens_and_closes_inline_does_not_count(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "pub fn add(a: i32, b: i32) -> i32 {\n    /* todo!() */\n    a + b\n}\n",
        )
        assert rules(scan(tmp_path), "VT001") == []

    def test_a_real_call_is_still_caught_beside_a_comment(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "//! documented\npub fn add(a: i32) -> i32 {\n    todo!() // not done\n}\n",
        )
        assert len(rules(scan(tmp_path), "VT001")) == 1

    def test_a_url_in_a_string_is_not_mangled_into_a_comment(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "pub fn docs() -> &'static str {\n"
            '    "https://example.com/todo/" \n}\n',
        )
        assert rules(scan(tmp_path), "VT001") == []

    def test_ignore_mentioned_in_a_comment_is_not_an_ignore(self, tmp_path):
        write(tmp_path, "src/lib.rs", "//! tests below are #[ignore]d deliberately\n")
        assert rules(scan(tmp_path), "VT004") == []


class TestUnambiguousPlaceholdersStillBlock:
    def test_todo_in_production_is_a_blocker(self, tmp_path):
        write(tmp_path, "src/lib.rs", "pub fn add() -> i32 { todo!() }\n")
        found = rules(scan(tmp_path), "VT001")
        assert len(found) == 1
        assert found[0].severity == "blocker"

    def test_unimplemented_in_production_is_a_blocker(self, tmp_path):
        write(tmp_path, "src/lib.rs", "pub fn add() -> i32 { unimplemented!() }\n")
        found = rules(scan(tmp_path), "VT002")
        assert len(found) == 1
        assert found[0].severity == "blocker"

    def test_a_placeholder_inside_a_test_module_is_not_a_blocker(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "pub fn add() -> i32 { 1 }\n#[cfg(test)]\nmod tests {\n"
            "    #[test]\n    fn t() { todo!() }\n}\n",
        )
        assert rules(scan(tmp_path), "VT001") == []


class TestWeakOraclesInFuzzTargetsAreNotFindings:
    def test_a_fuzz_harness_without_assertions_is_not_flagged(self, tmp_path):
        write(
            tmp_path,
            "tests/fuzz/mod.rs",
            "#[test]\nfn meta_stopat_specialize_start_states() {\n"
            "    let corpus = read_corpus();\n    run_engine(corpus);\n}\n",
        )
        assert rules(scan(tmp_path), "VT006") == []

    def test_a_fuzz_target_directory_is_excluded(self, tmp_path):
        write(
            tmp_path,
            "fuzz/fuzz_targets/add.rs",
            "#[test]\nfn fuzz_add() {\n    engine.run();\n}\n",
        )
        assert rules(scan(tmp_path), "VT006") == []

    def test_an_ordinary_test_without_an_assertion_is_still_reported(self, tmp_path):
        write(
            tmp_path,
            "tests/parser.rs",
            "#[test]\nfn parses_input() {\n    parse(\"x\");\n}\n",
        )
        assert len(rules(scan(tmp_path), "VT006")) == 1

    def test_it_is_informational_by_default(self, tmp_path):
        write(tmp_path, "tests/parser.rs", "#[test]\nfn parses() {\n    parse(\"x\");\n}\n")
        assert rules(scan(tmp_path), "VT006")[0].severity == "info"

    def test_it_blocks_only_when_the_project_asks(self, tmp_path):
        write(tmp_path, "tests/parser.rs", "#[test]\nfn parses() {\n    parse(\"x\");\n}\n")
        found = rules(
            scan(tmp_path, PolicyConfig(fail_on_weak_oracles=True)), "VT006"
        )
        assert found[0].severity == "warning"

    def test_an_ignored_test_is_reported_once_as_ignored_not_as_a_weak_oracle(
        self, tmp_path
    ):
        write(
            tmp_path,
            "tests/slow.rs",
            "#[test]\n#[ignore]\nfn slow_case() {\n    heavy();\n}\n",
        )
        found = scan(tmp_path)
        assert rules(found, "VT006") == []
        assert len(rules(found, "VT004")) == 1


class TestIgnoranceAndLintsAreJudgementCalls:
    def test_ignore_is_informational_by_default(self, tmp_path):
        write(tmp_path, "src/lib.rs", "#[test]\n#[ignore]\nfn slow() {}\n")
        assert rules(scan(tmp_path), "VT004")[0].severity == "info"

    def test_ignore_blocks_when_configured(self, tmp_path):
        write(tmp_path, "src/lib.rs", "#[test]\n#[ignore]\nfn slow() {}\n")
        found = rules(scan(tmp_path, PolicyConfig(fail_on_ignored_tests=True)), "VT004")
        assert found[0].severity == "error"

    def test_panic_in_an_invariant_is_informational_by_default(self, tmp_path):
        write(
            tmp_path,
            "src/bytes.rs",
            "pub fn group(i: usize) -> &str {\n    groups.get(i).unwrap_or_else(|| panic!(\"no group at index {i}\"))\n}\n",
        )
        assert rules(scan(tmp_path), "VT003")[0].severity == "info"

    def test_panic_blocks_when_configured(self, tmp_path):
        write(tmp_path, "src/bytes.rs", "pub fn f() { panic!(\"boom\"); }\n")
        found = rules(scan(tmp_path, PolicyConfig(fail_on_panic=True)), "VT003")
        assert found[0].severity == "warning"

    def test_lint_allow_is_informational_by_default(self, tmp_path):
        write(tmp_path, "src/lib.rs", "#[allow(dead_code)]\nfn helper() {}\n")
        assert rules(scan(tmp_path), "VT013")[0].severity == "info"

    def test_lint_allow_blocks_when_configured(self, tmp_path):
        write(tmp_path, "src/lib.rs", "#[allow(dead_code)]\nfn helper() {}\n")
        found = rules(scan(tmp_path, PolicyConfig(fail_on_lint_allow=True)), "VT013")
        assert found[0].severity == "warning"

    def test_unsafe_stays_informational(self, tmp_path):
        write(tmp_path, "src/lib.rs", "pub fn f(p: *const u8) { unsafe { *p }; }\n")
        assert rules(scan(tmp_path), "VT008")[0].severity == "info"


class TestCfgTricksAreJudgementCalls:
    def test_cfg_not_test_is_informational_by_default(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "#[cfg(not(test))]\npub fn only_in_prod() -> u8 { 1 }\n",
        )
        assert rules(scan(tmp_path), "VT010")[0].severity == "info"

    def test_cfg_not_test_blocks_when_configured(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "#[cfg(not(test))]\npub fn only_in_prod() -> u8 { 1 }\n",
        )
        found = rules(scan(tmp_path, PolicyConfig(fail_on_cfg_tricks=True)), "VT010")
        assert found[0].severity == "warning"

    def test_the_cfg_split_rule_is_informational_by_default(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "#[cfg(test)]\nfn helper() {}\n#[cfg(not(test))]\npub fn real() -> u8 { 1 }\n",
        )
        assert rules(scan(tmp_path), "VT015")[0].severity == "info"

    def test_the_trivial_constant_rule_is_informational_by_default(self, tmp_path):
        write(tmp_path, "src/lib.rs", "pub fn sentinel() -> u8 {\n    return 0;\n}\n")
        assert rules(scan(tmp_path), "VT014")[0].severity == "info"

    def test_a_guard_clause_is_still_not_a_placeholder(self, tmp_path):
        write(tmp_path, "src/lib.rs", "pub fn count(n: u8) -> u8 {\n    if n == 0 { return 0; }\n    n\n}\n")
        assert rules(scan(tmp_path), "VT014") == []

    def test_the_knob_is_documented_and_defaulted_off(self):
        assert PolicyConfig().fail_on_cfg_tricks is False


class TestIgnoredTestsAreNeverHidden:
    """cargo test reports an ignored test either way; only severity changes."""

    OUTPUT = "test suite::slow ... ignored\ntest suite::ok ... ok\n"

    def test_reported_as_info_by_default(self):
        found = parse_ignored_tests(self.OUTPUT)
        assert len(found) == 1
        assert found[0].severity == "info"
        assert "fail_on_ignored_tests=false" in found[0].explanation

    def test_escalates_to_error_when_configured(self):
        found = parse_ignored_tests(self.OUTPUT, severity="error")
        assert found[0].severity == "error"
        assert "fail_on_ignored_tests=true" in found[0].explanation

    def test_an_informational_note_is_labelled_as_one(self):
        """`FINDINGS` with nothing blocking must not read like a problem."""
        from veyra_proof.tools.base import classify_from_command

        status, severity, summary = classify_from_command(
            timed_out=False,
            exit_code=0,
            findings=parse_ignored_tests(self.OUTPUT),
            stdout=self.OUTPUT,
            stderr="",
            error=None,
        )
        assert status == "FINDINGS"
        assert severity == "info"
        assert "none blocking" in summary

    def test_a_blocking_finding_keeps_the_plain_wording(self):
        from veyra_proof.models import Finding
        from veyra_proof.tools.base import classify_from_command

        status, severity, summary = classify_from_command(
            timed_out=False,
            exit_code=101,
            findings=[
                Finding(
                    rule="test-failure",
                    severity="error",
                    confidence="high",
                    file=None,
                    line=None,
                    excerpt="suite::x",
                    explanation="failed",
                    recommendation="fix it",
                )
            ],
            stdout="",
            stderr="",
            error=None,
        )
        assert (status, severity) == ("FINDINGS", "error")
        assert summary == "1 finding(s) reported."


class TestUpstreamStyleProjectIsNotRejected:
    """The regression that matters: a healthy crate produces nothing blocking."""

    def test_a_typical_crate_yields_no_blocking_findings(self, tmp_path):
        write(
            tmp_path,
            "src/lib.rs",
            "//! Crate docs.\n"
            "use std::collections::HashMap;\n"
            "\n"
            "#[allow(dead_code)]\n"
            "fn legacy_helper() -> u8 { 7 }\n"
            "\n"
            "pub fn parse(input: &str) -> Result<u32, String> {\n"
            "    if input.is_empty() {\n"
            "        return Err(\"empty\".to_string());\n"
            "    }\n"
            "    input.parse().map_err(|e| e.to_string())\n"
            "}\n"
            "\n"
            "pub fn group(map: &HashMap<u8, u8>, key: u8) -> u8 {\n"
            "    *map.get(&key).unwrap_or_else(|| panic!(\"missing group {key}\"))\n"
            "}\n"
            "\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn parses_numbers() { assert_eq!(parse(\"1\").unwrap(), 1); }\n"
            "    #[test]\n"
            "    #[ignore]\n"
            "    fn slow_fuzz() { engine(); }\n"
            "}\n",
        )
        write(
            tmp_path,
            "tests/fuzz/mod.rs",
            "#[test]\nfn meta_specialize() {\n    let corpus = read();\n    engine(corpus);\n}\n",
        )
        findings = scan(tmp_path)
        blocking = [f for f in findings if f.severity != "info"]
        assert blocking == [], [f.rule for f in blocking]
        # They are still reported, just not as blockers.
        assert len(findings) > 0


class TestToolSummaryShowsWhatWasFound:
    def _context(self, tmp_path):
        workspace = WorkspaceInfo(
            path=str(tmp_path),
            workspace_root=str(tmp_path),
            packages=[],
            workspace_members=[],
            target_directory=str(tmp_path / "target"),
            default_members=[],
            root_package=None,
            has_lockfile=False,
            lockfile_path=None,
            metadata_raw={},
        )
        platform = PlatformInfo(
            os=PlatformOS.LINUX,
            os_raw="Linux",
            architecture="x86_64",
            architecture_raw="x86_64",
            python_version="3.12",
            python_executable="python",
            cargo_path="cargo",
            cargo_version="cargo 1.98.0",
            rustc_path="rustc",
            rustc_version="rustc 1.98.0",
        )
        return AuditContext(
            workspace=workspace,
            platform=platform,
            config=VeyraConfig(),
            runner=None,
            evidence_dir=tmp_path / "evidence",
            profile="quick",
            timeout_seconds=60.0,
        )

    def test_informational_findings_are_counted_not_hidden(self, tmp_path):
        from veyra_proof.tools.anti_theater import AntiTheaterTool

        write(tmp_path, "src/lib.rs", "#[allow(dead_code)]\nfn helper() {}\n")
        result = AntiTheaterTool().run(self._context(tmp_path))
        assert result.status == "OK"
        assert "1 informational" in result.summary
        assert "VT013=1" in result.environment["rule_histogram"]

    def test_the_log_opens_with_the_histogram(self, tmp_path):
        from veyra_proof.tools.anti_theater import AntiTheaterTool

        write(tmp_path, "src/lib.rs", "#[allow(dead_code)]\nfn helper() {}\n")
        result = AntiTheaterTool().run(self._context(tmp_path))
        log = Path(result.stdout_path).read_text(encoding="utf-8")
        assert log.splitlines()[0] == "rule histogram: VT013=1"


class TestPolicyComesFromTheConfigFile:
    def test_the_new_knobs_default_to_non_blocking(self):
        assert PolicyConfig().fail_on_ignored_tests is False
        assert PolicyConfig().fail_on_panic is False
        assert PolicyConfig().fail_on_lint_allow is False
        assert PolicyConfig().fail_on_weak_oracles is False
        assert PolicyConfig().fail_on_cfg_tricks is False

    def test_the_unambiguous_ones_still_default_to_blocking(self):
        assert PolicyConfig().fail_on_todo is True
        assert PolicyConfig().fail_on_unimplemented is True

    def test_veyra_toml_can_turn_them_back_on(self, tmp_path):
        from veyra_proof.config import load_veyra_config

        (tmp_path / "veyra.toml").write_text(
            "[policy]\n"
            "fail_on_ignored_tests = true\n"
            "fail_on_panic = true\n"
            "fail_on_lint_allow = true\n"
            "fail_on_weak_oracles = true\n",
            encoding="utf-8",
        )
        config = load_veyra_config(tmp_path)
        assert config.policy.fail_on_ignored_tests is True
        assert config.policy.fail_on_panic is True
        assert config.policy.fail_on_lint_allow is True
        assert config.policy.fail_on_weak_oracles is True


@pytest.mark.parametrize("rule", ["VT001", "VT002"])
def test_placeholder_rules_keep_their_confidence(rule, tmp_path):
    write(tmp_path, "src/lib.rs", "pub fn f() -> i32 { todo!() }\npub fn g() -> i32 { unimplemented!() }\n")
    hits = rules(scan(tmp_path), rule)
    assert hits and hits[0].confidence == "high"