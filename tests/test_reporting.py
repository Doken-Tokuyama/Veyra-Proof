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


def _result(tool_id, status, findings=None, severity="info"):
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


def test_decision_accepted():
    results = [_result("clippy", "OK"), _result("rustfmt", "OK")]
    d = decide(results, fail_on="findings", required_tool_ids=["clippy", "rustfmt"])
    assert d.decision == "ACCEPTED"


def test_decision_intentional_skip_not_required():
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


def test_decision_rejected_on_findings():
    findings = [Finding("x", "error", "high", "a.rs", 1, None, "bad", "fix")]
    results = [_result("clippy", "FINDINGS", findings, "error")]
    d = decide(results, fail_on="findings", required_tool_ids=["clippy"])
    assert d.decision == "REJECTED"


def test_decision_inconclusive_missing():
    d = decide(
        [],
        fail_on="findings",
        required_tool_ids=["clippy"],
        missing_required=["clippy"],
    )
    assert d.decision == "INCONCLUSIVE"


def test_ok_never_for_timeout_classification():
    status, _, _ = classify_from_command(
        timed_out=True,
        exit_code=None,
        findings=[],
        stdout="",
        stderr="",
        error="timeout",
    )
    assert status == "INCONCLUSIVE"


def test_ok_never_for_nonzero_without_analysis():
    status, _, _ = classify_from_command(
        timed_out=False,
        exit_code=2,
        findings=[],
        stdout="error: boom",
        stderr="",
        error=None,
    )
    assert status != "OK"


def test_parse_clippy():
    text = "error[clippy::foo]: bad thing\n  --> src/lib.rs:10:5\n"
    findings = parse_clippy_output(text)
    assert findings
    assert findings[0].file == "src/lib.rs"
    assert findings[0].line == 10


def test_parse_deny_unlicensed():
    from veyra_proof.tools.deny import parse_deny_output

    text = (
        "warning[no-license-field]: license expression was not specified\n"
        "error[unlicensed]: demo_crate = 0.1.0 is unlicensed\n"
    )
    findings = parse_deny_output(text)
    assert any(f.rule == "deny:unlicensed" and f.severity == "error" for f in findings)
    assert any(f.rule == "deny:no-license-field" for f in findings)


def test_parse_test_failures():
    text = "test foo::bar ... FAILED\n"
    findings = parse_test_failures(text)
    assert findings[0].excerpt == "foo::bar"


def test_anti_theater_todo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn x() { todo!() }\n", encoding="utf-8")
    findings = analyze_rust_sources(tmp_path, [], PolicyConfig(fail_on_todo=True))
    assert any(f.rule == "VT001" for f in findings)


def test_markdown_structure():
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
