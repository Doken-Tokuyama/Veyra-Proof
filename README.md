# Veyra Proof

[![CI](https://github.com/ExyonQ/Veyra-Proof/actions/workflows/ci.yml/badge.svg)](https://github.com/ExyonQ/Veyra-Proof/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A Rust audit that tells you what it can prove — and what it can't.**

An audit that says "all clear" without evidence is a story, not a result. Veyra Proof runs the real cargo toolchain on your machine, prints the real exit codes, and writes every log, hash and skip-reason into `.veyra/evidence/<UTC>/`.

It does **not** prove your code is correct or free of vulnerabilities. A fuzz run with no crashes is not a security proof. Miri and Kani cover limited paths. Faster to know that, than to find out from a customer.

If a tool is missing it says `MISSING`. If it timed out it says `SKIPPED`. `OK` is never assigned to a check that did not run.

## Honest limitations

- This does **not** prove total correctness or absence of vulnerabilities.
- Anti-theater heuristics can produce false positives; they never declare "fake code confirmed."
- A fuzz run without crashes is **not** a security proof.
- Miri/Kani cover limited paths and properties.
- Tool availability varies by OS, architecture, toolchain, and project configuration.

## Requirements

- Python 3.10+
- Rust/Cargo already installed (Veyra will **not** `curl | sh` install rustup)
- Optional: `rustup` for component installs (clippy, rustfmt, miri, llvm-tools)

## Install (local)

```bash
git clone https://github.com/ExyonQ/Veyra-Proof
cd Veyra-Proof
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

## Quick verification (demo fixture)

From the repo root:

```bash
cd Veyra-Proof
.venv/bin/python -m pytest
.venv/bin/python -m veyra_proof doctor
.venv/bin/python -m veyra_proof plan tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate --profile quick
```

## Usage

Use the **demo fixture** included in this repo, or point at a **real** Rust workspace (a path that contains `Cargo.toml`). Do not use placeholder paths like `/ruta/al/crate`.

### Doctor (no installs)

```bash
.venv/bin/python -m veyra_proof doctor
```

### List tools

```bash
.venv/bin/python -m veyra_proof list-tools
```

### Plan (no execution)

```bash
.venv/bin/python -m veyra_proof plan tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof plan /path/to/rust/workspace --profile standard --dry-run
```

### Audit examples

```bash
# Fixture de demo incluido en este proyecto
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate \
  --profile standard --install-missing

# O tu propio crate (donde haya un Cargo.toml)
.venv/bin/python -m veyra_proof audit /ruta/real/a/tu/proyecto-rust \
  --profile standard --install-missing

.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate --profile quick
.venv/bin/python -m veyra_proof audit . --profile hardened --yes --install-missing
.venv/bin/python -m veyra_proof audit . --update-lockfile
.venv/bin/python -m veyra_proof audit . --dry-run
```

`--install-missing` pedirá confirmación antes de instalar; añade `--yes` si quieres que lo haga sin preguntar.

Las instalaciones (`cargo install --locked`) compilan desde fuente y pueden tardar 10–40+ minutos. Sin `--verbose` verás solo un aviso de progreso; con `--verbose` el output de Cargo se muestra en vivo:

```bash
.venv/bin/python -m veyra_proof audit tests/fixtures/demo_crate \
  --profile standard --install-missing --verbose
```

Works the same on **Windows**, **macOS**, and **Linux** (use your Python launcher / `py -3` on Windows if needed).

## Profiles

| Profile | Intent |
|---------|--------|
| `quick` | fmt, check, clippy `-D warnings`, test, audit (if lockfile), anti-theater |
| `standard` | quick + nextest, llvm-cov, deny, geiger, machete, dylint (if configured), semver-checks (libs), full anti-theater |
| `hardened` | standard + miri, mutants (configured paths only), fuzz (configured targets), kani (harnesses), vet (existing config), check-external-types, reproducible-build attempt |

Heavy tools are **SKIPPED** with an explicit reason when configuration/targets are missing.

## Safety defaults

| Action | Default |
|--------|---------|
| Modify `Cargo.toml` | **Forbidden** |
| Run `cargo update` | **Forbidden** (needs `--update-lockfile`) |
| Install tools | **Forbidden** (needs `--install-missing`, plus confirmation or `--yes`) |
| `curl \| sh` / arbitrary downloads | **Never** |
| Disable lints/tests to pass | **Never** |

`--yes` authorizes compatible installs only — **not** lockfile or manifest edits.

`--allow-manifest-changes` records authorization for a future extension; **V1 performs no automatic `Cargo.toml` mutations**.

## Tool statuses

`INSTALLED`, `MISSING`, `INSTALLING`, `SKIPPED`, `ERROR`, `OK`, `FINDINGS`, `INCONCLUSIVE`

`OK` is never assigned for timeout, failed install, non-zero exit without analysis, or “not executed.”

## Exit codes (CI)

| Code | Meaning |
|------|---------|
| 0 | Required checks completed without blocking findings |
| 1 | Problems / findings / rejection |
| 2 | Missing prerequisites, unauthorized install, or required tools not executable |
| 3 | Internal application error |

## Evidence output

Default: `PATH/.veyra/evidence/<UTC_TIMESTAMP>/` containing `report.md`, `report.json`, `manifest.json`, per-tool logs, and hashes.

## Optional `veyra.toml`

Not created automatically. See the project specification for the full schema (`critical_paths`, policy flags, fuzz targets, mutants paths, etc.).

## Agent Skill

Canonical skill (model-invoked): `.cursor/skills/veyra-proof/`. It follows the open `SKILL.md` standard, so the same two files work across harnesses.

Fan it out to every harness installed on this machine:

```bash
python3 scripts/sync_skill.py                # write missing or stale copies
python3 scripts/sync_skill.py --check        # report drift, change nothing (exit 1 if stale)
python3 scripts/sync_skill.py --project      # also create project-scoped directories
python3 scripts/sync_skill.py --prune        # report copies left by an older name (dry run)
python3 scripts/sync_skill.py --prune --apply  # ...and delete them
```

Renaming the skill would otherwise leave a live copy behind in every harness, still declaring the old name and still triggering — `--check` stays green throughout, because it only inspects the name in use. Every copy the script writes is stamped with a `.veyra-skill-sync` marker whose filename never tracks the skill name, so the stamp survives the rename that made the copy stale; `--prune` deletes only directories carrying it. A harness skills directory is shared with every other skill you have installed, so anything without the stamp is left alone. Run `--prune` after a rename.

| Harness | Global | Project |
|---|---|---|
| Cursor | `~/.cursor/skills` | `.cursor/skills` |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| Codex | `~/.codex/skills` | — |
| Antigravity | `~/.gemini/config/skills` | `.agents/skills` |
| Freebuff | `~/.config/manicode/.agents/skills` | — |
| OpenCode | `~/.config/opencode/skills` | `.opencode/skills` |
| Cline | `~/.clinerules/skills` | — |
| Goose | `~/.goose/skills` | — |
| GitHub Copilot | — | `.github/skills` |
| Windsurf | — | `.windsurf/skills` |
| Continue | — | `.continue/skills` |
| Trae | — | `.trae/skills` |

A harness counts as installed when its home directory exists; `--all` overrides that. Writes are atomic (temp file plus rename), compared by SHA-256 rather than mtime, and logged to `~/.local/share/veyra-skill-sync.log`.

`Veyra/` is a standalone, publishable copy of this skill for public distribution. It carries its own `install.py` (standard library only, no `--all` needed) and is kept byte-identical to the canonical copy by `tests/test_skill_document.py`.

The safety rules in `SKILL.md` sit inside a `HARD_RULES` region that no automated mutation may edit. `tests/test_skill_document.py` enforces that, plus harness neutrality (no machine-local paths) and the token budget. Run it with the rest of the suite:

```bash
.venv/bin/python -m pytest
```

Triggers include: install veyra, auditar Rust, audit a Cargo workspace, wire veyra. Optional env: `VEYRA_PROOF_HOME`, `VEYRA_PROOF_BIN`.

### Maintaining the skill

`SKILL.md` is the only file loaded into the agent's context when the skill triggers, so keep it lean. Keep maintainer-facing material (portability tables, sync commands, editing policy) here in the README, not in the skill.

- Never edit inside `HARD_RULES_START` / `HARD_RULES_END`.
- Prefer one bounded add, delete, or replace over a wholesale rewrite.
- Keep `SKILL.md` under ~1,600 tokens; move lookup-only material to `reference.md`.
- Stay harness-neutral: no absolute machine paths, no vendor CLI names, no editor-only commands.
- If you adopt [SkillOpt](https://github.com/microsoft/SkillOpt), move the hard rules into its `SLOW_UPDATE` region, which normal edit patches cannot modify.
- After any edit: `python3 -m pytest` then `python3 scripts/sync_skill.py`.
- After renaming the skill or the folder: `python3 scripts/sync_skill.py` then `python3 scripts/sync_skill.py --prune --apply`.
- Before tagging a release: `python3 -m build && twine check dist/*`, and `python3 scripts/ci_assert_evidence.py tests/fixtures/demo_crate` to confirm the auditor still produces evidence that matches its own verdict.

## Packaging with PyInstaller

Build **on each target OS/arch** (no universal cross-compile promise):

```bash
python -m pip install pyinstaller
pyinstaller --onefile --name veyra -m veyra_proof
# or:
pyinstaller --onefile --name veyra src/veyra_proof/__main__.py
```

| Target | Build host |
|--------|------------|
| Windows x86_64 | Windows x86_64 |
| macOS Apple Silicon | macOS arm64 |
| macOS x86_64 | macOS x86_64 (or CI runner) |
| Linux x86_64 | Linux x86_64 |

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it proves |
|---|---|
| `test` | The 76-test suite on ubuntu, macos and windows across Python 3.10 and 3.13, plus `veyra --version` from the installed console script |
| `audit` | A real audit of the demo crate on a real Rust toolchain, followed by a check that the evidence on disk backs the verdict |

The `test` suite mocks `subprocess.run`, so on its own it cannot catch a broken tool invocation. The `audit` job closes that gap: it runs `scripts/ci_assert_evidence.py`, which executes the auditor for real and then requires that every tool reported `OK` has a log on disk *and* a per-tool `result.json` agreeing with the summary on both status and exit code. Two independently written artifacts must tell the same story.

That check has its own tests (`tests/test_ci_assert_evidence.py`), which inject each fault it is meant to catch. A verification script that cannot fail is worse than none: it manufactures the false confidence this project exists to refuse.

## Publishing to PyPI

`.github/workflows/publish.yml` builds the sdist and wheel and uploads them on a published release, using [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). **No API token is stored in the repository and there is nothing to rotate.**

One-time setup on PyPI, for the project `veyra-proof`:

1. Create the project at <https://pypi.org/manage/account/publishing/> and add a pending publisher: owner `ExyonQ`, repository `Veyra-Proof`, workflow `publish.yml`.
2. Create a `pypi` environment on the repository. The publish job declares it, which is what gates a release to someone who can approve it.

The sdist carries the agent skill, the fan-out script, the CI workflows and the test suite, not just the importable package — half of what this project ships is the skill. `.cursor` is listed file by file in `MANIFEST.in` rather than by wildcard, so a stray editor or agent state directory in a working copy can never ride along.

Check a build locally before tagging:

```bash
python -m pip install --upgrade build twine
python -m build
twine check dist/*
```

## Tests

```bash
cd Veyra-Proof
.venv/bin/python -m pytest
```

Tests mock `subprocess.run` and do not install Rust tools or mutate real projects.

## Contact

[contact@exyonq.org](mailto:contact@exyonq.org)

## Security

Report security problems to [security@exyonq.org](mailto:security@exyonq.org). See [SECURITY.md](SECURITY.md).

## License

MIT — Copyright (c) 2026 Antonio Cantallops Alba. See `LICENSE`.
