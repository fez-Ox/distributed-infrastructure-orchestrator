# LLM Guardrail Implementation Plan — AI Orchestrator

## Purpose

This document is an execution plan for adding defense-in-depth guardrails
around the AI Orchestrator's LLM component (see `DESIGN.md`). It replaces
"human reviews every playbook" as the *sole* safety mechanism with seven
independent, stackable layers. An implementing agent should work through
the phases in order — each phase is usable on its own and does not require
later phases to already exist.

Read `DESIGN.md` first for full system context (NATS, Ansible, State DB,
Agent Tools) before starting any phase below.

---

## Guiding principle

Do not treat this as "choose template vs. free-form generation." The two
are endpoints of a spectrum. Each phase below narrows the gap between them
by adding an independent check or containment mechanism, so the system can
safely move toward more LLM flexibility over time without a corresponding
increase in blast radius.

---

## Phase 1 — Compositional action DSL (replace 1:1 templates)

**Goal:** Stop writing one Ansible template per workflow. Define a small
set of composable, individually-vetted primitives that the LLM can
sequence to satisfy arbitrary requests.

Tasks:
1. Enumerate an initial primitive catalog, e.g.:
   - `install_package(node_id, package, version?)`
   - `ensure_service_state(node_id, service, state: running|stopped|restarted)`
   - `open_firewall_port(node_id, port, proto, source_cidr?)`
   - `set_sysctl(node_id, key, value)`
   - `mount_volume(node_id, source, target, fstype, options?)`
   - `deploy_container(node_id, image, config)`
2. For each primitive, write:
   - A JSON schema for its parameters
   - A deterministic Ansible task-template that renders it (idempotent,
     no `shell`/`command`/`raw` modules unless explicitly reviewed)
   - A declared "expected effect" descriptor (see Phase 4) — what
     resources/files/services this primitive touches when run
3. Update `create_task_intent()` so the LLM outputs a **sequence of
   primitive calls with parameters**, not free-form YAML and not a single
   monolithic template selection.
4. Update the deterministic renderer to compile a primitive sequence into
   one Ansible playbook (multiple tasks), preserving per-task idempotency.
5. Add a catalog-versioning mechanism: adding a new primitive is a
   reviewed, logged, deliberate action — not something the LLM can do
   implicitly.

Acceptance criteria:
- The LLM never emits raw Ansible YAML directly; it only emits primitive
  calls validated against the JSON schema.
- Any primitive sequence can be rejected at parse time if it references
  an unknown primitive or malformed parameters.

---

## Phase 2 — Independent verifier pass (adversarial second opinion)

**Goal:** Use a second LLM call, with no shared reasoning trace from the
first, to check that the generated intent actually matches the original
request's scope.

Tasks:
1. After `create_task_intent()` produces a primitive sequence, send a
   **fresh context** (original user request + resulting primitive
   sequence, nothing else) to a verifier prompt/model.
2. Verifier's only allowed outputs: `MATCH`, `SCOPE_MISMATCH(reason)`, or
   `AMBIGUOUS(reason)`.
3. Prompt the verifier to specifically check:
   - Are all target `node_id`s implied by the request?
   - Does the primitive count/type match the request's apparent
     complexity (e.g. a "restart nginx" request producing a
     `mount_volume` call is a red flag)?
   - Any primitive touching nodes tagged `role=critical` without explicit
     mention in the request?
4. `SCOPE_MISMATCH` or `AMBIGUOUS` → task halts, escalates to human
   review with the verifier's stated reason attached (do not silently
   retry the first model).

Acceptance criteria:
- Verifier runs on 100% of generated intents before Phase 3.
- Verifier disagreement is logged with both the original request and the
  full primitive sequence for later audit/tuning.

---

## Phase 3 — Policy-as-code gate (deterministic, non-negotiable)

**Goal:** A rules engine that runs on the compiled artifact regardless of
how it was produced, enforcing hard invariants no LLM output can override.

Tasks:
1. Stand up a policy engine (OPA/Rego or equivalent) as a pipeline step
   between "renderer" and "Ansible executor."
2. Encode rules such as:
   - Deny any task targeting a node with `role=critical` unless a human
     approval flag is set.
   - Deny use of `shell`/`command`/`raw` modules unless an explicit
     override flag was set by a human, logged with justification.
   - Cap blast radius: reject (require escalation) any single task
     touching more than N nodes.
   - Enforce resource bounds (e.g. disk/package operation size limits).
3. Policy evaluation result (`ALLOW` / `DENY(reason)` / `ESCALATE(reason)`)
   is stored on the task record alongside the playbook.
4. Policy rules live in version control, separate from the
   agent/orchestrator codebase, with their own review process.

Acceptance criteria:
- No playbook reaches the Ansible executor without a policy engine
  verdict recorded.
- Policy rule changes are auditable independently of LLM/agent changes.

