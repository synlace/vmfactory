# vmfactory Architecture

## 1. Purpose

vmfactory is an execution control plane for software and machine artefacts.

A user provides a **target** and optionally an **intent**. vmfactory determines what the target is, what success should mean, and which execution approaches are plausible.

The first target type is a Git repository. The architecture must not make Git the core data model because later targets may include:

- ISO images
- APKs
- OVAs
- QCOW2 / raw VM images
- container images
- local archives
- other executable or bootable artefacts

The key abstraction is therefore **Target**, not repository.

---

## 2. Product direction

The user-facing interface should remain small:

```bash
vmf plan <target>
vmf run <target>
```

The internal system is workflow-oriented.

A high-level user action such as `plan` or `run` is composed from atomic operations that are independently meaningful, observable, retryable where appropriate, cancellable where appropriate, and testable.

The first implementation target is the planning path:

```text
CLI
  ↓
core planning workflow
  ↓
atomic operations
  ↓
persistent state + artefact store
  ↓
event envelope
  ↓
CLI renderer
```

A daemon/API layer may wrap the same core later:

```text
CLI / Web UI
   ↓
HTTP API
   ↓
same core planning workflow
```

The transport must not redefine the domain model.

---

## 3. Process model

### MVP-1: in-process CLI

The first executable implementation may run the planning workflow in-process.

It should still use the same interfaces intended for the daemon:

- workflow execution
- operation execution
- event emission
- persistence
- cancellation primitives

This avoids introducing daemon discovery/autostart complexity before it provides measurable value.

### Later: local daemon

The daemon may later own:

- HTTP API handling
- SSE transport
- workflow scheduling
- persistence access
- target inspection
- plan generation
- later: resource scheduling and VM lifecycle management

The daemon is **daemon-shaped but on-demand**. It may remain active while useful work exists and terminate after an idle timeout.

### CLI

The CLI should remain a thin renderer/client.

In daemon mode it should:

1. discover whether the local daemon is running;
2. start it if necessary;
3. submit an API request;
4. receive an execution ID;
5. subscribe to the execution event stream;
6. render progress;
7. display the final result.

The CLI should contain as little business logic as possible.

---

## 4. Core domain objects

### Target

Represents what the user wants vmfactory to work with.

Stable fields:

```text
id
kind
source
content_hash
artifact_id
metadata
created_at
```

Examples of `kind`:

```text
git
iso
apk
ova
qcow2
container
```

Type-specific details belong in structured metadata rather than forcing a new relational schema for every target kind.

### Inspection

Evidence collected from a target.

The inspection owns a bounded, content-addressed evidence bundle and records:

```text
id
target_id
content_hash
evidence
limits
created_at
```

For Git targets this may include:

- README / documentation
- Dockerfile
- Docker Compose files
- devcontainer definitions
- CI workflows
- language manifests
- build scripts
- ports
- runtime requirements
- platform assumptions

The implementation may impose limits on evidence collection, but exact byte limits are implementation policy unless clients need to depend on them.

Other target kinds will have different inspectors.

### ExecutionSpec

Defines **what success means** independently from any candidate implementation.

It is intentionally bounded and versioned.

Example shape:

```yaml
schema_version: "1"
deliverable: web
serve:
  protocol: http
  port: 8080
  path: /
auth: none
env_required:
  - DATABASE_URL
user: non-root
hold: true
provenance:
  source: intent+inspection
spec_hash: sha256:...
```

Suggested bounded fields for v1:

```text
deliverable: web | cli
serve:
  protocol
  port
  path
auth
env_required[]
user
hold
```

Not every plan needs every field.

If the user supplied no intent and the inspection does not justify a richer spec, the system must not invent one.

`spec_hash` is computed from the canonical ExecutionSpec representation and is part of plan/cache invalidation.

Spec provenance should identify whether the spec came from:

- user override
- intent + inspection
- inspection only
- cached replay

### Plan

A structured proposal for how to execute a target under a given `ExecutionSpec`.

Stable fields should include:

```text
id
target_id
inspection_id
execution_spec
spec_hash
schema_version
state
candidates
provenance
created_at
```

A plan can contain one or more candidate approaches.

Examples:

- Docker Compose
- direct/native install on Ubuntu 24.04
- devcontainer
- prebuilt image
- boot ISO
- Android emulator / APK execution

