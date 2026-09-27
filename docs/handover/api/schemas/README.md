# API schema notes

The canonical machine-readable schemas are embedded in `../openapi.yaml`.

This directory exists for examples and schema design notes that are easier to
review outside the OpenAPI document.

## Resource relationships

```text
Target
  ↓
Inspection (content_hash)
  ↓
ExecutionSpec (spec_hash)
  ↓
Plan
  ├── Candidate(method A)
  ├── Candidate(method B)
  └── Candidate(method C)
```

Workflow state is represented separately:

```text
WorkflowExecution
  └── OperationExecution*
```

Events reference the workflow execution and optionally an operation execution
and candidate.

Artefacts store large payloads outside the relational database.

## Target extensibility

A target is intentionally generic.

Stable top-level fields:

```text
id
kind
source
content_hash
artifact_id
metadata
created_at
```

The implementation should dispatch target-specific behaviour by `kind`.

Example kinds:

```text
git
iso
apk
ova
qcow2
container
artifact
```

Git-specific assumptions must stay inside the Git target resolver/inspector.

## ExecutionSpec

The ExecutionSpec is bounded and versioned.

Suggested v1 fields:

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

Absence of user intent remains absence. There is no implicit `"run the application"` intent.

Every spec has:

```text
schema_version
spec_hash
provenance
```

## Candidate verdicts

Candidates use:

```text
runnable
blocked
transient_error
```

`blocked` is a durable deterministic verdict and may be cached.

`transient_error` is retried later and must not be treated as a durable negative cache result.

## Plan evolution

`Plan.schema_version` is required.

Future plan shapes should be versioned instead of silently changing the meaning
of persisted plans.

Candidate-specific data belongs under `candidate.spec`, but each candidate
method should validate that payload against a versioned method-specific schema.

## Provenance

Persist enough provenance to distinguish:

- fresh generation
- deterministic generation
- cached replay
- user-overridden spec
- intent + inspection-derived spec

Cache replay should make zero fresh LLM calls for replayed candidate entries.

## Events

The base event envelope supports a generic `metrics` object.

Useful metrics may include:

```text
llm_calls
cache_hit
elapsed_ms
tokens_in
tokens_out
```

Keep transport-specific concerns such as SSE outside the core event type.


## CandidateSpec catalogue

Normative v1 candidate schemas are now included:

```text
candidates/docker-compose-v1.schema.json
candidates/dockerfile-v1.schema.json
candidates/native-v1.schema.json
candidates/container-v1.schema.json
candidates/prebuilt-v1.schema.json
```

These schemas deliberately clamp:

- port ranges;
- HTTP path format;
- env-key syntax;
- user syntax;
- minimum memory declarations;
- check shapes;
- method identity/version.

Some semantic clamps cannot be expressed in JSON Schema alone and remain deterministic validator duties:

- registry/image existence checks;
- remote asset existence checks;
- check floors derived from the ExecutionSpec;
- Docker memory floor escalation;
- keep-alive shell-payload rejection;
- deliverable compatibility.

For the v1 port, behaviour in the existing Python planner's `scripts/vmf_plan.py`
is the normative reference where a machine-readable rule is not yet encoded here.


### v1 method semantics

`native` supports top-level `install`, `command`, `environment`, concrete `user`,
and structured checks.

`dockerfile` is a distinct method with `dockerfile`, build `context`, optional
build target/args, runtime command/environment, ports, and checks.

`container` means running an already-resolved image and explicitly carries
`needs_docker: true`.

`prebuilt` supports install steps, runtime command/environment, TCP/HTTP/exec/cmd
checks, and a concrete runtime user.

Structured `cmd` checks use:

```json
{
  "kind": "cmd",
  "bin": "mvt",
  "probes": [["--version"], ["--help"]]
}
```

Candidate-level `user` is a concrete account name. ExecutionSpec-level user
policy remains separate.


### `container` vs `prebuilt(kind: image)`

`container` represents guest/runtime execution of a container image and requires
bounded `image_provenance` (`pull` or `build`).

`prebuilt(kind: image)` represents an already-resolved image artefact and must
not also define an image-production path.

They are separate candidate lanes and should not both be emitted for the same
underlying approach.
