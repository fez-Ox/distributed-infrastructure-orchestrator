# Option B Distributed Agent Implementation Plan

## Context

Option B is the eventual distributed-agent evolution: VM1 remains the central policy, approval, and global scheduling authority while managed nodes run local child agents.

Initial repository audit shows that Option B is **not absent**. Partial implementation already exists:

- `node_agent/child_agent.py`
- `node_agent/child_catalog.py`
- `orchestrator/child_bridge.py`
- child-agent fields in `orchestrator/db.py`
- child-agent actions and routing in `orchestrator/models.py`, `orchestrator/policy.py`, and `orchestrator/nodes.py`
- `/agents`, `/capabilities`, and `/delegate` CLI paths in `orchestrator/agent.py`
- tests including `tests/test_child_agent.py` and `tests/test_child_catalog.py`

Therefore this plan must first audit and harden the existing Option B slice rather than assume a greenfield implementation.

## Approach

1. Establish an explicit Option B contract and threat model.
2. Audit existing child-agent code, bridge behavior, schemas, authentication, and deployment coverage.
3. Make all child-agent communication schema-validated, authenticated, correlated, idempotent, bounded, and observable.
4. Keep LLMs out of direct child execution: LLMs may propose typed intents only; deterministic policy and child catalogs decide what can run.
5. Introduce Option B in stages: read-only reporting, workload-readiness negotiation, then carefully bounded remediation and workload actions.
6. Permit mutation only through versioned, cataloged, schema-validated primitives with explicit policy, resource, authorization, audit, and recovery controls; never through arbitrary LLM-generated code.
7. Preserve Ansible as bootstrap, upgrade, recovery, and deterministic fallback.

## Current Scope Audit

The existing code is a partial prototype, not a complete Option B implementation:

- `node_agent/child_agent.py` exists and subscribes to task requests, publishes heartbeat/capabilities/status/results, and runs a local LangGraph graph.
- `node_agent/child_catalog.py` currently permits only inspection actions, despite the desired direction allowing contained local action.
- `orchestrator/child_bridge.py` receives child events and sends requests, but currently uses in-memory result waiting and does not provide durable request correlation/state transitions.
- `orchestrator/child_token.py` uses a shared HMAC secret for task authorization; this is separate from NATS broker authentication and must not be treated as a replacement for NATS credentials/permissions.
- `ansible/playbooks/install_child_agent.yml` and systemd/OpenRC templates exist, but deployment must be audited for dependency size, secret handling, idempotency, and generic init-system behavior.
- Existing child tests primarily validate local policy/rendering/token behavior; they do not yet prove a live multi-node protocol, replay resistance, broker authorization, or recovery behavior.
- The current child implementation embeds the orchestrator graph and Ansible path locally. This should be reduced to a small deterministic supervisor/action runtime so child nodes do not need the full planner stack.
- The current implementation contains reliability risks to resolve during the audit, including unbounded/weakly correlated message handling, no durable deduplication record, no explicit request deadline/cancellation protocol, and a thread-pool implementation that must be exercised outside unit-only paths.

Therefore Option B is **partially implemented but not production-ready**. The implementation plan is a hardening, contract, and controlled-autonomy plan.

Required audit checkpoints:

- [ ] Confirm which existing child-agent code is production-connected versus only unit-tested.
- [ ] Confirm whether child agents are deployed and running on VM2, LXC1, and LXC2.
- [ ] Replace shared-secret-only transport assumptions with per-node NATS credentials and subject permissions.
- [ ] Define every request with a unique ID, idempotency key, expiry/deadline, source identity, target identity, schema version, and reply correlation.
- [ ] Make duplicate, delayed, reordered, malformed, and replayed messages safe.
- [ ] Ensure child-agent actions cannot become arbitrary shell or arbitrary LLM-generated code, even when mutation is allowed.

## Files to modify

_To be finalized after the audit._ Expected areas include:

- `node_agent/child_agent.py`
- `node_agent/child_catalog.py`
- `orchestrator/child_bridge.py`
- `orchestrator/models.py`
- `orchestrator/db.py`
- `orchestrator/policy.py`
- `orchestrator/agent.py`
- `ansible/` child-agent deployment playbooks/templates
- `systemd/` or OpenRC service definitions
- tests for protocol, security, failure, and integration behavior
- `DESIGN.md` and `PLAN.md` for the finalized contract and rollout gates

## Reuse

- Existing NATS telemetry subjects and state-manager patterns.
- Existing `nodes` database record and agent metadata columns.
- Existing `PolicyEngine`, `PreflightEngine`, primitive/action validation, and human approval flow.
- Existing Ansible inventory and platform-specific handling for Alpine/OpenRC and Debian/systemd.
- Existing task/step status model, verifier, snapshots, and rollback mechanisms where applicable.
- Existing child-agent and bridge tests as regression coverage.

## Implementation Status

### Initial hardening slice — complete

- Added versioned Pydantic protocol models in `orchestrator/child_protocol.py` for heartbeats, capabilities, status, task requests, acknowledgements, and results.
- Added strict message validation, bounded task lifetimes, message IDs, node identity, idempotency keys, and correlation fields.
- Added durable `child_task_runs` storage and idempotent task claiming/result persistence.
- Updated the child bridge and child agent to use typed task/result messages.
- Added support for NATS username/password or credentials-file configuration while retaining the separate short-lived task authorization token.
- Expanded the child action catalog with bounded local remediation primitives:
  - install/remove an approved package
  - ensure an approved service state
  - write bounded state files only under `/opt/mao/state/`
