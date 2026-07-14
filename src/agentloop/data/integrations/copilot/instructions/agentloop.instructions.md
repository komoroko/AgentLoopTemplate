---
applyTo: "**"
---

# AgentLoop — VS Code GitHub Copilot capability mapping

This repo runs the AgentLoop Human-on-the-Loop lifecycle. The operating rules are in `AGENTS.md`
(read it first); the phase procedures are in `.agentloop/prompts/commands/*.md`. This file only
maps AGENTS.md's capability vocabulary onto VS Code Copilot mechanisms.

## Capability mapping (VS Code Copilot)

| Capability | VS Code Copilot mechanism |
|---|---|
| `phase-invocation` | prompt files `/req` `/design` `/tasks` `/build` `/verify` `/status` `/revise` `/onboard` (`.github/prompts/*.prompt.md`) |
| `structured-question` | ask numbered, multiple-choice options (with a recommended one) in chat, then end the turn and wait |
| `notify-and-wait` | end the turn with an explicit "waiting on gate N approval" summary (there is no push channel) |
| `approval-presentation` | present the summary in Plan mode or plain chat and ask for an explicit "approve" |
| `session-compaction` | the human starts a new chat; the next command rehydrates from the SSOT (`.agentloop/state.md`, `tasks.yaml`, `docs/**`) |
| `role-delegation` | custom agents `@requirements-analyst` / `@architect` / `@implementer` (`.github/agents/*.agent.md`); if delegation is unavailable, adopt the role inline per its file in `.agentloop/prompts/agents/` — parallel leaves degrade to serial |
| `autonomous-build-iteration` | re-invoke the `/build` prompt each iteration (no /loop equivalent); the lead re-enacts mode B by hand |
| `command-preauthorization` | VS Code's tool-approval settings (allow the AgentLoop `make`/`uv run … scripts/agentloop/*` commands) |

Notes:
- **Headless mode A (`make build-loop`) requires a headless agent CLI** (installed and authenticated) — the orchestrator launches the command in `build.headless.cmd` (default `claude -p`; `codex exec` / `gemini -p` also work), so Copilot may invoke it too when such a CLI is present. Without one, run the interactive mode B in `/build`.
- The gates' **mechanism layer** also runs under Copilot: `.github/hooks/agentloop.json` registers `scripts/agentloop/gate_guard.py` as a PreToolUse hook (VS Code agent hooks, preview), which denies edits to next-phase deliverables while the prerequisite gate is `pending`. VS Code may additionally parse `.claude/settings.json` and run the same guard twice — harmless (read-only, idempotent deny).
- The security review before gate ④ / at `/verify`: Copilot has no `/security-review` command — perform an equivalent security-focused review pass and record it the same way (`.agentloop/security-review.md` with the reviewed HEAD hash, and the test plan's security column).

## Role-agent tool mapping (single source)

| Claude Code tools | VS Code Copilot tools |
|---|---|
| Read, Grep, Glob | `search` |
| WebFetch | `fetch` |
| WebSearch | (no built-in equivalent — omitted; role degrades to fetch-only research) |
| Edit, Write | `edit` |
| Bash | `runCommands` |

VS Code tool identifiers are not versioned by this template — if an identifier is renamed
upstream, fix it here and in `.github/agents/*.agent.md` only (the shared role bodies never
name tools).
