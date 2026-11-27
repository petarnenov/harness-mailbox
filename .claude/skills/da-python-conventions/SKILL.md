---
name: da-python-conventions
description: "Production rules for the harness-mailbox dashboard: Python 3.13 standard-library read-only server, mailbox dispatch gate and orchestrator scripts, buildless JavaScript frontend and hardened Docker/Make workflow. Preloaded into the Python writer and reviewer."
user-invocable: false
globs: []
---

RULEBOOK: da-python@2026-11-27.1

# Python production rules

Apply these rules to production code in this repository: root modules `server.py`, `dependency_graph.py`, `delivery_process.py`, `dispatch_gate.py`; the frontend in `static/`; `scripts/*.sh`; `Dockerfile`, `Dockerfile.tools`, `compose.yaml`, `Makefile`, `workspace.Makefile`; demo data in `demo/`; and the tracked workspace template under `workspace/` (instructions, skills and the `mailbox-orchestrator` scripts). The behavioral contract is documented in `README.md` and the workspace `AGENTS.md`/skill references. Do not treat a passing build or health check as completed product behavior.

## §K1. Read-only dashboard boundary
- **§K1.1 (P1)** The web server is a read-only projection of mailbox records: it must never create, modify or delete files under `MAILBOX_ROOT`, and every mutating HTTP method keeps returning `405 read_only`.
- **§K1.2 (P1)** The server makes no Jira, AIO or other network calls and holds no credentials. External integrations belong only in the orchestrator scripts that the workspace instructions authorize.
- **§K1.3 (P1)** Read mailbox files only through a guarded reader that rejects symlinks and paths outside the data root, bounds file size and rejects non-finite JSON numbers; report a rejected file as a warning instead of failing the whole snapshot.
- **§K1.4 (P2)** Unknown, missing or malformed source data stays visible as unknown or a warning. Do not infer completion, ownership, coverage or verification that the records do not state with evidence.

## §K2. Dispatch gate and workspace scripts
- **§K2.1 (P1)** `dispatch_gate.py` and the orchestrator scripts deny on unknown, unmet, cyclic or malformed prerequisites and on missing, stale, tampered or wrongly addressed receipts. There is no bypass flag.
- **§K2.2 (P1)** Validate every caller-supplied path against the mailbox root and its expected layout (for example `tasks/<project>/<uuid>.json`, `inbox/<recipient>/<uuid>.json`) before reading or writing.
- **§K2.3 (P1)** Publish mailbox records atomically: write a uniquely named temporary file in the destination directory, flush and fsync, then link or rename it into place; never overwrite an existing message ID or immutable receipt.
- **§K2.4 (P2)** Identify records with canonical UUIDs and stamp them with timezone-aware UTC ISO-8601 timestamps; reject timestamps without an explicit timezone.
- **§K2.5 (P1)** Keep the dashboard and the gate on one dependency-graph projection, so eligibility shown in the UI and enforced by `dispatch` cannot diverge.
- **§K2.6 (P1)** Scripts that write to Jira or AIO perform only the event types the workspace `AGENTS.md` allows, read tokens only from the documented local environment file and never print, log or persist them.
- **§K2.7** (P2) When a workspace script publishes a request with a reply template, list in that template and in the workspace protocol docs every field that the reply's consumers validate, including the reply's id (K03).

