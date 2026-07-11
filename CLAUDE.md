# AgentLoopTemplate — Claude Code capability mapping

The operating rules live in `AGENTS.md` (the canonical, agent-neutral rules file) — imported below.
This file only maps AGENTS.md's capability vocabulary onto Claude Code's mechanisms.
(If your Claude Code version reads `AGENTS.md` natively and you see the rules twice, drop the
import line — keep the mapping table.)

@AGENTS.md

## Capability mapping (Claude Code)

| Capability | Claude Code mechanism |
|---|---|
| `phase-invocation` | slash commands `/req` `/design` `/tasks` `/build` `/verify` `/status` `/revise` `/onboard` (`.claude/commands/*.md`) |
| `structured-question` | `AskUserQuestion` (batch up to ~4 questions, multiple-choice with a recommended option) |
| `notify-and-wait` | `PushNotification`, then end the turn |
| `approval-presentation` | plan mode + `ExitPlanMode`; outside plan mode, present the summary and ask for an explicit "approve" |
| `session-compaction` | `/compact` (human-run; the agent only suggests it) |
| `role-delegation` | subagents in `.claude/agents/` (`requirements-analyst`, `architect`, `implementer`); parallel leaves use `isolation: "worktree"` |
| `autonomous-build-iteration` | `/loop /build` (mode B) — and headless mode A, `make build-loop`, which drives `claude -p` (Claude Code only) |
| `command-preauthorization` | `permissions.allow` in `.claude/settings.json` |

Claude Code also carries the **mechanism layer** of the gates: the PreToolUse hook in
`.claude/settings.json` runs `scripts/agentloop/gate_guard.py` on every Write/Edit
(AGENTS.md "Gate rules"). The security review before gate ④ is `/security-review`.