---

## Phase 4 — Predicted-effect diffing

**Goal:** Catch cases where the LLM's *stated intent* and the *actual
compiled playbook behavior* diverge — the core hallucination failure mode.

Tasks:
1. Extend each primitive (Phase 1) with a declared "expected effect"
   descriptor: services touched, files modified, packages
   installed/removed, ports opened/closed.
2. Have the agent emit its own expected-effect summary alongside the
   primitive sequence (from `create_task_intent()`).
3. Statically analyze the actual rendered playbook (via
   `ansible-playbook --syntax-check` plus a task parser) to compute the
   *actual* effect set.
4. Diff declared vs. actual effect sets. Any actual effect not covered by
   the declared set → reject and escalate (this typically indicates a
   mis-rendered template, an unexpected primitive interaction, or drift
   between the primitive's declared and real behavior).

Acceptance criteria:
- Every task has a stored expected-effect set and actual-effect set.
- Mismatches block execution and are logged with both sets for review.

---

## Phase 5 — Dry-run + snapshot before real execution

**Goal:** Make the worst case survivable regardless of what passed the
earlier checks.

Tasks:
1. Before real execution, always run `ansible-playbook --check --diff`
   and feed the diff through the Phase 3 policy engine again (dry-run
   diff may reveal effects the static analysis in Phase 4 missed).
2. Immediately before real execution, snapshot the target LXC/VM via
   Proxmox (`pct snapshot` / `qm snapshot`), tagging the snapshot with the
   task ID.
3. Store the snapshot reference on the task record so rollback (Phase 7)
   has a concrete target.
4. Constrain the Ansible SSH connection user's privileges at the OS level
   (scoped `sudoers`, no destructive commands outside declared paths,
   read-only mounts for anything outside the primitive catalog's stated
   scope) so even an unanticipated action has a bounded worst case.

Acceptance criteria:
- No real (non-check) Ansible run happens without a preceding snapshot
  recorded against the task.
- Dry-run diff is policy-checked before the real run proceeds.

---

## Phase 6 — Canary rollout + progressive trust scoring

**Goal:** Scale autonomy based on evidence, not blanket trust or blanket
suspicion.

Tasks:
1. When a task's primitive sequence targets multiple nodes, execute on
   one canary node first; require `verify_deployment()` to pass and a
   cooldown/observation window before propagating to remaining nodes.
2. Maintain a per-primitive-type (or per-primitive-sequence-signature)
   trust score in the State DB: success rate, variance, incident count.
3. Define an autonomy policy: action types above a trust threshold and
   below an age/incident threshold may skip human approval (still
   subject to Phases 3–5); novel or historically-flaky types always
   require full human review regardless of verifier/policy outcomes.
4. Trust scores decay or reset on any incident (failed
   `verify_deployment`, triggered rollback, policy denial).

Acceptance criteria:
- Trust scores are visible in the task/inventory dashboard.
- Auto-approval is only possible for action types meeting the defined
  threshold; this threshold is configurable and defaults conservative.

---

## Phase 7 — Automatic rollback on failed post-check

**Goal:** Close the loop — self-heal without waiting for a human to
notice a regression.

Tasks:
1. Extend `verify_deployment()` to run automatically after every task at
   a fixed interval post-execution (not only on demand).
2. On failure (telemetry regression, service health check failure,
   verify_deployment reporting bad state), automatically trigger rollback
   using the Phase 5 snapshot (`pct rollback` / `qm rollback`) or an
   Ansible revert playbook if a snapshot isn't available.
3. Log the rollback event, the triggering failure, and notify the human
   operator after the fact (rollback should not block on human
   confirmation — speed matters here).
4. Update the trust score (Phase 6) for the relevant primitive
   type/signature on every rollback event.

Acceptance criteria:
- Every executed task has a scheduled post-check.
- Rollback triggers automatically without human confirmation, and is
  logged with enough detail to reconstruct what happened.

---

## Suggested build order

Phases 1, 3, and 5 give the most safety per unit of effort and have no
dependencies on each other — a reasonable starting trio. Phase 2 and 4
add semantic and static-analysis catches on top. Phases 6 and 7 assume
1–5 are stable and are where autonomy actually starts scaling up.

| Phase | Depends on | Effort | Risk reduced |
|---|---|---|---|
| 1. DSL | — | High | Hallucination surface area |
| 2. Verifier | 1 | Medium | Scope drift |
| 3. Policy engine | — | Medium | Hard invariant violations |
| 4. Effect diffing | 1 | Medium | Template/primitive mis-render |
| 5. Dry-run + snapshot | — | Low–Medium | Worst-case blast radius |
| 6. Canary + trust | 1, 3, 5 | Medium | Multi-node cascading failure |
| 7. Auto-rollback | 5 | Low | Time-to-recovery |