## §K3. HTTP behavior
- **§K3.1 (P1)** Keep the security headers on every response (`Content-Security-Policy` without inline script, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`) and serve only the allow-listed static files.
- **§K3.2 (P2)** Validate query parameters at the boundary and answer invalid input with `400` and a structured `{error: {code, message}}` body; do not turn failures into empty successful results.
- **§K3.3 (P2)** Emit JSON with `allow_nan=False`; numeric outputs are finite, and missing values stay `null` rather than zero.

## §K4. Frontend
- **§K4.1 (P1)** Insert record content into the DOM only as text (`textContent`, `createElement`, `setAttribute`); do not use `innerHTML`, `eval` or inline handlers, which the CSP forbids.
- **§K4.2 (P2)** The frontend stays buildless vanilla JavaScript in `static/`: no bundler, framework or package runtime dependency, and both `app.js` and `theme.js` pass `node --check`.
- **§K4.3 (P2)** Preserve light/dark themes, narrow-screen layout and keyboard focus return for panels when changing UI.

## §K5. Runtime and deployment
- **§K5.1 (P1)** Preserve container hardening in `compose.yaml` and `Dockerfile`: localhost-only port binding, read-only root filesystem with tmpfs `/tmp`, read-only mailbox mount, `cap_drop: ALL`, `no-new-privileges` and a non-root user.
- **§K5.2 (P2)** Keep the bounded `/healthz` health check; health reports process liveness only and must not imply data validity.
- **§K5.3 (P2)** Copy into the runtime image only the files the server needs; tests, local configuration and mailbox data stay out of the build context.
- **§K5.4 (P2)** Setup and doctor scripts preserve existing `.env`, root `Makefile` and working dependencies; `doctor` never changes the system.

## §K6. Python implementation
- **§K6.1 (P2)** Target Python 3.13 as pinned in the Dockerfiles. Use `pathlib`, explicit encodings where text format matters and timezone-aware `datetime`.
- **§K6.2 (P2)** Keep pure projection and decision logic in small functions that take their inputs explicitly; isolate filesystem, clock and environment access at the edges.
- **§K6.3 (P3)** Keep modules cohesive, name conversions explicitly and avoid comments that merely repeat code.
- **§K6.4** (P2) When a fix adds a shape or type check to one read of an external reply or stored record, apply the same check to every other read of that reply or record in the touched module under the same accepted criterion, and report any read outside that scope as an open finding (K01).

## §K7. Secrets and repository hygiene
- **§K7.1 (P1)** No credentials, tokens, `.env` files, private keys or real mailbox records in the repository; `projects.json`, `epics/`, `tasks/`, `inbox/`, `outbox/`, `archive/`, `decisions/`, `deliveries/`, `dispatch-gates/` and other runtime data stay ignored.
- **§K7.2 (P1)** Demo and test fixtures are synthetic and clearly non-production. Do not commit coverage output, virtual environments, caches or local run evidence.

## §K8. Workspace template
- **§K8.1 (P1)** Changes to `workspace/` instructions or skills must not weaken the Jira read-only policy, the dependency gate, G1/G2 gates or the mandatory project-loop rule stated in the workspace `AGENTS.md`.
- **§K8.2 (P2)** Keep `workspace/` and the live mailbox copies consistent when the task changes both, and state which copy was verified.

## §K9. Dependencies
- **§K9.1 (P1)** The runtime uses the Python standard library only; add a runtime dependency only for a criterion that needs it and with the owner's explicit approval.
- **§K9.2 (P2)** Development tools are pinned in `requirements-dev.txt` and container images; do not require host Python or Node installations.
- **§K9.3 (P2)** Pin container base images and development tools to maintained versions and check advisories before raising them.

## §K10. Observability
- **§K10.1 (P2)** Warnings and errors name the record path and reason without echoing secret values or full record payloads.

## §K11. Scope and compatibility
- **§K11.1 (P2)** Implement only the accepted criteria. No opportunistic refactors, unrelated filtering changes or undocumented behavior changes.
- **§K11.2 (P2)** A widened condition or newly reachable branch must be identified and reviewed as new behavior.
- **§K11.3 (P2)** Accept the documented record formats (list, wrapped list, keyed object) consistently; a format change must keep older snapshots readable or be stated as breaking.
- **§K11.4 (P1)** Do not claim runtime or integration behavior from unit tests, syntax checks or health checks alone.

## §K12. Documentation
- **§K12.1 (P2)** Update `README.md` for any new command, setting, record field or user-visible behavior in the same change.
- **§K12.2 (P2)** Workspace protocol rules belong in the workspace `AGENTS.md` and skill references, not only in code comments.
