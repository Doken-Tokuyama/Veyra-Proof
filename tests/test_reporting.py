from pathlib import Path
from typing import Any

from veyra_proof.config import PolicyConfig
from veyra_proof.models import Finding, ToolResult
from veyra_proof.reporting import decide, render_markdown
from veyra_proof.tools.anti_theater import analyze_rust_sources
from veyra_proof.tools.base import (
    classify_from_command,
    parse_clippy_output,
    parse_test_failures,
    utc_now_iso,
)


def _result(tool_id: str, status: Any, findings: Any = None, severity: Any = "info") -> Any:
    now = utc_now_iso()
    return ToolResult(
        tool_id=tool_id,
        display_name=tool_id,
        status=status,
        severity=severity,
        command=["x"],
        exit_code=0 if status == "OK" else 1,
        started_at=now,
        finished_at=now,
        duration_seconds=0.1,
        stdout_path=None,
        stderr_path=None,
        findings=findings or [],
        summary=status,
    )


def test_decision_accepted() -> None:
    results = [_result("clippy", "OK"), _result("rustfmt", "OK")]
    d = decide(results, fail_on="findings", required_tool_ids=["clippy", "rustfmt"])
    assert d.decision == "ACCEPTED"


def test_decision_intentional_skip_not_required() -> None:
    """Config-gated tools skipped intentionally must not block ACCEPTED."""
    results = [
        _result("clippy", "OK"),
        _result("rustfmt", "OK"),
        _result("cargo-check", "OK"),
        _result("cargo-test", "OK"),
        _result("veyra-anti-theater", "OK"),
        ToolResult(
            tool_id="audit",
            display_name="cargo-audit",
            status="SKIPPED",
            severity="info",
            command=None,
            exit_code=None,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            duration_seconds=0.0,
            stdout_path=None,
            stderr_path=None,
            findings=[],
            summary="no Cargo.lock",
            environment={"skip_kind": "intentional"},
        ),
    ]
    d = decide(
        results,
        fail_on="findings",
        required_tool_ids=["clippy", "rustfmt", "cargo-check", "cargo-test", "veyra-anti-theater"],
    )
    assert d.decision == "ACCEPTED"


def test_decision_rejected_on_findings() -> None:
    findings = [Finding("x", "error", "high", "a.rs", 1, None, "bad", "fix")]
    results = [_result("clippy", "FINDINGS", findings, "error")]
    d = decide(results, fail_on="findings", required_tool_ids=["clippy"])
    assert d.decision == "REJECTED"


def test_decision_inconclusive_missing() -> None:
    d = decide(
        [],
        fail_on="findings",
        required_tool_ids=["clippy"],
        missing_required=["clippy"],
    )
    assert d.decision == "INCONCLUSIVE"


def test_ok_never_for_timeout_classification() -> None:
    status, _, _ = classify_from_command(
        timed_out=True,
        exit_code=None,
        findings=[],
        stdout="",
        stderr="",
        error="timeout",
    )
    assert status == "INCONCLUSIVE"


def test_ok_never_for_nonzero_without_analysis() -> None:
    status, _, _ = classify_from_command(
        timed_out=False,
        exit_code=2,
        findings=[],
        stdout="error: boom",
        stderr="",
        error=None,
    )
    assert status != "OK"


def test_parse_clippy() -> None:
    text = "error[clippy::foo]: bad thing\n  --> src/lib.rs:10:5\n"
    findings = parse_clippy_output(text)
    assert findings
    assert findings[0].file == "src/lib.rs"
    assert findings[0].line == 10


def test_parse_deny_unlicensed() -> None:
    from veyra_proof.tools.deny import parse_deny_output

    text = (
        "warning[no-license-field]: license expression was not specified\n"
        "error[unlicensed]: demo_crate = 0.1.0 is unlicensed\n"
    )
    findings = parse_deny_output(text)
    assert any(f.rule == "deny:unlicensed" and f.severity == "error" for f in findings)
    assert any(f.rule == "deny:no-license-field" for f in findings)


def test_parse_test_failures() -> None:
    text = "test foo::bar ... FAILED\n"
    findings = parse_test_failures(text)
    assert findings[0].excerpt == "foo::bar"


def test_anti_theater_todo(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn x() { todo!() }\n", encoding="utf-8")
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig(fail_on_todo=True))
    assert any(f.rule == "VT001" for f in findings)


def test_anti_theater_panic_in_cfg_test_module_is_not_a_src_finding(tmp_path: Path) -> None:
    """`panic!()` used as a test oracle inside `#[cfg(test)] mod tests` is not
    unfinished production error handling, even though the file lives in src/."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn half(n: i32) -> i32 {\n"
        "    n / 2\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn half_is_half() {\n"
        "        match half(3) {\n"
        "            1 => {}\n"
        '            other => panic!("unexpected {other}"),\n'
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    vt003 = [f for f in findings if f.rule == "VT003"]
    assert vt003 == [], f"test-only panic! reported as production code: {vt003}"


def test_anti_theater_panic_in_production_path_is_still_reported(tmp_path: Path) -> None:
    """Exempting cfg(test) blocks must not blanket-exempt src/ files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn checked(n: i32) -> i32 {\n"
        "    if n < 0 {\n"
        '        panic!("negative input");\n'
        "    }\n"
        "    n\n"
        "}\n",
        encoding="utf-8",
    )
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    assert [f.line for f in findings if f.rule == "VT003"] == [3]


