# scripts/

Where scripts live. Split by purpose so the **two kinds are not mixed**.

| Path | Purpose | Owner |
|------|------|------|
| `scripts/agentloop/` | The AgentLoop template's foundational tools (the deterministic orchestrator `build_loop.py` / DAG derivation `dag.py` / the gate hook `gate_guard.py` / the one-way Issues mirror `issue_sync.py` and their unit tests). Shipped with the template and referenced by `make build-loop`/`make test-tools`/`make issue-sync` and the hook in `.claude/settings.json`. | template |
| `scripts/` (directly under / other subfolders) | **Product-specific** scripts (data prep, operational helpers, etc.). Add freely per product. | product |

The contents under `scripts/agentloop/` are part of the template, so do not rewrite them for product reasons (tune behavior you want to change via `.agentloop/config.yaml`).

## Relation to the gate (`gate_guard.py`)

- A Write/Edit to `scripts/` (directly under / product) is treated as **implementation code** and is **denied** by the mechanism hook unless `gates.tasks` is approved (same as `backend/**`/`frontend/**`).
- `scripts/agentloop/` (foundational tools) is **always allowed regardless of gates** (so as not to block the hook's own maintenance / speculative work).