The plan records evidence and provenance used to derive each candidate.

### Candidate

Each candidate represents one execution method and has its own verdict.

Candidate states/verdicts:

```text
runnable
blocked
transient_error
```

Semantics:

- **runnable** — passed deterministic planning checks and is actionable.
- **blocked** — deterministically unsuitable; may be negatively cached.
- **transient_error** — planner could not establish a durable verdict because of a temporary/internal failure; do not cache as blocked.

A plan succeeds if it has at least one runnable candidate.

A plan may still succeed while also containing blocked candidates.

A plan is blocked when candidate methods were evaluated but none are runnable and the non-runnable outcomes are durable `blocked` verdicts.

A plan fails when planning itself could not complete reliably.

### WorkflowExecution

An instance of a named workflow definition.

Example:

```text
workflow_id: plan-v1
execution_id: wf_01...
state: running
```

### OperationExecution

An execution of one atomic operation inside a workflow.

Examples:

```text
resolve_target
inspect_target
derive_execution_spec
generate_candidate
validate_candidate
persist_plan
```

### Event

Append-only record of state changes and useful progress.

Events are used for:

- in-process rendering
- SSE streaming
- reconnect/catch-up
- auditing
- debugging
- telemetry/cost accounting

### Artifact

Metadata describing large or binary content stored outside the relational database.

Examples:

- cloned source trees
- source archives
- ISO files
- APKs
- VM images
- generated files
- logs
- snapshots

---

## 5. Persistence and cache model

Use a hybrid model.

### SQLite

Store:

- IDs
- resource relationships
- workflow state
- operation state
- plans
- candidate verdicts
- inspection documents
- JSON metadata
- provenance
- errors
- timestamps
- append-only events
- cache metadata

### Filesystem artefact store

Store heavy bytes outside SQLite.

Suggested initial layout:

```text
~/.local/share/vmfactory/
├── vmfactory.db
├── artifacts/
│   └── sha256-...
├── workspaces/
└── vms/
```

Artefacts should be content-addressed when practical.

### Cache keys

Plan/candidate reuse must be based on content, not only names or URLs.

At minimum, cache identity should include:

```text
target content hash
inspection content hash
spec hash
candidate method
planner/schema version
```

Each candidate method should have its own cache entry.

Example conceptual keys:

```text
plan-docker-compose.json
plan-native.json
plan-devcontainer.blocked
```

Exact filenames are implementation details; the important contract is per-method cache identity and verdict semantics.

### Cache semantics

- `runnable` candidate results may be reused when all validity inputs still match.
- `blocked` candidate results may be cached when the block is deterministic.
- `transient_error` is not a durable negative cache entry.
- transient failures should be retried on the next run.
- replayed plans/candidates must record cache provenance.
- cached replay should be distinguishable from fresh model-generated planning.
- a replayed candidate may legitimately record zero fresh LLM calls.

### Schema evolution

Use SQL migrations.

Do not choose NoSQL solely to avoid schema migration. Stable system relationships are relational, while fast-changing target-specific details can be stored in JSON fields.

---

## 6. Planning workflow v1

The documentary flow is:

```text
resolve_target
    ↓
inspect_target
    ↓
derive_execution_spec
    ↓
generate_candidates_by_method
    ↓
validate_candidates
    ↓
persist_plan
```

The Go implementation should hand-code this workflow for v1.

`workflows/plan-v1.yaml` documents the flow and contracts but is **not** a request to build a generic DAG/workflow interpreter.

### 6.1 Resolve target

Purpose:

- normalise target input;
- determine target kind;
- resolve refs where possible;
- create or reuse a Target resource.

For a Git URL, resolution may eventually pin a mutable branch name to a specific commit.

Output:

```text
Target
```

### 6.2 Inspect target

Purpose:

- collect bounded evidence needed for planning;
- avoid guessing when useful evidence is available;
- produce a structured inspection document;
- compute an evidence bundle `content_hash`.

Output:

```text
Inspection
```

### 6.3 Derive execution spec

Purpose:

- convert explicit user intent and grounded inspection facts into a bounded `ExecutionSpec`;
- avoid inventing requirements when intent is absent;
- preserve provenance;
- compute `spec_hash`.

If the user does not pass intent, intent is absent.

There is no default `"run the application"` intent.

Output:

```text
ExecutionSpec
```

### 6.4 Generate candidates per method