def test_anti_theater_guard_clause_is_not_a_constant_stub(tmp_path: Path) -> None:
    """An early `return 0;` guard is edge-case logic; only a body that *is* a
    literal is a placeholder, so the guard must not be read as one."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub fn clamp_low(n: i32) -> i32 {\n    if n < 0 {\n        return 0;\n    }\n    n\n}\n",
        encoding="utf-8",
    )
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    assert [f.rule for f in findings if f.rule == "VT014"] == []


def test_anti_theater_trivial_constant_body_is_still_reported(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn answer() -> i32 { 42 }\n", encoding="utf-8")
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    assert [f.rule for f in findings if f.rule == "VT014"] == ["VT014"]


def test_anti_theater_test_without_any_oracle_is_reported(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn only_runs_the_code() {\n"
        "        std::hint::black_box(1 + 1);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    assert [f.rule for f in findings if f.rule == "VT006"] == ["VT006"]


def test_anti_theater_panicking_oracle_counts_as_an_oracle(tmp_path: Path) -> None:
    """A failed `unwrap`/`expect`/`panic!` makes the test fail observably, so
    the test does have an oracle even though it holds no `assert` macro."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn parses_a_valid_document() {\n"
        '        let parsed = parse("1 + 1").expect("valid input must parse");\n'
        "        std::hint::black_box(parsed);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig())
    assert [f.rule for f in findings if f.rule == "VT006"] == []


def test_markdown_structure() -> None:
    md = render_markdown(
        workspace="/tmp/w",
        timestamp="t",
        os_arch="macOS / aarch64",
        toolchain="stable",
        profile="quick",
        decision=decide(
            [_result("clippy", "OK")],
            fail_on="findings",
            required_tool_ids=["clippy"],
        ),
        results=[_result("clippy", "OK")],
        changes=[],
    )
    assert "# Veyra Proof Report" in md
    assert "## OK" in md
    assert "## Problems" in md
    assert "## Skipped" in md
    assert "## Errors" in md
    assert "## Project Changes" in md


def _advisory(rule: str = "RUSTSEC-2026-0367") -> Finding:
    return Finding(
        rule=rule,
        severity="error",
        confidence="high",
        file="Cargo.lock",
        line=None,
        excerpt=rule,
        explanation="advisory reported by cargo-audit",
        recommendation="inspect the advisory",
    )


def test_advisories_reject_by_default() -> None:
    result = _result("audit", "FINDINGS", [_advisory()], "error")
    decision = decide([result], fail_on="findings", required_tool_ids=["audit"])
    assert decision.decision == "REJECTED"
    assert "audit:RUSTSEC-2026-0367" in decision.reason


def test_fail_on_advisories_false_keeps_the_finding_but_accepts() -> None:
    result = _result("audit", "FINDINGS", [_advisory()], "error")
    decision = decide(
        [result],
        fail_on="findings",
        required_tool_ids=["audit"],
        fail_on_advisories=False,
    )
    assert decision.decision == "ACCEPTED"
    assert "policy.fail_on_advisories=false" in decision.reason


def test_fail_on_advisories_false_does_not_excuse_other_errors() -> None:
    other = Finding(
        rule="missing-lockfile",
        severity="error",
        confidence="high",
        file="Cargo.lock",
        line=None,
        excerpt=None,
        explanation="policy requires a lockfile",
        recommendation="commit Cargo.lock",
    )
    result = _result("audit", "FINDINGS", [other], "error")
    decision = decide(
        [result],
        fail_on="findings",
        required_tool_ids=["audit"],
        fail_on_advisories=False,
    )
    assert decision.decision == "REJECTED"
    assert "missing-lockfile" in decision.reason


def test_the_advisory_policy_is_off_by_default_and_read_from_toml(tmp_path: Path) -> None:
    from veyra_proof.config import load_veyra_config

    assert PolicyConfig().fail_on_advisories is True
    (tmp_path / "veyra.toml").write_text("[policy]\nfail_on_advisories = false\n", encoding="utf-8")
    assert load_veyra_config(tmp_path).policy.fail_on_advisories is False


def test_markdown_lists_deliberate_exclusions() -> None:
    md = render_markdown(
        workspace="/tmp/w",
        timestamp="t",
        os_arch="Linux / x86_64",
        toolchain="stable",
        profile="quick",
        decision=decide(
            [_result("clippy", "OK")],
            fail_on="findings",
            required_tool_ids=["clippy"],
        ),
        results=[_result("clippy", "OK")],
        changes=[],
        excluded=[{"tool_id": "veyra-e2e", "source": "--exclude", "reason": None}],
    )
    assert "## Excluded" in md
    assert "veyra-e2e: removed by --exclude" in md