- Mutation remains declarative and catalog-rendered; arbitrary shell and LLM-generated code remain forbidden.
- Added protocol, idempotency, and bounded-remediation tests.

### Next implementation slice — complete

- Added `scripts/render_nats_config.py` for authenticated NATS configuration with per-node publish/subscribe permissions.
- Added `ansible/playbooks/configure_nats_auth.yml` for Vault/external-secret-backed credential distribution and config rendering.
- Updated local NATS startup to optionally render and use authenticated configuration with `NATS_AUTH_REQUIRED=true`.
- Added explicit task acknowledgement, progress, cancellation, and expiry handling.
- Added durable cancellation flags and task lifecycle updates.
- Added heartbeat stale detection and persisted readiness state in SQLite.
- Added typed workload-readiness request/response negotiation with deterministic disk/load checks.
- Added reconnect callbacks, source-node/subject validation, and bridge subscriptions for the expanded protocol.
- Added durable protocol-message deduplication, outbound idempotency registration, terminal-state protection, and replay-safe result handling.
- Added tests for NATS permissions, readiness, lifecycle events, stale detection, replay protection, and protocol validation.
- Hardened freeform execution with module validation and syntax checks before Ansible execution.
- Added a deterministic read-only audit fallback when the LLM is unavailable; unsafe mutation requests now fail closed instead of falling back to package updates.
- Prevented snapshots for strictly read-only playbooks.

### Deployment verification — complete for the current cluster

- Authenticated NATS is running on VM1 with separate orchestrator, child-agent, and telemetry credentials.
- Child agents are deployed and active on LXC1, LXC2, and VM2.
- Telemetry publishers are active on LXC1 and LXC2.
- VM1 state manager and orchestrator bridge are active as persistent user services.
- Verified live heartbeats, capabilities, telemetry persistence, readiness negotiation, and delegated child tasks on LXC1 and VM2.

### Remaining implementation slice

- [x] Add live NATS integration tests using an ephemeral broker when `NATS_SERVER_BIN` is provided.
- [ ] Exercise credential rotation/revocation in live integration tests; broker, agent, and orchestrator restart recovery has been exercised manually.
- [x] Add deployment tasks to distribute per-node credential input through Ansible Vault or an external secret source.

## Steps

- [x] Step 1: Complete the initial repository audit; classify existing Option B code as partial and not production-ready.
- [x] Step 2: Define versioned message schemas and state transitions for registration, heartbeat, capabilities, status, task request, task acknowledgement, progress, result, cancellation, and expiry.
- [x] Step 3: Define node identity, NATS credentials/permissions, registration/rotation/revocation, and trust boundaries for the initial authenticated server configuration.
- [x] Step 4: Harden the child agent into a deterministic local supervisor with no unrestricted LLM or command execution.
- [ ] Step 5: Add automated credential rotation/revocation; initial per-node least-privilege credentials are deployed.
- [x] Step 6: Harden the orchestrator bridge with correlation, reconnect handling, stale-agent detection, cancellation/expiry, typed lifecycle events, and durable task state.
- [x] Step 7: Deploy the agent to one canary node, then all workers through capability-based systemd/OpenRC Ansible roles.
- [x] Step 8: Add capability reporting and workload-readiness negotiation; keep VM1 as final scheduler and policy authority.
- [ ] Step 9: Add explicitly approved local actions with resource/time/output/concurrency limits, audit records, preconditions, postconditions, and rollback or compensating-action definitions.
- [ ] Step 10: Add fault-injection and adversarial tests for LLM uncertainty, malformed messages, replay, duplicate delivery, agent loss, NATS loss, credential revocation, and partial execution.
- [ ] Step 11: Define rollout/rollback gates and only then expand the catalog or autonomy boundary.

## Verification

- [ ] All existing tests pass.
- [ ] Protocol/schema/property tests cover malformed, duplicate, delayed, reordered, and replayed messages.
- [ ] Security tests prove unauthorized nodes/actions/topics are rejected.
- [x] Canary child agent reports heartbeat and capabilities to VM1.
- [x] VM1 marks agents stale/offline after missed heartbeats.
- [x] A delegated read-only task completes with correlated durable result.
- [x] Repeating the same request does not duplicate side effects.
- [x] NATS restart, child-agent restart, and orchestrator restart recover safely.
- [ ] LLM invalid/ambiguous output results in refusal or clarification, never execution.
- [ ] Ansible remains able to bootstrap, repair, disable, or remove a child agent.

## Confirmed Decisions

- The first Option B milestone includes both read-only inspection and workload-readiness negotiation.
- Child agents use NATS credentials for broker authentication and authorization.
- Child agents may act beyond read-only inspection from the first release, but every action must be contained by deterministic policy, a restricted action catalog, resource/time/output limits, authorization, audit logging, and rollback/recovery controls.
- Deployment must support the node's init system generically: systemd for VM2 and OpenRC for the Alpine LXCs in the current cluster, with capability-based service installation rather than hardcoding only these three nodes.

## Remaining Decisions

- Does “act however” mean the first release should include workload start/stop/reconfiguration primitives, or should it begin with cataloged local remediation plus readiness negotiation?
- Confirm that child-agent NATS credentials should be provisioned per node by Ansible and stored only on that node, with a separate orchestrator credential.
- What resource ceilings should apply to child actions (wall-clock time, output size, CPU/memory, and concurrent actions)?