Purpose:

- derive plausible execution approaches from inspection evidence and ExecutionSpec;
- generate candidates independently per method;
- permit per-method cache hits and misses;
- permit a mix of runnable, blocked, and transient outcomes.

Candidate generation may use bounded LLM calls, deterministic heuristics, cached results, or a combination.

Model-generated outputs must be structured and bounded.

### 6.5 Validate candidates

Validation is **clamp-grade semantic validation**, not mere schema checking.

Validation should deterministically reject unsupported or unsafe assumptions rather than guess.

Examples of validation duties:

- registry-check image references before accepting them;
- independently check remote asset URLs where relevant;
- derive verification/check floors from declared service ports/spec;
- reject keep-alive-only behaviour when the deliverable requires an actual web service;
- enforce deliverable guards from the ExecutionSpec;
- reject candidates whose assumptions contradict known inspection evidence;
- prefer dropping a candidate over fabricating missing facts.

The public contract should describe these invariants, not require a specific tool such as `skopeo`.

Output per candidate:

```text
runnable
blocked
transient_error
```

### 6.6 Persist plan

Purpose:

- assign durable identity;
- store the plan;
- persist per-candidate verdicts and cache provenance;
- associate it with target, inspection and ExecutionSpec;
- publish completion event.

Output:

```text
Plan
```

---

## 7. Atomic operation rule

An operation should be exposed as a first-class operation only when it is independently meaningful.

Good reasons to make something an operation:

- it has a distinct input and output;
- it may take non-trivial time;
- it can fail independently;
- it is useful to observe separately;
- it may be retried;
- another workflow could reuse it.

Do **not** expose every internal function as an API operation.

---

## 8. Model seam and deterministic degradation

The model boundary is explicit.

### Rule 1: models propose; deterministic mechanisms dispose

Models may:

- summarise evidence;
- derive a bounded ExecutionSpec;
- propose candidate approaches;
- explain rationale.

Deterministic code must:

- validate structured output;
- clamp unsupported fields;
- verify references where possible;
- decide durable candidate verdicts;
- compute cache keys;
- persist canonical state.

### Rule 2: AI is an accelerator, never a dependency

The system should have deterministic degrade paths.

Examples:

- cached plans can replay without an LLM;
- base approaches can be generated from grounded manifests;
- missing enrichment should reduce confidence/detail, not crash the whole planner where a deterministic path exists.

LLM calls should be bounded per candidate/method.

---

## 9. API behaviour

The API follows three broad conventions:

```text
POST = request that work happen
GET  = inspect current truth
SSE  = observe changes as they happen
```

Long-running actions return quickly with `202 Accepted`.

Example:

```http
POST /v1/workflow-executions
```

Response:

```json
{
  "id": "wf_01...",
  "workflow_id": "plan-v1",
  "state": "queued"
}
```

The client may then subscribe:

```http
GET /v1/workflow-executions/wf_01.../events
Accept: text/event-stream
```

The exact same event envelope should also be emitted by in-process execution.

---

## 10. Event model

All events use a shared envelope.

Example:

```json
{
  "id": "evt_01...",
  "sequence": 12,
  "type": "candidate.validated",
  "timestamp": "2026-09-27T11:30:00Z",
  "workflow_execution_id": "wf_01...",
  "operation_execution_id": "op_01...",
  "candidate_id": "cand_01...",
  "data": {
    "method": "docker-compose",
    "verdict": "blocked"
  },
  "metrics": {
    "llm_calls": 1,
    "cache_hit": false,
    "elapsed_ms": 418
  }
}
```

Recommended event characteristics:

- append-only
- monotonically sequenced per workflow execution
- persisted before/while publishing
- reconnectable using last event ID / sequence
- versionable
- capable of carrying candidate-level detail
- capable of carrying generic metrics/usage data

Keep cost accounting generic in the base schema.

Useful metrics may include:

```text
llm_calls
cache_hit
elapsed_ms
tokens_in
tokens_out
```

Not every event needs every metric.

---

## 11. Planning states

### Workflow execution

```text
queued
running
succeeded
blocked
failed
cancelled
```

### Operation execution

```text
queued
running
succeeded
failed
cancelled
```

### Candidate verdict

```text
runnable
blocked
transient_error
```

The current operation should be derived from operation executions rather than encoded as an ever-growing workflow-specific state enum.

