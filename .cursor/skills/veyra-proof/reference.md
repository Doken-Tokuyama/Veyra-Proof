# Veyra reference

## Profiles

| Profile | Intent |
|---------|--------|
| `quick` | fmt, check, clippy `-D warnings`, test, audit (if lockfile), anti-theater |
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
