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

**Built** — the dirty team, end to end: ingest → index → plan → analyse → specification.
Four analysis stages, each planned by an agent and scored by a separate judge. Every
artifact is schema-validated before storage, and crossing artifacts are scanned for leaks.

**Not built** — Border (leak findings are recorded as advisory `BORDER-REVIEW` notes, not
enforced) and the clean team.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                               # add your Gemini API key
python main.py https://github.com/owner/project
```

Output lands in `artifacts/<repo>-<hash>/` — `specification.md` plus every intermediate
artifact. `--stub` runs the whole pipeline without calling a model or spending credits.

## Layout

```
main.py                    CLI entry point
config/                    agent → model mapping, model profiles
packages/agents/           planning (+ judge), dirt_team analysis agents, base agent
packages/modules/boundary/ the alias map, neutralisation, leak scanners
packages/modules/          ingesting, indexing, storing, supervising, skills
iatin_vault/               design notes — why things are the way they are
tests/                     pytest
```

Models are reached through **AgentProvider** (prompt → text), never a provider SDK
directly. Which agent uses which model lives in `config/agents_config.json`.
