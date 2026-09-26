---
name: veyra-proof
description: >-
  Audits a Rust Cargo workspace by actually running the real toolchain (fmt,
  clippy, test, llvm-cov, cargo-deny, cargo-geiger, machete, semver-checks,
  miri, cargo-fuzz, kani, mutants) plus anti-theater heuristics, then writes
  reproducible evidence. Use when the user asks to audit, scan, or check a
  Rust/Cargo workspace, run veyra doctor/plan/audit, wire it into a repo, or
  wants a CI-grade pass/fail verdict. Reports MISSING or SKIPPED instead of a
  green checkmark it cannot justify.
---

# Veyra Proof

Evidence-based auditor for local Rust Cargo workspaces. Results come only from tools that actually ran.

This folder is the **canonical copy**. It follows the open `SKILL.md` standard, so the same file works in Cursor, Claude Code, Codex, Antigravity, Freebuff, OpenCode, Copilot, and Windsurf. Maintainers: see the repository README for the fan-out command.

<!-- HARD_RULES_START -->
## Hard rules

The block between `HARD_RULES_START` and `HARD_RULES_END` is frozen. No automated mutation — skill optimizer, `mcp_mutate_skill`, self-revision, or linter — may edit, reorder, reword, or drop anything inside it. Only a human may change it, deliberately, in a reviewable diff.

- Never invent tool output, pass/fail, versions, or evidence.
- Never `curl | sh`, never arbitrary download installers, never disable lints or tests to pass.
- Never mutate `Cargo.toml` (V1 has no automatic manifest edits).
- Never run `cargo update` unless the user explicitly asked for `--update-lockfile`.
- `--install-missing` only when the user confirmed installs **or** passed `--yes`.
- If Cargo or rustup is missing, stop and report; do not install Rust toolchains via scripts.
- Quote real exit codes and paths from the run; say SKIPPED or MISSING when that is what happened.
- Never report a tool as OK when it was not run, not installed, or timed out.
<!-- HARD_RULES_END -->

## Resolve the CLI (`VEYRA`)

Pick the first that works:

1. `veyra` on PATH — check with `command -v veyra`
2. Env `VEYRA_PROOF_BIN` — absolute path to the binary
3. Env `VEYRA_PROOF_HOME` — then `"$VEYRA_PROOF_HOME/.venv/bin/python" -m veyra_proof` (Windows: `.venv\Scripts\python`)
4. A clone in the current repo — `.venv/bin/python -m veyra_proof` (Windows: `.venv\Scripts\python`), after confirming `.venv` exists
5. `python3 -m veyra_proof` if the package is importable

Set the shell variable `VEYRA` to the winning invocation. Examples: `VEYRA="veyra"`, `VEYRA=".venv/bin/python -m veyra_proof"`.

Never hardcode a machine-specific absolute path. If none of the five resolve, ask the user for the clone path or binary, then export one of the two env vars.

**Done when:** `$VEYRA --version` prints a version, or the install branch is required.

## Branch: install

When the user wants Veyra available (any project, this machine):

1. If resolution already works, run `$VEYRA doctor` and stop. Print cargo and rustc status honestly.
2. Otherwise ask once for the source: **editable clone path** or **existing PyInstaller binary**.
3. Editable install (preferred for agents):
   ```bash
   cd "$CLONE" && python3 -m venv .venv
   .venv/bin/pip install -e .
   export VEYRA_PROOF_HOME="$CLONE"
   ```
4. Binary: copy or symlink `veyra` onto PATH, or set `VEYRA_PROOF_BIN`.
5. Re-resolve `VEYRA`, run `$VEYRA doctor`. If cargo is missing, give guidance only — no rustup bootstrap scripts.

**Done when:** `$VEYRA doctor` returns JSON and cargo is OK, or you reported the exact blocker.

## Branch: audit

Target is a path containing `Cargo.toml` (workspace or package). Default profile is `standard` unless the user asked for `quick` or `hardened`.

1. Resolve `VEYRA` (use the install branch if missing).
2. `$VEYRA plan "$TARGET" --profile <profile>`
3. If the plan shows pending installs:
   - The user already said yes, or `--yes` was passed — add `--install-missing --yes`, plus `--verbose` and a higher `--timeout` when installs will compile.
   - Otherwise list the pending tools and **ask** before installing.
4. Run:
   ```bash
   $VEYRA audit "$TARGET" --profile <profile> [flags]
   ```
5. Report from real output only: the decision, the exit code, and the evidence dir under `$TARGET/.veyra/evidence/<UTC>/` (or wherever `--evidence-dir` pointed).

**Done when:** the audit finished (or dry-run / plan-only if that is what was asked) and the user sees the real verdict plus the evidence path.

Useful flags, profiles, and exit codes live in [reference.md](reference.md).

## Branch: wire

Lightweight project wiring, with no `Cargo.toml` edits:

1. Confirm the target is a Cargo root.
2. Only if the user asked for config, write a minimal `veyra.toml`. Do not invent fuzz targets, CodeQL databases, or mutants paths.
3. Optionally add a short note to the project README: how to run `$VEYRA doctor` and `$VEYRA audit . --profile quick`.
4. Do **not** auto-commit. Do **not** add CI secrets or remote download steps.

Minimal optional `veyra.toml` skeleton — edit only what the user wants:

```toml
[project]
critical_paths = []

[policy]
# leave defaults unless the user specifies policy knobs
```

**Done when:** the requested files exist and the commands to run are stated, with no "wired and passing" claim that no audit run supports.

## Honesty checklist

- [ ] Every claimed status came from a real command
- [ ] Missing tools are reported as MISSING or SKIPPED, never OK
- [ ] Installs happened only with confirmation or `--yes`
- [ ] The evidence path is cited whenever an audit ran
