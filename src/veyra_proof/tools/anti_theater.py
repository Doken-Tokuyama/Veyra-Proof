"""Conservative static anti-theater heuristics over Rust sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from veyra_proof.models import DetectionResult, Finding, Severity
from veyra_proof.tools.base import AuditContext, Tool, utc_now_iso

SKIP_DIR_NAMES = {".git", "target", ".veyra", "node_modules", ".cargo"}


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


ASSERT_MACROS = ("assert!", "assert_eq!", "assert_ne!", "matches!", "pretty_assertions")


def analyze_rust_sources(root: Path, critical_paths: list[str], policy) -> list[Finding]:
    findings: list[Finding] = []
    todo_sev: Severity = "blocker" if policy.fail_on_todo else "warning"
    unimpl_sev: Severity = "blocker" if policy.fail_on_unimplemented else "warning"
    ignore_sev: Severity = "error" if policy.fail_on_ignored_tests else "warning"

    for path in _iter_rs_files(root):
        rel = _rel(root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        in_cfg_test = False
        cfg_test_depth = 0
        brace_balance = 0

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()

            if "#[cfg(test)]" in stripped:
                in_cfg_test = True
            if "#[cfg(not(test))]" in stripped and _is_production_src(rel):
                findings.append(
                    Finding(
                        rule="VT010",
                        severity="warning",
                        confidence="medium",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="#[cfg(not(test))] may alter critical production paths (possible bypass; requires human review).",
                        recommendation="Ensure production and test code paths are intentional and reviewed.",
                    )
                )

            if "todo!()" in stripped or re.search(r"\btodo!\s*\(", stripped):
                if _is_production_src(rel) and not _is_test_path(rel):
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
                if _is_production_src(rel) and not _is_test_path(rel):
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

            if re.search(r"\bpanic!\s*\(", stripped) and _is_production_src(rel) and not _is_test_path(rel):
                findings.append(
                    Finding(
                        rule="VT003",
                        severity="warning",
                        confidence="medium",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="`panic!()` in src/ may indicate unfinished or theatrical error handling (requires human review).",
                        recommendation="Prefer Result-based error handling in production paths.",
                    )
                )

            if "#[ignore]" in stripped:
                findings.append(
                    Finding(
                        rule="VT004",
                        severity=ignore_sev,
                        confidence="high",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="Test marked #[ignore] (test weakness / possible bypass; requires human review).",
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
                        severity="warning",
                        confidence="medium",
                        file=rel,
                        line=idx,
                        excerpt=stripped[:120],
                        explanation="Critical lint allow attribute near code (possible bypass; requires human review).",
                        recommendation="Avoid broad allow attributes around suspicious or unfinished code.",
                    )
                )

            # trivial public constant return — low/medium confidence warning only
            if re.match(r"^\s*pub\s+fn\s+\w+", stripped) and idx < len(lines):
                window = "\n".join(lines[idx - 1 : min(idx + 6, len(lines))])
                if re.search(r"\{\s*return\s+\d+\s*;\s*\}", window) or re.search(
                    r"pub\s+fn\s+\w+[^{]*\{\s*\d+\s*\}", window
                ):
                    findings.append(
                        Finding(
                            rule="VT014",
                            severity="warning",
                            confidence="low",
                            file=rel,
                            line=idx,
                            excerpt=stripped[:120],
                            explanation="Public function appears to return a trivial constant (suspicious placeholder; not proof of fake code; requires human review).",
                            recommendation="Confirm the constant is intentional domain logic.",
                        )
                    )

        # Test functions without assertions
        if _is_test_path(rel) or "#[cfg(test)]" in text:
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
                if not any(a in body for a in ASSERT_MACROS) and "should_panic" not in text[max(0, m.start() - 80) : m.start()]:
                    line_no = text[: m.start()].count("\n") + 1
                    findings.append(
                        Finding(
                            rule="VT006",
                            severity="warning",
                            confidence="medium",
                            file=rel,
                            line=line_no,
                            excerpt=f"fn {m.group(1)}",
                            explanation="Test has no observable assertion (weak test oracle; requires human review).",
                            recommendation="Add assertions that validate behavior, not only that code runs.",
                        )
                    )

        # #[cfg(test)] public impl theatrical split — look for cfg(test) pub fn near cfg(not(test))
        if "#[cfg(test)]" in text and "#[cfg(not(test))]" in text and _is_production_src(rel):
            findings.append(
                Finding(
                    rule="VT015",
                    severity="warning",
                    confidence="medium",
                    file=rel,
                    line=None,
                    excerpt="#[cfg(test)] / #[cfg(not(test))]",
                    explanation="File splits behavior between test and non-test cfg (possible theatrical implementation; requires human review).",
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
        findings = analyze_rust_sources(
            context.root,
            context.config.project.critical_paths,
            context.config.policy,
        )
        # Drop pure info from blocker decision path but keep in result
        visible = [f for f in findings if f.severity != "info"]
        out, err = self.write_logs(
            context,
            "\n".join(
                f"{f.rule} {f.severity} {f.file}:{f.line} {f.explanation}" for f in findings
            ),
            "",
        )
        if visible:
            max_sev = max(
                (f.severity for f in visible),
                key=lambda s: {"info": 0, "warning": 1, "error": 2, "blocker": 3}[s],
            )
            status = "FINDINGS"
            summary = f"{len(visible)} heuristic finding(s); not definitive proof of fake code"
            severity = max_sev
        else:
            status = "OK"
            summary = "No anti-theater heuristic findings above informational level"
            severity = "info"
        tr = self.make_result(
            status=status,
            severity=severity,
            summary=summary,
            started_at=started,
            command=["builtin:veyra-anti-theater", "scan", str(context.root)],
            exit_code=0,
            duration_seconds=0.0,
            stdout_path=out,
            stderr_path=err,
            findings=findings,
        )
        self.save_result_json(context, tr)
        return tr
