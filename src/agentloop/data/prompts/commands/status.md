# /status — Progress dashboard

Read `.agentloop/state.yaml` (phase / gates / task status) and `.agentloop/plan.yaml` (the Expected Model and its task DAG), and concisely show the following as a Human-on-the-Loop monitoring view. **Do not change state (read-only).**

1. **Project / work branch** (`project`/`branch`), the **current phase**, and the command to run next.
2. **Gate status**: list requirements / design / tasks / build / release as `approved`/`pending`.
3. **Task progress**: run `agentloop dag --render` and show its deterministic output (counts, execution layers, critical path, executable frontier). Also list `blocked`/`needs-revision` tasks individually (they need human intervention). Skip if the plan has no tasks yet (before `/tasks`).
   - **Dependency graph**: run `agentloop dag --mermaid` and present its Mermaid (`graph TD`, status color-coding, critical-path bold border) as well. The whole picture renders directly in GitHub/VS Code/Markdown.
   - **Consistency trace**: only when the plan has tasks (after `/tasks`) and the requirements document (`docs/10-requirements.md`) exists, run `agentloop dag --trace` (skip if not generated, same as render). Highlight broken requirement → design → task linkage (uncovered requirements, dangling references). Distinguish the cause by exit code: **1=missing (include in "needs attention") / 2=cannot check (requirements document absent, 0 requirement IDs, no tasks in the plan → guide as a path/notation setup problem)**.
4. **Needs attention**: highlight gates awaiting human approval and open escalations in the event log (`agentloop events --render` lists them). If `done` is reached but `docs/retrospective.md` is unfilled or logs have open items (unresolved escalation events, blank adoption columns), prompt about them.
5. **Speculative work**: if there is provisional work done while waiting for approval (the speculative work log), show the ones whose adoption is undecided.
6. **(Only with GitHub integration)** If `github.enabled: true` in `.agentloop/config.yaml`, give a one-line note that `agentloop issue-sync` can bring Issues into line with this dashboard (`plan.yaml` tasks) (Issues are a one-way mirror, not the SSOT).

End with 1–2 lines on "what you (the human) should do now".

For a live browser view of the same board, `agentloop ui` serves a local read-only-by-default dashboard (phase/gates/tasks and the deterministically computed next command; safe operations and gate-approval recording can be run from it — `agentloop ui --read-only` disables those).
