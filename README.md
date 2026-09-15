# IATIN

Reads a repository and writes a **behavioural specification** of it — what the code does,
never how the original wrote it. Another team can then rebuild the project from that spec
alone, having never seen the source.

## Three teams

| Team | Sees | Job |
|---|---|---|
| **Dirty** | the original repo | read it, produce the spec |
| **Border** | both sides | judge whether anything original leaked |
| **Clean** | the spec only | build from it |

The one rule everything else serves: **nothing from the original reaches the clean team.**
No names, paths, commands, or literal text — only behaviour. The dirty team keeps a private
original→neutral map (`Calculador` → `Component A`, `README.md:16` → `EV-205`) that never
crosses.

## Status

**Built** — Dirty (ingest → index → plan → analyse → specification), Border, and Clean.
Dirty annotates `BORDER-REVIEW` notes; Border re-scans, asks the configured
`border_gate` model to adjudicate soft findings, auto-fails hard leaks, and on
refusal asks Dirty to rewrite (`--border-max-repairs`, default 2) before exiting
non-zero. Intermediate fail/repair specs and `border_verdict.round-N.json` are kept.

A passing Border gate exports a verified two-file
`clean_handoff/`. Clean runs separately, receives only that handoff, generates bounded
files, performs non-executing structural checks, and requests whole-file repairs when
needed.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                               # add your Gemini API key
python main.py https://github.com/owner/project
python clean_main.py artifacts/<run>/clean_handoff --output clean_workspaces/<run>
```

Output lands in `artifacts/<repo>-<hash>/` — `specification.md`, `border_verdict.json`,
plus every intermediate artifact. A passing run also writes `clean_handoff/`. The Clean
command writes the reconstruction to `<output>/project/` and its plan/report to
`<output>/_clean/`. `--stub` on either command makes no model calls. `--skip-border`
keeps Dirty's advisory notes but creates no Clean handoff.

Clean planning and structural generation are language-neutral. Executable validation is
provided by opt-in adapters: each adapter declares its supported runtimes, capabilities,
and plan requirements before generation. The currently shipped adapter is local Python
import and pytest validation, enabled with `--execute-generated-code`; projects without a
matching adapter remain partial and are never reported as executable successes.

Only the planner receives the complete approved specification. Builder and repair calls
receive deterministic task-scoped requirement excerpts, symbol contracts, plan entries,
and generated dependency context. Clean records the included IDs and context byte counts
under `<output>/_clean/generation/` and repair iteration metadata.

Dirty/Border exit codes: `0` ok, `2` configuration, `3` Border refused after repairs.
Clean exit codes: `0` success, `2` invalid input/configuration, `4` failed build/checks.

`--border-max-repairs N` controls how many Dirty rewrites Border may request (default 2).

## Layout

```
main.py                    CLI entry point (Dirty → Border → verified handoff)
clean_main.py              isolated Clean CLI
config/                    agent → model mapping, model profiles
packages/agents/           planning, dirt_team, border_team, clean_team, base agent
packages/modules/boundary/ alias map, neutralisation, leak scanners
packages/modules/border/   enforcement gate and verdict
packages/modules/handoff/  approved two-file crossing boundary
packages/modules/clean/    safe workspace, validation, and orchestration
packages/modules/          ingesting, indexing, storing, supervising, skills
iatin_vault/               design notes — why things are the way they are
tests/                     pytest
```

Models are reached through **AgentProvider** (prompt → text), never a provider SDK
directly. Which agent uses which model lives in `config/agents_config.json`. Border's
gate uses scanners for hard leaks and the configured `border_gate` model to adjudicate
soft (DESCRIPTIVE / UNCERTAIN) findings.
