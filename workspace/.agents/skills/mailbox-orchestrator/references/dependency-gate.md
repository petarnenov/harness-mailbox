# Dependency-checked execution delivery

Read this before assigning or resuming project execution. The gate imports the dashboard's `server.build_dashboard` projection in-process, so it reads the same task, issue, Epic and milestone dependencies without relying on a possibly stale HTTP snapshot.

## Commands

Run from the mailbox root. The script finds the mailbox by the `projects.json` above it, so `--root` is needed only to work on another mailbox. Where the skills are installed directly under the mailbox root, drop the `workspace/` prefix. Substitute the actual registered project, task UUID and endpoint; a saved session timestamp does not prove reachability.

```sh
python3 workspace/.agents/skills/mailbox-orchestrator/scripts/dispatch_task.py check --task tasks/PROJECT/TASK-UUID.json
python3 workspace/.agents/skills/mailbox-orchestrator/scripts/dispatch_task.py dispatch --task tasks/PROJECT/TASK-UUID.json --worker-id ACTUAL-ENDPOINT-ID
python3 workspace/.agents/skills/mailbox-orchestrator/scripts/dispatch_task.py validate --message inbox/PROJECT/MESSAGE-UUID.json --worker-id ACTUAL-ENDPOINT-ID
```

The same interface is available as `python3 dispatch_gate.py --root "$PWD"` (`dashboard/dispatch_gate.py` in an installed mailbox); called directly, the gate always needs `--root`. Output is JSON: `allowed`, `operation`, `task_id`, `task_path`, `project`, `endpoint`, `fingerprint`, `prerequisites`, `reasons` and, when published, `audit_path` / `message_path`. Exit codes are 0 for an allowed check, 2 for a dependency/hold denial, and 1 for invalid input or publication failure. Only `operation: dispatch` with an actual bound inbox message is executable; an allowed `check` is never an execution request.

## Orchestrator

1. Prepare the owned task record with its canonical UUID filename/ID, registry project, explicit `depends_on` list, actual project loop name/path, scope and acceptance criteria. Preserve the verified integration binding and the user's quota/hold decisions. Do not edit a claimed worker task to clear blockers.
2. Optionally run `check` for a durable explanation. Fix missing authoritative evidence through its owner; never turn unknown into verified merely to obtain an allowed result. Independently scoped preparation needs its own truthful task and full loop; there is no analysis bypass.
3. Establish current worker reachability and use `dispatch` with its self-registered endpoint. The script verifies its project/directory registration but does not establish liveness or wake a session. If denied, record/report its reasons; no new execution message is published.
4. Notify the verified thread only with the exact successful message ID/path. Do not repeat the task as a separate direct execution prompt. Registration, status, evidence consultation and gate decisions remain non-execution coordination and must not silently start or resume work.

Dependencies include direct and transitive `requires` edges, plus prerequisites inherited through the target task's owning issue, parent story and Epic. Membership alone does not require an issue to be completed before implementing it. Jira Closed is not completion evidence. Unknown/unverified/unresolved prerequisites, relevant cycles, malformed authoritative inputs, missing loop/scope identity and explicit target holds deny delivery. Unrelated unresolved downstream references do not become prerequisites. An empty list can clear this dependency check but does not prove the task is otherwise authorized or ready.

The gate writes immutable UUID audit records under `dispatch-gates/<task-uuid>/`. An allowed dispatch receipt binds the canonical mailbox root, task/path, project, worker endpoint, message UUID/hash and task-specific dependency fingerprint. It binds the target specification, incoming membership and dependency closure, prerequisite evidence, applicable scope constraints, target project registration, loop contents and gate code. The recipient's registration identity is bound separately. The whole graph is rebuilt before validation and publication, so newly added dependencies or changed parents are detected. Publication uses complete, non-overwriting files; a receipt without its exact inbox message cannot execute.

Use `--message-id UUID` for a retry of the same request. A valid fresh existing envelope is returned without duplicate delivery. An archived, stale, tampered or differently bound UUID is never overwritten or republished. A fresh outstanding envelope prevents a second assignment of the same task. Resolve/archive stale requests through the normal lifecycle, then create a new gated dispatch when needed; never recycle a processed UUID.

## Worker

Run `validate` with your actual endpoint ID **before claiming or changing the task**. Confirm its result is allowed and matches your message/task. Missing or legacy receipts, check-only receipts, wrong recipients, tampered or absent messages, archived requests and changed input fingerprints deny work. Publish the blocker through the mailbox and request a fresh dispatch; do not clear it yourself.

Unrelated task progress, other issue rows, dashboard aggregates and other workers' registrations do not invalidate a queued receipt. Relevant task, dependency, scope, recipient identity, loop or gate-code changes do. Session check-in timestamps do not change identity. A concurrent unrelated write during evaluation triggers one consistency rebuild; changed authorization or continued unstable input fails closed. Schema-v1 global receipts require fresh schema-v2 dispatch; never rewrite old receipts. Independent tasks may be delivered in parallel without waiting for another worker's claim. Validation happens before the worker's own task mutation; it is not a background lease or a permanent permission for future resumes. Continue the existing project loop, integration synchronization, task lock, G1/G2, quota and user-authorization requirements after a successful validation.

## Limits

This is enforced by the supported publisher and receiving-worker protocol, not an operating-system interceptor for arbitrary file writes or tool calls. Workers must reject ungated execution instructions. Validation re-evaluates current dependencies, closing the gap between publication and receipt; no filesystem snapshot can guarantee that external state never changes afterward. The gate does not verify remote artifact contents, infer production deployment, modify Jira, mutate worker-owned tasks, or automatically resume project sessions.
