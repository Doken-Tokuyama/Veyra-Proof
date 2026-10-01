# Veyra Proof — Agent Skill

An evidence-based auditor for Rust Cargo workspaces, packaged as an
[Agent Skill](https://github.com/anthropics/skills). Ask your coding agent to
audit a Rust project and it runs real open-source tools, writes real evidence to
disk, and reports exactly what happened.

**It never invents results.** Every status in the report comes from a tool that
actually ran. A missing tool is reported as `MISSING` or `SKIPPED` with a
reason, never as `OK`.

## Why this exists

Most "AI code review" output is a plausible-sounding summary with no receipt.
Veyra is built the other way round: it shells out to the real toolchain, keeps
the logs, and hashes them into an evidence directory you can audit later. When
something was not checked, the report says so instead of implying success.

## Install

```bash
git clone https://github.com/ExyonQ/Veyra-Proof
cd Veyra-Proof/Veyra
python3 install.py
```

`install.py` uses only the Python standard library, needs Python 3.10+, and
detects every harness already present on your machine. It copies the skill into:

| Harness | `--harness` | Global | Project |
|---|---|---|---|
| Cursor | `cursor` | `~/.cursor/skills` | — |
| Claude Code | `claude` | `~/.claude/skills` | — |
| Codex | `codex` | `~/.codex/skills` | — |
| Antigravity | `antigravity` | `~/.gemini/config/skills` | `.agents/skills` |
| Freebuff | `freebuff` | `~/.config/manicode/.agents/skills` | — |
| OpenCode | `opencode` | `~/.config/opencode/skills` | `.opencode/skills` |
| GitHub Copilot | `copilot` | — | `.github/skills` |
| Windsurf | `windsurf` | — | `.windsurf/skills` |
| Cline | `cline` | `~/.clinerules/skills` | — |
| Goose | `goose` | `~/.goose/skills` | — |
| Continue | `continue` | — | `.continue/skills` |
| Trae | `trae` | — | `.trae/skills` |

Project-scoped rows are only written when that directory already exists in the
project you are standing in.

```bash
python3 install.py --list                 # what was detected, and where
python3 install.py --check                 # report drift, change nothing
python3 install.py --harness claude       # one harness only
python3 install.py --target ~/my/skills   # somewhere else entirely
python3 install.py --uninstall            # remove what it installed
```

Prefer to do it by hand? Copy the folder:

```bash
mkdir -p ~/.claude/skills
cp -R skills/veyra-proof ~/.claude/skills/
```

Restart your agent afterwards so it reloads the skill list.

## Install the auditor CLI

The skill drives the `veyra` CLI. The installer can set it up in the same pass:

```bash
python3 install.py --with-cli https://github.com/ExyonQ/Veyra-Proof
```

That clones the source to `~/.veyra`, creates a virtualenv, and prints the line
to add to your shell profile:

```bash
export VEYRA_PROOF_HOME="$HOME/.veyra"
```

Verify:

```bash
"$HOME/.veyra/.venv/bin/python" -m veyra_proof doctor
```

Prerequisites: **Rust and Cargo already installed.** Veyra will not install a
toolchain for you and will not run `curl | sh`. If `cargo` is missing, it stops
and tells you.

## Use it

Ask your agent in plain language:

- "Audit this Rust workspace"
- "Run veyra on this crate with the quick profile"
- "Install veyra and run a doctor check"
- "Wire veyra into this repo"

The skill resolves the CLI, plans the run, asks before installing anything, and
then reports the verdict with a path to the evidence directory:

```
target/.veyra/evidence/<UTC_TIMESTAMP>/
├── report.md          # human-readable
├── report.json        # machine-readable
├── manifest.json      # what ran, what was skipped and why
└── <tool>/            # per-tool logs and hashes
```

### Profiles

| Profile | Intent |
|---|---|
| `quick` | fmt, check, clippy `-D warnings`, test, `cargo audit`, anti-theater heuristics |
| `standard` | quick + nextest, coverage, `cargo deny`, `geiger`, `machete`, `semver-checks` |
| `hardened` | standard + miri, mutants, fuzz, kani, vet, reproducible-build attempt |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Required checks completed without blocking findings |
| 1 | Problems, findings, or rejection |
| 2 | Missing prerequisites, or unauthorized install |
| 3 | Internal application error |

## What it does not do

- It does not prove your code is correct or free of vulnerabilities.
- Anti-theater heuristics can produce false positives. They never declare "fake
  code confirmed".
- A fuzz run that finds no crashes is not a security proof.
- Miri and Kani cover a limited set of paths and properties.
- Tool availability varies by OS, architecture, toolchain, and project config.

## Safety defaults

| Action | Default |
|---|---|
| Modify `Cargo.toml` | **Forbidden** |
| Run `cargo update` | **Forbidden** (needs `--update-lockfile`) |
| Install tools | **Forbidden** (needs `--install-missing` plus confirmation) |
| `curl \| sh` or arbitrary downloads | **Never** |
| Disable lints or tests to pass | **Never** |

`--yes` authorizes compatible tool installs only. It never authorizes lockfile
or manifest edits.

## Repository layout

```
Veyra/
├── README.md
├── LICENSE                       # MIT
├── install.py                    # stdlib-only installer
├── skills/
│   └── veyra-proof/
│       ├── SKILL.md              # the skill (~1.4k tokens, loaded on trigger)
│       └── reference.md          # profiles, exit codes, evidence layout
└── examples/
    └── veyra.toml                # optional project config
```

`SKILL.md` is deliberately small. It is loaded into the agent's context every
time the skill triggers, so maintainer material lives in this README instead.

The safety rules in `SKILL.md` sit between `HARD_RULES_START` and
`HARD_RULES_END`. **Never edit inside that region.** Automated mutators — skill
optimizers, `mcp_mutate_skill`, self-revision — are forbidden from touching it,
because those rules are the reason the output is trustworthy.

## Contributing

1. Edit `skills/veyra-proof/SKILL.md`.
2. Keep it under ~1,600 tokens. Move lookup-only material to `reference.md`.
3. Keep it harness-neutral: no absolute machine paths, no vendor CLI names.
4. If you add a harness, add it to `install.py` **and** to the table above.

## Security

Report security problems to [security@exyonq.org](mailto:security@exyonq.org).

## License

MIT — Copyright (c) 2026 Antonio Cantallops Alba. See [LICENSE](LICENSE).
