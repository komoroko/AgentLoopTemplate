# scripts/

**Product-specific** scripts only (data prep, operational helpers, etc.). Add freely per product.

The AgentLoop harness no longer lives here: it is an **installed CLI** (`uv tool install
git+<this repo>@vX.Y.Z`, then `agentloop <verb>`) whose code ships in the `agentloop` package,
not as copied repo source. There is therefore no `scripts/agentloop/` foundational-tools
directory anymore — the orchestrator, DAG derivation, gate hook, Issues mirror, and lifecycle
verbs are all reached through `agentloop …`.

## Relation to the gate (`agentloop guard`)

- A Write/Edit under `scripts/` is treated as **implementation code** and is **denied** by the
  mechanism hook unless `gates.tasks` is approved (same as `backend/**` / `frontend/**`; see
  `guard_paths` in `.agentloop/config.yaml`).
- The old always-allowed carve-out for `scripts/agentloop/` is gone with the directory — the
  installed package is not repo source, so it needs no self-protection path.
