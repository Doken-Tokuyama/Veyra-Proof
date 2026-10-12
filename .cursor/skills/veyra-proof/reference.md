# Veyra reference

## Profiles

| Profile | Intent |
|---------|--------|
| `quick` | fmt, check, clippy `-D warnings`, test, audit (if lockfile), anti-theater, veyra-e2e (if scenarios declared) |
| `standard` | quick + nextest, llvm-cov, deny, geiger, machete, dylint (if configured), semver-checks (libs), full anti-theater |
| `hardened` | standard + miri, mutants (configured paths), fuzz (configured targets), kani, vet, check-external-types, reproducible-build attempt |

Heavy tools SKIP with an explicit reason when config/targets are missing.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Required checks completed without blocking findings |
| 1 | Problems / findings / rejection |
| 2 | Missing prerequisites, unauthorized install, or required tools not executable |
| 3 | Internal application error |

## End-to-end behavior (`veyra-e2e`)

Builds the workspace with the real toolchain and runs the binary cargo produced through the journeys declared in `veyra.toml`, comparing exit code, stdout, stderr and written files with what was declared. It only proves what the scenarios cover.

```toml
[e2e]
build_profile = "release"     # or "debug"
features = []                 # feature flags of the shipped configuration
no_default_features = false
binaries = []                 # defaults to the workspace's first built bin target
timeout_seconds = 60

[[e2e.scenarios]]
name = "add sums two integers"
argv = ["add", "1", "2"]
expect_exit = 0
expect_stdout_regex = '^3\r?\n?$'
# stdin = ""
# expect_stderr_regex = 'division by zero'
# expect_files = ["out/report.txt"]
# expect_failure = true       # any non-zero exit; set expect_exit for an exact one
```

| Outcome | Meaning |
|---------|---------|
| `OK` | every scenario observed what it declared |
| `FINDINGS` | `E2E00` vacuous scenario, `E2E01` exit mismatch, `E2E02` output/file mismatch, `E2ECFG` invalid configuration, `E2E03` informational reachability note |
| `INCONCLUSIVE` | the build or a scenario timed out, or the workspace builds no binary |
| `SKIPPED` | no scenarios declared: behavior was **not** verified |
| `ERROR` | `cargo build` failed; nothing was executed |

Required in every profile exactly when scenarios are declared (and available to `--include`). `E2E03` never blocks a verdict.

## Evidence

Default: `PATH/.veyra/evidence/<UTC_TIMESTAMP>/` with `report.md`, `report.json`, `manifest.json`, per-tool logs, hashes.

## Safety defaults

| Action | Default |
|--------|---------|
| Modify `Cargo.toml` | Forbidden |
| `cargo update` | Needs `--update-lockfile` |
| Install tools | Needs `--install-missing` + confirm or `--yes` |
| `curl \| sh` | Never |

## Packaging note

PyInstaller binaries are OS/arch-specific. Build on the target host; unsigned macOS binaries may need Gatekeeper “Open anyway.”
