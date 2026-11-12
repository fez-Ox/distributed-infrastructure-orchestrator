# Multi-Agent Orchestration

A self-hosted control plane that turns a natural-language infrastructure request into a reviewed, audited Ansible change across a Proxmox cluster.

The LLM plans. Deterministic code decides what is allowed, renders the change from a fixed template, validates it, and waits for a human before anything runs.

> **Status:** research MVP, built during a 2026 ML internship at CENTech (NESCOM). Single-cluster, no production incident data. Addresses and node IDs in this repository are placeholders.

## Architecture: Brain vs. Hands

```
 natural-language request
          │
          ▼
 ┌──────────────────┐
 │  Planner (LLM)   │   structured intent only: action, target, params
 └────────┬─────────┘
          ▼
 ┌──────────────────┐
 │  Policy engine   │   allowed actions, parameter shapes, known targets
 └────────┬─────────┘
          ▼
 ┌──────────────────┐
 │    Preflight     │   node reachability, capabilities, resource bounds
 └────────┬─────────┘
          ▼
 ┌──────────────────┐
 │ Renderer         │   fixed Jinja2 templates — no model-authored YAML
 └────────┬─────────┘
          ▼
 ┌──────────────────┐
 │ Syntax + guard-  │   ansible --syntax-check, module allowlist,
 │ rail validation  │   parameter patterns, banned paths and users
 └────────┬─────────┘
          ▼
 ┌──────────────────┐
 │ Human approval   │   interactive y/N before execution
 └────────┬─────────┘
          ▼
   Ansible over SSH → managed nodes
```

Telemetry and state are separate from the control path: NATS carries heartbeats and events, SQLite stores inventory, task state, and step history.

## Safety model

The default **template path** has five independent layers:

1. **Policy** (`orchestrator/policy.py`) — rejects unknown actions, targets, and malformed parameters before anything is rendered.
2. **Preflight** (`orchestrator/preflight.py`) — checks that the node is reachable and can accept the action.
3. **Deterministic render** (`orchestrator/primitives.py`, `orchestrator/renderer.py`) — the model never writes playbook YAML; it selects an action whose playbook is a reviewed template.
4. **Syntax and guardrail validation** (`orchestrator/playbook_validator.py`) — `ansible-playbook --syntax-check` plus an allowlist of modules, parameter patterns, and banned paths and usernames.
5. **Human approval** — the rendered playbook is shown and requires an explicit `y` before execution.

Two honest caveats:

- **Freeform mode** exists for sandbox experimentation. It bypasses policy validation and template rendering, and still requires a syntax check and interactive approval. It is a deliberate escape hatch, not a second safe path.
- **`orchestrator/verifier.py`** contains a second-LLM consistency check with its own tests, but it is not wired into the default graph in this checkout. Do not count it as a shipped layer.

This is policy enforcement, not formal verification. A wrong-but-allowed change is still possible.

## Repository layout

| Path | Contents |
|---|---|
| `orchestrator/` | planner graph, policy, preflight, renderer, validator, executor, snapshots, state |
| `node_agent/` | child agent, child action catalog, NATS telemetry publisher |
| `ansible/` | inventory, templates, and playbooks |
| `tests/` | unit, integration, and end-to-end coverage |
| `config/nodes.yaml` | illustrative node inventory — replace with your own |
| `plans/` | example plans |

## Quickstart

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Ansible, and a NATS server.

```bash
uv sync
cp .env.example .env          # set your provider key and NATS URL
cp ansible/inventory.example.ini ansible/inventory.ini   # edit with your hosts
make setup-vm1                # local NATS, state DB, service bootstrap
make smoke-test
```

`ansible/inventory.ini` is gitignored because it holds real addresses. If it is absent, the code falls back to `ansible/inventory.example.ini`, whose RFC 5737 addresses are placeholders and will not resolve.

Run a request non-interactively:

```bash
uv run python -m orchestrator
```

## Tests

```bash
uv run pytest -q
```

The suite covers policy decisions, preflight, primitive rendering and validation, snapshot and rollback behavior, child-agent protocol, and multi-step workflows.

## Configuration

All configuration is environment-based; see `.env.example`. The important ones:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` | planner model, routed through LiteLLM |
| `NATS_URL` | local broker for orchestrator services |
| `TELEMETRY_NATS_URL` | broker address reachable from managed nodes |
| `DATABASE_PATH` | SQLite state |
| `ANSIBLE_INVENTORY` | inventory file |
| `CHILD_AUTH_SECRET` | shared secret between orchestrator and child agents |

Secrets belong in `.env`, an Ansible vault, or the environment. Nothing in this repository is a real credential.

## Limitations

- Single-cluster MVP; no multi-tenant isolation.
- The verifier is designed and tested but not on the default path.
- Policy checks are not formal verification.
- No production incident data; the demo is illustrative.
- Freeform mode trades safety for experimentation by design.

## License

MIT. See `LICENSE`.