A blocked candidate does not fail the workflow.

---

## 12. Errors

Errors should be structured and distinguish durable candidate blocks from transient failures.

Example:

```json
{
  "code": "target_unreachable",
  "message": "The Git target could not be fetched.",
  "retryable": true,
  "classification": "transient",
  "details": {}
}
```

Error classifications:

```text
invalid_input
blocked
transient
internal
cancelled
```

Candidate blocked reasons are durable domain outcomes and may also be represented as candidate verdict detail rather than workflow errors.

Error categories should distinguish:

- invalid input
- target access failure
- inspection failure
- spec derivation failure
- candidate generation failure
- validation block
- persistence failure
- cancellation
- internal error

---

## 13. Idempotency

Creation endpoints should support an idempotency key.

Example:

```http
Idempotency-Key: 3c471...
```

This prevents accidental duplicate workflow creation if a client retries after a transport failure.

---

## 14. Security assumptions for planning MVP

The planning MVP should minimise execution of untrusted target code.

Inspection should prefer reading files and metadata.

If any inspection step executes target-provided code, that must be explicitly isolated and documented.

Later execution workflows will require stronger isolation boundaries around:

- filesystem access
- network access
- host mounts
- secrets
- device access
- GPU access
- VM lifecycle

---

## 15. Future execution workflow

Not in the first implementation, but the architecture should support:

```text
profile_host
resolve_plan
create_candidates
allocate_resources
execute_candidates      [parallel]
verify_candidates
select_candidate
crystallise_plan
promote
verify_promotion
```

Candidate failures should not automatically mean workflow failure.

The clean promoted VM is the eventual reproducibility test.

---

## 16. Acceptance principle

The central acceptance rule is:

> Candidate generation may be probabilistic; acceptance must be deterministic.

The planner should not declare success merely because:

- a schema validated;
- an LLM said the plan looked correct;
- a process remained alive;
- a command exited zero without satisfying the deliverable.

For the planning MVP, acceptance means at least one candidate survives deterministic validation against the target evidence and ExecutionSpec.

---

## 17. Planning MVP acceptance scenarios

### Scenario A — minimal CLI target

A simple CLI-oriented repository produces a CLI ExecutionSpec and at least one runnable candidate.

### Scenario B — explicit web intent

A repository plus explicit user intent produces a web ExecutionSpec with grounded serve requirements and runnable candidate(s).

### Scenario C — blocked sidecar candidate

A superficially plausible candidate is rejected deterministically because it does not satisfy the ExecutionSpec or grounded target facts. The plan still succeeds if another candidate is runnable.

### Scenario D — cached replay

A repeated plan request with unchanged target evidence/spec reuses cached candidate verdicts. Provenance records replay/cache hits and zero fresh model calls for replayed entries.

---

## 18. Non-goals for planning MVP

Do not implement yet:

- VM scheduling
- QEMU or Firecracker execution
- promotion
- multi-host scheduling
- remote control plane
- browser UI
- distributed queue
- PostgreSQL
- Kubernetes
- generic workflow/DAG interpreter

The first milestone is intentionally narrower:

```text
vmf plan <target>
```

with a durable structured plan and streamed/in-process progress.

---

## 19. Suggested Go project shape

```text
cmd/
  vmf/
internal/
  api/
  daemon/
  domain/
  events/
  persistence/
  workflow/
  operations/
    resolve_target/
    inspect_target/
    derive_execution_spec/
    generate_candidate/
    validate_candidate/
    persist_plan/
  targets/
    git/
pkg/
api/
  openapi.yaml
workflows/
  plan-v1.yaml
```

The exact package layout can change, but the dependency direction should keep the domain/workflow model separate from HTTP and CLI rendering.


---

## 20. Method-specific CandidateSpec catalogue

The planning architecture depends on candidate specs being both versioned and method-specific.

For v1, the normative schemas live under:

```text
api/schemas/candidates/
```

At minimum:

```text
docker-compose-v1.schema.json
dockerfile-v1.schema.json
native-v1.schema.json
container-v1.schema.json
prebuilt-v1.schema.json
```

These schemas should encode deterministic clamps rather than leaving them to prose.

### Normative clamp categories

The first Go implementation should preserve the measured planner's constraints in these areas:

