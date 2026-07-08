# Retrospective

> `/verify` generates/updates this at gate ⑤ approval / reaching `done`. It recovers the process metacognition and
> leaves learning for the next cycle (next product / next iteration).
> It **closes the open items** of each log in `state.md` (escalation, speculative work).

## 1. Where rework originated
Classify needs-revision / blocked by origin. Upstream defects are material for improving the next requirements/design.

| Task/gate | Event | Class (upstream defect / implementation convenience / external factor) | Root cause | Countermeasure for next time |
|---------------|------|------------------------------------------|----------|------------|
| | | | | |

## 2. Recovering the escalation log
Conclude every open escalation in the event log: `make events ARGS=--render` lists them,
`make events ARGS='--resolve <ID> --note "…"'` closes each (summarize the conclusions here).

-

## 3. Adoption of speculative work
For each item in `state.md` "speculative work log", finalize adopt / discard (fill the "Adopt? (human)" column).

-

## 4. Lessons for upstream
When building something similar next time, what should have been firmed up first at the requirements/design stage.
Durable ones are promoted the same way as §5 (`upstream` included) — record where each landed.

| Lesson | Promote? | Promoted to (file) |
|--------|----------|--------------------|
| | | |

## 5. Process / template improvement proposals
Improvement ideas for how this loop is run, the gates, self-assessment, and deterministic orchestration
(feedback to the template itself is welcome too). **Before `cycle-close` archives this file, promote any keeper
into the always-loaded template (`CLAUDE.md` / `.claude/commands/*` / `.claude/agents/*`) and record where** —
a durable lesson must not stay only here. A proposal for the upstream template itself gets `Promote? = upstream`:
/verify drafts it into `.agentloop/feedback.yaml`, the human files it with `make feedback`, and the issue URL
goes in "Promoted to (file)".

| Proposal | Promote? | Promoted to (file) |
|----------|----------|--------------------|
| | | |

## 6. What went well (ways worth keeping)
-
