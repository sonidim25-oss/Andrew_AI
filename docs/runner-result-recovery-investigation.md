# Runner result recovery: findings and proposed next increment

Status: source investigation complete; proposed architectural scope for review.
No runtime code, service, external destination, or live state was changed.

## Goal

Close the next concrete P0 gap: recover a newly journaled Python, Node, or shell
tool result after the runner finishes but the controller loses the reply or
crashes before saving its receipt. Recovery must fetch existing evidence without
running the command again. A saved successful tool response proves the invocation
returned that response; it does not independently prove an arbitrary external
publication or recipient delivery.

## Findings in the current code

- `prototype/andrewbot/runner_client.py`: requests contain only tool type and code
  or command. The strict request protocol has no durable operation identity or
  read-only result lookup. A broken reply becomes a generic transport failure.
- `prototype/andrewbot/runner_service.py:158`: `RunnerSocketServer` keeps one
  executor active at a time and cancels on peer disconnect. The result is sent
  directly over the socket, without a durable receiver receipt. In the disconnect
  branch it waits for cleanup and returns without retaining the worker response.
- `prototype/andrewbot/recovery_dispatch.py:9`: the controller records admission
  before the callback, then confirms the result after the callback returns. A
  crash between runner completion and confirmation leaves an admitted effect.
- `prototype/andrewbot/recovery.py:101`: an admitted unknown effect blocks model
  reconciliation. Receiver evidence therefore needs an explicit reconciliation
  step before normal continuation; adding a result table alone is insufficient.
- `prototype/andrewbot/controller.py:328`: legacy cycle tool attempts and research
  bookkeeping are distinct from the journaled recovery effect. Both must remain
  consistent when importing evidence. External output must stay untrusted.
- `prototype/andrewbot/tool_runtime.py`: result formatting is deterministic for
  Python/Node/shell. Browser handling also records visits and materializes files,
  so replaying that wrapper would have additional local effects.
- `prototype/andrewbot/browser_service.py` has its own process and protocol, and
  `browser_tools.py` handles account submissions and artifacts. Browser recovery
  should follow as a separate adapter, not be inferred from runner receipts.

## Alternatives

| Approach | Benefit | Limitation |
| --- | --- | --- |
| Receiver-side durable runner receipts (recommended first) | Exact bounded result recovery using existing isolated services; no repeated command | Cannot settle a crash before the runner durably saves its result |
| Destination-specific publication adapters | Can verify a particular remote object's identity and content | Requires a concrete supported destination and separate permission/evidence rules |
| Retry arbitrary commands with a new ID | Simple implementation | Can repeat external effects; rejected |

## Proposed first increment

Limit the first implementation to new journaled `python.run`, `node.run`, and
`shell.run` operations. Owner requests and legacy calls keep their existing
behavior. Do not add browser mutations, media generation, billing reconciliation,
or a generic mechanism for clearing held operations.

Introduce a versioned private controller-to-runner envelope. It binds the exact
tool admission, backend/controller store identities, effect identity, tool request
digest, operation identity, and controller authority. The identity is generated
by the controller, not supplied through model arguments. A separate result-lookup
operation must have no execution path and must reject identity/digest mismatches.
Keep the current request validation and output limits for the worker payload;
recovery metadata never reaches arbitrary code or the model.

The runner saves admission before dispatch and a validated bounded response before
sending it. Its durable ledger lives in service-owned storage outside the code
workspace and container mounts, with a stable store identity, restrictive access,
and a single-owner lock. The specification must settle authenticated lookup,
at-rest handling of output, retention/capacity, backup/restore binding, and
service-user permissions before implementation. The controller must not treat a
worker-writable file as trusted evidence.

Repeated operation submission must never start a second worker. A saved result
can be returned; an admitted operation without a saved result remains unknown.
An empty lookup, vanished container, timeout, process exit, cancellation, or
`status: error` never proves that external side effects did not happen. Preserve
disconnect cancellation and wait for worker cleanup before admitting another
execution. Persist a complete validated response when available even if the peer
has disconnected; otherwise retain uncertainty.

Reconciliation reads the exact receiver receipt and validates it against the
original controller admission before saving the normal controller result. It
preserves the original operation/input IDs, legacy cycle linkage, formatted tool
result, and untrusted provenance. It does not rerun the tool callback. Resuming
model work still requires valid owner revision, no pause/sleep, no pending owner
input, restore fencing, and no unrelated uncertain effect or provider cost.

If a backend checkpoint already records `tool_uncertain`, continuation requires a
narrow receipt-backed transition for that exact admitted step, with the current
revision/fence. Never reset a generic hold or infer success from a newer result.
Evidence import and authority to dispatch further work are separate decisions.

## Required verification for the eventual implementation

- Terminate the controller after receiver commit but before reply or controller
  confirmation; reopen stores and recover the exact result with one execution.
- Terminate the runner before and after receiver result commit. Missing evidence
  remains held; a saved result survives restart and never starts another worker.
- Disconnect/cancel during execution and during response delivery. Keep cleanup,
  exclusive worker ownership, and uncertainty for possible partial effects.
- Reject duplicate IDs with changed content, store mismatch, stale authority,
  malformed/oversized results, corrupt receipts, and worker-writable evidence.
- Preserve legacy cycle/research links and untrusted output. Importing evidence
  must not perform local callback side effects or grant memory provenance.
- Exercise pause, owner input/revision changes, unrelated unknown effects,
  restore of an older snapshot, storage exhaustion, and interrupted installation.
- Verify actual service-user permissions and coordinated rollout. Use local fake
  workers/destinations; no paid inference or real publication is needed.

## Handoff

This changes a cross-service protocol and durable state. The next step is approval
of the proposed scope, then a written design covering the unresolved storage,
authority, restore, and receipt-import contracts, followed by implementation
planning. Browser/destination verification remains a subsequent P0 increment;
automatic memory consolidation and procedural memory remain P2.
