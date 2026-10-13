"""Conservative static anti-theater heuristics over Rust sources."""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from veyra_proof.models import DetectionResult, Finding, Severity
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso

SKIP_DIR_NAMES = {".git", "target", ".veyra", "node_modules", ".cargo"}

# Fuzzing harnesses are not unit tests. Their "body" is a corpus-driven loop
# whose oracle lives in the fuzzing engine, so "no assert!() here" is a property
# of how libfuzzer works, not a weak test.
FUZZ_PATH_MARKERS = ("/fuzz/", "/fuzz_targets/", "/fuzz_targets")


@dataclass
class RuleHit:
    rule: str
    severity: Severity
    confidence: str
    explanation: str
    recommendation: str


def _iter_rs_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.rs"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return files


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _in_critical(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    norm = path.replace("\\", "/")
    for pat in patterns:
        p = pat.replace("\\", "/").replace("**/", "").replace("**", "").rstrip("/")
        if p and p in norm:
            return True
        if pat.endswith("/**") and norm.startswith(pat[:-3]):
            return True
    return False


def _is_production_src(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    return "/src/" in f"/{norm}" or norm.startswith("src/")


def _is_test_path(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    return (
        norm.startswith("tests/")
        or "/tests/" in norm
        or norm.endswith("_test.rs")
        or "/test/" in norm
    )


def _is_fuzz_path(rel: str) -> bool:
    norm = f"/{rel.replace(chr(92), '/')}"
    return any(marker in norm for marker in FUZZ_PATH_MARKERS)


ASSERT_MACROS = ("assert!", "assert_eq!", "assert_ne!", "matches!", "pretty_assertions")

# Oracles that also make a test fail observably: a failed `expect`/`unwrap`
# panics, and a bare `panic!()` used as an oracle is deliberate.
TEST_ORACLE_MARKERS = ASSERT_MACROS + ("panic!(", ".expect(", ".unwrap(", "debug_assert")


def _test_module_lines(lines: list[str]) -> set[int]:
    """1-based line numbers that fall inside a `#[cfg(test)]` module.

    Production-path heuristics must not read test code as production: a
    `panic!()` used as a test oracle is not unfinished error handling. The
    attribute may sit on its own line or share the module's opening line.
    """
    inside: set[int] = set()
    armed_at = 0
    depth = 0
    start = 0
    for idx, line in enumerate(lines, start=1):
        code = line.split("//", 1)[0]
        if start == 0:
            if not armed_at:
                if "#[cfg(test)]" in code:
                    armed_at = idx
                continue
            if "{" not in code:
                # Attribute with no module behind it: it opens no test block.
                if idx > armed_at + 3:
                    armed_at = 0
                continue
            start = idx
        depth += code.count("{") - code.count("}")
        if depth <= 0:
            inside.update(range(start, idx + 1))
            armed_at = depth = start = 0
    if start:
        inside.update(range(start, len(lines) + 1))
    return inside


def _strip_line_comment(line: str) -> str:
    """Drop a trailing ``//`` comment, respecting string literals.

    A doc comment that *names* `todo!()` is not a call to it. Reading one as the
    other turns a module's own explanation of its placeholders into a blocker,
    which is the worst way for this rule to be wrong.
    """
    in_string = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string and char == "/" and line[index + 1 : index + 2] == "/":
            return line[:index]
        index += 1
    return line


def _code_lines(lines: list[str]) -> list[tuple[str, str]]:
    """(code, stripped code) per line, with comments and block comments removed."""
    out: list[tuple[str, str]] = []
    in_block = False
    for raw in lines:
        code = _strip_line_comment(raw) if not in_block else ""
        if not in_block and "/*" in code:
            before, _, after = code.partition("/*")
            if "*/" in after:
                code = before + after.split("*/", 1)[1]
            else:
                code = before
                in_block = True
        out.append((code, code.strip()))
    return out


def analyze_rust_sources(root: Path, critical_paths: list[str], policy) -> list[Finding]:
    findings: list[Finding] = []
    todo_sev: Severity = "blocker" if policy.fail_on_todo else "warning"
    unimpl_sev: Severity = "blocker" if policy.fail_on_unimplemented else "warning"
    ignore_sev: Severity = "error" if policy.fail_on_ignored_tests else "info"
    # `todo!()` and `unimplemented!()` are unambiguous: there is no reading of a
    # shipping function that calls them. `panic!` in an invariant, `#[allow]`
    # and `#[ignore]` are ordinary engineering choices that upstream projects
    # make daily, so they are reported for review and never block by default.
    # Each of these escalates to a blocking severity when the project says so.
    panic_sev: Severity = "warning" if policy.fail_on_panic else "info"
    allow_sev: Severity = "warning" if policy.fail_on_lint_allow else "info"
    weak_oracle_sev: Severity = "warning" if policy.fail_on_weak_oracles else "info"
    cfg_sev: Severity = "warning" if policy.fail_on_cfg_tricks else "info"

    for path in _iter_rs_files(root):
        rel = _rel(root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        test_lines = _test_module_lines(lines)
        # Every macro/attribute rule below reads the code, never the prose. A
        # module that documents why it has placeholders must not be reported as
        # a module that ships them.
        code_lines = _code_lines(lines)
        in_cfg_test = False
        cfg_test_depth = 0
        brace_balance = 0

        for idx, line in enumerate(lines, start=1):
            _raw, stripped = code_lines[idx - 1]

            if "#[cfg(test)]" in stripped:
                in_cfg_test = True
            if "#[cfg(not(test))]" in stripped and _is_production_src(rel):
                findings.append(
                    Finding(
                        rule="VT010",
                        severity=cfg_sev,
                        confidence="low",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="#[cfg(not(test))] splits production from test behaviour (may hide a bypass; informational; requires human review).",
                        recommendation="Ensure production and test code paths are intentional and reviewed.",
                    )
                )

            # `#[cfg(test)]` code never reaches a release binary, so a placeholder
            # there is a scratch note to the author, not a shipped stub. Reading it
            # as production is how a crate's own test scaffolding gets reported as
            # "fake code".
            in_test_module = idx in test_lines

            if "todo!()" in stripped or re.search(r"\btodo!\s*\(", stripped):
                if _is_production_src(rel) and not _is_test_path(rel) and not in_test_module:
                    findings.append(
                        Finding(
                            rule="VT001",
                            severity=todo_sev,
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="Reachable `todo!()` placeholder in a production path (suspicious placeholder; requires human review).",
                            recommendation="Replace todo!() with a real implementation or gate unfinished code out of release builds.",
                        )
                    )

            if "unimplemented!()" in stripped or re.search(r"\bunimplemented!\s*\(", stripped):
                if _is_production_src(rel) and not _is_test_path(rel) and not in_test_module:
                    findings.append(
                        Finding(
                            rule="VT002",
                            severity=unimpl_sev,
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="Reachable `unimplemented!()` in a production path (suspicious placeholder; requires human review).",
                            recommendation="Implement the function or remove it from production surfaces.",
                        )
                    )

            if (
                re.search(r"\bpanic!\s*\(", stripped)
                and _is_production_src(rel)
                and not _is_test_path(rel)
                and idx not in test_lines
            ):
                findings.append(
                    Finding(
                        rule="VT003",
                        severity=panic_sev,
                        confidence="low",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="`panic!()` in src/ may be an unfinished invariant or theatrical error handling (informational; requires human review).",
                        recommendation="Prefer Result-based error handling in production paths unless the panic documents a true invariant.",
                    )
                )

            if "#[ignore]" in stripped:
                findings.append(
                    Finding(
                        rule="VT004",
                        severity=ignore_sev,
                        confidence="medium",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="Test marked #[ignore] (test is not exercised by a default run; requires human review).",
                        recommendation="Un-ignore the test or document why it must remain ignored.",
                    )
                )

            if "assert!(true)" in stripped or "assert!( true )" in stripped:
                findings.append(
                    Finding(
                        rule="VT005",
                        severity="warning",
                        confidence="high",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="`assert!(true)` is a weak/no-op oracle (test weakness; requires human review).",
                        recommendation="Replace with assertions that observe meaningful behavior.",
                    )
                )

            if _in_critical(rel, critical_paths):
                if ".unwrap()" in stripped or ".expect(" in stripped:
                    findings.append(
                        Finding(
                            rule="VT007",
                            severity="warning",
                            confidence="medium",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="unwrap/expect in a configured critical path (possible bypass; requires human review).",
                            recommendation="Handle errors explicitly in critical modules.",
                        )
                    )

            if re.search(r"\bunsafe\b", stripped) and not stripped.startswith("//"):
                sev: Severity = "error" if policy.deny_unsafe else "info"
                if policy.deny_unsafe:
                    findings.append(
                        Finding(
                            rule="VT008",
                            severity=sev,
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="`unsafe` encountered and policy.deny_unsafe=true.",
                            recommendation="Justify and minimize unsafe, or adjust policy with documented approval.",
                        )
                    )
                elif _is_production_src(rel):
                    findings.append(
                        Finding(
                            rule="VT008",
                            severity="info",
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="`unsafe` block/function observed (informational; requires human review).",
                            recommendation="Ensure unsafe is documented and audited.",
                        )
                    )

            if 'extern "C"' in stripped or "extern \"C\"" in stripped:
                if policy.deny_ffi:
                    findings.append(
                        Finding(
                            rule="VT009",
                            severity="error",
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation='extern "C" FFI boundary observed and policy.deny_ffi=true.',
                            recommendation="Isolate FFI and review safety contracts.",
                        )
                    )

            if "std::process::Command" in stripped or "process::Command" in stripped:
                if policy.deny_process:
                    findings.append(
                        Finding(
                            rule="VT011",
                            severity="error",
                            confidence="high",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="process::Command observed and policy.deny_process=true.",
                            recommendation="Avoid process spawning in constrained modules or adjust policy.",
                        )
                    )

            if policy.deny_network_in_pure_modules and _in_critical(rel, critical_paths):
                if any(tok in stripped for tok in ("TcpStream", "UdpSocket", "reqwest::", "hyper::", "tokio::net")):
                    findings.append(
                        Finding(
                            rule="VT012",
                            severity="error",
                            confidence="medium",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="Network API in a critical/pure module path (possible policy violation; requires human review).",
                            recommendation="Move network I/O out of pure/critical modules.",
                        )
                    )

            if re.search(r"#\[allow\((dead_code|unused|clippy::all|clippy::correctness)\)\]", stripped):
                findings.append(
                    Finding(
                        rule="VT013",
                        severity=allow_sev,
                        confidence="low",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="Lint allow attribute near code (may hide a real defect; informational; requires human review).",
                        recommendation="Keep broad allow attributes narrow and justified.",
                    )
                )

            # trivial public constant return — low/medium confidence warning only.
            # Only the function's own body counts: a guard like
            # `if n == 0 { return 0; }` is edge-case logic, not a placeholder.
            if re.match(r"^\s*pub\s+fn\s+\w+", stripped):
                # The window includes the signature itself and needs no line
                # after it, so a `pub fn` on the file's last line is still read.
                window = "\n".join(code for code, _ in code_lines[idx - 1 : min(idx + 6, len(code_lines))])
                if re.search(
                    r"pub\s+fn\s+\w+[^{]*\{\s*(?:return\s+)?\d+\s*;?\s*\}", window
                ):
                    findings.append(
                        Finding(
                            rule="VT014",
                            severity=cfg_sev,
                            confidence="low",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="Public function appears to return a trivial constant (suspicious placeholder; informational; not proof of fake code; requires human review).",
                            recommendation="Confirm the constant is intentional domain logic.",
                        )
                    )

        # Test functions without assertions
        if (_is_test_path(rel) or "#[cfg(test)]" in text) and not _is_fuzz_path(rel):
            for m in re.finditer(r"#\[test\]\s*(?:async\s+)?fn\s+(\w+)\s*\([^)]*\)\s*\{", text):
                # crude body extract until next fn or end — limited scan
                start = m.end()
                depth = 1
                i = start
                while i < len(text) and depth:
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                    i += 1
                body = text[start:i]
                preceding = text[max(0, m.start() - 120) : m.start()]
                # An ignored test asserts nothing on purpose; the harness chose
                # not to run it. That is VT004's business, not VT006's.
                if "#[ignore]" in preceding:
                    continue
                if not any(a in body for a in TEST_ORACLE_MARKERS) and "should_panic" not in preceding:
                    line_no = text[: m.start()].count("\n") + 1
                    findings.append(
                        Finding(
                            rule="VT006",
                            severity=weak_oracle_sev,
                            confidence="low",
                            file=rel,
                            line=line_no,
                            excerpt=f"fn {m.group(1)}",
                            explanation="Test has no observable assertion (may be a weak oracle; informational; requires human review).",
                            recommendation="Add assertions that validate behavior, not only that code runs.",
                        )
                    )

        # #[cfg(test)] public impl theatrical split — look for cfg(test) pub fn near cfg(not(test))
        if "#[cfg(test)]" in text and "#[cfg(not(test))]" in text and _is_production_src(rel):
            findings.append(
                    Finding(
                        rule="VT015",
                        severity=cfg_sev,
                        confidence="low",
                        file=rel,
                        line=None,
                        excerpt="#[cfg(test)] / #[cfg(not(test))]",
                        explanation="File splits behavior between test and non-test cfg (informational; not proof of a theatrical implementation; requires human review).",
                        recommendation="Ensure non-test builds expose real logic, not panic/todo stubs.",
                    )
                )

    # Downgrade info findings out of Problems by filtering in reporting; keep them in JSON
    return findings


class AntiTheaterTool(Tool):
    id = "veyra-anti-theater"
    display_name = "veyra-anti-theater"
    homepage = ""
    license = "MIT"
    command = "veyra-anti-theater"
    version_command = []
    install_method = "none"
    default_profiles = ["quick", "standard", "hardened"]
    prerequisites = []
    open_source = True

    def detect(self, context: AuditContext) -> DetectionResult:
        return DetectionResult(
            self.id,
            "INSTALLED",
            True,
            version="builtin",
            message="Built-in conservative static analysis",
        )

    def install_plan(self, context: AuditContext):
        return None


    def planned_commands(self, context: AuditContext):
        return [["builtin:veyra-anti-theater", "scan", str(context.root)]]

    def run(self, context: AuditContext):
        started = utc_now_iso()
        clock = time.perf_counter()
        findings = analyze_rust_sources(
            context.root,
            context.config.project.critical_paths,
            context.config.policy,
        )
        # Drop pure info from blocker decision path but keep in result
        visible = [f for f in findings if f.severity != "info"]
        counts = Counter(f.rule for f in findings)
        histogram = ", ".join(f"{rule}={n}" for rule, n in sorted(counts.items()))
        out, err = self.write_logs(
            context,
            "\n".join(
                [
                    f"rule histogram: {histogram or 'none'}",
                    "",
                ]
                + [
                    f"{f.rule} {f.severity} {f.file}:{f.line} {f.explanation}"
                    for f in findings
                ]
            ),
            "",
        )
        if visible:
            max_sev = max(
                (f.severity for f in visible),
                key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
            )
            status = "FINDINGS"
            blocking = sorted({f.rule for f in visible})
            summary = (
                f"{len(visible)} finding(s) above informational level "
                f"({', '.join(blocking)}) out of {len(findings)} total; "
                "not definitive proof of fake code"
            )
            severity = max_sev
        else:
            status = "OK"
            summary = (
                f"No anti-theater heuristic findings above informational level "
                f"({len(findings)} informational: {histogram or 'none'})"
            )
            severity = "info"
        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=["builtin:veyra-anti-theater", "scan", str(context.root)],
            exit_code=0,
            duration_seconds=time.perf_counter() - clock,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
            environment={"rule_histogram": histogram},
        )
        self.save_result_json(context, tr)
        return tr