- service ports must be bounded and valid;
- image references must be normalized and independently resolvable where possible;
- environment variable names must use strict uppercase key syntax;
- user declarations must be constrained to explicit supported forms;
- health/check definitions must be bounded;
- web deliverables must include service checks derived from the declared serve contract;
- memory floors may be raised deterministically for methods that require Docker/container sidecars;
- keep-alive shell payloads must not count as a successful web service;
- candidates should be dropped rather than repaired by invention when required facts are missing.

The Python implementation's clamp behaviour in `scripts/vmf_plan.py` is the normative behavioural source for the v1 port where this pack does not spell out an equivalent machine-readable rule.

This reference is temporary bootstrap guidance. As clamp tests and acceptance fixtures are ported, those tests plus the schemas become the normative contract and the Python implementation should cease to be required as a reference.

---

## 21. Optional external enrichment

Inspection is target-derived evidence.

Planning may also use bounded external current facts, for example:

- current image tags;
- package/install documentation;
- release metadata;
- authoritative runtime instructions.

This is an optional enrichment stage:

```text
resolve_target
    ↓
inspect_target
    ↓
derive_execution_spec
    ↓
enrich_facts (optional, degradable)
    ↓
generate_candidates
```

Requirements:

- enrichment must be bounded;
- every enriched fact carries provenance;
- target evidence remains distinguishable from external evidence;
- failure to enrich must degrade safely;
- junk, ambiguous, or weakly grounded enrichment must be dropped rather than promoted into a requirement;
- enrichment must never become a hidden dependency for replaying an already-valid cached plan.

---

## 22. CLI exit codes

The first CLI contract is:

```text
0  usable plan produced
1  planning completed but no usable candidate exists
2  usage error or ambiguous/invalid invocation
3  required model capability unavailable or unconfigured
```

These exit codes are intentionally coarse.

Examples:

- workflow state `succeeded` with at least one runnable candidate → `0`
- workflow state `blocked` → `1`
- malformed CLI invocation → `2`
- planning path requires a model and no configured model/degrade path exists → `3`

Internal/transient failures may initially map to `1` unless and until a separate operational-error exit code is justified by real automation needs.

---

## 23. HTTP 422 semantics

For plan validation endpoints, HTTP `422 Unprocessable Entity` means:

> the submitted/generated plan is structurally understood but is not executable under the current ExecutionSpec and deterministic validation rules.

It does **not** imply that the workflow engine or daemon failed.

A workflow whose candidates are all deterministically blocked should finish as `blocked`, not `failed`.

---

## 24. Serve path constraint

`ExecutionSpec.serve.path`, when present, must:

- start with `/`;
- represent a URL path rather than a full URL;
- be normalized before hashing into `spec_hash`.

---

## 25. Port-completion rule

The architecture is considered implemented only when:

1. the method schemas exist;
2. the deterministic clamps are ported;
3. CLI exit codes match this contract;
4. acceptance fixtures pass with the expected verdicts;
5. cached replay is observable through provenance and metrics.

The remaining work after this point is implementation fidelity, not architecture discovery.


---

## 26. Candidate method distinctions

The v1 catalogue treats these as distinct methods:

```text
native
container
dockerfile
docker-compose
prebuilt
```

They should not be collapsed into one generic Docker/container shape.

Conceptually:

```text
install   = prepare the environment
command   = launch the deliverable
checks    = prove the deliverable works
```

For CLI deliverables, prefer structured `cmd` checks:

```yaml
kind: cmd
bin: mvt
probes:
  - ["--version"]
  - ["--help"]
```

over free-form shell checks when the same verification can be expressed structurally.

At the ExecutionSpec layer, `user` may express a policy such as `non-root`.

At the CandidateSpec layer, `user` means a concrete account name or null. Policy words such as `non-root` must not be interpreted as Unix account names.


---

## 27. `container` vs `prebuilt(kind: image)`

These methods are intentionally distinct and must not represent the same candidate lane.

- `container` means: run a container image inside the guest/runtime environment. The candidate must identify how that image is obtained or produced using a bounded provenance such as `pull` or `build`.
- `prebuilt(kind: image)` means: use an already-resolved image artefact as the primary input/deliverable. It must not also define a separate image-production path.

Normative rule:

> A `container` candidate must name its image provenance. A `prebuilt(kind: image)` candidate must reference an already-resolved artefact and must not also define how that image is built or produced.

This prevents duplicate candidate fanout for image-shaped targets.
