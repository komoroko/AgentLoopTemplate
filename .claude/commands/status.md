---
description: Progress dashboard. Show current phase, gate approval status, and task progress at a glance.
---

# /status — Progress dashboard

Read `.agentloop/state.md` (phase/gates/logs) and `.agentloop/tasks.yaml` (task graph), and concisely show the following as a Human-on-the-Loop monitoring view. **Do not change state (read-only).**

1. **Project / work branch** (`project`/`branch`), the **current phase**, and the command to run next.
2. **Gate status**: list requirements / design / tasks / build / release as `approved`/`pending`.
3. **Task progress**: run `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` and show its deterministic output (counts, execution layers, critical path, executable frontier). Also list `blocked`/`needs-revision` tasks individually (they need human intervention). Skip if tasks.yaml is not generated yet (before `/tasks`).
   - **Dependency graph**: run `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --mermaid` and present its Mermaid (`graph TD`, status color-coding, critical-path bold border) as well. The whole picture renders directly in GitHub/VS Code/Markdown.
   - **Consistency trace**: only when tasks.yaml is generated (after `/tasks`) and the requirements document (`docs/10-requirements.md`) exists, run `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --trace` (skip if not generated, same as render). Highlight broken requirement → design → task linkage (uncovered requirements, dangling references). Distinguish the cause by exit code: **1=missing (include in "needs attention") / 2=cannot check (requirements document absent, 0 requirement IDs, tasks.yaml absent → guide as a path/notation setup problem)**.
4. **Needs attention**: highlight gates awaiting human approval and unresolved items in the escalation log. If `done` is reached but `docs/retrospective.md` is unfilled or logs have open items (blank resolution/adoption columns), prompt about them.
5. **Speculative work**: if there is provisional work done while waiting for approval (the speculative work log), show the ones whose adoption is undecided.
6. **(Only with GitHub integration)** If `github.enabled: true` in `.agentloop/config.yaml`, give a one-line note that `make issue-sync` can bring Issues into line with this dashboard (tasks.yaml) (Issues are a one-way mirror, not the SSOT).

End with 1–2 lines on "what you (the human) should do now".
